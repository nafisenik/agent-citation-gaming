"""Command line interface.

    acg corpus download          fetch the ACL Anthology snapshot and build the corpus
    acg corpus info              what is in the local corpus
    acg search "query"           rank the corpus for one query (no model call)
    acg endpoints                list endpoint presets
    acg run "request"            one agent episode; prints the queries it issued
    acg attack "request"         capture queries, rewrite a paper, measure what changes

Only ``run`` and ``attack`` make model requests, and both require ``--max-requests``.
"""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .adversary import STRATEGIES
from .attack import DEFAULT_STRATEGIES, run_attack, save_report
from .budget import BudgetExceeded, Meter
from .corpus import Corpus
from .download import build, fetch
from .endpoints import PRESETS, Endpoint, from_preset
from .paths import corpus_path, data_dir, manifest_path


def _endpoint(arguments) -> Endpoint:
    overrides = {
        key: value
        for key, value in (
            ("model", arguments.model),
            ("base_url", arguments.base_url),
            ("api_style", arguments.api_style),
            ("api_key_env", arguments.api_key_env),
        )
        if value
    }
    return from_preset(arguments.endpoint, **overrides)


def _add_endpoint_arguments(parser):
    parser.add_argument(
        "--endpoint", default="local", choices=sorted(PRESETS), help="endpoint preset"
    )
    parser.add_argument("--model", help="override the preset's model name")
    parser.add_argument("--base-url", help="override the preset's base URL")
    parser.add_argument("--api-style", choices=("messages", "chat"), help="wire format")
    parser.add_argument("--api-key-env", help="env var holding the API key")
    parser.add_argument(
        "--max-requests",
        type=int,
        required=True,
        help="hard cap on live model requests for this command",
    )


def cmd_corpus_download(arguments) -> int:
    fetch(force=arguments.force)
    manifest = build()
    print(json.dumps({k: v for k, v in manifest.items() if k != "notes"}, indent=2))
    return 0


def cmd_corpus_info(arguments) -> int:
    if not corpus_path().exists():
        print(f"no corpus at {corpus_path()}\nrun: acg corpus download", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path().read_text()) if manifest_path().exists() else {}
    corpus = Corpus.published()
    print(f"data directory : {data_dir()}")
    print(f"papers         : {len(corpus.records):,}")
    print(f"source         : {manifest.get('source_url', 'unknown')}")
    print(f"source sha256  : {manifest.get('source_sha256', 'unknown')}")
    print(f"built at       : {manifest.get('built_at', 'unknown')}")
    for code, count in (manifest.get("by_venue_code") or {}).items():
        print(f"  {code:24s} {count:>6,}")
    return 0


def cmd_search(arguments) -> int:
    corpus = Corpus.published()
    hits = corpus.search(arguments.query, arguments.top)
    if not hits:
        print("no matches")
        return 0
    for hit in hits:
        record = hit["record"]
        print(f"{hit['rank']:>3}. [{hit['score']:7.3f}] {record.paper_id}")
        print(f"     {record.title}")
    return 0


def cmd_endpoints(arguments) -> int:
    for name, preset in sorted(PRESETS.items()):
        endpoint = from_preset(name)
        description = endpoint.describe()
        marker = "key set" if description["api_key_present"] else "no key"
        print(f"{name:12s} {preset['api_style']:9s} {endpoint.url}")
        print(f"             model={preset['model']}  {preset['api_key_env']} ({marker})")
    return 0


