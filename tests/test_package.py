"""Tests for the package. No test here makes a network request."""

import json

import pytest

from acg import endpoints as endpoints_module
from acg.adversary import AttemptFeedback, QueryProfile, overlap_variant
from acg.agent import SEARCH_TOOL, extract_citations, run_agent
from acg.attack import observe, pick_target, run_attack
from acg.budget import BudgetExceeded, Meter
from acg.corpus import Corpus, Record, tokens
from acg.download import detex, parse_authors, parse_fields
from acg.endpoints import Endpoint, EndpointError, ToolResult, from_preset
from acg.indexing import SparseBM25
from acg.paths import corpus_path

HAS_CORPUS = corpus_path().exists()
needs_corpus = pytest.mark.skipif(HAS_CORPUS is False, reason="run `acg corpus download`")


# --- a corpus small enough to assert on exactly -------------------------------------


def tiny_records() -> dict[str, Record]:
    rows = [
        ("acl:a", "Multilingual sycophancy in dialogue", "We study sycophancy across languages."),
        ("acl:b", "Retrieval augmented generation survey", "A survey of retrieval augmented generation."),
        ("acl:c", "Sycophancy benchmarks", "We build a benchmark for sycophancy evaluation."),
    ]
    return {
        paper_id: Record(
            paper_id=paper_id,
            title=title,
            abstract=abstract,
            authors=("A Person",),
            venue="ACL",
            year=2025,
            track="main",
            url=f"https://aclanthology.org/{paper_id}",
            peer_review_status="peer_reviewed_published",
        )
        for paper_id, title, abstract in rows
    }


@pytest.fixture
def tiny() -> Corpus:
    return Corpus(records=tiny_records())


# --- retrieval ----------------------------------------------------------------------


def test_tokenizer_keeps_content_words_a_general_stoplist_would_drop():
    # A general-purpose stoplist drops these. In an NLP corpus they are topical.
    produced = set(tokens("evaluation of research study evidence comparing papers"))
    for word in ("evaluation", "research", "study", "evidence", "papers"):
        assert word in produced
    # Function words still go.
    assert "of" not in produced and "the" not in tokens("the model")


def test_index_requires_an_explicit_tokenizer():
    """A defaulted tokenizer once silently corrupted a study; it must stay required."""
    with pytest.raises(TypeError):
        SparseBM25({"a": "some text"})


