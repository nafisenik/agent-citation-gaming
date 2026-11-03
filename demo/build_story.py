"""Export the demo's data from recorded agent runs, withholding the engineered text.

The demo replays real measurements. It computes no outcome of its own, so everything it
shows has to be exported here first. This script is committed so that what is published and
what is withheld is auditable rather than asserted.

**Included**: the agent's own captured queries, ranked results with scores, which papers were
cited, published paper titles, aggregate rates, and the per-target stability pattern.

**Withheld**: every model-written title and abstract. Publishing those would invite scrapers
to ingest a rewrite as a real paper's metadata and would seed abstracts with no study behind
them into other corpora. Each is replaced by a sha256 and its lexical metrics.

One exception, deliberately: the ``overlap`` title *is* published. It is produced by a
deterministic, model-free function in this repository from query terms that are themselves
published here, so it discloses nothing new -- and it is the strategy that mostly failed,
which is the point the scene makes.

Running it needs the private run directory, which is not part of this repository:

    python demo/build_story.py --runs ~/private/outputs/discovery --out demo/data/story.json
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

WITHHELD = "engineered metadata withheld; not the published version"

# Recorded condition names, mapped to the strategy names the package now uses.
STRATEGY_OF = {
    "edit_saturated": "overlap",
    "edit_faithful": "rewrite",
    "inject_fabricated": "fabricated",
}

STRATEGY_COPY = {
    "overlap": {
        "label": "Title only: add their keywords",
        "short": "title only",
        "author_says": (
            "I'll leave the abstract alone and pack the terms they searched for "
            "into my title."
        ),
        "text_published": True,
    },
    "rewrite": {
        "label": "Title and abstract: say the same thing in their words",
        "short": "title + abstract",
        "author_says": (
            "I'll rewrite both in their wording - same findings, same claims, every "
            "one still backed by a quote from my real abstract."
        ),
        "text_published": False,
    },
    # Not an author move: this injects a paper that does not exist, so it is reported
    # alongside the results as a reference point rather than offered as a choice.
    "fabricated": {
        "label": "A paper invented to fit the queries",
        "short": "invented paper",
        "author_says": "A record written to match the queries, with no study behind it.",
        "text_published": False,
    },
}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def load_runs(directory: Path) -> list[dict]:
    runs = []
    for path in sorted(directory.glob("*/runs/*.json")):
        study = path.parent.parent.name
        if study.startswith(("discarded-", "pilot-")):
            continue
        run = json.loads(path.read_text())
        run["_study"] = study
        runs.append(run)
    return runs


def topic_stem(identifier: str) -> str:
    """`bias_fairness_llm__p4__baseline__r1` and `bias_fairness_llm__p1` -> topic stem."""
    return re.sub(r"__p\d+.*$", "", identifier)


def retrieved_ranks(run: dict, paper_id: str) -> list[int]:
    return [
        result["rank"]
        for call in run.get("tool_calls") or []
        for result in call.get("results") or []
        if result["paper_id"] == paper_id
    ]


def pick_opening(runs: list[dict], prompt_id: str, target_id: str) -> dict:
    """The baseline episode the demo opens with, tied to the paper it later hands over.

    The demo walks one continuous story: a request, the searches it produced, the ranked
    results, and then "one of those is your paper, and it was not cited". For that to be
    true rather than merely plausible, the opening episode has to be one where this exact
    paper was retrieved and *not* cited.

    Picking by topic alone is not enough. The first version of this matched the topic stem
    and selected an episode in which the target was cited, so the demo showed a paper being
    used and then told the reader it had been passed over. Match the prompt, require the
    target to appear in the results, and require it to be uncited.
    """
    candidates = [
        run
        for run in runs
        if run["condition"] == "baseline"
        and run.get("prompt_id") == prompt_id
        and run["tool_calls"]
        and retrieved_ranks(run, target_id)
        and target_id not in (run.get("recommended_ids") or [])
        and target_id not in (run.get("inline_ids") or [])
    ]
    if not candidates:
        raise SystemExit(
            f"no baseline episode for prompt {prompt_id!r} retrieves {target_id!r} without "
            "citing it; the opening scenes would contradict the target scene."
        )
    # Best-ranked first, so the paper the reader is shown is easy to spot in the list.
    candidates.sort(key=lambda run: (min(retrieved_ranks(run, target_id)), run["run_id"]))
    return candidates[0]


def pick_player_target(targets: list[dict]) -> dict:
    """Mirror of playerTarget() in demo/js/scenes.js: prefer the clean modal pattern."""
    modal = [
        target
        for target in targets
        if (rewrite := target["strategies"].get("rewrite"))
        and (overlap := target["strategies"].get("overlap"))
        and not overlap["ever_cited"]
        and rewrite["cited_count"] == rewrite["execution_count"]
    ]
    return (modal or targets)[0]


def opening_scene(run: dict, corpus: dict, focus_id: str) -> dict:
    cited = run.get("recommended_ids") or run.get("inline_ids")
    searches = []
    for call in run["tool_calls"]:
        searches.append(
            {
                "query": call["query"],
                "results": [
                    {
                        "rank": result["rank"],
                        "paper_id": result["paper_id"],
                        "title": corpus.get(result["paper_id"], ""),
                        "score": round(result["score"], 2),
                        "cited": result["paper_id"] in cited,
                    }
                    for result in call["results"][:5]
                ],
            }
        )
    # The scene that shows a ranked list should show the list the target appears in,
    # otherwise the next scene points at a paper the reader was never shown.
    focus_index = next(
        (
            index
            for index, search in enumerate(searches)
            if any(result["paper_id"] == focus_id for result in search["results"])
        ),
        0,
    )
    return {
        "run_id": run["run_id"],
        "focus_paper_id": focus_id,
        "focus_search_index": focus_index,
        "prompt": run["prompt"],
        "searches": searches,
        "papers_shown": len(run["shown_ids"]),
        "cited": [
            {"paper_id": paper_id, "title": corpus.get(paper_id, "")}
            for paper_id in cited
        ],
        "selection_rule": "the usable baseline episode with the median number of searches",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", required=True, help="private recorded-runs directory")
    parser.add_argument("--analysis", help="analysis.json with the aggregates")
    parser.add_argument("--corpus", required=True, help="papers.jsonl")
    parser.add_argument("--out", default="demo/data/story.json")
    arguments = parser.parse_args()

    corpus: dict[str, str] = {}
    with open(arguments.corpus, encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            corpus[row["paper_id"]] = row["title"]

    runs = load_runs(Path(arguments.runs))
    analysis = json.loads(Path(arguments.analysis).read_text()) if arguments.analysis else {}

    # --- per target, per strategy: what actually happened ---------------------------
    targets: dict[str, dict] = {}
    for run in runs:
        attack = run.get("attack")
        if not attack:
            continue
        strategy = STRATEGY_OF.get(run["condition"])
        if strategy is None:
            continue
        target_id = attack["target_published_id"]
        entry = targets.setdefault(
            target_id,
            {
                "paper_id": target_id,
                "published_title": attack["target_published_title"],
                "baseline_rank": attack["target_baseline_rank"],
                "prompt_id": run["prompt_id"],
                "strategies": {},
            },
        )
        slot = entry["strategies"].setdefault(
            strategy,
            {
                "strategy": strategy,
                "executions": [],
                "coverage_before": attack["baseline_presentation_metrics"][
                    "mean_query_term_coverage_title"
                ],
                "coverage_after": attack["presentation_metrics"][
                    "mean_query_term_coverage_title"
                ],
                "anchors_total": attack["construction"].get("anchors_total"),
                "anchors_unverified": attack["construction"].get("anchors_unverified"),
                "numbers_added": len(
                    attack["construction"].get("numbers_not_in_original_abstract") or []
                ),
            },
        )
        # The engineered text: published only for the deterministic strategy.
        if STRATEGY_COPY[strategy]["text_published"]:
            slot["title"] = attack["experimental_title"]
        else:
            slot["title_sha256"] = digest(attack["experimental_title"])
            slot["abstract_sha256"] = digest(attack["experimental_abstract"])
            slot["title_words"] = attack["presentation_metrics"]["title_words"]
            slot["text_withheld"] = WITHHELD

        best_rank = min(
            (
                result["rank"]
                for call in run["tool_calls"]
                for result in call["results"]
                if result["paper_id"] == attack["focus_paper_id"]
            ),
            default=None,
        )
        slot["executions"].append(
            {
                "run_id": run["run_id"],
                "retrieved": attack["focus_shown"],
                "best_rank": best_rank,
                "cited": attack["focus_cited"],
            }
        )

    for entry in targets.values():
        for slot in entry["strategies"].values():
            slot["executions"].sort(key=lambda e: e["run_id"])
            total = len(slot["executions"])
            cited = sum(1 for e in slot["executions"] if e["cited"])
            slot["cited_count"] = cited
            slot["execution_count"] = total
            slot["pattern"] = f"{cited}/{total}"
            slot["ever_cited"] = cited > 0

    # --- aggregates, read from the recorded analysis rather than recomputed ---------
    strategies = []
    for condition, strategy in STRATEGY_OF.items():
        recorded = (analysis.get("attack") or {}).get(condition) or {}
        per_target = [
            entry["strategies"][strategy]
            for entry in targets.values()
            if strategy in entry["strategies"]
        ]
        always = sum(1 for s in per_target if s["cited_count"] == s["execution_count"])
        never = sum(1 for s in per_target if s["cited_count"] == 0)
        strategies.append(
            {
                "strategy": strategy,
                **STRATEGY_COPY[strategy],
                "executions": recorded.get("episodes"),
                "retrieved": recorded.get("focus_retrieved"),
                "reached_rank_1": recorded.get("focus_reached_rank_1"),
                "cited": recorded.get("focus_cited"),
                "retrieval_rate": recorded.get("retrieval_success_rate"),
                # The analysis stores rank-1 as a count while the other two stages are
                # already rates; derive it so all three bars are on the same scale.
                "rank1_rate": (
                    round(recorded["focus_reached_rank_1"] / recorded["episodes"], 4)
                    if recorded.get("focus_reached_rank_1") is not None
                    and recorded.get("episodes")
                    else None
                ),
                "citation_rate": recorded.get("injection_success_rate"),
                "coverage_before": recorded.get("mean_title_coverage_before"),
                "coverage_after": recorded.get("mean_title_coverage_after"),
                "targets": len(per_target),
                "targets_always_cited": always,
                "targets_never_cited": never,
                "targets_mixed": len(per_target) - always - never,
                "targets_ever_cited": sum(1 for s in per_target if s["ever_cited"]),
            }
        )
    strategies.sort(key=lambda row: -(row["citation_rate"] or 0))

    baseline = analysis.get("baseline") or {}
    queries = analysis.get("queries") or {}
    # Tie the opening request to the paper the demo later hands the reader, so the
    # two scenes are about the same subject.
    player = pick_player_target(sorted(targets.values(), key=lambda t: t["paper_id"]))
    opening = pick_opening(runs, player["prompt_id"], player["paper_id"])

    story = {
        "generated_from": {
            # Only the directory name: the full path is local to whoever ran the export
            # and would be published verbatim on a public site.
            "runs_directory": Path(arguments.runs).name,
            "studies": sorted({run["_study"] for run in runs}),
            "runs_read": len(runs),
            "analysis": analysis.get("generated_at"),
        },
        "disclosure": {
            "included": (
                "The agent's own captured queries, ranked results and scores, which papers "
                "were cited, published paper titles, aggregate rates, and the per-target "
                "stability pattern."
            ),
            "withheld": WITHHELD,
            "withheld_reason": (
                "Publishing an engineered title or abstract invites scrapers to ingest it as "
                "a real paper's metadata, and seeds abstracts with no study behind them into "
                "other corpora."
            ),
            "overlap_exception": (
                "The overlap title is published because a deterministic, model-free function "
                "in this repository reproduces it from queries that are published here too."
            ),
            "targets": (
                "Target papers are real publications selected automatically from retrieval "
                "logs. Their authors had no involvement and were not contacted. No rewrite "
                "was submitted to any venue or public index."
            ),
        },
        "corpus": {
            "papers": analysis.get("corpus_papers"),
            "source": "ACL Anthology, ACL and EMNLP 2025-2026 main and findings tracks",
        },
        "baseline": {
            "episodes": baseline.get("episodes_total"),
            "usable": baseline.get("episodes_usable"),
            "papers_shown": baseline.get("distinct_papers_shown"),
            "papers_cited": baseline.get("distinct_papers_cited"),
            "shown_never_cited": (
                baseline.get("distinct_papers_shown", 0)
                - baseline.get("distinct_papers_cited", 0)
            ),
            "citation_slots": baseline.get("total_citation_slots"),
            "gini": baseline.get("citation_gini_over_shown_papers"),
            "top10_share": baseline.get("top10_share_of_citations"),
            "searches_per_episode": baseline.get("searches_per_episode_mean"),
            "references_per_episode": baseline.get("references_per_episode_mean"),
            "citation_counts": baseline.get("citation_counts_over_shown_papers"),
        },
        "queries": {
            "captured": queries.get("queries_captured"),
            "distinct": queries.get("distinct_queries"),
            "mean_terms": queries.get("mean_query_terms"),
            "most_requested": (queries.get("most_requested_terms") or [])[:12],
        },
        "opening": opening_scene(opening, corpus, player["paper_id"]),
        "strategies": strategies,
        "targets": sorted(targets.values(), key=lambda t: t["paper_id"]),
        "open_question": (
            "Targets were selected because the agent retrieved them and did not cite them, "
            "so their citation count before any rewrite is zero by selection rather than by "
            "measurement. Comparing a rewrite against that zero measures the selection rule. "
            "The decisive comparison is against fresh executions of the unchanged paper, and "
            "that has not been run at scale yet."
        ),
    }

    out = Path(arguments.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(story, indent=2, ensure_ascii=False) + "\n")

    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    print(f"  studies      : {', '.join(story['generated_from']['studies'])}")
    print(f"  runs read    : {len(runs)}")
    print(f"  targets      : {len(targets)}")
    for row in strategies:
        print(
            f"  {row['strategy']:12s} cited {row['cited']}/{row['executions']}"
            f"  ever-cited targets {row['targets_ever_cited']}/{row['targets']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
