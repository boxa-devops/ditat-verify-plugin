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
from functools import partial
from typing import Any, Optional

from .rules import default_rules

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

def _cmp_weight(a: Any, b: Any, critical_pct: float = 5.0,
                warn_pct: float = 1.0) -> tuple[str, str] | None:
    """Return (severity, message) or None if both missing."""
    fa, fb = _to_float(a), _to_float(b)
    if fa is None and fb is None:
        return None
    if fa is None or fb is None:
        return (WARN, "missing on one side")
    base = max(abs(fa), abs(fb), 1.0)
    delta_pct = abs(fa - fb) / base * 100.0
    if delta_pct > critical_pct:
        return (CRIT, f"Δ {delta_pct:.1f}%")
    if delta_pct >= warn_pct:
        return (WARN, f"Δ {delta_pct:.1f}%")
    if delta_pct > 0.0:
        return (INFO, f"Δ {delta_pct:.1f}%")
    return (INFO, "match")


def _cmp_overage_weight(a: Any, b: Any, threshold_pct: float = 10.0) -> tuple[str, str] | None:
    """BOL↔RC weight: only flag when BOL > RC by ≥ threshold%. BOL < RC is OK."""
    fa, fb = _to_float(a), _to_float(b)
    if fa is None and fb is None:
        return None
    if fa is None or fb is None:
        return (WARN, "missing on one side")
    if fa <= fb:
        return (INFO, "bol≤rc ok")
    base = max(abs(fb), 1.0)
    delta_pct = (fa - fb) / base * 100.0
    if delta_pct >= threshold_pct:
        return (CRIT, f"bol>rc Δ {delta_pct:.1f}%")
    return (INFO, f"bol>rc Δ {delta_pct:.1f}%")


def _cmp_overage_int(a: Any, b: Any, threshold_pct: float = 10.0) -> tuple[str, str] | None:
    """BOL↔RC pieces: only flag when BOL > RC by ≥ threshold%. BOL < RC is OK."""
    ia, ib = _to_int(a), _to_int(b)
    if ia is None and ib is None:
        return None
    if ia is None or ib is None:
        return (WARN, "missing on one side")
    if ia <= ib:
        return (INFO, "bol≤rc ok")
    base = max(abs(ib), 1)
    delta_pct = (ia - ib) / base * 100.0
    if delta_pct >= threshold_pct:
        return (CRIT, f"bol>rc Δ {ia - ib:+d} ({delta_pct:.1f}%)")
    return (INFO, f"bol>rc Δ {ia - ib:+d}")


def _cmp_money(a: Any, b: Any, critical_abs: float = 1.0,
               critical_pct: float = 1.0) -> tuple[str, str] | None:
    fa, fb = _to_float(a), _to_float(b)
    if fa is None and fb is None:
        return None
    if fa is None or fb is None:
        return (WARN, "missing on one side")
    delta = abs(fa - fb)
    base = max(abs(fa), abs(fb), 1.0)
    delta_pct = delta / base * 100.0
    if delta > critical_abs or delta_pct > critical_pct:
        return (CRIT, f"Δ ${delta:.2f} ({delta_pct:.1f}%)")
    if delta > 0:
        return (INFO, f"Δ ${delta:.2f}")
    return (INFO, "match")


def _cmp_date(a: Any, b: Any, critical_days: int = 1) -> tuple[str, str] | None:
    da, db = _to_date(a), _to_date(b)
    if da is None and db is None:
        return None
    if da is None or db is None:
        return (WARN, "missing on one side")
    delta_days = (da - db).days
    if abs(delta_days) > critical_days:
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


def _cmp_ditat_qty(a: Any, b: Any, inner) -> tuple[str, str] | None:
    """Ditat↔RC weight/pieces: some tenants never enter these in Ditat, so a
    Ditat 0/None isn't a real discrepancy — it's a not-entered field. Downgrade
    to WARN when Ditat is empty but the RC has a value; otherwise defer to `inner`.
    """
    fa = _to_float(a)
    if fa is None or fa == 0:
        if _to_float(b) is None:
            return None
        return (WARN, "Ditat not entered")
    return inner(a, b)


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
          cmp, include_info: bool = False) -> None:
    """Run comparator `cmp(a, b)` once and append a finding if it's notable."""
    result = cmp(a, b)
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


