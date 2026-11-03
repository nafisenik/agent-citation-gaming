"""Where the package keeps the corpus it downloads.

An installed package cannot assume it is being run from a checkout, so data lives in a
stable per-user directory rather than a path relative to the working directory. Set
``ACG_DATA_DIR`` to override, which is also how tests point at a temporary directory.
"""

import os
from pathlib import Path

ENV_VAR = "ACG_DATA_DIR"
DEFAULT = Path.home() / ".cache" / "agent-citation-gaming"


def data_dir() -> Path:
    """Root of the local data directory. Not created as a side effect of reading it."""
    override = os.environ.get(ENV_VAR)
    return Path(override).expanduser() if override else DEFAULT


def ensure_data_dir() -> Path:
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def corpus_path() -> Path:
    return data_dir() / "papers.jsonl"


def manifest_path() -> Path:
    return data_dir() / "manifest.json"


def bibliography_path() -> Path:
    return data_dir() / "anthology+abstracts.bib.gz"


def runs_dir() -> Path:
    return data_dir() / "runs"