def test_search_is_ranked_and_scores_descend(tiny):
    hits = tiny.search("sycophancy evaluation", 3)
    assert [hit["rank"] for hit in hits] == list(range(1, len(hits) + 1))
    scores = [hit["score"] for hit in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(hit["score"] > 0 for hit in hits)
    assert not any(hit["injected"] for hit in hits)
    # The benchmark paper mentions both query terms, so it must outrank the one that does not.
    assert hits[0]["paper_id"] == "acl:c"


def test_search_only_returns_documents_containing_a_query_term(tiny):
    assert [hit["paper_id"] for hit in tiny.search("retrieval", 5)] == ["acl:b"]
    assert tiny.search("quantum chromodynamics", 5) == []


# --- experimental overlays ----------------------------------------------------------


def test_injected_record_is_flagged_and_never_claims_peer_review(tiny):
    record = Record(
        paper_id="exp:1",
        title="A Benchmark",
        abstract="An experimental record with no underlying study.",
        peer_review_status="unknown",
        injected=True,
        provenance_note="preprint, review status unknown",
    )
    variant = tiny.variant(injections=(record,))
    assert variant.injected_ids == ("exp:1",)
    payload = variant.records["exp:1"].tool_payload()
    # What the agent sees must not imply a venue or acceptance.
    assert payload["venue"] == "preprint, review status unknown"
    assert "ACL" not in payload["venue"]
    assert "exp:1" not in tiny.records  # the published corpus is untouched


def test_injection_flag_cannot_be_forged(tiny):
    sneaky = Record(paper_id="exp:2", title="t", abstract="a", injected=False, venue="ACL")
    assert tiny.variant(injections=(sneaky,)).records["exp:2"].injected is True


def test_variant_refuses_unknown_edits_and_duplicate_injections(tiny):
    with pytest.raises(KeyError):
        tiny.variant(edits={"acl:missing": ("t", "a")})
    with pytest.raises(ValueError):
        tiny.variant(injections=(Record(paper_id="acl:a", title="t", abstract="a"),))


def test_edit_changes_only_the_target_and_preserves_identity(tiny):
    variant = tiny.variant(edits={"acl:a": ("New Title", "New abstract.")})
    assert variant.records["acl:a"].title == "New Title"
    assert variant.edited_ids == ("acl:a",)
    assert tiny.records["acl:a"].title == "Multilingual sycophancy in dialogue"
    assert len(variant.records) == len(tiny.records)
    # Identity and provenance survive a presentation edit.
    assert variant.records["acl:a"].url == tiny.records["acl:a"].url
    assert variant.records["acl:a"].peer_review_status == "peer_reviewed_published"


# --- citation parsing ---------------------------------------------------------------


def test_citation_parsing_drops_papers_never_shown():
    text = "Work is active [acl:a].\nRECOMMENDED: acl:a, acl:9999.fake-main.7"
    recommended, inline = extract_citations(text, {"acl:a"})
    assert recommended == ["acl:a"]
    assert inline == ["acl:a"]


def test_citation_parsing_uses_the_last_recommended_line():
    text = "cites [acl:a]\nRECOMMENDED: acl:a\nmore [acl:b]\nRECOMMENDED: acl:b"
    recommended, _ = extract_citations(text, {"acl:a", "acl:b"})
    assert recommended == ["acl:b"]


# --- budget -------------------------------------------------------------------------


def test_budget_is_reserved_before_the_request_not_counted_after():
    meter = Meter(max_requests=2)
    meter.reserve("a")
    meter.reserve("b")
    with pytest.raises(BudgetExceeded):
        meter.reserve("c")
    assert meter.requests == 2


# --- endpoints ----------------------------------------------------------------------


def fake_transport(monkeypatch, response):
    """Capture the request body instead of sending it, and return a canned response."""
    sent = {}

    def _post(url, body, headers, timeout):
        sent["url"], sent["body"], sent["headers"] = url, body, headers
        return response

    monkeypatch.setattr(endpoints_module, "_post", _post)
    return sent


MESSAGES_REPLY = {
    "model": "some-model",
    "content": [
        {"type": "text", "text": "thinking"},
        {"type": "tool_use", "id": "call_1", "name": "search_papers", "input": {"query": "x"}},
    ],
    "usage": {"input_tokens": 11, "output_tokens": 7},
}

CHAT_REPLY = {
    "model": "some-model",
    "choices": [
        {
            "message": {
                "content": "thinking",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "search_papers", "arguments": '{"query": "x"}'},
                    }
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
}


@pytest.mark.parametrize(
    "style,response", [("messages", MESSAGES_REPLY), ("chat", CHAT_REPLY)]
)
def test_both_wire_formats_parse_to_the_same_reply(monkeypatch, style, response):
    endpoint = Endpoint(name="t", base_url="http://x", model="m", api_style=style)
    sent = fake_transport(monkeypatch, response)
    reply = endpoint.send(endpoint.start("hello"), system="sys", tools=[SEARCH_TOOL])

    assert reply.text == "thinking"
    assert [(c.call_id, c.name, c.arguments) for c in reply.tool_calls] == [
        ("call_1", "search_papers", {"query": "x"})
    ]
    assert (reply.input_tokens, reply.output_tokens) == (11, 7)
    assert reply.reported_model == "some-model"

    # Each style must shape the tool schema and the system prompt its own way.
    body = sent["body"]
    if style == "messages":
        assert body["system"] == "sys"
        assert body["tools"][0]["input_schema"] == SEARCH_TOOL["parameters"]
        assert sent["url"].endswith("/v1/messages")
    else:
        assert body["messages"][0] == {"role": "system", "content": "sys"}
        assert body["tools"][0]["function"]["parameters"] == SEARCH_TOOL["parameters"]
        assert sent["url"].endswith("/v1/chat/completions")


@pytest.mark.parametrize("style", ["messages", "chat"])
def test_tool_results_re_enter_the_conversation_in_the_right_shape(style):
    endpoint = Endpoint(name="t", base_url="http://x", model="m", api_style=style)
    state = endpoint.start("hello")
    endpoint.append_tool_results(
        state,
        [ToolResult(call_id="call_1", payload={"ok": 1}),
         ToolResult(call_id="call_2", payload={"ok": 2})],
    )
    if style == "messages":
        # One user message carrying both tool_result blocks.
        assert len(state) == 2
        assert [b["tool_use_id"] for b in state[1]["content"]] == ["call_1", "call_2"]
    else:
        # One tool message per call.
        assert len(state) == 3
        assert [m["tool_call_id"] for m in state[1:]] == ["call_1", "call_2"]
        assert all(m["role"] == "tool" for m in state[1:])


def test_url_is_not_doubled_when_base_url_already_has_the_path():
    assert from_preset("local").url == "http://127.0.0.1:18765/v1/messages"
    explicit = Endpoint(
        name="t", base_url="http://x/v1/messages", model="m", api_style="messages"
    )
    assert explicit.url == "http://x/v1/messages"


def test_malformed_tool_arguments_are_reported_not_guessed(monkeypatch):
    broken = json.loads(json.dumps(CHAT_REPLY))
    broken["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
    endpoint = Endpoint(name="t", base_url="http://x", model="m", api_style="chat")
    fake_transport(monkeypatch, broken)
    with pytest.raises(EndpointError, match="not valid JSON"):
        endpoint.send(endpoint.start("hi"), system="s", tools=[])


def test_describe_never_exposes_the_key(monkeypatch):
    monkeypatch.setenv("ACG_TEST_KEY", "super-secret-value")
    endpoint = Endpoint(
        name="t", base_url="http://x", model="m", api_key_env="ACG_TEST_KEY"
    )
    description = json.dumps(endpoint.describe())
    assert "super-secret-value" not in description
    assert endpoint.describe()["api_key_present"] is True
    # The key does reach the header, which is the only place it belongs.
    assert endpoint._headers()["Authorization"] == "Bearer super-secret-value"


def test_unknown_api_style_is_rejected_at_construction():
    with pytest.raises(ValueError, match="unknown api_style"):
        Endpoint(name="t", base_url="http://x", model="m", api_style="grpc")


# --- the agent loop, driven by a scripted endpoint ----------------------------------


class ScriptedEndpoint(Endpoint):
    """An endpoint that replays a list of canned replies. Never touches the network."""

    def __init__(self, replies):
        super().__init__(name="scripted", base_url="http://x", model="scripted")
        self._replies = list(replies)
        self.sent = 0

    def send(self, state, *, system, tools, max_tokens=1600):
        self.sent += 1
        if not self._replies:
            raise EndpointError("script exhausted")
        return self._parse(self._replies.pop(0), 0.0)


def searched(query, call_id="c1"):
    return {
        "model": "scripted",
        "content": [
            {"type": "tool_use", "id": call_id, "name": "search_papers",
             "input": {"query": query}}
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def wrote(text):
    return {
        "model": "scripted",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def test_agent_captures_queries_verbatim_and_records_what_was_returned(tiny):
    endpoint = ScriptedEndpoint(
        [
            searched("  Sycophancy, EVALUATION?  "),
            wrote("We cite [acl:c].\nRECOMMENDED: acl:c"),
        ]
    )
    run = run_agent(
        tiny, "request", endpoint=endpoint, meter=Meter(max_requests=5), run_id="r"
    )
    assert run.status == "complete"
    # Logged before normalisation: casing, punctuation and whitespace survive.
    assert run.captured_queries == ["Sycophancy, EVALUATION?"]
    assert run.searches[0]["results"][0]["rank"] == 1
    assert run.bibliography == ["acl:c"]
    assert "acl:c" in run.shown_ids


def test_agent_stops_searching_at_the_quota_instead_of_running_free(tiny):
    endpoint = ScriptedEndpoint(
        [searched("sycophancy", f"c{i}") for i in range(4)] + [wrote("done [acl:a]")]
    )
    run = run_agent(
        tiny, "request", endpoint=endpoint, meter=Meter(max_requests=10), run_id="r",
        max_searches=2,
    )
    assert len(run.searches) == 2


def test_transport_failure_is_recorded_as_a_failure_not_an_empty_result(tiny):
    class Broken(ScriptedEndpoint):
        def send(self, state, *, system, tools, max_tokens=1600):
            raise EndpointError("connection refused")

    run = run_agent(
        tiny, "request", endpoint=Broken([]), meter=Meter(max_requests=3), run_id="r"
    )
    assert run.status == "transport_error"
    assert "connection refused" in run.failure
    # Crucially not reported as a clean run with zero citations.
    assert run.bibliography == []
    assert run.final_text is None


def test_inline_citations_count_when_the_recommended_line_is_missing(tiny):
    endpoint = ScriptedEndpoint(
        [searched("sycophancy"), wrote("Prior work [acl:a] is relevant.")]
    )
    run = run_agent(
        tiny, "request", endpoint=endpoint, meter=Meter(max_requests=3), run_id="r"
    )
    assert run.recommended_ids == []
    assert run.bibliography == ["acl:a"]
    assert run.status == "cited_inline_without_recommended_line"


# --- adversary ----------------------------------------------------------------------


def test_query_profile_counts_each_term_once_per_query():
    profile = QueryProfile.from_queries(
        ["sycophancy evaluation", "sycophancy benchmark", "sycophancy benchmark", ""]
    )
    assert profile.query_count == 2  # the duplicate collapses
    assert profile.term_frequency["sycophancy"] == 2
    assert profile.term_frequency["evaluation"] == 1


def test_overlap_variant_is_deterministic_and_keeps_the_published_abstract(tiny):
    original = tiny.records["acl:a"]
    profile = QueryProfile.from_queries(["sycophancy evaluation", "sycophancy languages"])
    first = overlap_variant(profile, original)
    assert first == overlap_variant(profile, original)
    title, abstract, construction = first
    assert construction["model_used"] is None
    assert construction["abstract_claims_changed"] is False
    assert abstract.endswith(original.abstract)  # published text survives verbatim
    assert title != original.title


def test_attempt_feedback_reports_only_what_an_author_could_observe():
    retrieved = AttemptFeedback(
        attempt=1, title="T", abstract="A", retrieved=True, best_rank=4, cited=False,
        competitor_titles=("Rival Paper",), queries=("sycophancy",),
    )
    rendered = retrieved.render()
    assert "retrieved at best rank 4" in rendered and "NOT cited" in rendered
    assert "Rival Paper" in rendered
    absent = AttemptFeedback(
        attempt=2, title="T", abstract="A", retrieved=False, best_rank=None, cited=False
    )
    assert "NOT retrieved at all" in absent.render()
    # There is no field in which the agent's hidden reasoning could be passed back.
    assert not hasattr(retrieved, "reasoning")


# --- the attack loop ----------------------------------------------------------------


def test_pick_target_chooses_the_best_ranked_paper_that_was_not_cited(tiny):
    endpoint = ScriptedEndpoint(
        [searched("sycophancy evaluation"), wrote("cite [acl:c]\nRECOMMENDED: acl:c")]
    )
    run = run_agent(
        tiny, "request", endpoint=endpoint, meter=Meter(max_requests=5), run_id="r"
    )
    target = pick_target(run, tiny)
    assert target is not None
    assert target != "acl:c"  # the cited paper is never the target
    assert target in run.shown_ids


def test_observe_separates_retrieval_rank_and_citation(tiny):
    endpoint = ScriptedEndpoint(
        [searched("sycophancy evaluation"), wrote("cite [acl:c]\nRECOMMENDED: acl:c")]
    )
    run = run_agent(
        tiny, "request", endpoint=endpoint, meter=Meter(max_requests=5), run_id="r"
    )
    seen = observe(run, "acl:a")
    assert seen["retrieved"] is True
    assert isinstance(seen["best_rank"], int)
    assert seen["cited"] is False  # retrieved but not cited: the distinction that matters


def test_attack_always_includes_the_unchanged_paper_control(tiny):
    """A caller cannot drop the control by omission, because the numbers need it."""
    endpoint = ScriptedEndpoint(
        [
            searched("sycophancy evaluation"),
            wrote("cite [acl:c]\nRECOMMENDED: acl:c"),
            # baseline strategy, one repetition
            searched("sycophancy evaluation"),
            wrote("cite [acl:c]\nRECOMMENDED: acl:c"),
            # overlap strategy, one repetition
            searched("sycophancy evaluation"),
            wrote("cite [acl:a]\nRECOMMENDED: acl:a"),
        ]
    )
    report = run_attack(
        tiny, "request", endpoint=endpoint, meter=Meter(max_requests=20),
        strategies=("overlap",), target_id="acl:a", repetitions=1,
    )
    names = [row["strategy"] for row in report["strategies"]]
    assert names[0] == "baseline"
    assert "overlap" in names

    control = next(r for r in report["strategies"] if r["strategy"] == "baseline")
    overlap = next(r for r in report["strategies"] if r["strategy"] == "overlap")
    assert control["construction"]["is_control"] is True
    assert control["construction"]["model_used"] is None
    assert control["citation_rate"] == 0.0
    assert overlap["citation_rate"] == 1.0
    # The difference is only reported because a control existed.
    assert overlap["citation_rate_minus_control"] == 1.0
    assert report["control_citation_rate"] == 0.0


def test_attack_states_why_the_target_selection_needs_a_control(tiny):
    endpoint = ScriptedEndpoint(
        [
            searched("sycophancy"), wrote("cite [acl:c]\nRECOMMENDED: acl:c"),
            searched("sycophancy"), wrote("cite [acl:c]\nRECOMMENDED: acl:c"),
        ]
    )
    report = run_attack(
        tiny, "request", endpoint=endpoint, meter=Meter(max_requests=10),
        strategies=("baseline",), target_id="acl:a",
    )
    assert "by selection" in report["target"]["selection_caveat"]


def test_attack_reports_discovery_failure_rather_than_a_zero_result(tiny):
    class Broken(ScriptedEndpoint):
        def send(self, state, *, system, tools, max_tokens=1600):
            raise EndpointError("refused")

    report = run_attack(
        tiny, "request", endpoint=Broken([]), meter=Meter(max_requests=5)
    )
    assert report["status"] == "discovery_failed"
    assert "strategies" not in report


# --- the corpus builder -------------------------------------------------------------


def test_multiline_quoted_bibtex_values_keep_every_author():
    """Line-based parsing used to drop every author after the first line."""
    entry = (
        '@inproceedings{key,\n'
        '    title = "A Paper",\n'
        '    author = "Zhang, Yongdong  and\n'
        '      Chi, Hao  and\n'
        '      Li, Wei",\n'
        '    url = "https://aclanthology.org/2025.acl-long.1/",\n'
        '}'
    )
    fields = parse_fields(entry)
    assert parse_authors(fields["author"]) == ["Yongdong Zhang", "Hao Chi", "Wei Li"]


def test_detex_renders_markup_without_discarding_content():
    # Brace-protected capitalisation is not content.
    assert detex("{BERT} and {AI}") == "BERT and AI"
    # Accents compose to real characters.
    assert detex(r"Sch\"olkopf") == "Schölkopf"
    # Symbol commands resolve rather than leaving a backslash behind.
    assert detex(r"3 \texttimes{} 4") == "3 × 4"
    # \b, \c, \d, \r, \u and \v are accent commands that also start longer names.
    assert detex(r"\alpha and \beta") == "α and β"
    assert detex(r"\delta \rho \rightarrow \uparrow \cdot") == "δ ρ → ↑ ·"
    # Genuine accents still apply, including the braced form.
    assert detex(r"\v{s}") == "š"
    assert detex(r"\c{c}") == "ç"
    assert "\\" not in detex(r"100\% of \textbf{cases}")
    assert detex(r"100\% of \textbf{cases}") == "100% of cases"


# --- against the real corpus, when it is present ------------------------------------


@needs_corpus
def test_real_corpus_is_all_published_papers_with_resolvable_urls():
    corpus = Corpus.published()
    assert len(corpus.records) > 1000
    assert all(not record.injected for record in corpus.records.values())
    assert all(
        record.url.startswith("https://aclanthology.org/")
        for record in corpus.records.values()
    )
    assert all(
        record.peer_review_status == "peer_reviewed_published"
        for record in corpus.records.values()
    )


@needs_corpus
def test_real_corpus_matches_its_manifest():
    from acg.paths import manifest_path

    manifest = json.loads(manifest_path().read_text())
    assert len(Corpus.published().records) == manifest["papers"]
    assert manifest["all_real_published_papers"] is True


def test_a_citation_for_a_paper_never_searched_for_is_dropped(tiny):
    """An agent that cites without searching has hallucinated, not retrieved."""
    endpoint = ScriptedEndpoint([wrote("Prior work [acl:a] is relevant.")])
    run = run_agent(
        tiny, "request", endpoint=endpoint, meter=Meter(max_requests=3), run_id="r"
    )
    assert run.shown_ids == []
    assert run.bibliography == []
    assert run.status == "no_citations"
