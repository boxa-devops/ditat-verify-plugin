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
from datetime import date, datetime, timedelta
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


_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}
_MONTHS.update({m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
     "Nov", "Dec"], start=1)})
_MONTHS["sept"] = 9


def _to_date(v: Any) -> Optional[date]:
    """Parse a date out of almost any carrier-doc string.

    Finds the date ANYWHERE in the text, so trailing times/separators don't break
    it: "Jun 3, 2026 · 08:00", "Delivery: 06/03/2026", "2026-06-03T08:00Z" all
    work. Numeric M/D/Y is read US-style.
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        pass

    def _mk(y: int, mo: int, d: int) -> Optional[date]:
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    # ISO yyyy-mm-dd anywhere
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return _mk(int(m[1]), int(m[2]), int(m[3]))
    # "Jun 3, 2026" / "June 3 2026" (month name first)
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", s)
    if m and m[1].lower() in _MONTHS:
        return _mk(int(m[3]), _MONTHS[m[1].lower()], int(m[2]))
    # "3 Jun 2026" (day first)
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b", s)
    if m and m[2].lower() in _MONTHS:
        return _mk(int(m[3]), _MONTHS[m[2].lower()], int(m[1]))
    # Numeric M/D/Y or M-D-Y (US order)
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", s)
    if m:
        yr = int(m[3])
        return _mk(yr + 2000 if yr < 100 else yr, int(m[1]), int(m[2]))
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
        return None  # one-sided absence (often the RC omits a field) is not a discrepancy
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
        return None  # one-sided absence (often the RC omits a field) is not a discrepancy
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
        return None  # one-sided absence (often the RC omits a field) is not a discrepancy
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
        return None  # one-sided absence (often the RC omits a field) is not a discrepancy
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
    # Only compare when BOTH dates are present. A doc that simply omits a date
    # (e.g. a BOL with no delivery date) is not a discrepancy — the date is read
    # from whichever doc carries it (POD → BOL → Ditat). No flag for one-sided.
    if da is None or db is None:
        return None
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
        return None  # one-sided absence (often the RC omits a field) is not a discrepancy
    if ia != ib:
        delta = ia - ib
        return (CRIT if abs(delta) > 0 else INFO, f"Δ {delta:+d}")
    return (INFO, "match")


def _cmp_str(a: Any, b: Any, severity_on_diff: str = WARN) -> tuple[str, str] | None:
    na, nb = _norm_str(a), _norm_str(b)
    if na is None and nb is None:
        return None
    if na is None or nb is None:
        return None  # one-sided absence (often the RC omits a field) is not a discrepancy
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
        return None  # one-sided absence (often the RC omits a field) is not a discrepancy
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
    # bol_number intentionally NOT compared here — the RC does not carry a BOL number.
    # pickup/delivery dates are handled by diff_dates (resolved POD→BOL→Ditat vs RC).
    _emit(out, pair, "weight_lbs",
          _doc_get(bol, "weight_lbs", "weight"), _doc_get(rc, "weight_lbs", "weight"), cmp_w)
    _emit(out, pair, "pieces",
          _doc_get(bol, "pieces"), _doc_get(rc, "pieces"), cmp_p)
    # commodity is judged by the LLM at extraction (semantic), relayed via the
    # `commodity_mismatch` flag — see diff_commodity. Not compared here.
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
    # bol_number: skip when BOL doc present — already covered by BOL↔POD.
    if not bol:
        _emit(out, pair, "bol_number",
              _doc_get(pod, "bol_number"), _doc_get(rc, "bol_number"), _cmp_id)
    # delivery_date handled by diff_dates (resolved POD→BOL→Ditat vs RC).
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
    # equipment_type intentionally NOT compared — "53Van" vs "Dry Van 53'" etc are
    # the same trailer in different words; produced noise with no value.
    _emit(out, pair, "pickup_location",
          _city_state(ditat.get("pickup")),
          _city_state(_doc_get(rc, "pickup_location", "pickup")), cmp_str)
    _emit(out, pair, "delivery_location",
          _city_state(ditat.get("delivery")),
          _city_state(_doc_get(rc, "delivery_location", "delivery")), cmp_str)
    _emit(out, pair, "revenue_vs_rate",
          ditat.get("total_revenue"), _doc_get(rc, "agreed_rate", "rate"), cmp_money)
    return out


def _clock_hours(v: Any) -> Optional[float]:
    """Hours-since-midnight for a time/datetime value. Accepts ISO datetimes,
    'HH:MM', and 'H:MM AM/PM'. Returns None if unparseable.
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.hour + v.minute / 60.0
    s = str(v).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.hour + dt.minute / 60.0
    except (ValueError, TypeError):
        pass
    m = re.search(r"(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?", s, re.I)
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    ap = (m.group(3) or "").lower().replace(".", "")
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if h > 23 or mn > 59:
        return None
    return h + mn / 60.0


