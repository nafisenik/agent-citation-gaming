"""The demo must not republish engineered metadata.

The recorded attack runs attach model-written titles and abstracts to eight real, named,
published papers, and include eight fabricated records with no study behind them. Publishing
that text would let it be scraped as a paper's own metadata and would imply the real authors
took part. So ``demo/build_story.py`` publishes the measurements and withholds the text,
keeping only a sha256 and a word count.

One condition is exempt on purpose. ``edit_saturated`` titles are produced by
``acg.adversary.overlap_variant``: no model is involved, and anyone holding the published
queries can regenerate them exactly. Withholding a string that the reader can derive from
the rest of the page would be theatre, not protection.

These tests read the full-fidelity runs, which live outside the repository. Without them
there is nothing to compare against, so they skip rather than pass silently.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

PUBLISHED_CONDITION = "edit_saturated"
MIN_LENGTH = 40  # below this, a match is more likely coincidence than a leak
DIGEST_CHARS = 16  # the export publishes a sha256 prefix, not the full digest

REPO = Path(__file__).resolve().parent.parent

# The full-fidelity runs are deliberately not in this repository. Point ACG_PRIVATE_RUNS at
# the directory holding the `attack-*` studies to enable these checks; without it they skip.
PRIVATE_RUNS = Path(
    os.environ.get("ACG_PRIVATE_RUNS", str(REPO.parent / "geo" / "outputs" / "discovery"))
)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _engineered_text() -> dict[str, set[str]]:
    """Every engineered string in the recorded runs, grouped by attack condition."""
    by_condition: dict[str, set[str]] = {}
    for study in PRIVATE_RUNS.glob("attack*"):
        for path in study.rglob("*.json"):
            try:
                loaded = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            for record in loaded if isinstance(loaded, list) else [loaded]:
                if not isinstance(record, dict):
                    continue
                attack = record.get("attack") or {}
                if not attack:
                    continue
                # Some run files carry the condition on the record rather than the attack.
                condition = attack.get("condition") or record.get("condition") or "UNLABELLED"
                bucket = by_condition.setdefault(condition, set())
                for key in ("experimental_title", "experimental_abstract"):
                    if attack.get(key):
                        bucket.add(attack[key].strip())
                for anchor in (attack.get("construction") or {}).get("anchors") or []:
                    # The anchor's `quote` is verbatim from the real published abstract and
                    # is fine to show; the `claim` is phrased from the rewrite.
                    if anchor.get("claim"):
                        bucket.add(anchor["claim"].strip())
                if record.get("final_text"):
                    # The review paraphrases the rewritten abstract, so it leaks by proxy.
                    bucket.add(record["final_text"].strip())
    return by_condition


def _repo_text() -> str:
    skip = {".venv", ".git", ".github"}
    parts = []
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        if skip & set(path.parts) or any(p.endswith("cache") for p in path.parts):
            continue
        parts.append(path.read_text(errors="ignore"))
    return _squash("\n".join(parts))


@pytest.fixture(scope="module")
def engineered() -> dict[str, set[str]]:
    found = _engineered_text() if PRIVATE_RUNS.is_dir() else {}
    if not found:
        pytest.skip(f"full-fidelity runs not available at {PRIVATE_RUNS}")
    return found


@pytest.fixture(scope="module")
def repo_text() -> str:
    return _repo_text()


def test_model_written_rewrites_are_absent_from_the_repository(engineered, repo_text):
    leaked = sorted(
        text[:80]
        for condition, texts in engineered.items()
        if condition != PUBLISHED_CONDITION
        for text in texts
        if len(text) >= MIN_LENGTH and _squash(text) in repo_text
    )
    assert leaked == [], f"engineered text published verbatim: {leaked}"


def test_the_withheld_conditions_are_actually_present_in_the_runs(engineered):
    # Guards the test above against passing because the extraction silently found nothing.
    withheld = {c: t for c, t in engineered.items() if c != PUBLISHED_CONDITION}
    assert withheld, "no withheld conditions found; the leak check would be vacuous"
    for condition, texts in withheld.items():
        assert any(len(t) >= MIN_LENGTH for t in texts), f"{condition} yielded no long strings"


def test_each_withheld_title_is_published_only_as_its_hash():
    story = json.loads((REPO / "demo" / "data" / "story.json").read_text())
    checked = 0
    for target in story["targets"]:
        for name, slot in target["strategies"].items():
            if slot.get("title"):  # the derivable condition, published in full
                continue
            digest = slot.get("title_sha256")
            assert digest, f"{target['paper_id']}/{name}: neither title nor hash"
            assert re.fullmatch(rf"[0-9a-f]{{{DIGEST_CHARS}}}", digest), f"not a digest: {digest}"
            assert slot.get("title_words"), f"{target['paper_id']}/{name}: no word count"
            checked += 1
    assert checked, "no withheld titles found in the exported story"


def test_the_published_hash_matches_the_private_text(engineered):
    """A hash nobody can check is decoration. Verify it against the real withheld text."""
    import hashlib

    story = json.loads((REPO / "demo" / "data" / "story.json").read_text())
    known = {
        hashlib.sha256(text.encode()).hexdigest()[:DIGEST_CHARS]
        for texts in engineered.values()
        for text in texts
    }
    digests = [
        slot["title_sha256"]
        for target in story["targets"]
        for slot in target["strategies"].values()
        if not slot.get("title") and slot.get("title_sha256")
    ]
    assert digests, "no withheld hashes to verify"
    unmatched = [d for d in digests if d not in known]
    assert unmatched == [], f"hashes do not correspond to any recorded text: {unmatched[:3]}"
