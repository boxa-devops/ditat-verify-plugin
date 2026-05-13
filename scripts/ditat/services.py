"""Business logic services for shipments and documents."""

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .client import DitatClient
from .extractors import KeyExtractor
from .response import DitatApiError, ResponseParser

log = logging.getLogger("ditat")


class ShipmentService:
    """Handle shipment API operations."""

    def __init__(self, client: DitatClient):
        self.client = client
        self.parser = ResponseParser()

    # Common column-name candidates for "last updated" across Ditat lookups
    UPDATED_COLUMN_CANDIDATES = [
        "UpdatedOn", "Updated", "LastUpdated", "ModifiedOn", "Modified",
        "UpdateDate", "LastModified", "LastModifiedOn", "DateUpdated",
        "DateModified", "CreatedOn",
    ]

    def list_shipments(
        self,
        since: Optional[datetime] = None,
        filter_column: str = "updatedOn",
    ) -> list[dict]:
        """POST api/tms/lookup/shipments. since=None → no filter (empty FilterItems)."""
        log.info("=" * 70)
        log.info("STEP 1: List shipments")
        log.info("=" * 70)

        filter_items = self._build_filter_items(since, filter_column)
        body = {"FilterItems": filter_items}
        log.info("Request body: %s", body)
        resp = self.client.post("api/tms/lookup/shipments", json_body=body)
        data = self.parser.unwrap(resp)

        items = self.parser.extract_entity_list(data)
        log.info("Found %d shipments", len(items))
        for s in items[:5]:
            log.info("  - %s", {k: s.get(k) for k in list(s.keys())[:6]})

        return items

    def diagnose(self) -> None:
        """Probe shipments lookup: dump raw envelope, try body variants and paths.

        Prior diagnose showed 0 for unfiltered + all column candidates. Means either:
          - response envelope key is not Data.EntityList
          - endpoint needs paging / SelectItems / extra body fields
          - path is wrong (singular vs plural, different segment)
        This version dumps raw bodies so the real shape is visible.
        """
        import json as _json

        log.info("=" * 70)
        log.info("DIAGNOSE: probing shipments lookup")
        log.info("=" * 70)

        body_variants: list[tuple[str, dict]] = [
            ("empty",                {"FilterItems": []}),
            ("paged_take_100",       {"FilterItems": [], "PageNumber": 1, "PageSize": 100}),
            ("skip_take",            {"FilterItems": [], "Skip": 0, "Take": 100}),
            ("select_all_star",      {"FilterItems": [], "SelectItems": [{"ColumnName": "*"}]}),
            ("with_include_total",   {"FilterItems": [], "IncludeTotalCount": True}),
        ]
        path_variants = [
            "api/tms/lookup/shipments",
            "api/tms/lookup/shipment",
        ]

        first_raw_logged = False
        for path in path_variants:
            log.info("-" * 70)
            log.info("PATH: %s", path)
            for label, body in body_variants:
                try:
                    resp = self.client.post(path, json_body=body)
                except Exception as e:
                    log.info("  %-22s → request error: %s", label, e)
                    continue

                log.info("  %-22s → HTTP %s  ContentType=%s",
                         label, resp.status_code, resp.headers.get("Content-Type", ""))

                if resp.status_code != 200:
                    log.info("    body: %s", resp.text[:400])
                    continue

                try:
                    raw = resp.json()
                except Exception as e:
                    log.info("    non-JSON body (%s): %s", e, resp.text[:300])
                    continue

                # Dump first successful raw envelope in full (truncated)
                if not first_raw_logged:
                    log.info("    RAW ENVELOPE (truncated 2000ch):")
                    log.info("    %s", _json.dumps(raw, default=str)[:2000])
                    first_raw_logged = True

                # Envelope shape report
                err = raw.get("Error") if isinstance(raw, dict) else None
                if err:
                    log.info("    Error in envelope: %s", err)

                data = raw.get("Data") if isinstance(raw, dict) else None
                if isinstance(data, dict):
                    keys = list(data.keys())
                    log.info("    Data keys: %s", keys)
                    for k in keys:
                        v = data[k]
                        if isinstance(v, list):
                            log.info("      Data[%s] = list(len=%d)", k, len(v))
                            if v and isinstance(v[0], dict):
                                log.info("        first item keys: %s", sorted(v[0].keys())[:20])
                        elif isinstance(v, dict):
                            log.info("      Data[%s] = dict(keys=%s)", k, list(v.keys())[:10])
                        else:
                            log.info("      Data[%s] = %r", k, v if not isinstance(v, str) else v[:80])
                elif isinstance(data, list):
                    log.info("    Data is list(len=%d)", len(data))
                    if data and isinstance(data[0], dict):
                        log.info("      first item keys: %s", sorted(data[0].keys())[:20])
                else:
                    log.info("    Data = %r", data)

        log.info("-" * 70)
        log.info("Diagnose complete. Inspect Data shape above to find real items key.")

    def get_details(self, key: str, include_documents: bool = True, include_notes: bool = True) -> dict:
        """GET api/tms/data/shipment?key=<key>&includeEntityGraph=true with optional include flags.

        Requires API user to have View role on shipment + (documents/notes) subpermissions
        in TMS admin. If documents/notes role is missing, server returns code 900
        'User does not have permission to view documents/notes for this type of data'
        AND blanks the entire `data` payload (short-circuits). When that happens, retry
        without the offending flag so the entity still loads.
        """
        log.info("-" * 70)
        log.info("STEP 2: Get details for shipment key=%s", key)
        log.info("-" * 70)

        params = {"key": key, "includeEntityGraph": "true"}
        if include_documents:
            params["includeDocuments"] = "true"
        if include_notes:
            params["includeNotes"] = "true"

        try:
            resp = self.client.get("api/tms/data/shipment", params=params)
            data = self.parser.unwrap(resp)
        except DitatApiError as e:
            if e.code == 900 and (include_documents or include_notes):
                msg = (e.message or "").lower()
                drop_docs = include_documents and "document" in msg
                drop_notes = include_notes and "note" in msg
                if drop_docs or drop_notes:
                    log.warning("code 900 on include flags (%s) — retrying without them. "
                                "Grant View role on documents/notes in TMS admin to fix.", e.message)
                    return self.get_details(
                        key,
                        include_documents=include_documents and not drop_docs,
                        include_notes=include_notes and not drop_notes,
                    )
            raise

        entity = ResponseParser._ci_get(data, "EntityGraph") if isinstance(data, dict) else None
        docs = ResponseParser._ci_get(data, "Documents") if isinstance(data, dict) else None
        notes = ResponseParser._ci_get(data, "Notes") if isinstance(data, dict) else None
        log.info("Top-level Data keys: %s", list(data.keys()) if isinstance(data, dict) else type(data).__name__)
        log.info("EntityGraph fields: %d", len(entity) if isinstance(entity, dict) else 0)
        log.info("Documents in envelope: %d", len(docs) if isinstance(docs, list) else 0)
        log.info("Notes in envelope: %d", len(notes) if isinstance(notes, list) else 0)

        return data

    @staticmethod
    def _format_ditat_datetime(dt: datetime) -> str:
        """Format datetime as Ditat-required yyyy-MM-ddTHH:mm:ss.fffZ (millisecond precision)."""
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    @classmethod
    def _build_filter_items(cls, since: Optional[datetime], filter_column: str = "updatedOn") -> list[dict]:
        """Build filter items for shipment lookup. since=None → no filter (empty list)."""
        if since is None:
            return []
        return [{
            "ColumnName": filter_column,
            "FilterType": "After",
            "FilterFromValue": cls._format_ditat_datetime(since),
            "FilterToValue": None,
        }]