def cmd_run(arguments) -> int:
    from .agent import run_agent

    corpus = Corpus.published()
    endpoint = _endpoint(arguments)
    meter = Meter(max_requests=arguments.max_requests)
    print(f"corpus   : {len(corpus.records):,} papers")
    print(f"endpoint : {endpoint.url} ({endpoint.api_style}), model {endpoint.model}")
    try:
        run = run_agent(
            corpus,
            arguments.query,
            endpoint=endpoint,
            meter=meter,
            run_id="cli-run",
            results_per_query=arguments.results_per_query,
            max_searches=arguments.max_searches,
        )
    except BudgetExceeded as error:
        print(f"stopped: {error}", file=sys.stderr)
        return 1

    print(f"\nstatus   : {run.status}")
    if run.failure:
        print(f"failure  : {run.failure}", file=sys.stderr)
        return 1
    print(f"reported model(s): {', '.join(run.reported_models) or 'none reported'}")
    print(f"\nqueries it issued ({len(run.captured_queries)}):")
    for index, query in enumerate(run.captured_queries, 1):
        print(f"  {index}. {query}")
    print(f"\npapers shown: {len(run.shown_ids)}")
    print(f"cited ({len(run.bibliography)}):")
    for paper_id in run.bibliography:
        title = corpus.records[paper_id].title if paper_id in corpus.records else "?"
        print(f"  {paper_id}  {title[:70]}")
    print(f"\nrequests used: {meter.requests}/{meter.max_requests}")
    if arguments.out:
        path = Path(arguments.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(run.to_json(), indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {path}")
    return 0


def cmd_attack(arguments) -> int:
    corpus = Corpus.published()
    endpoint = _endpoint(arguments)
    meter = Meter(max_requests=arguments.max_requests)

    print(f"corpus     : {len(corpus.records):,} papers")
    print(f"endpoint   : {endpoint.url} ({endpoint.api_style}), model {endpoint.model}")
    print(f"strategies : {', '.join(arguments.strategies)}")
    print(f"budget     : {arguments.max_requests} requests\n")

    def event(kind, **payload):
        if kind == "discovery_start":
            print("discovery run ...")
        elif kind == "discovery_done":
            print(f"  {len(payload['queries'])} queries captured, {payload['status']}")
            for query in payload["queries"]:
                print(f"    - {query}")
            print(f"  cited: {', '.join(payload['cited']) or 'nothing'}")
        elif kind == "target_chosen":
            print(f"\ntarget: {payload['target_id']} (rank {payload['rank']}, uncited)")
            print(f"  {payload['title'][:76]}")
            print()
        elif kind == "execution":
            flag = "CITED" if payload["cited"] else ("shown" if payload["retrieved"] else "absent")
            rank = payload["best_rank"]
            print(
                f"  {payload['strategy']:10s} a{payload['attempt']}r{payload['repetition']} "
                f"rank={str(rank) if rank else '-':>3}  {flag:6s} "
                f"refs={payload['references']}  requests={meter.requests}"
            )
        elif kind == "strategy_failed":
            print(f"  {payload['strategy']:10s} FAILED {payload['error']}")

    report = run_attack(
        corpus,
        arguments.query,
        endpoint=endpoint,
        meter=meter,
        strategies=tuple(arguments.strategies),
        target_id=arguments.target,
        repetitions=arguments.repetitions,
        max_attempts=arguments.max_attempts,
        results_per_query=arguments.results_per_query,
        max_searches=arguments.max_searches,
        on_event=event,
    )

    if report["status"] in ("discovery_failed", "no_target"):
        print(f"\n{report['status']}: {report.get('failure') or report.get('note')}")
        return 1

    control = report["control_citation_rate"]
    print("\n" + "=" * 68)
    print(f"{'strategy':12s} {'retrieved':>10s} {'cited':>10s} {'vs control':>12s}")
    print("-" * 68)
    for row in report["strategies"]:
        rate = row["citation_rate"]
        difference = row["citation_rate_minus_control"]
        print(
            f"{row['strategy']:12s} "
            f"{_percent(row['retrieval_rate']):>10s} "
            f"{_percent(rate):>10s} "
            f"{('—' if difference is None else f'{difference:+.2f}'):>12s}"
        )
    print("=" * 68)
    if control is None:
        print("No usable control executions, so no difference can be computed.")
    print(f"requests used: {meter.requests}/{meter.max_requests}")

    if arguments.out:
        directory = Path(arguments.out)
        private = {
            row["strategy"]: {
                "construction_method": row["construction"].get("method"),
            }
            for row in report["strategies"]
        }
        path = save_report(report, directory, private_text=private)
        print(f"wrote {path}")
    return 0


def _percent(value) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acg",
        description=(
            "Measure whether rewriting a paper's title and abstract from an agent's own "
            "observed search queries changes whether that agent cites it."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    corpus = sub.add_parser("corpus", help="manage the local paper corpus")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)
    download = corpus_sub.add_parser("download", help="fetch and build the corpus")
    download.add_argument("--force", action="store_true", help="re-download the snapshot")
    download.set_defaults(handler=cmd_corpus_download)
    info = corpus_sub.add_parser("info", help="show local corpus details")
    info.set_defaults(handler=cmd_corpus_info)

    search = sub.add_parser("search", help="rank the corpus for a query (no model call)")
    search.add_argument("query")
    search.add_argument("--top", type=int, default=10)
    search.set_defaults(handler=cmd_search)

    endpoints = sub.add_parser("endpoints", help="list endpoint presets")
    endpoints.set_defaults(handler=cmd_endpoints)

    run = sub.add_parser("run", help="one agent episode over the unmodified corpus")
    run.add_argument("query", help="the research request to give the agent")
    run.add_argument("--results-per-query", type=int, default=8)
    run.add_argument("--max-searches", type=int, default=6)
    run.add_argument("--out", help="write the run JSON here")
    _add_endpoint_arguments(run)
    run.set_defaults(handler=cmd_run)

    attack = sub.add_parser("attack", help="capture queries, rewrite a paper, re-measure")
    attack.add_argument("query", help="the research request to give the agent")
    attack.add_argument(
        "--strategies",
        nargs="+",
        default=list(DEFAULT_STRATEGIES),
        choices=list(STRATEGIES),
        help="baseline is always included: it is the unchanged-paper control",
    )
    attack.add_argument("--target", help="paper_id to target instead of auto-selecting")
    attack.add_argument("--repetitions", type=int, default=1)
    attack.add_argument("--max-attempts", type=int, default=3, help="adaptive budget")
    attack.add_argument("--results-per-query", type=int, default=8)
    attack.add_argument("--max-searches", type=int, default=6)
    attack.add_argument("--out", help="directory to write the report into")
    _add_endpoint_arguments(attack)
    attack.set_defaults(handler=cmd_attack)

    return parser


def main(argv=None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        return arguments.handler(arguments)
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
