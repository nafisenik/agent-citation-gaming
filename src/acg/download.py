"""Download the paper corpus from the ACL Anthology and render it to plain text.

Input is the Anthology's own published ``anthology+abstracts.bib.gz``. Every record is a
real, peer-reviewed, archivally published paper. Nothing is generated: titles, authors and
abstracts are the published strings, and the manifest keeps the byte hash of the snapshot so
a later experiment can prove which snapshot it ran against.

Two things here are easy to get wrong and were got wrong before:

1. **BibTeX values quoted with ``"..."`` span multiple lines.** Line-based parsing that only
   tracks brace depth silently drops every author after the first line. Entries are scanned
   whole.
2. **The Anthology brace-protects capitalisation** (``{AI}``, ``{BERT}``) and uses LaTeX
   accents and math commands. A real de-TeX layer is required, and the count of records with
   residual markup is reported in the manifest rather than assumed to be zero.
"""

import gzip
import hashlib
import json
import re
import unicodedata
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from .paths import bibliography_path, corpus_path, ensure_data_dir, manifest_path

SOURCE_URL = "https://aclanthology.org/anthology+abstracts.bib.gz"

# Venue codes verified present in the snapshot, not guessed. Main-conference and Findings
# tracks only: demo, industry, SRW and shared-task papers have different review regimes and
# would confound any peer-review-status analysis.
VENUES = {
    "2025.acl-long": ("ACL", 2025, "main", "long"),
    "2025.acl-short": ("ACL", 2025, "main", "short"),
    "2025.findings-acl": ("ACL", 2025, "findings", "unspecified"),
    "2025.emnlp-main": ("EMNLP", 2025, "main", "unspecified"),
    "2025.findings-emnlp": ("EMNLP", 2025, "findings", "unspecified"),
    "2026.acl-long": ("ACL", 2026, "main", "long"),
    "2026.acl-short": ("ACL", 2026, "main", "short"),
    "2026.findings-acl": ("ACL", 2026, "findings", "unspecified"),
    "2026.emnlp-main": ("EMNLP", 2026, "main", "unspecified"),
    "2026.findings-emnlp": ("EMNLP", 2026, "findings", "unspecified"),
}

ENTRY = re.compile(r"^@(\w+)\s*\{\s*([^,]*),", re.MULTILINE)
ANTHOLOGY_URL = re.compile(r"^https://aclanthology\.org/([0-9]{4}\.[a-z0-9-]+\.[0-9]+)/?$")


def fetch(force: bool = False, progress=print) -> Path:
    """Download the bibliography snapshot unless it is already present."""
    ensure_data_dir()
    target = bibliography_path()
    if target.exists() and not force:
        progress(f"snapshot present: {target} ({target.stat().st_size:,} bytes)")
        return target
    progress(f"downloading {SOURCE_URL}")
    request = urllib.request.Request(
        SOURCE_URL, headers={"User-Agent": "agent-citation-gaming"}
    )
    partial = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(request, timeout=600) as response:
        partial.write_bytes(response.read())
    partial.replace(target)
    progress(f"saved {target} ({target.stat().st_size:,} bytes)")
    return target


def raw_entries(path: Path):
    """Yield complete ``@type{...}`` entry texts, tracking brace depth outside escapes."""
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as stream:
        text = stream.read()
    position = 0
    while True:
        start = text.find("@", position)
        if start < 0:
            return
        header = ENTRY.match(text, start)
        if not header:
            position = start + 1
            continue
        index = text.index("{", start)
        depth = 0
        while index < len(text):
            character = text[index]
            if character == "\\":
                index += 2
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    yield header.group(1), header.group(2).strip(), text[start : index + 1]
                    break
            index += 1
        position = index + 1


