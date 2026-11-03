"""Run a literature-search agent over the corpus and capture every query it issues.

The capture is structural, not inferred. The agent has no web access and no built-in search:
the only way it can find a paper is to call the ``search_papers`` tool defined here, so every
search arrives as an argument string. That string is logged verbatim, together with the exact
ranked list returned for it, before the agent sees any results.

Nothing is simulated. Each turn is one request to the configured endpoint, and a failed
request is recorded as a failure -- never as an empty result or a zero citation count. There
are no automatic retries and no fallback between endpoints.
"""

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from .budget import Meter
from .corpus import Corpus, term_coverage
from .endpoints import Endpoint, EndpointError, ToolResult

SYSTEM = (
    "You are a literature-search assistant helping a researcher write the related-work "
    "section of a paper. Use the search_papers tool to find relevant published work. You may "
    "call it several times with different queries to cover the topic; search before you "
    "write.\n\n"
    "When you have searched enough, write a concise related-work paragraph (150-300 words) "
    "grounded only in the abstracts you were shown. Cite papers inline by their exact "
    "paper_id in square brackets, for example [acl:2025.emnlp-main.1]. Do not cite a paper "
    "you were not shown, and do not invent findings that the abstract does not state.\n\n"
    "Finish with a final line in exactly this format, listing only papers you actually "
    "cite:\n"
    "RECOMMENDED: <paper_id>, <paper_id>, <paper_id>"
)

SEARCH_TOOL = {
    "name": "search_papers",
    "description": (
        "Search a corpus of research papers by keyword query. Returns ranked matches with "
        "paper_id, title, authors, venue and abstract. Call once per distinct query."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword search query describing the work you are looking for.",
            }
        },
        "required": ["query"],
    },
}

RECOMMENDED_LINE = re.compile(r"^\s*RECOMMENDED\s*:\s*(.+)$", re.MULTILINE | re.IGNORECASE)
PAPER_ID = re.compile(r"(?:acl|arxiv|exp):[A-Za-z0-9._\-]+")


@dataclass
class CapturedSearch:
    """One captured search: the agent's own query and exactly what was returned for it."""

    turn: int
    query: str
    results: list[dict]
    injected_ranks: list[int]


@dataclass
class AgentRun:
    run_id: str
    condition: str
    topic_id: str
    prompt_id: str
    prompt: str
    endpoint: dict
    reported_models: list[str]
    status: str
    searches: list[dict]
    captured_queries: list[str]
    final_text: str | None
    recommended_ids: list[str]
    inline_ids: list[str]
    shown_ids: list[str]
    injected_shown: bool
    injected_recommended: bool
    corpus_manifest: dict
    usage: dict
    turns: int
    created_at: str
    failure: str | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @property
    def bibliography(self) -> list[str]:
        """Citations by a single rule: the RECOMMENDED line, else the inline citations.

        Some otherwise-complete reviews omit the trailing line while citing properly inline.
        Treating those as zero-citation runs would discard real measurements, so the same
        fallback is applied everywhere and recorded in ``status``.
        """
        return self.recommended_ids or self.inline_ids


def extract_citations(text: str, shown: set[str]) -> tuple[list[str], list[str]]:
    """Parse the bibliography line and inline citations, keeping only papers actually shown.

    A paper_id the agent never saw is dropped rather than counted: it is a hallucinated
    citation, not evidence that the paper was retrieved.
    """
    recommended: list[str] = []
    match = None
    for match in RECOMMENDED_LINE.finditer(text):
        pass
    if match:
        for candidate in PAPER_ID.findall(match.group(1)):
            if candidate in shown and candidate not in recommended:
                recommended.append(candidate)
    body = text[: match.start()] if match else text
    inline: list[str] = []
    for candidate in PAPER_ID.findall(body):
        if candidate in shown and candidate not in inline:
            inline.append(candidate)
    return recommended, inline


def ask_json(
    endpoint: Endpoint,
    system: str,
    user: str,
    meter: Meter,
    label: str,
    max_tokens: int = 1200,
) -> tuple[dict, dict]:
    """One request that must answer with a JSON object. Parse failures are reported, not patched."""
    meter.reserve(label)
    reply = endpoint.send(
        endpoint.start(user), system=system, tools=[], max_tokens=max_tokens
    )
    meter.record(
        label, reply.reported_model, reply.input_tokens, reply.output_tokens, reply.seconds
    )
    text = reply.text
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in response: {text[:200]!r}")
    return json.loads(candidate[start : end + 1], strict=False), {
        "requested_model": endpoint.model,
        "reported_model": reply.reported_model,
        "raw_text": text,
    }


