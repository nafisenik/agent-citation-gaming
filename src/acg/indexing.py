"""BM25 retrieval over the paper corpus, with a posting-list candidate scan.

Only documents containing at least one query term are scored, which is what makes a
16,000-paper corpus searchable in milliseconds inside an agent loop.

The tokenizer is a required argument rather than a default. An earlier version of this code
carried a general-purpose stopword list as its default, and a corpus that needed a different
one silently got the wrong tokenizer for part of a study; the runs had to be thrown away.
Passing it explicitly makes that mistake impossible to make quietly.
"""

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable

# Standard BM25 term-saturation and length-normalisation parameters.
K1 = 1.2
B = 0.75


class SparseBM25:
    name = "bm25-sparse"

    def __init__(
        self,
        documents: dict[str, str],
        tokenizer: Callable[[str], Iterable[str]],
    ):
        self.tokenizer = tokenizer
        counts = {
            paper_id: Counter(self.tokenizer(text))
            for paper_id, text in documents.items()
        }
        self.docs = counts
        self.lengths = {pid: sum(c.values()) for pid, c in counts.items()}
        self.avg = sum(self.lengths.values()) / max(1, len(counts)) or 1

        postings: defaultdict[str, set[str]] = defaultdict(set)
        for paper_id, terms in counts.items():
            for term in terms:
                postings[term].add(paper_id)
        self.postings = dict(postings)

        total = len(counts)
        self.idf = {
            term: math.log(1 + (total - len(ids) + 0.5) / (len(ids) + 0.5))
            for term, ids in postings.items()
        }

    def rank(self, query: str, limit: int, tie_order: dict[str, int]) -> list[dict]:
        """Score only documents that contain a query term. Ties break on a stable order."""
        terms = sorted(set(self.tokenizer(query)))
        candidates: set[str] = set()
        for term in terms:
            candidates |= self.postings.get(term, set())

        hits = []
        for paper_id in candidates:
            counts = self.docs[paper_id]
            normaliser = K1 * (1 - B + B * self.lengths[paper_id] / self.avg)
            score = 0.0
            for term in terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                score += (
                    self.idf.get(term, 0.0)
                    * frequency
                    * (K1 + 1)
                    / (frequency + normaliser)
                )
            if score > 0:
                hits.append({"paper_id": paper_id, "score": score})
        return sorted(hits, key=lambda hit: (-hit["score"], tie_order[hit["paper_id"]]))[
            :limit
        ]