def parse_fields(entry: str) -> dict[str, str]:
    """Scan ``name = {..}`` / ``name = ".."`` / ``name = bare`` pairs at the top level."""
    body = entry[entry.index("{") + 1 :].rstrip()
    if body.endswith("}"):
        body = body[:-1]
    fields: dict[str, str] = {}
    index = body.find(",") + 1
    size = len(body)
    while index < size:
        assign = re.compile(r"\s*([A-Za-z][A-Za-z0-9_-]*)\s*=\s*").match(body, index)
        if not assign:
            # Not a field assignment; skip to the next candidate separator.
            following = body.find(",", index)
            if following < 0:
                break
            index = following + 1
            continue
        name = assign.group(1).lower()
        cursor = assign.end()
        if cursor >= size:
            break
        opener = body[cursor]
        if opener in '{"':
            closer = "}" if opener == "{" else '"'
            depth = 0
            scan = cursor
            while scan < size:
                character = body[scan]
                if character == "\\":
                    scan += 2
                    continue
                if opener == "{":
                    if character == "{":
                        depth += 1
                    elif character == "}":
                        depth -= 1
                        if depth == 0:
                            break
                elif character == closer and scan > cursor:
                    break
                scan += 1
            fields[name] = body[cursor + 1 : scan]
            index = scan + 1
        else:
            following = body.find(",", cursor)
            following = size if following < 0 else following
            fields[name] = body[cursor:following].strip()
            index = following
        comma = body.find(",", index)
        if comma < 0:
            break
        index = comma + 1
    return fields


# Commands that wrap content: drop the command, keep the braces so content survives.
WRAPPER = re.compile(
    r"\\(?:textbf|textit|textsl|textsc|texttt|textrm|textnormal|emph|text|mathrm"
    r"|mathbf|mathit|mathcal|boldsymbol|mbox|hbox|ensuremath|protect|relax|uppercase"
    r"|lowercase|textsuperscript|textsubscript|acute|grave|bar|vec|overline|underline)\s*(?=\{)"
)
# Accent commands applied to the following character or braced group.
#
# Two forms, deliberately separated. A symbolic accent (\"o, \'{e}) cannot be confused with
# anything else because its command character is not a letter. A letter accent (\v, \c, \u,
# ...) is also the first letter of many command names, so it is only recognised in the
# braced form or followed by whitespace. Matching a bare letter accent greedily turns every
# unrecognised command into mojibake: \unknown would become "n" plus a breve plus "known".
ACCENT_COMMAND = re.compile(
    r"\\([\"'`^~=.,])\s*\{?([A-Za-z])\}?"
    r"|\\([bcdHkruv])(?:\s*\{([A-Za-z])\}|\s+([A-Za-z])(?![A-Za-z]))"
)

ACCENT_SUFFIX = {
    '"': "\u0308", "'": "\u0301", "`": "\u0300", "^": "\u0302", "~": "\u0303",
    "=": "\u0304", ".": "\u0307", ",": "\u0327", "c": "\u0327", "k": "\u0328",
    "v": "\u030c", "u": "\u0306", "r": "\u030a", "H": "\u030b", "b": "\u0331",
    "d": "\u0323",
}


def _apply_accent(match: re.Match) -> str:
    symbolic, symbolic_letter, letter, braced, spaced = match.groups()
    if symbolic:
        return symbolic_letter + ACCENT_SUFFIX.get(symbolic, "")
    return (braced or spaced) + ACCENT_SUFFIX.get(letter, "")