def diff_bol_rc(bol: Optional[dict], rc: Optional[dict], rules: dict) -> list[dict]:
    out: list[dict] = []
    if not bol or not rc:
        return out
    pair = "BOL↔RC"
    cmp_date = partial(_cmp_date, critical_days=rules["date"]["critical_days"])
    cmp_str = partial(_cmp_str, severity_on_diff=rules["string_mismatch_severity"])
    cmp_w = partial(_cmp_overage_weight,
                    threshold_pct=rules["bol_rc_overage"]["weight_threshold_pct"])
    cmp_p = partial(_cmp_overage_int,
                    threshold_pct=rules["bol_rc_overage"]["pieces_threshold_pct"])
    _emit(out, pair, "bol_number",
          _doc_get(bol, "bol_number"), _doc_get(rc, "bol_number"), _cmp_id)
    _emit(out, pair, "pickup_date",
          _doc_get(bol, "pickup_date"), _doc_get(rc, "pickup_date"), cmp_date)
    _emit(out, pair, "delivery_date",
          _doc_get(bol, "delivery_date"), _doc_get(rc, "delivery_date"), cmp_date)
    _emit(out, pair, "weight_lbs",
          _doc_get(bol, "weight_lbs", "weight"), _doc_get(rc, "weight_lbs", "weight"), cmp_w)
    _emit(out, pair, "pieces",
          _doc_get(bol, "pieces"), _doc_get(rc, "pieces"), cmp_p)
    _emit(out, pair, "commodity",
          _doc_get(bol, "commodity"), _doc_get(rc, "commodity"), cmp_str)
    _emit(out, pair, "pickup_location",
          _city_state(_doc_get(bol, "shipper", "pickup")),
          _city_state(_doc_get(rc, "pickup_location", "pickup")), cmp_str)
    _emit(out, pair, "delivery_location",
          _city_state(_doc_get(bol, "consignee", "delivery")),
          _city_state(_doc_get(rc, "delivery_location", "delivery")), cmp_str)
    return out


def diff_pod_rc(pod: Optional[dict], rc: Optional[dict], rules: dict,
                bol: Optional[dict] = None) -> list[dict]:
    out: list[dict] = []
    if not pod or not rc:
        return out
    pair = "POD↔RC"
    cmp_date = partial(_cmp_date, critical_days=rules["date"]["critical_days"])
    # bol_number: skip when BOL doc present — already covered by BOL↔RC and BOL↔POD.
    if not bol:
        _emit(out, pair, "bol_number",
              _doc_get(pod, "bol_number"), _doc_get(rc, "bol_number"), _cmp_id)
    _emit(out, pair, "delivery_date",
          _doc_get(pod, "delivery_date"), _doc_get(rc, "delivery_date"), cmp_date)
    # weight_received + pieces_received intentionally dropped — POD quantities
    # routinely diverge from RC (partial deliveries, short-loads) and produced noise.
    damages = _doc_get(pod, "damages_notes", "damages")
    if damages:
        out.append({
            "pair": pair, "field": "damages_notes",
            "a": _fmt(damages), "b": "(none expected)",
            "severity": WARN, "message": "POD reports damages",
        })
    return out


def diff_ditat_rc(ditat: Optional[dict], rc: Optional[dict], rules: dict) -> list[dict]:
    out: list[dict] = []
    if not ditat or not rc:
        return out
    pair = "Ditat↔RC"
    cmp_str = partial(_cmp_str, severity_on_diff=rules["string_mismatch_severity"])
    cmp_weight = partial(_cmp_weight,
                         critical_pct=rules["weight_ditat_rc"]["critical_pct"],
                         warn_pct=rules["weight_ditat_rc"]["warn_pct"])
    cmp_money = partial(_cmp_money,
                        critical_abs=rules["money"]["critical_abs"],
                        critical_pct=rules["money"]["critical_pct"])
    _emit(out, pair, "bol_number",
          ditat.get("bol_number"), _doc_get(rc, "bol_number"), _cmp_id)
    _emit(out, pair, "load_number",
          ditat.get("load_number"), _doc_get(rc, "load_number"), _cmp_id)
    _emit(out, pair, "total_weight_lbs",
          ditat.get("total_weight_lbs"), _doc_get(rc, "weight_lbs", "weight"),
          partial(_cmp_ditat_qty, inner=cmp_weight))
    _emit(out, pair, "total_pieces",
          ditat.get("total_pieces"), _doc_get(rc, "pieces"),
          partial(_cmp_ditat_qty, inner=_cmp_int))
    _emit(out, pair, "equipment_type",
          ditat.get("equipment_type"), _doc_get(rc, "equipment_type"), cmp_str)
    _emit(out, pair, "pickup_location",
          _city_state(ditat.get("pickup")),
          _city_state(_doc_get(rc, "pickup_location", "pickup")), cmp_str)
    _emit(out, pair, "delivery_location",
          _city_state(ditat.get("delivery")),
          _city_state(_doc_get(rc, "delivery_location", "delivery")), cmp_str)
    _emit(out, pair, "revenue_vs_rate",
          ditat.get("total_revenue"), _doc_get(rc, "agreed_rate", "rate"), cmp_money)
    return out


