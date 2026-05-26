"""Deterministic cross-check / diff logic for Ditat shipment verification.

Pure functions. No I/O. Consumes:
  - `ditat`     : slim Ditat-record dict (from _slim_ditat in ditat_verify.py)
  - `extracted` : { "rc": {...}, "bol": {...}, "pod": {...} } produced by the
                  agent after Read-ing the PDFs

Emits a flat list of findings; the caller groups by severity.

Finding shape:
  { "pair": "BOL↔RC", "field": "weight_lbs",
    "a": "42,000", "b": "24,000",
    "severity": "critical" | "warn" | "info",
    "message": "delta 75%" }
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

# Severity tiers
CRIT = "critical"
WARN = "warn"
INFO = "info"


# ---------------------------------------------------------------- normalization

_WS_RE = re.compile(r"\s+")


def _norm_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().lower()
    s = _WS_RE.sub(" ", s)
    return s or None


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v))
    if not s or s in {"-", "."}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


_DATE_PATTERNS = (
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y",
    "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
)


def _to_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    # Try ISO first via fromisoformat (handles offsets)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        pass
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(s[: len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    # Last resort — extract YYYY-MM-DD substring
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _fmt(v: Any) -> str:
    if v is None:
        return "(missing)"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


# ---------------------------------------------------------------- comparators

def _cmp_weight(a: Any, b: Any) -> tuple[str, str] | None:
    """Return (severity, message) or None if both missing."""
    fa, fb = _to_float(a), _to_float(b)
    if fa is None and fb is None:
        return None
    if fa is None or fb is None:
        return (WARN, "missing on one side")
    base = max(abs(fa), abs(fb), 1.0)
    delta_pct = abs(fa - fb) / base * 100.0
    if delta_pct > 5.0:
        return (CRIT, f"Δ {delta_pct:.1f}%")
    if delta_pct >= 1.0:
        return (WARN, f"Δ {delta_pct:.1f}%")
    if delta_pct > 0.0:
        return (INFO, f"Δ {delta_pct:.1f}%")
    return (INFO, "match")


def _cmp_money(a: Any, b: Any) -> tuple[str, str] | None:
    fa, fb = _to_float(a), _to_float(b)
    if fa is None and fb is None:
        return None
    if fa is None or fb is None:
        return (WARN, "missing on one side")
    delta = abs(fa - fb)
    base = max(abs(fa), abs(fb), 1.0)
    delta_pct = delta / base * 100.0
    if delta > 1.0 or delta_pct > 1.0:
        return (CRIT, f"Δ ${delta:.2f} ({delta_pct:.1f}%)")
    if delta > 0:
        return (INFO, f"Δ ${delta:.2f}")
    return (INFO, "match")


def _cmp_date(a: Any, b: Any) -> tuple[str, str] | None:
    da, db = _to_date(a), _to_date(b)
    if da is None and db is None:
        return None
    if da is None or db is None:
        return (WARN, "missing on one side")
    delta_days = (da - db).days
    if abs(delta_days) > 1:
        return (CRIT, f"{delta_days:+d}d")
    if delta_days != 0:
        return (WARN, f"{delta_days:+d}d")
    return (INFO, "match")


def _cmp_int(a: Any, b: Any) -> tuple[str, str] | None:
    ia, ib = _to_int(a), _to_int(b)
    if ia is None and ib is None:
        return None
    if ia is None or ib is None:
        return (WARN, "missing on one side")
    if ia != ib:
        delta = ia - ib
        return (CRIT if abs(delta) > 0 else INFO, f"Δ {delta:+d}")
    return (INFO, "match")


def _cmp_str(a: Any, b: Any, severity_on_diff: str = WARN) -> tuple[str, str] | None:
    na, nb = _norm_str(a), _norm_str(b)
    if na is None and nb is None:
        return None
    if na is None or nb is None:
        return (WARN, "missing on one side")
    if na == nb:
        return (INFO, "match")
    # one contains the other → info (likely abbreviation, e.g. "Reefer" vs "Refrigerated Van")
    if na in nb or nb in na:
        return (INFO, "fuzzy match")
    return (severity_on_diff, "mismatch")


def _cmp_id(a: Any, b: Any) -> tuple[str, str] | None:
    """Identifier compare: any diff is critical."""
    na, nb = _norm_str(a), _norm_str(b)
    if na is None and nb is None:
        return None
    if na is None or nb is None:
        return (WARN, "missing on one side")
    if na == nb:
        return (INFO, "match")
    return (CRIT, "mismatch")


# ---------------------------------------------------------------- record helpers

def _doc_get(doc: Optional[dict], *keys: str) -> Any:
    if not isinstance(doc, dict):
        return None
    for k in keys:
        if k in doc and doc[k] not in (None, "", []):
            return doc[k]
    return None


def _city_state(loc: Optional[dict]) -> Optional[str]:
    if not isinstance(loc, dict):
        return None
    city = _norm_str(loc.get("city"))
    state = _norm_str(loc.get("state"))
    if city and state:
        return f"{city}, {state}"
    return city or state


# ---------------------------------------------------------------- cross-checks

def _emit(findings: list[dict], pair: str, field: str, a: Any, b: Any,
          result: tuple[str, str] | None, include_info: bool = False) -> None:
    if result is None:
        return
    severity, msg = result
    if severity == INFO and not include_info:
        return
    findings.append({
        "pair": pair,
        "field": field,
        "a": _fmt(a),
        "b": _fmt(b),
        "severity": severity,
        "message": msg,
    })


def diff_bol_rc(bol: Optional[dict], rc: Optional[dict]) -> list[dict]:
    out: list[dict] = []
    if not bol or not rc:
        return out
    pair = "BOL↔RC"
    _emit(out, pair, "bol_number",
          _doc_get(bol, "bol_number"), _doc_get(rc, "bol_number"),
          _cmp_id(_doc_get(bol, "bol_number"), _doc_get(rc, "bol_number")))
    _emit(out, pair, "pickup_date",
          _doc_get(bol, "pickup_date"), _doc_get(rc, "pickup_date"),
          _cmp_date(_doc_get(bol, "pickup_date"), _doc_get(rc, "pickup_date")))
    _emit(out, pair, "delivery_date",
          _doc_get(bol, "delivery_date"), _doc_get(rc, "delivery_date"),
          _cmp_date(_doc_get(bol, "delivery_date"), _doc_get(rc, "delivery_date")))
    _emit(out, pair, "weight_lbs",
          _doc_get(bol, "weight_lbs", "weight"), _doc_get(rc, "weight_lbs", "weight"),
          _cmp_weight(_doc_get(bol, "weight_lbs", "weight"),
                      _doc_get(rc, "weight_lbs", "weight")))
    _emit(out, pair, "pieces",
          _doc_get(bol, "pieces"), _doc_get(rc, "pieces"),
          _cmp_int(_doc_get(bol, "pieces"), _doc_get(rc, "pieces")))
    _emit(out, pair, "commodity",
          _doc_get(bol, "commodity"), _doc_get(rc, "commodity"),
          _cmp_str(_doc_get(bol, "commodity"), _doc_get(rc, "commodity")))
    _emit(out, pair, "pickup_location",
          _city_state(_doc_get(bol, "shipper", "pickup")),
          _city_state(_doc_get(rc, "pickup_location", "pickup")),
          _cmp_str(_city_state(_doc_get(bol, "shipper", "pickup")),
                   _city_state(_doc_get(rc, "pickup_location", "pickup"))))
    _emit(out, pair, "delivery_location",
          _city_state(_doc_get(bol, "consignee", "delivery")),
          _city_state(_doc_get(rc, "delivery_location", "delivery")),
          _cmp_str(_city_state(_doc_get(bol, "consignee", "delivery")),
                   _city_state(_doc_get(rc, "delivery_location", "delivery"))))
    return out


def diff_pod_rc(pod: Optional[dict], rc: Optional[dict], bol: Optional[dict] = None) -> list[dict]:
    out: list[dict] = []
    if not pod or not rc:
        return out
    pair = "POD↔RC"
    _emit(out, pair, "bol_number",
          _doc_get(pod, "bol_number"), _doc_get(rc, "bol_number"),
          _cmp_id(_doc_get(pod, "bol_number"), _doc_get(rc, "bol_number")))
    _emit(out, pair, "delivery_date",
          _doc_get(pod, "delivery_date"), _doc_get(rc, "delivery_date"),
          _cmp_date(_doc_get(pod, "delivery_date"), _doc_get(rc, "delivery_date")))
    # `or` would short-circuit on a legitimate 0-weight / 0-pieces fall through
    # to the BOL value, so check None explicitly.
    rc_weight = _doc_get(rc, "weight_lbs", "weight")
    if rc_weight is None and bol is not None:
        rc_weight = _doc_get(bol, "weight_lbs", "weight")
    _emit(out, pair, "weight_received",
          _doc_get(pod, "weight_received_lbs", "weight_received"), rc_weight,
          _cmp_weight(_doc_get(pod, "weight_received_lbs", "weight_received"), rc_weight))
    rc_pieces = _doc_get(rc, "pieces")
    if rc_pieces is None and bol is not None:
        rc_pieces = _doc_get(bol, "pieces")
    _emit(out, pair, "pieces_received",
          _doc_get(pod, "pieces_received"), rc_pieces,
          _cmp_int(_doc_get(pod, "pieces_received"), rc_pieces))
    damages = _doc_get(pod, "damages_notes", "damages")
    if damages:
        out.append({
            "pair": pair, "field": "damages_notes",
            "a": _fmt(damages), "b": "(none expected)",
            "severity": WARN, "message": "POD reports damages",
        })
    return out


def diff_ditat_rc(ditat: Optional[dict], rc: Optional[dict]) -> list[dict]:
    out: list[dict] = []
    if not ditat or not rc:
        return out
    pair = "Ditat↔RC"
    _emit(out, pair, "bol_number",
          ditat.get("bol_number"), _doc_get(rc, "bol_number"),
          _cmp_id(ditat.get("bol_number"), _doc_get(rc, "bol_number")))
    _emit(out, pair, "load_number",
          ditat.get("load_number"), _doc_get(rc, "load_number"),
          _cmp_id(ditat.get("load_number"), _doc_get(rc, "load_number")))
    _emit(out, pair, "total_weight_lbs",
          ditat.get("total_weight_lbs"), _doc_get(rc, "weight_lbs", "weight"),
          _cmp_weight(ditat.get("total_weight_lbs"), _doc_get(rc, "weight_lbs", "weight")))
    _emit(out, pair, "total_pieces",
          ditat.get("total_pieces"), _doc_get(rc, "pieces"),
          _cmp_int(ditat.get("total_pieces"), _doc_get(rc, "pieces")))
    _emit(out, pair, "equipment_type",
          ditat.get("equipment_type"), _doc_get(rc, "equipment_type"),
          _cmp_str(ditat.get("equipment_type"), _doc_get(rc, "equipment_type")))
    _emit(out, pair, "pickup_location",
          _city_state(ditat.get("pickup")), _city_state(_doc_get(rc, "pickup_location", "pickup")),
          _cmp_str(_city_state(ditat.get("pickup")),
                   _city_state(_doc_get(rc, "pickup_location", "pickup"))))
    _emit(out, pair, "delivery_location",
          _city_state(ditat.get("delivery")),
          _city_state(_doc_get(rc, "delivery_location", "delivery")),
          _cmp_str(_city_state(ditat.get("delivery")),
                   _city_state(_doc_get(rc, "delivery_location", "delivery"))))
    _emit(out, pair, "revenue_vs_rate",
          ditat.get("total_revenue"), _doc_get(rc, "agreed_rate", "rate"),
          _cmp_money(ditat.get("total_revenue"), _doc_get(rc, "agreed_rate", "rate")))
    return out


def diff_bol_pod(bol: Optional[dict], pod: Optional[dict]) -> list[dict]:
    out: list[dict] = []
    if not bol or not pod:
        return out
    pair = "BOL↔POD"
    _emit(out, pair, "bol_number",
          _doc_get(bol, "bol_number"), _doc_get(pod, "bol_number"),
          _cmp_id(_doc_get(bol, "bol_number"), _doc_get(pod, "bol_number")))
    _emit(out, pair, "weight",
          _doc_get(bol, "weight_lbs", "weight"),
          _doc_get(pod, "weight_received_lbs", "weight_received"),
          _cmp_weight(_doc_get(bol, "weight_lbs", "weight"),
                      _doc_get(pod, "weight_received_lbs", "weight_received")))
    _emit(out, pair, "pieces",
          _doc_get(bol, "pieces"), _doc_get(pod, "pieces_received"),
          _cmp_int(_doc_get(bol, "pieces"), _doc_get(pod, "pieces_received")))
    return out


# ---------------------------------------------------------------- top-level

def run_diff(ditat: dict, extracted: dict) -> dict:
    """Run all cross-checks. Returns dict with findings + counters + verdict."""
    rc = extracted.get("rc") if isinstance(extracted, dict) else None
    bol = extracted.get("bol") if isinstance(extracted, dict) else None
    pod = extracted.get("pod") if isinstance(extracted, dict) else None

    findings: list[dict] = []
    findings.extend(diff_bol_rc(bol, rc))
    findings.extend(diff_pod_rc(pod, rc, bol))
    findings.extend(diff_ditat_rc(ditat, rc))
    findings.extend(diff_bol_pod(bol, pod))

    crit = [f for f in findings if f["severity"] == CRIT]
    warn = [f for f in findings if f["severity"] == WARN]
    info = [f for f in findings if f["severity"] == INFO]

    if rc is None:
        verdict = "RC MISSING"
    elif crit:
        verdict = "ISSUES"
    elif warn:
        verdict = "WARN"
    else:
        verdict = "OK"

    return {
        "verdict": verdict,
        "critical": crit,
        "warn": warn,
        "info": info,
        "critical_count": len(crit),
        "warn_count": len(warn),
    }


def is_problematic(diff_result: dict) -> bool:
    """A shipment is problematic if it has any critical or warn findings, or RC is missing."""
    v = diff_result.get("verdict")
    return v in {"ISSUES", "WARN", "RC MISSING"}
