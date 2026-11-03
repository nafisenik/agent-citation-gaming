"""Measure whether query-informed metadata rewriting changes which papers an agent cites.

A literature-search agent issues its own search queries. This package captures those
queries, lets an author rewrite a paper's title and abstract against them, and re-measures
whether the agent cites that paper -- keeping retrieval, rank and citation distinct, and
always running the unchanged paper as a control.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
