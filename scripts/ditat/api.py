"""Ditat API surface: error type, response unwrap, ci_get, shipment + document services.

Replaces the old `response.py` + `extractors.py` + `services.py` split. One file
because all three are the same concept — "talk to Ditat" — and each was <100
lines that cross-referenced the others.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from .client import DitatClient

log = logging.getLogger("ditat")


# ---------------------------------------------------------------- error type

class DitatApiError(RuntimeError):
    """Single exception type for all Ditat API failures (HTTP + envelope-level)."""

    def __init__(self, message: str, *, code: Optional[int] = None,
                 http_status: Optional[int] = None, url: Optional[str] = None):
        self.code = code
        self.http_status = http_status
        self.message = message
        self.url = url
        suffix = []
        if http_status is not None:
            suffix.append(f"http={http_status}")
        if code is not None:
            suffix.append(f"code={code}")
        if url:
            suffix.append(f"url={url}")
        tag = f" [{', '.join(suffix)}]" if suffix else ""
        super().__init__(f"{message}{tag}")


# ---------------------------------------------------------------- helpers

def ci_get(d: Any, *keys: str) -> Any:
    """Case-insensitive first-match dict getter. Returns None for non-dict input."""
    if not isinstance(d, dict):
        return None
    lowered = {k.lower(): k for k in d.keys()}
    for k in keys:
        real = lowered.get(k.lower())
        if real is not None:
            return d[real]
    return None


_ENTITY_KEYS = ("EntityList", "data", "Entities", "Items", "Records", "Results")


def unwrap(resp: requests.Response) -> Any:
    """Return the `Data` property from a Ditat envelope. Raise DitatApiError on failure."""
    if resp.status_code != 200:
        raise DitatApiError(
            f"HTTP {resp.status_code}: {resp.text[:300]}",
            http_status=resp.status_code,
            url=resp.url,
        )
    body = resp.json()
    err = ci_get(body, "Error")
    if err:
        url = ci_get(body, "Url")
        if isinstance(err, dict):
            raise DitatApiError(
                ci_get(err, "Message") or "unknown",
                code=ci_get(err, "Code"),
                url=url,
            )
        raise DitatApiError(str(err), url=url)
    return ci_get(body, "Data")


def extract_entity_list(data: Any) -> list[dict]:
    """Pull the list of entities from response data — handles list, dict-wrapping, etc."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for k in _ENTITY_KEYS:
        v = ci_get(data, k)
        if isinstance(v, list):
            return v
    return []


# Document-key candidates (case-insensitive). Shipment keys are looked up under
# Key/ShipmentKey/Id/ShipmentId — `ci_get(obj, "Key", "ShipmentKey", "Id", ...)`
# is enough for that, no separate class needed.
_DOC_KEY_FIELDS = ("Key", "DocumentKey", "Id", "DocumentId", "Guid", "Uid")
_SHIPMENT_KEY_FIELDS = ("Key", "ShipmentKey", "Id", "ShipmentId")


def shipment_key(obj: Any) -> Optional[str]:
    v = ci_get(obj, *_SHIPMENT_KEY_FIELDS)
    return str(v) if v not in (None, "") else None


def document_key(obj: Any) -> Optional[str]:
    v = ci_get(obj, *_DOC_KEY_FIELDS)
    return str(v) if v not in (None, "") else None


# ---------------------------------------------------------------- ShipmentService

class ShipmentService:
    """Shipment lookup + detail fetch."""

    def __init__(self, client: DitatClient):
        self.client = client

    def list_shipments(
        self,
        since: Optional[datetime] = None,
        filter_column: str = "updatedOn",
        page_size: int = 1000,
    ) -> list[dict]:
        """POST api/tms/lookup/shipments. since=None → no filter.

        Asks the API to return `TotalCount`; warns when truncated by `page_size`
        so the caller knows the window is too wide.
        """
        body: dict[str, Any] = {
            "FilterItems": self._build_filter_items(since, filter_column),
            "PageNumber": 1,
            "PageSize": page_size,
            "IncludeTotalCount": True,
        }
        log.info("list_shipments body=%s", body)
        resp = self.client.post("api/tms/lookup/shipments", json_body=body)
        data = unwrap(resp)

        items = extract_entity_list(data)
        total = ci_get(data, "TotalCount") if isinstance(data, dict) else None
        log.info("list_shipments → %d items%s",
                 len(items),
                 f" (TotalCount={total})" if total else "")
        if isinstance(total, int) and total > len(items):
            log.warning(
                "Ditat returned %d shipments but TotalCount=%d — narrow the window "
                "or raise --page-size to avoid silent truncation.",
                len(items), total,
            )
        return items

    def get_details(self, key: str,
                    include_documents: bool = True,
                    include_notes: bool = True) -> dict:
        """GET api/tms/data/shipment?key=... — auto-retries without docs/notes on code 900."""
        params = {"key": key, "includeEntityGraph": "true"}
        if include_documents:
            params["includeDocuments"] = "true"
        if include_notes:
            params["includeNotes"] = "true"

        try:
            resp = self.client.get("api/tms/data/shipment", params=params)
            return unwrap(resp)
        except DitatApiError as e:
            if e.code == 900 and (include_documents or include_notes):
                msg = (e.message or "").lower()
                drop_docs = include_documents and "document" in msg
                drop_notes = include_notes and "note" in msg
                if drop_docs or drop_notes:
                    log.warning(
                        "code 900 on include flags (%s) — retrying without them. "
                        "Grant the appropriate View role to fix.",
                        e.message,
                    )
                    return self.get_details(
                        key,
                        include_documents=include_documents and not drop_docs,
                        include_notes=include_notes and not drop_notes,
                    )
            raise

    @staticmethod
    def _fmt_ditat_dt(dt: datetime) -> str:
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    @classmethod
    def _build_filter_items(cls, since: Optional[datetime], filter_column: str) -> list[dict]:
        if since is None:
            return []
        return [{
            "ColumnName": filter_column,
            "FilterType": "After",
            "FilterFromValue": cls._fmt_ditat_dt(since),
            "FilterToValue": None,
        }]


