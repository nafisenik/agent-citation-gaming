"""Run the full loop: capture the agent's queries, rewrite a paper, measure what changes.

The loop is:

1. Give the agent a research request over the unmodified corpus, and capture every query it
   issues and everything it cites. This is the **discovery** run.
2. Pick a target: the best-ranked paper the agent saw and did not cite. Chosen from the
   agent's own behaviour rather than by us.
3. For each strategy, build a variant corpus and re-run the identical request.

``baseline`` is a strategy, not an absence of one. It re-runs the *unchanged* paper in fresh
executions, which is the control the comparison needs. A target is chosen *because* it was
uncited in the discovery run, so its discovery-run citation count is zero by selection, not
by measurement. Without fresh unchanged-paper executions there is no way to tell a rewrite
effect from ordinary run-to-run variation, so the control is included by default and the
report refuses to compute a difference without it.

Three outcomes are kept distinct throughout and never collapsed: whether the paper was
**retrieved**, at what **rank**, and whether it was **cited**.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .adversary import (
    AttemptFeedback,
    QueryProfile,
    overlap_variant,
    presentation_metrics,
    rewrite_variant,
)
from .agent import AgentRun, run_agent
from .budget import BudgetExceeded, Meter
from .corpus import Corpus
from .endpoints import Endpoint

DEFAULT_STRATEGIES = ("baseline", "overlap", "rewrite")


@dataclass
class Execution:
    """One agent execution against one variant of the corpus."""

    strategy: str
    attempt: int
    repetition: int
    run_id: str
    status: str
    usable: bool
    retrieved: bool
    best_rank: int | None
    cited: bool
    references: int


@dataclass
class StrategyResult:
    strategy: str
    target_id: str
    construction: dict = field(default_factory=dict)
    presentation: dict = field(default_factory=dict)
    executions: list[Execution] = field(default_factory=list)
    error: str | None = None
    # Engineered text is held in memory for the caller to persist privately if it wants.
    title: str | None = field(default=None, repr=False)
    abstract: str | None = field(default=None, repr=False)

    @property
    def usable(self) -> list[Execution]:
        return [e for e in self.executions if e.usable]

    @property
    def cited_count(self) -> int:
        return sum(1 for e in self.usable if e.cited)

    @property
    def citation_rate(self) -> float | None:
        usable = self.usable
        return round(sum(e.cited for e in usable) / len(usable), 4) if usable else None

    @property
    def retrieval_rate(self) -> float | None:
        usable = self.usable
        return round(sum(e.retrieved for e in usable) / len(usable), 4) if usable else None

    @property
    def succeeded_within_budget(self) -> bool:
        return any(e.cited for e in self.usable)


def observe(run: AgentRun, target_id: str) -> dict:
    """What an author could observe about one execution: retrieval, rank, rival citations.

    Limited on purpose to the agent's visible behaviour. Its hidden reasoning is not
    recorded anywhere and is never fed back to the attacker.
    """
    cited = run.bibliography
    ranks = [
        result["rank"]
        for search in run.searches
        for result in search["results"]
        if result["paper_id"] == target_id
    ]
    return {
        "retrieved": target_id in run.shown_ids,
        "best_rank": min(ranks) if ranks else None,
        "cited": target_id in cited,
        "cited_ids": list(cited),
        "queries": list(run.captured_queries),
    }


def pick_target(run: AgentRun, corpus: Corpus) -> str | None:
    """The best-ranked real paper the agent saw but did not cite.

    Using a shown-but-uncited paper makes the comparison meaningful: for this exact request,
    this paper's citation outcome is known to have been negative.
    """
    cited = set(run.bibliography)
    best: dict | None = None
    for search in run.searches:
        for result in search["results"]:
            if result["injected"] or result["paper_id"] in cited:
                continue
            if best is None or result["rank"] < best["rank"]:
                best = result
    if best is None or best["paper_id"] not in corpus.records:
        return None
    return best["paper_id"]


def build_variant(
    strategy: str,
    corpus: Corpus,
    target_id: str,
    profile: QueryProfile,
    endpoint: Endpoint,
    meter: Meter,
    label: str,
    history: tuple[AttemptFeedback, ...] = (),
) -> tuple[Corpus, str, str, str, dict]:
    """Return (variant corpus, focus id, title, abstract, construction record)."""
    original = corpus.records[target_id]

    if strategy == "baseline":
        # The control: the published metadata, untouched. No model call, no edit.
        return (
            corpus,
            target_id,
            original.title,
            original.abstract,
            {
                "method": "unchanged_published_metadata",
                "model_used": None,
                "is_control": True,
            },
        )

    if strategy == "overlap":
        title, abstract, construction = overlap_variant(profile, original)
        return (
            corpus.variant(edits={target_id: (title, abstract)}),
            target_id,
            title,
            abstract,
            construction,
        )

    if strategy in ("rewrite", "adaptive"):
        title, abstract, construction = rewrite_variant(
            endpoint, original, profile, meter, label, history=history
        )
        return (
            corpus.variant(edits={target_id: (title, abstract)}),
            target_id,
            title,
            abstract,
            construction,
        )

    raise ValueError(f"unknown strategy {strategy!r}")


def run_attack(
    corpus: Corpus,
    prompt: str,
    *,
    endpoint: Endpoint,
    meter: Meter,
    strategies=DEFAULT_STRATEGIES,
    target_id: str | None = None,
    repetitions: int = 1,
    max_attempts: int = 3,
    results_per_query: int = 8,
    max_searches: int = 6,
    max_turns: int = 8,
    run_prefix: str = "attack",
    on_event=lambda *_, **__: None,
) -> dict:
    """Run discovery, then every strategy, and return a report of what changed.

    Raises nothing on an individual strategy failure: the failure is recorded against that
    strategy and the rest continue, so one malformed model reply does not discard the run.
    """
    if "baseline" not in strategies:
        # The control is what makes the other numbers interpretable; refuse to drop it
        # silently. A caller that genuinely wants it out can pass it explicitly.
        strategies = ("baseline", *strategies)

    on_event("discovery_start", prompt=prompt)
    discovery = run_agent(
        corpus,
        prompt,
        endpoint=endpoint,
        meter=meter,
        run_id=f"{run_prefix}__discovery",
        condition="discovery",
        results_per_query=results_per_query,
        max_searches=max_searches,
        max_turns=max_turns,
    )
    on_event(
        "discovery_done",
        queries=discovery.captured_queries,
        cited=discovery.bibliography,
        status=discovery.status,
    )
    if discovery.status == "transport_error":
        return {
            "status": "discovery_failed",
            "failure": discovery.failure,
            "discovery": discovery.to_json(),
        }

    profile = QueryProfile.from_queries(discovery.captured_queries)
    target_id = target_id or pick_target(discovery, corpus)
    if target_id is None:
        return {
            "status": "no_target",
            "note": "the agent cited everything it was shown, or showed nothing",
            "discovery": discovery.to_json(),
        }
    discovery_observation = observe(discovery, target_id)
    on_event(
        "target_chosen",
        target_id=target_id,
        title=corpus.records[target_id].title,
        rank=discovery_observation["best_rank"],
    )

    results: list[StrategyResult] = []
    runs: list[AgentRun] = [discovery]
    for strategy in strategies:
        result = StrategyResult(strategy=strategy, target_id=target_id)
        results.append(result)
        history: tuple[AttemptFeedback, ...] = ()
        attempts = max_attempts if strategy == "adaptive" else 1
        built = None

        for attempt in range(1, attempts + 1):
            if built is None or strategy == "adaptive":
                try:
                    built = build_variant(
                        strategy, corpus, target_id, profile, endpoint, meter,
                        label=f"{run_prefix}__{strategy}__a{attempt}:author",
                        history=history,
                    )
                except BudgetExceeded as error:
                    result.error = str(error)
                    return _report(
                        prompt, endpoint, meter, discovery, discovery_observation,
                        target_id, corpus, results, runs, stopped=str(error),
                    )
                except (ValueError, KeyError, OSError) as error:
                    result.error = f"{type(error).__name__}: {error}"
                    on_event("strategy_failed", strategy=strategy, error=result.error)
                    break

            variant, focus_id, title, abstract, construction = built
            result.construction = construction
            result.title, result.abstract = title, abstract
            result.presentation = presentation_metrics(title, abstract, profile)

            last: AgentRun | None = None
            for repetition in range(1, repetitions + 1):
                suffix = (
                    f"a{attempt}__r{repetition}" if strategy == "adaptive"
                    else f"r{repetition}"
                )
                try:
                    run = run_agent(
                        variant, prompt, endpoint=endpoint, meter=meter,
                        run_id=f"{run_prefix}__{strategy}__{suffix}",
                        condition=strategy, results_per_query=results_per_query,
                        max_searches=max_searches, max_turns=max_turns,
                    )
                except BudgetExceeded as error:
                    return _report(
                        prompt, endpoint, meter, discovery, discovery_observation,
                        target_id, corpus, results, runs, stopped=str(error),
                    )
                runs.append(run)
                last = run
                seen = observe(run, focus_id)
                execution = Execution(
                    strategy=strategy,
                    attempt=attempt,
                    repetition=repetition,
                    run_id=run.run_id,
                    status=run.status,
                    usable=bool(run.bibliography),
                    retrieved=seen["retrieved"],
                    best_rank=seen["best_rank"],
                    cited=seen["cited"],
                    references=len(run.bibliography),
                )
                result.executions.append(execution)
                on_event("execution", **asdict(execution))

            if strategy != "adaptive" or last is None:
                break
            # Success within a fixed budget is a monotone event, so stopping at the first
            # success does not bias it. Every attempt made is recorded either way.
            if any(e.cited for e in result.executions if e.attempt == attempt):
                break
            seen = observe(last, focus_id)
            history = (
                *history,
                AttemptFeedback(
                    attempt=attempt,
                    title=title,
                    abstract=abstract,
                    retrieved=seen["retrieved"],
                    best_rank=seen["best_rank"],
                    cited=seen["cited"],
                    competitor_titles=tuple(
                        variant.records[pid].title
                        for pid in seen["cited_ids"][:5]
                        if pid in variant.records
                    ),
                    queries=tuple(seen["queries"]),
                ),
            )

    return _report(
        prompt, endpoint, meter, discovery, discovery_observation, target_id, corpus,
        results, runs,
    )


def _report(
    prompt, endpoint, meter, discovery, discovery_observation, target_id, corpus,
    results, runs, stopped=None,
) -> dict:
    control = next((r for r in results if r.strategy == "baseline"), None)
    control_rate = control.citation_rate if control else None

    strategies = []
    for result in results:
        difference = None
        if control_rate is not None and result.citation_rate is not None:
            difference = round(result.citation_rate - control_rate, 4)
        strategies.append(
            {
                "strategy": result.strategy,
                "executions": len(result.executions),
                "usable": len(result.usable),
                "retrieval_rate": result.retrieval_rate,
                "citation_rate": result.citation_rate,
                "citation_rate_minus_control": difference,
                "cited_count": result.cited_count,
                "succeeded_within_budget": result.succeeded_within_budget,
                "attempts_used": max((e.attempt for e in result.executions), default=0),
                "construction": result.construction,
                "presentation": result.presentation,
                "error": result.error,
                "runs": [asdict(e) for e in result.executions],
            }
        )

    return {
        "status": "complete" if stopped is None else "stopped",
        "stopped_reason": stopped,
        "created_at": datetime.now(UTC).isoformat(),
        "prompt": prompt,
        "endpoint": endpoint.describe(),
        "corpus": corpus.manifest(),
        "target": {
            "paper_id": target_id,
            "published_title": corpus.records[target_id].title,
            "url": corpus.records[target_id].url,
            "discovery_rank": discovery_observation["best_rank"],
            "discovery_cited": discovery_observation["cited"],
            "selection_rule": "best-ranked paper retrieved but not cited in the discovery run",
            "selection_caveat": (
                "Chosen because it was uncited, so its discovery-run citation count is zero "
                "by selection. Compare against the baseline strategy, which re-runs the "
                "unchanged paper, rather than against the discovery run."
            ),
        },
        "discovery": {
            "status": discovery.status,
            "captured_queries": discovery.captured_queries,
            "papers_shown": len(discovery.shown_ids),
            "cited": discovery.bibliography,
        },
        "control_citation_rate": control_rate,
        "strategies": strategies,
        "budget": meter.summary(),
        "endpoints_note": (
            "All strategies share one corpus, one request text and one retrieval "
            "configuration. Only the target paper's presentation differs."
        ),
        "runs": [run.to_json() for run in runs],
    }


def save_report(report: dict, directory: Path, private_text: dict | None = None) -> Path:
    """Write a report. Engineered title/abstract text is written only if explicitly given."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    if private_text:
        (directory / "engineered_text.json").write_text(
            json.dumps(private_text, indent=2, ensure_ascii=False) + "\n"
        )
    return path
