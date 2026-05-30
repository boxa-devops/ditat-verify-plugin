"""Pure transform from Ditat `EntityGraph` JSON to the slim shipment record we diff.

No I/O. No side effects. Lives in its own file because this is the most likely
shape to change as Ditat's API evolves and it's the entry point worth testing
in isolation.
"""

from __future__ import annotations

from typing import Any

from .api import ci_get


# Fields the rest of the pipeline relies on. Anything outside this set should NOT
# be added without a matching diff rule.
_SLIM_KEYS = (
    "bol_number", "load_number", "equipment_type",
    "total_weight_lbs", "total_pieces", "total_revenue", "commodity",
    "customer", "pickup", "delivery",
)


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _stop_summary(stops: Any, *needles: str) -> dict:
    if not isinstance(stops, list):
        return {}
    for s in stops:
        if not isinstance(s, dict):
            continue
        stype = str(ci_get(s, "stopType", "type") or "").lower()
        if any(n in stype for n in needles):
            return {
                "city":  ci_get(s, "city"),
                "state": ci_get(s, "state"),
                "appointment_from": ci_get(s, "appointmentFrom", "scheduledFrom"),
                "appointment_to":   ci_get(s, "appointmentTo", "scheduledTo"),
            }
    return {}


def slim_ditat(details: dict) -> dict:
    """Flatten a `details` payload to the small dict the diff layer expects.

    Empty/null totals are reported as `None` only when no contributing data was
    found — a true 0-weight or 0-piece shipment is reported as 0, not None.
    """
    eg = ci_get(details, "EntityGraph") or details or {}
    if not isinstance(eg, dict):
        return {k: None for k in _SLIM_KEYS}

    stops = ci_get(eg, "dspShipmentStops") or []
    items = ci_get(eg, "dspShipmentItems") or []
    revenues = ci_get(eg, "rnpShipmentRevenues") or []

    total_weight: float | None = None
    total_pieces: int | None = None
    commodities: list[str] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        w = _safe_float(ci_get(it, "weight", "weightLbs"))
        if w is not None:
            total_weight = (total_weight or 0.0) + w
        p = _safe_int(ci_get(it, "pieces", "quantity"))
        if p is not None:
            total_pieces = (total_pieces or 0) + p
        c = ci_get(it, "commodity", "description")
        if c:
            commodities.append(c)

    total_revenue: float | None = None
    for r in revenues if isinstance(revenues, list) else []:
        if not isinstance(r, dict):
            continue
        amt = _safe_float(ci_get(r, "amount", "total", "totalAmount"))
        if amt is not None:
            total_revenue = (total_revenue or 0.0) + amt
    # Fall back to a top-level revenue column when the rnpShipmentRevenues
    # collection is absent (some Ditat tenants only populate the scalar field).
    if total_revenue is None:
        total_revenue = _safe_float(ci_get(eg, "revenue", "totalRevenue"))

    customer = ci_get(eg, "customer", "customerName", "billToCustomer",
                      "billTo", "customerLegalName")
    if isinstance(customer, dict):
        customer = ci_get(customer, "name", "legalName", "displayName")

    return {
        "bol_number":       ci_get(eg, "bolNumber", "bol"),
        "load_number":      ci_get(eg, "loadId", "loadNumber"),
        "equipment_type":   ci_get(eg, "equipment", "equipmentType", "trailerType"),
        "total_weight_lbs": total_weight,
        "total_pieces":     total_pieces,
        "total_revenue":    total_revenue,
        "commodity":        commodities[0] if commodities else None,
        "customer":         customer,
        "pickup":           _stop_summary(stops, "pickup", "origin"),
        "delivery":         _stop_summary(stops, "delivery", "destination", "consignee"),
    }
