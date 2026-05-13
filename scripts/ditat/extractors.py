"""Key extraction utilities for API objects."""

from typing import Optional


class KeyExtractor:
    """Extract identifiers from API objects (case-insensitive)."""

    FIELD_PRIORITY = {
        "document": ("Key", "DocumentKey", "Id", "DocumentId", "Guid", "Uid"),
        "shipment": ("Key", "ShipmentKey", "Id", "ShipmentId"),
    }

    @classmethod
    def extract(cls, obj: dict, entity_type: str = "document") -> Optional[str]:
        """Extract the most likely identifier from an object (case-insensitive)."""
        candidates = cls.FIELD_PRIORITY.get(entity_type, ("Key", "Id"))
        if not isinstance(obj, dict):
            return None
        lowered = {k.lower(): k for k in obj.keys()}
        for candidate in candidates:
            real = lowered.get(candidate.lower())
            if real is None:
                continue
            v = obj[real]
            if v not in (None, ""):
                return str(v)
        return None