def run_agent(
    corpus: Corpus,
    prompt: str,
    *,
    endpoint: Endpoint,
    meter: Meter,
    run_id: str,
    condition: str = "baseline",
    topic_id: str = "",
    prompt_id: str = "",
    results_per_query: int = 8,
    max_turns: int = 8,
    max_searches: int = 6,
    max_tokens: int = 1600,
) -> AgentRun:
    """Drive one agent episode to completion, capturing all searches and the bibliography."""
    state = endpoint.start(prompt)
    searches: list[CapturedSearch] = []
    reported_models: list[str] = []
    shown: dict[str, dict] = {}
    final_text: str | None = None
    status = "turn_budget_exhausted"
    failure: str | None = None
    turn = 0

    while turn < max_turns:
        meter.reserve(f"{run_id}:turn{turn}")
        try:
            reply = endpoint.send(
                state, system=SYSTEM, tools=[SEARCH_TOOL], max_tokens=max_tokens
            )
        except EndpointError as error:
            status, failure = "transport_error", str(error)
            break

        if reply.reported_model and reply.reported_model not in reported_models:
            reported_models.append(reply.reported_model)
        meter.record(
            f"{run_id}:turn{turn}",
            reply.reported_model,
            reply.input_tokens,
            reply.output_tokens,
            reply.seconds,
        )
        endpoint.append_reply(state, reply)

        if not reply.tool_calls:
            final_text = reply.text
            status = "complete" if final_text else "empty_response"
            break

        if len(searches) >= max_searches:
            # Refuse further searching rather than silently granting an unbounded budget.
            endpoint.append_tool_results(
                state,
                [
                    ToolResult(
                        call_id=call.call_id,
                        payload={
                            "error": "search quota exhausted",
                            "searches_used": len(searches),
                            "instruction": (
                                "Write the related-work paragraph now using the papers "
                                "already shown."
                            ),
                        },
                        is_error=True,
                    )
                    for call in reply.tool_calls
                ],
            )
            turn += 1
            continue

        results: list[ToolResult] = []
        for call in reply.tool_calls:
            query = str(call.arguments.get("query", "")).strip()
            hits = corpus.search(query, results_per_query) if query else []
            payload, captured = [], []
            for hit in hits:
                record = hit["record"]
                shown.setdefault(record.paper_id, {"injected": record.injected})
                payload.append(record.tool_payload())
                captured.append(
                    {
                        "rank": hit["rank"],
                        "paper_id": record.paper_id,
                        "score": hit["score"],
                        "injected": record.injected,
                        "term_coverage": round(
                            term_coverage(query, record.searchable_text), 4
                        ),
                    }
                )
            searches.append(
                CapturedSearch(
                    turn=turn,
                    query=query,
                    results=captured,
                    injected_ranks=[r["rank"] for r in captured if r["injected"]],
                )
            )
            results.append(
                ToolResult(
                    call_id=call.call_id,
                    payload=payload if payload else {"results": [], "note": "no matches"},
                )
            )
        endpoint.append_tool_results(state, results)
        turn += 1

    recommended: list[str] = []
    inline: list[str] = []
    if final_text:
        recommended, inline = extract_citations(final_text, set(shown))
        if status == "complete" and not recommended:
            status = "cited_inline_without_recommended_line" if inline else "no_citations"

    return AgentRun(
        run_id=run_id,
        condition=condition,
        topic_id=topic_id,
        prompt_id=prompt_id,
        prompt=prompt,
        endpoint=endpoint.describe(),
        reported_models=reported_models,
        status=status,
        searches=[asdict(search) for search in searches],
        captured_queries=[search.query for search in searches],
        final_text=final_text,
        recommended_ids=recommended,
        inline_ids=inline,
        shown_ids=sorted(shown),
        injected_shown=any(meta["injected"] for meta in shown.values()),
        injected_recommended=any(
            shown.get(paper_id, {}).get("injected") for paper_id in recommended
        ),
        corpus_manifest=corpus.manifest(),
        usage={"turns": turn, "searches": len(searches), **meter.summary()},
        turns=turn,
        created_at=datetime.now(UTC).isoformat(),
        failure=failure,
    )