def _parse_dt(v: Any) -> Optional[datetime]:
    """Full datetime (date AND time) for a value, else None.

    Used for exact detention/layover math — when both in/out carry a date the
    wait can span midnight or multiple days (layover), which clock-only can't do.
    """
    if isinstance(v, datetime):
        return v
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        pass
    d, t = _to_date(s), _clock_hours(s)
    if d is not None and t is not None:  # e.g. "Jun 3, 2026 14:30"
        return datetime(d.year, d.month, d.day) + timedelta(hours=t)
    return None


def _stop_wait_hours(t_in: Any, t_out: Any) -> Optional[float]:
    """Wait (hours) between an in and out time. Prefers full datetimes (handles
    multi-day layover); falls back to clock-of-day with a midnight wrap."""
    di, do = _parse_dt(t_in), _parse_dt(t_out)
    if di is not None and do is not None:
        h = (do - di).total_seconds() / 3600.0
        return h if h >= 0 else None  # negative on full dates = bad data, ignore
    ci, co = _clock_hours(t_in), _clock_hours(t_out)
    if ci is None or co is None:
        return None
    wait = co - ci
    return wait + 24.0 if wait < 0 else wait


def _max_wait_hours(pod: Optional[dict], bol: Optional[dict]) -> Optional[float]:
    """Longest in/out wait across the delivery and pickup stops. The agent
    extracts the exact in/out times; detention/layover can occur at either end."""
    waits = [
        _stop_wait_hours(
            _doc_get(pod, "arrival_time", "in_time", "arrived_at", "time_in"),
            _doc_get(pod, "departure_time", "out_time", "departed_at", "time_out")),
    ]
    for d in (pod, bol):
        waits.append(_stop_wait_hours(
            _doc_get(d, "pickup_arrival_time", "pickup_in"),
            _doc_get(d, "pickup_departure_time", "pickup_out")))
    vals = [w for w in waits if w is not None]
    return max(vals) if vals else None


def diff_rc_policy(rc: Optional[dict], pod: Optional[dict], bol: Optional[dict],
                   rules: dict) -> list[dict]:
    """Accessorial policy check — the RC governs.

    When the RC states detention/layover terms, those are the agreed contract;
    we do NOT flag them (the company default is dropped). The default only acts
    as a fallback: when the RC is SILENT on an accessorial but the POD's in/out
    times show it actually occurred, we performed an accessorial with no
    contractual basis to bill → critical.
    """
    out: list[dict] = []
    if not rc:
        return out
    pair = "RC-policy"
    pol = rules["accessorial"]

    wait = _max_wait_hours(pod, bol)
    if wait is None:
        return out  # no in/out times → can't detect occurrence, nothing to flag

    has_detention = _to_float(_doc_get(rc, "detention_rate")) is not None
    has_layover = _to_float(_doc_get(rc, "layover_rate")) is not None
    free = pol["detention_free_hrs"]
    lay_thr = pol["layover_threshold_hrs"]

    if wait > free and not has_detention:
        out.append({
            "pair": pair, "field": "detention",
            "a": f"{wait:.1f}h wait", "b": f"> {free:g}h free (default)",
            "severity": CRIT,
            "message": "detention occurred but RC silent on detention terms",
        })
    if wait >= lay_thr and not has_layover:
        out.append({
            "pair": pair, "field": "layover",
            "a": f"{wait:.1f}h wait", "b": f"≥ {lay_thr:g}h (default)",
            "severity": CRIT,
            "message": "layover occurred but RC silent on layover terms",
        })
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