# ---------------------------------------------------------------- DocumentService

class DocumentService:
    """Document listing + binary download.

    Download endpoint:
      GET api/tms/data/shipment/{shipmentKey}/document/{documentKey}/file
    Returns binary; OAuth Bearer accepted. Falls back to inline base64 when the
    endpoint returns a JSON envelope instead of the file blob.
    """

    INLINE_CONTENT_FIELDS = ("Content", "FileContent", "Data", "Base64")
    DOWNLOAD_RETRIES = 2  # one initial + one retry on stream failure

    def __init__(self, client: DitatClient):
        self.client = client

    def list_documents(self, shipment_data: dict) -> list[dict]:
        docs = ci_get(shipment_data, "Documents") or []
        if not docs:
            log.info("No documents on this shipment (or `documents` View role not granted).")
            return []
        return docs

    def download(self, doc: dict, shipment_key: str, out_dir: Path) -> Optional[Path]:
        """Download to disk via shipment subresource. Returns the saved Path, or None."""
        dkey = document_key(doc)
        if not dkey:
            log.warning("No key field on document: %s", list(doc.keys())[:8])
            return None

        path = f"api/tms/data/shipment/{shipment_key}/document/{dkey}/file"
        last_err: Optional[Exception] = None
        for attempt in range(1, self.DOWNLOAD_RETRIES + 1):
            try:
                resp = self.client.get(path, stream=True)
            except Exception as e:
                last_err = e
                log.warning("Download request failed (%d/%d) %s: %s",
                            attempt, self.DOWNLOAD_RETRIES, path, e)
                continue

            ct = resp.headers.get("Content-Type", "")
            if resp.status_code == 200 and "json" not in ct.lower():
                filename = self._extract_filename(resp, doc, dkey)
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / filename
                total = 0
                try:
                    with out_path.open("wb") as fh:
                        for chunk in resp.iter_content(chunk_size=64 * 1024):
                            if chunk:
                                fh.write(chunk)
                                total += len(chunk)
                except Exception as e:
                    # Half-written file — clean up so next run doesn't see a corrupt PDF.
                    try:
                        out_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    last_err = e
                    log.warning("Stream interrupted (%d/%d) %s: %s",
                                attempt, self.DOWNLOAD_RETRIES, path, e)
                    continue
                log.info("Downloaded %d bytes (CT=%s) → %s", total, ct, out_path)
                return out_path

            log.warning("Download %s returned HTTP %s CT=%s — trying inline fallback",
                        path, resp.status_code, ct)
            break  # don't retry HTTP errors via stream; try inline once

        inline = self._try_inline_content(doc, dkey, out_dir)
        if inline is None and last_err is not None:
            log.error("Download permanently failed for %s: %s", path, last_err)
        return inline

    @staticmethod
    def _extract_filename(resp, doc: dict, doc_key: str) -> str:
        cd = resp.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            raw = cd.split("filename=", 1)[1].strip().strip('"').strip("'")
            if raw:
                return raw
        name = doc.get("fileName") or doc.get("FileName") or doc.get("Name")
        if name:
            ct = resp.headers.get("Content-Type", "").lower()
            if "pdf" in ct and not name.lower().endswith(".pdf"):
                name = f"{name}.pdf"
            return name
        return f"document_{doc_key}.bin"

    def _try_inline_content(self, doc: dict, doc_key: str, out_dir: Path) -> Optional[Path]:
        for field in self.INLINE_CONTENT_FIELDS:
            blob_b64 = doc.get(field)
            if not blob_b64:
                continue
            try:
                blob = base64.b64decode(blob_b64)
            except Exception as e:
                log.warning("Inline decode failed for field '%s': %s", field, e)
                continue
            filename = doc.get("FileName") or doc.get("Name") or f"document_{doc_key}.bin"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / filename
            path.write_bytes(blob)
            log.info("Saved %d bytes (inline) → %s", len(blob), path)
            return path
        return None