# Standalone symbol and letter commands with a direct character equivalent.
SYMBOL = {
    "texttwosuperior": "\u00b2", "textthreesuperior": "\u00b3",
    "textonesuperior": "\u00b9", "textdegree": "\u00b0", "textmu": "\u00b5",
    "textendash": "\u2013", "textemdash": "\u2014", "textquoteright": "\u2019",
    "textquoteleft": "\u2018", "textbullet": "\u2022", "textrightarrow": "\u2192",
    "textleftarrow": "\u2190", "ss": "\u00df", "ae": "\u00e6", "AE": "\u00c6",
    "oe": "\u0153", "OE": "\u0152", "aa": "\u00e5", "AA": "\u00c5", "o": "\u00f8",
    "O": "\u00d8", "l": "\u0142", "L": "\u0141", "i": "\u0131", "j": "\u0237",
    "dag": "\u2020", "ldots": "\u2026", "dots": "\u2026", "times": "\u00d7",
    "to": "\u2192", "rightarrow": "\u2192", "leftarrow": "\u2190", "alpha": "\u03b1",
    "beta": "\u03b2", "gamma": "\u03b3", "delta": "\u03b4", "epsilon": "\u03b5",
    "theta": "\u03b8", "lambda": "\u03bb", "mu": "\u03bc", "pi": "\u03c0",
    "sigma": "\u03c3", "tau": "\u03c4", "phi": "\u03c6", "psi": "\u03c8",
    "omega": "\u03c9", "Delta": "\u0394", "Gamma": "\u0393", "Lambda": "\u039b",
    "Omega": "\u03a9", "Sigma": "\u03a3", "Phi": "\u03a6", "Psi": "\u03a8",
    "Theta": "\u0398", "Pi": "\u03a0", "texttimes": "\u00d7",
    "textasciitilde": "~", "textasciicircum": "^", "textbackslash": "\\",
    "textless": "<", "textgreater": ">", "textbar": "|", "sim": "~", "par": " ",
    "star": "\u2605", "in": "\u2208", "notin": "\u2209", "subset": "\u2282",
    "subseteq": "\u2286", "cup": "\u222a", "cap": "\u2229", "ge": "\u2265",
    "le": "\u2264", "gg": "\u226b", "ll": "\u226a", "equiv": "\u2261",
    "propto": "\u221d", "cdot": "\u00b7", "cdots": "\u22ef", "div": "\u00f7",
    "circ": "\u2218", "forall": "\u2200", "exists": "\u2203", "nabla": "\u2207",
    "partial": "\u2202", "sum": "\u2211", "prod": "\u220f", "sqrt": "\u221a",
    "leftrightarrow": "\u2194", "Leftrightarrow": "\u21d4", "Rightarrow": "\u21d2",
    "Leftarrow": "\u21d0", "uparrow": "\u2191", "downarrow": "\u2193",
    "mapsto": "\u21a6", "degree": "\u00b0", "euro": "\u20ac", "pounds": "\u00a3",
    "copyright": "\u00a9", "eta": "\u03b7", "zeta": "\u03b6", "iota": "\u03b9",
    "kappa": "\u03ba", "nu": "\u03bd", "xi": "\u03be", "rho": "\u03c1",
    "upsilon": "\u03c5", "chi": "\u03c7", "varepsilon": "\u03b5",
    "varphi": "\u03c6", "vartheta": "\u03d1", "Xi": "\u039e", "Upsilon": "\u03a5",
    "infty": "\u221e", "leq": "\u2264", "geq": "\u2265", "neq": "\u2260",
    "approx": "\u2248", "pm": "\u00b1",
}
LITERAL = {
    "{``}": '"', "{''}": '"', "{---}": "\u2014", "{--}": "\u2013",
    "---": "\u2014", "--": "\u2013", "``": '"', "''": '"',
}
ESCAPED_PUNCTUATION = re.compile(r"\\([%&_$#{}])")