def _resolve_trip_date(extracted: Optional[dict], ditat: Optional[dict],
                       which: str) -> Any:
    """Actual pickup/delivery date, sourced POD → BOL → Ditat trip (fallback).

    `which` is "pickup" or "delivery". So an unreadable or blank doc date still
    resolves from the Ditat trip appointment rather than dropping the check.
    """
    key = f"{which}_date"
    if isinstance(extracted, dict):
        for doc in (extracted.get("pod"), extracted.get("bol")):
            v = _doc_get(doc, key)
            if v:
                return v
    stop = ditat.get(which) if isinstance(ditat, dict) else None
    if isinstance(stop, dict):
        return stop.get("appointment_from") or stop.get("appointment_to") or stop.get("date")
    return None


def diff_dates(ditat: Optional[dict], extracted: dict, rc: Optional[dict],
               rules: dict) -> list[dict]:
    """Pickup/delivery date vs the RC, using the resolved (POD→BOL→Ditat) date."""
    out: list[dict] = []
    if not rc:
        return out
    cmp_date = partial(_cmp_date, critical_days=rules["date"]["critical_days"])
    _emit(out, "Dates", "pickup_date",
          _resolve_trip_date(extracted, ditat, "pickup"),
          _doc_get(rc, "pickup_date"), cmp_date)
    _emit(out, "Dates", "delivery_date",
          _resolve_trip_date(extracted, ditat, "delivery"),
          _doc_get(rc, "delivery_date"), cmp_date)
    return out


# ---------------------------------------------------------------- delivery status

def delivery_date(ditat: Optional[dict]) -> Optional[date]:
    """Scheduled delivery date from the Ditat record (appointment end → start → date)."""
    if not isinstance(ditat, dict):
        return None
    d = ditat.get("delivery") or {}
    return _to_date(d.get("appointment_to") or d.get("appointment_from") or d.get("date"))


_TERMINAL_STATUSES = {"completed", "cancelled", "canceled"}


def _status(ditat: Optional[dict]) -> Optional[str]:
    return _norm_str(ditat.get("status") if isinstance(ditat, dict) else None)


def is_pending(ditat: Optional[dict], as_of: Optional[date]) -> bool:
    """True when the shipment is not yet done.

    A terminal status (Completed / Cancelled) is authoritative → never pending.
    Without a status, fall back to the delivery-date proxy (future = pending).
    """
    if _status(ditat) in _TERMINAL_STATUSES:
        return False
    if as_of is None:
        return False
    d = delivery_date(ditat)
    return d is not None and d > as_of


def _rc_exempt(ditat: Optional[dict], rules: dict) -> bool:
    customer = _norm_str(ditat.get("customer") if isinstance(ditat, dict) else None)
    ok_customers = rules.get("rc_missing_ok_customers") or []
    return bool(customer and any(c.lower() in customer for c in ok_customers))


def is_skipped_customer(ditat: Optional[dict], rules: dict) -> bool:
    """True when the shipment's customer is on the skip list (e.g. Amazon) —
    those loads are not verified at all."""
    customer = _norm_str(ditat.get("customer") if isinstance(ditat, dict) else None)
    skip = rules.get("skip_customers") or []
    return bool(customer and any(c.lower() in customer for c in skip))


def diff_doc_pages(extracted: dict) -> list[dict]:
    """A document uploaded with fewer pages than it declares ("Page 1 of 11" but
    only 1 page present) is incomplete → critical. The agent records
    `pages_expected` (the "of N") and `pages_present` (pages actually in the PDF).
    """
    out: list[dict] = []
    if not isinstance(extracted, dict):
        return out
    for doc in ("rc", "bol", "pod"):
        d = extracted.get(doc)
        if not isinstance(d, dict):
            continue
        exp = _to_int(d.get("pages_expected"))
        pres = _to_int(d.get("pages_present"))
        if exp and pres and exp > pres:
            out.append({
                "pair": "Docs", "field": f"{doc.upper()} pages",
                "a": f"{pres} uploaded", "b": f"{exp} expected",
                "severity": CRIT,
                "message": "incomplete document — not all pages uploaded",
            })
    return out