class DocumentService:
    """Handle document operations and downloads.

    Download endpoint (locked 2026-05-12 from TMS web UI capture):
      GET api/tms/data/shipment/{shipmentKey}/document/{documentKey}/file
    Returns binary file (Content-Type per file type, Content-Disposition with filename).
    OAuth Bearer auth accepted; legacy `?ditat-token=<accountId:sessionToken>`
    query param also works but is not needed.
    """

    INLINE_CONTENT_FIELDS = ("Content", "FileContent", "Data", "Base64")

    def __init__(self, client: DitatClient):
        self.client = client
        self.key_extractor = KeyExtractor()

    def list_documents(self, shipment_data: dict) -> list[dict]:
        """Extract documents list from shipment data (case-insensitive)."""
        log.info("-" * 70)
        log.info("STEP 3: Extract documents list")
        log.info("-" * 70)

        docs = ResponseParser._ci_get(shipment_data, "Documents") or []
        if not docs:
            log.info("No documents on this shipment (or `documents` View role not granted — "
                     "check earlier code 900 warning)")
            return []

        for i, d in enumerate(docs):
            keys_preview = {k: d.get(k) for k in list(d.keys())[:8]}
            log.info("  Doc %d: %s", i, keys_preview)
        return docs

    def download(self, doc: dict, shipment_key: str, out_dir: Path) -> Optional[Path]:
        """Download document binary via shipment subresource path.

        GET api/tms/data/shipment/{shipmentKey}/document/{documentKey}/file
        Falls back to inline base64 content if endpoint returns non-binary.
        """
        log.info("-" * 70)
        log.info("STEP 4: Download document")
        log.info("-" * 70)

        doc_key = self.key_extractor.extract(doc, "document")
        if not doc_key:
            log.warning("Could not find a key field on the document object: %s", list(doc.keys()))
            return None

        path = f"api/tms/data/shipment/{shipment_key}/document/{doc_key}/file"
        try:
            resp = self.client.get(path, stream=True)
        except Exception as e:
            log.warning("Download request failed for %s: %s", path, e)
            return self._try_inline_content(doc, doc_key, out_dir)

        ct = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and "json" not in ct.lower():
            filename = self._extract_filename(resp, doc, doc_key)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / filename
            out_path.write_bytes(resp.content)
            log.info("Downloaded %d bytes (CT=%s) → %s", len(resp.content), ct, out_path)
            return out_path

        log.warning("Download %s returned HTTP %s CT=%s — falling back to inline content",
                    path, resp.status_code, ct)
        return self._try_inline_content(doc, doc_key, out_dir)

    @staticmethod
    def _extract_filename(resp, doc: dict, doc_key: str) -> str:
        """Prefer Content-Disposition filename, then doc.fileName, else doc_<key>.bin."""
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
        """Decode inline base64 content from one of the known fields, save to disk."""
        for inline_field in self.INLINE_CONTENT_FIELDS:
            blob_b64 = doc.get(inline_field)
            if not blob_b64:
                continue
            log.info("Document has inline content in field '%s' — decoding", inline_field)
            try:
                blob = base64.b64decode(blob_b64)
            except Exception as e:
                log.warning("Inline decode failed for field '%s': %s", inline_field, e)
                continue
            filename = doc.get("FileName") or doc.get("Name") or f"document_{doc_key}.bin"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / filename
            path.write_bytes(blob)
            log.info("Saved %d bytes to %s", len(blob), path)
            return path
        return None
