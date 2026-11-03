"""The paper corpus and the index a literature-search agent searches through.

Records loaded here are real published papers, built from the ACL Anthology's own bulk
bibliography by :mod:`acg.download`. Two kinds of change can be layered on top:

* an **edit**, which rewrites the title and abstract of a paper already in the corpus; and
* an **injection**, which adds a record that is not in the published corpus at all.

Both are experiment-local. They exist in this process's index and in run logs, are never
written back to ``papers.jsonl``, and are never published or submitted anywhere. An injected
record is flagged ``injected=True`` everywhere it appears so no analysis can mistake it for a
real publication.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .indexing import SparseBM25
from .paths import corpus_path, manifest_path

# English function words only. A general-purpose stopword list that also removes words like
# "evaluation", "research", "study" and "compare" is wrong for a corpus of NLP papers: there
# those are topical content words, and dropping them from a query measurably degrades
# retrieval.
STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because been before
being below between both but by can cannot could did do does doing down during each few for
from further had has have having he her here hers herself him himself his how i if in into is
it its itself me more most my myself nor not of off on once only or other ought our ours
ourselves out over own same she should so some such than that the their theirs them themselves
then there these they this those through to too under until up very was we were what when where
which while who whom why will with would you your yours yourself yourselves""".split()
)

TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    """Tokenise for retrieval: lowercase alphanumerics minus English function words."""
    return [t for t in TOKEN.findall(text.lower()) if t not in STOPWORDS]


@dataclass(frozen=True)
class Record:
    """One searchable entry. ``injected`` separates experimental records from published ones."""

    paper_id: str
    title: str
    abstract: str
    authors: tuple[str, ...] = ()
    venue: str = ""
    year: int = 0
    track: str = ""
    url: str = ""
    peer_review_status: str = "unknown"
    hosting: str = ""
    injected: bool = False
    provenance_note: str = ""

    @property
    def searchable_text(self) -> str:
        return f"{self.title} {self.abstract}"

    def venue_label(self) -> str:
        if self.injected:
            return self.provenance_note or "preprint, review status unknown"
        return f"{self.venue} {self.year} ({self.track})".strip()

    def tool_payload(self, abstract_characters: int = 600) -> dict:
        """What the agent actually sees. Truthful for real records; never forges a venue."""
        abstract = self.abstract
        if len(abstract) > abstract_characters:
            cut = abstract.rfind(" ", 0, abstract_characters)
            abstract = abstract[: cut if cut > 0 else abstract_characters] + "..."
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "authors": list(self.authors[:8]),
            "venue": self.venue_label(),
            "abstract": abstract,
        }


def load_records(path: Path | None = None) -> dict[str, Record]:
    path = path or corpus_path()
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run `acg corpus download` first")
    records: dict[str, Record] = {}
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            records[row["paper_id"]] = Record(
                paper_id=row["paper_id"],
                title=row["title"],
                abstract=row["abstract"],
                authors=tuple(row.get("authors", ())),
                venue=row.get("venue", ""),
                year=row.get("year", 0),
                track=row.get("track", ""),
                url=row.get("url", ""),
                peer_review_status=row.get("peer_review_status", "unknown"),
                hosting=row.get("hosting", ""),
                injected=False,
            )
    return records


@dataclass
class Corpus:
    """A searchable corpus state: published records plus any experimental overlay."""

    records: dict[str, Record]
    index: SparseBM25 = field(init=False)
    tie_order: dict[str, int] = field(init=False)
    edited_ids: tuple[str, ...] = ()
    injected_ids: tuple[str, ...] = ()

    def __post_init__(self):
        self._rebuild()

    def _rebuild(self):
        ordered = sorted(self.records)
        self.tie_order = {pid: i for i, pid in enumerate(ordered)}
        self.index = SparseBM25(
            {pid: self.records[pid].searchable_text for pid in ordered},
            tokenizer=tokens,
        )

    @classmethod
    def published(cls, path: Path | None = None) -> "Corpus":
        return cls(records=load_records(path))

    def manifest(self) -> dict:
        path = manifest_path()
        source = json.loads(path.read_text()) if path.exists() else {}
        return {
            "published_papers": sum(1 for r in self.records.values() if not r.injected),
            "injected_records": list(self.injected_ids),
            "edited_records": list(self.edited_ids),
            "source_sha256": source.get("source_sha256"),
            "source_url": source.get("source_url"),
        }

    def variant(
        self,
        *,
        edits: dict[str, tuple[str, str]] | None = None,
        injections: tuple[Record, ...] = (),
    ) -> "Corpus":
        """Return a new corpus with metadata edits and/or injected records applied.

        ``edits`` maps an existing paper_id to a (title, abstract) pair. The paper must
        already exist: an edit changes how a real paper presents itself, it does not create
        one. ``injections`` adds records absent from the published corpus; each is
        force-flagged ``injected=True`` so it stays distinguishable downstream.
        """
        records = dict(self.records)
        edited: list[str] = []
        for paper_id, (title, abstract) in (edits or {}).items():
            if paper_id not in records:
                raise KeyError(f"cannot edit unknown paper {paper_id}")
            original = records[paper_id]
            if original.injected:
                raise ValueError(f"{paper_id} is an injected record, not a publication")
            records[paper_id] = Record(
                **{**original.__dict__, "title": title, "abstract": abstract}
            )
            edited.append(paper_id)
        injected: list[str] = []
        for record in injections:
            if record.paper_id in records:
                raise ValueError(
                    f"{record.paper_id} already exists; injection must use a fresh id"
                )
            records[record.paper_id] = Record(**{**record.__dict__, "injected": True})
            injected.append(record.paper_id)
        return Corpus(
            records=records,
            edited_ids=tuple(edited),
            injected_ids=tuple(injected),
        )

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Rank the corpus for one query. Returns ranked hits with scores and flags."""
        hits = self.index.rank(query, limit, self.tie_order)
        results = []
        for rank, hit in enumerate(hits, 1):
            record = self.records[hit["paper_id"]]
            results.append(
                {
                    "rank": rank,
                    "paper_id": record.paper_id,
                    "score": round(hit["score"], 6),
                    "injected": record.injected,
                    "record": record,
                }
            )
        return results

    def query_terms(self, query: str) -> list[str]:
        return sorted(set(tokens(query)))


def term_coverage(query: str, text: str) -> float:
    """Fraction of a query's distinct terms present in a text. A lexical measure only."""
    terms = set(tokens(query))
    if not terms:
        return 0.0
    present = set(tokens(text))
    return len(terms & present) / len(terms)


WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")


def word_count(text: str) -> int:
    return len(WORD.findall(text))