def diff_rc_policy(rc: Optional[dict], rules: dict) -> list[dict]:
    """RC-only check: does the rate confirmation honor our accessorial policy?

    Missing terms → warn (RC should spell them out). Worse-than-default terms →
    critical/warn per the configured severities (we'd be undercompensated).
    """
    out: list[dict] = []
    if not rc:
        return out
    pair = "RC-policy"
    pol = rules["accessorial"]
    sev = pol["severities"]

    det_rate = _to_float(_doc_get(rc, "detention_rate"))
    det_free = _to_float(_doc_get(rc, "detention_free_hrs", "detention_free_hours"))
    det_max  = _to_float(_doc_get(rc, "detention_max_hrs", "detention_max_hours"))
    lay_rate = _to_float(_doc_get(rc, "layover_rate"))
    lay_thr  = _to_float(_doc_get(rc, "layover_threshold_hrs", "layover_threshold_hours"))

    def _missing(field: str, label: str) -> None:
        out.append({
            "pair": pair, "field": field,
            "a": "(missing)", "b": label,
            "severity": WARN, "message": "RC silent on policy term",
        })

    def _worse(field: str, actual: float, default: float, unit: str) -> None:
        out.append({
            "pair": pair, "field": field,
            "a": f"{actual:g} {unit}", "b": f"{default:g} {unit} (default)",
            "severity": sev[field], "message": "RC term worse than company default",
        })

    # (value, "lower"|"higher" is-worse direction, default, unit, missing-label)
    if det_rate is None:
        _missing("detention_rate", f"≥ ${pol['detention_rate']:g}/hr")
    elif det_rate < pol["detention_rate"]:
        _worse("detention_rate", det_rate, pol["detention_rate"], "$/hr")

    if det_free is None:
        _missing("detention_free_hrs", f"≤ {pol['detention_free_hrs']:g} hrs")
    elif det_free > pol["detention_free_hrs"]:
        _worse("detention_free_hrs", det_free, pol["detention_free_hrs"], "hrs")

    if det_max is None:
        _missing("detention_max_hrs", f"≥ {pol['detention_max_hrs']:g} hrs")
    elif det_max < pol["detention_max_hrs"]:
        _worse("detention_max_hrs", det_max, pol["detention_max_hrs"], "hrs")

    if lay_rate is None:
        _missing("layover_rate", f"≥ ${pol['layover_rate']:g}/24h")
    elif lay_rate < pol["layover_rate"]:
        _worse("layover_rate", lay_rate, pol["layover_rate"], "$/24h")

    if lay_thr is None:
        _missing("layover_threshold_hrs", f"≤ {pol['layover_threshold_hrs']:g} hrs")
    elif lay_thr > pol["layover_threshold_hrs"]:
        _worse("layover_threshold_hrs", lay_thr, pol["layover_threshold_hrs"], "hrs")

    return out


def diff_bol_pod(bol: Optional[dict], pod: Optional[dict]) -> list[dict]:
    out: list[dict] = []
    if not bol or not pod:
        return out
    pair = "BOL↔POD"
    # Only bol_number is trusted across BOL↔POD. POD quantities (weight_received,
    # pieces_received) routinely diverge from the BOL on partial deliveries —
    # surfaced too much noise so they were dropped.
    _emit(out, pair, "bol_number",
          _doc_get(bol, "bol_number"), _doc_get(pod, "bol_number"), _cmp_id)
    return out


# ---------------------------------------------------------------- top-level

def run_diff(ditat: dict, extracted: dict, rules: Optional[dict] = None) -> dict:
    """Run all cross-checks. Returns dict with findings + counters + verdict.

    `rules` defaults to the built-in defaults; pass a dict from rules.load_rules()
    to override thresholds/policy.
    """
    if rules is None:
        rules = default_rules()
    rc = extracted.get("rc") if isinstance(extracted, dict) else None
    bol = extracted.get("bol") if isinstance(extracted, dict) else None
    pod = extracted.get("pod") if isinstance(extracted, dict) else None

    findings: list[dict] = []
    findings.extend(diff_rc_policy(rc, rules))
    findings.extend(diff_bol_rc(bol, rc, rules))
    findings.extend(diff_pod_rc(pod, rc, rules, bol))
    findings.extend(diff_ditat_rc(ditat, rc, rules))
    findings.extend(diff_bol_pod(bol, pod))

    crit = [f for f in findings if f["severity"] == CRIT]
    warn = [f for f in findings if f["severity"] == WARN]
    info = [f for f in findings if f["severity"] == INFO]

    if rc is None:
        # Some customers (e.g. Amazon) routinely arrive without a rate
        # confirmation PDF — downgrade to OK so they don't surface as problematic.
        customer = _norm_str(ditat.get("customer") if isinstance(ditat, dict) else None)
        ok_customers = rules.get("rc_missing_ok_customers") or []
        if customer and any(c.lower() in customer for c in ok_customers):
            verdict = "OK"
        else:
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
