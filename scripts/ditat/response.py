"""Response parsing utilities for Ditat API envelopes."""

from typing import Any, Optional

import requests


class DitatApiError(RuntimeError):
    """App-level error from a Ditat envelope (HTTP 200 + non-null Error)."""

    def __init__(self, code: Optional[int], message: str, url: Optional[str] = None):
        self.code = code
        self.message = message
        self.url = url
        super().__init__(f"[code={code}] {message} (url={url})")


class ResponseParser:
    """Parse Ditat API envelope responses (case-insensitive)."""

    @staticmethod
    def _ci_get(d: dict, *keys: str) -> Any:
        """Get first matching key from dict, case-insensitive."""
        if not isinstance(d, dict):
            return None
        lowered = {k.lower(): k for k in d.keys()}
        for k in keys:
            real = lowered.get(k.lower())
            if real is not None:
                return d[real]
        return None

    @classmethod
    def unwrap(cls, resp: requests.Response) -> Any:
        """Return the Data property from a Ditat envelope, raising on error."""
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} on {resp.url}: {resp.text[:300]}")
        body = resp.json()
        err = cls._ci_get(body, "Error")
        if err:
            url = cls._ci_get(body, "Url")
            raise DitatApiError(
                code=cls._ci_get(err, "Code") if isinstance(err, dict) else None,
                message=cls._ci_get(err, "Message") if isinstance(err, dict) else str(err),
                url=url,
            )
        return cls._ci_get(body, "Data")

    @classmethod
    def extract_entity_list(cls, data: Any) -> list[dict]:
        """Extract list of entities from response data (handles multiple shapes)."""
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        # Try known item-list keys, case-insensitive
        for k in ("EntityList", "data", "Entities", "Items", "Records", "Results"):
            v = cls._ci_get(data, k)
            if isinstance(v, list):
                return v
        return []