def diff_doc_completeness(ditat: Optional[dict], extracted: dict, rules: dict,
                          as_of: Optional[date]) -> list[dict]:
    """A COMPLETED shipment must have its docs. Missing RC/BOL/POD → critical.

    Fires when status is Completed, or (no status) the delivery date has passed.
    Cancelled loads are exempt — never delivered, so missing docs are expected.
    RC is also exempt for the configured rc_missing_ok_customers (e.g. Amazon).
    """
    out: list[dict] = []
    status = _status(ditat)
    if status in {"cancelled", "canceled"}:
        return out  # cancelled load was never delivered — missing docs expected
    if status != "completed":
        # No/unknown status → fall back to the delivery-date proxy.
        if as_of is None:
            return out
        ddate = delivery_date(ditat)
        if ddate is None or ddate > as_of:
            return out  # not yet delivered — can't require docs
    missing = extracted.get("docs_missing") if isinstance(extracted, dict) else None
    if not missing:
        return out
    rc_exempt = _rc_exempt(ditat, rules)
    for doc in ("RC", "BOL", "POD"):
        if doc not in missing:
            continue
        if doc == "RC" and rc_exempt:
            continue
        out.append({
            "pair": "Docs", "field": doc,
            "a": "(missing)", "b": "required (delivered)",
            "severity": CRIT,
            "message": f"delivered shipment missing {doc}",
        })
    return out


def diff_commodity(extracted: dict) -> list[dict]:
    """Commodity is judged by the LLM (semantic), not Python. The agent sets
    `commodity_mismatch: true` only when the BOL and RC describe genuinely
    different freight; we relay that as a warn."""
    if not isinstance(extracted, dict) or not extracted.get("commodity_mismatch"):
        return []
    bol = extracted.get("bol") or {}
    rc = extracted.get("rc") or {}
    return [{
        "pair": "BOL↔RC", "field": "commodity",
        "a": _fmt(bol.get("commodity")), "b": _fmt(rc.get("commodity")),
        "severity": WARN, "message": "commodity differs (LLM)",
    }]


# ---------------------------------------------------------------- top-level

def run_diff(ditat: dict, extracted: dict, rules: Optional[dict] = None,
             as_of: Optional[date] = None) -> dict:
    """Run all cross-checks. Returns dict with findings + counters + verdict.

    `rules` defaults to the built-in defaults. `as_of` (today's date) enables the
    delivered-shipment doc-completeness check; pass it from the caller.
    """
    if rules is None:
        rules = default_rules()
    rc = extracted.get("rc") if isinstance(extracted, dict) else None
    bol = extracted.get("bol") if isinstance(extracted, dict) else None
    pod = extracted.get("pod") if isinstance(extracted, dict) else None

    findings: list[dict] = []
    findings.extend(diff_doc_completeness(ditat, extracted, rules, as_of))
    findings.extend(diff_doc_pages(extracted))
    findings.extend(diff_rc_policy(rc, pod, bol, rules))
    findings.extend(diff_bol_rc(bol, rc, rules))
    findings.extend(diff_pod_rc(pod, rc, rules, bol))
    findings.extend(diff_ditat_rc(ditat, rc, rules))
    findings.extend(diff_bol_pod(bol, pod))
    findings.extend(diff_dates(ditat, extracted, rc, rules))
    findings.extend(diff_commodity(extracted))

    crit = [f for f in findings if f["severity"] == CRIT]
    warn = [f for f in findings if f["severity"] == WARN]
    info = [f for f in findings if f["severity"] == INFO]

    if crit:
        verdict = "ISSUES"
    elif rc is None:
        # No RC and nothing else flagged. Exempt customers (e.g. Amazon) → OK;
        # otherwise surface as RC MISSING (delivered+missing RC already became a
        # critical above, so this path is the not-confirmed-delivered case).
        verdict = "OK" if _rc_exempt(ditat, rules) else "RC MISSING"
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