def detex(value: str) -> str:
    """Render a BibTeX value as the plain text a reader sees, without dropping content."""
    text = value.strip()
    if text.startswith('"') and text.endswith('"') and len(text) > 1:
        text = text[1:-1]
    text = re.sub(r"\\(?:url|href)\s*(?=\{)", "", text)
    for _ in range(8):
        replaced = WRAPPER.sub("", text)
        if replaced == text:
            break
        text = replaced
    # Named commands resolve BEFORE accents, and the order matters. Several accent
    # commands are single letters that also begin a longer command name -- \b, \c, \d,
    # \r, \u, \v -- so an accent-first pass turns \beta into "e" plus a macron-below plus
    # "ta", and \rightarrow into "h" plus a ring plus "ightarrow". The damage leaves no
    # backslash behind, so a residual-markup count does not notice it.
    text = re.sub(
        r"\\([A-Za-z]+)(?![A-Za-z])",
        lambda m: SYMBOL.get(m.group(1), m.group(0)),
        text,
    )
    for _ in range(4):
        replaced = ACCENT_COMMAND.sub(_apply_accent, text)
        if replaced == text:
            break
        text = replaced
    for source, target in LITERAL.items():
        text = text.replace(source, target)
    text = ESCAPED_PUNCTUATION.sub(r"\1", text)
    text = text.replace("~", " ").replace("\\ ", " ")
    # The Anthology brace-protects capitalisation ({AI}, {BERT}); braces are not content.
    for _ in range(8):
        replaced = re.sub(r"\{([^{}]*)\}", r"\1", text)
        if replaced == text:
            break
        text = replaced
    text = unicodedata.normalize("NFC", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_authors(value: str) -> list[str]:
    names = []
    for chunk in detex(value).split(" and "):
        name = chunk.strip().rstrip(",").strip()
        if not name:
            continue
        if "," in name:
            family, _, given = name.partition(",")
            name = f"{given.strip()} {family.strip()}".strip()
        names.append(name)
    return names


def build(source: Path | None = None, progress=print) -> dict:
    """Parse the snapshot into ``papers.jsonl`` and write the manifest. Returns the manifest."""
    source = source or bibliography_path()
    if not source.exists():
        raise FileNotFoundError(f"missing snapshot: {source}; run fetch() first")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    rows: list[dict] = []
    seen: set[str] = set()
    skipped_no_abstract = 0
    for kind, _key, entry in raw_entries(source):
        if kind not in {"inproceedings", "article"}:
            continue
        fields = parse_fields(entry)
        url = detex(fields.get("url", ""))
        match = ANTHOLOGY_URL.match(url)
        if not match:
            continue
        anthology_id = match.group(1)
        venue_code = anthology_id.rsplit(".", 1)[0]
        venue = VENUES.get(venue_code)
        if venue is None:
            continue
        title = detex(fields.get("title", ""))
        abstract = detex(fields.get("abstract", ""))
        if not title:
            continue
        if not abstract:
            # Keep the count: a missing abstract is a gap in the published record, and a
            # paper with no abstract cannot take part in a metadata-presentation study.
            skipped_no_abstract += 1
            continue
        if anthology_id in seen:
            continue
        seen.add(anthology_id)
        series, year, track, length = venue
        rows.append(
            {
                "paper_id": f"acl:{anthology_id}",
                "anthology_id": anthology_id,
                "title": title,
                "authors": parse_authors(fields.get("author", "")),
                "abstract": abstract,
                "venue": series,
                "venue_code": venue_code,
                "year": year,
                "track": track,
                "paper_length": length,
                "booktitle": detex(fields.get("booktitle", "")),
                "month": detex(fields.get("month", "")),
                "pages": detex(fields.get("pages", "")),
                "doi": detex(fields.get("doi", "")) or None,
                "url": url,
                "peer_review_status": "peer_reviewed_published",
                "peer_review_evidence": (
                    "Published in the ACL Anthology proceedings of a reviewed "
                    f"{series} {year} {track} track."
                ),
                "hosting": "aclanthology.org",
                "evidence_scope": "published_abstract",
                "record_sha256": hashlib.sha256(
                    json.dumps(
                        {"id": anthology_id, "title": title, "abstract": abstract},
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
            }
        )

    rows.sort(key=lambda row: row["anthology_id"])
    ensure_data_dir()
    with corpus_path().open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    by_venue: dict[str, int] = {}
    for row in rows:
        by_venue[row["venue_code"]] = by_venue.get(row["venue_code"], 0) + 1
    manifest = {
        "built_at": datetime.now(UTC).isoformat(),
        "source_url": SOURCE_URL,
        "source_sha256": digest,
        "source_bytes": source.stat().st_size,
        "papers": len(rows),
        "skipped_missing_abstract": skipped_no_abstract,
        "records_with_residual_backslash": sum(
            "\\" in row["title"] or "\\" in row["abstract"] for row in rows
        ),
        "records_with_no_parsed_author": sum(not row["authors"] for row in rows),
        "median_authors_per_paper": (
            sorted(len(row["authors"]) for row in rows)[len(rows) // 2] if rows else 0
        ),
        "by_venue_code": dict(sorted(by_venue.items())),
        "all_real_published_papers": True,
        "notes": (
            "Main-conference and Findings tracks of ACL and EMNLP 2025-2026 only. Demo, "
            "industry, SRW and shared-task tracks are excluded because their review regimes "
            "differ. Titles and abstracts are the published strings with BibTeX markup "
            "rendered to plain text; no text was summarised, shortened or generated."
        ),
    }
    manifest_path().write_text(json.dumps(manifest, indent=2) + "\n")
    progress(f"wrote {corpus_path()}: {len(rows):,} papers")
    return manifest
