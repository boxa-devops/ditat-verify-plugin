"""Single source of truth for BOL / POD / RC document classification.

On the server this tags each document and computes docs_missing. In the plugin
it backs the docx report's RC/BOL/POD ✓/✗ marks (reading the classification the
server already assigned). One classifier here avoids the slightly-different
copies this logic used to have.
"""

from __future__ import annotations

import re

LABELS = ("RC", "BOL", "POD")
UNKNOWN = "UNKNOWN"

# Boundary-aware on alphanumerics only, so an underscore counts as a separator:
# matches standalone "RC", "BOL", "POD" tokens *and* the underscore-suffixed
# forms carriers love ("RateCon_123.pdf", "bill_of_lading_99.pdf") — which a
# plain \b misses because "_" is itself a word character.
_EDGE = (r"(?<![a-z0-9])", r"(?![a-z0-9])")
_TOKEN_RE = {
    "RC":  re.compile(_EDGE[0] + r"(rc|ratecon|rate[\s_-]?con(?:firmation)?|ratecnf)" + _EDGE[1], re.I),
    "POD": re.compile(_EDGE[0] + r"(pod|proof[\s_-]?of[\s_-]?delivery|delivery[\s_-]?receipt)" + _EDGE[1], re.I),
    "BOL": re.compile(_EDGE[0] + r"(bol|bill[\s_-]?of[\s_-]?lading)" + _EDGE[1], re.I),
}


def classify_filename(file_name: str | None) -> str:
    """Classify a doc by filename. Returns 'RC' | 'BOL' | 'POD' | 'UNKNOWN'."""
    n = file_name or ""
    # Order matters: POD's substrings can collide with BOL; check POD first.
    if _TOKEN_RE["RC"].search(n):
        return "RC"
    if _TOKEN_RE["POD"].search(n):
        return "POD"
    if _TOKEN_RE["BOL"].search(n):
        return "BOL"
    return UNKNOWN


def classify_doc(doc: dict) -> str:
    """Classify a doc record, trusting an explicit `classification` field and
    falling back to the filename heuristic when it's absent or UNKNOWN.
    """
    cls = (doc.get("classification") or "").upper()
    if cls in LABELS:
        return cls
    return classify_filename(doc.get("file_name") or doc.get("fileName"))


def doc_present(docs: list[dict], label: str) -> bool:
    """True if any doc in the list classifies as `label`."""
    return any(classify_doc(d) == label for d in docs)


def missing_labels(docs: list[dict]) -> list[str]:
    """Which of RC/BOL/POD are absent from this doc list, in canonical order."""
    present = {classify_doc(d) for d in docs}
    return [lbl for lbl in LABELS if lbl not in present]
