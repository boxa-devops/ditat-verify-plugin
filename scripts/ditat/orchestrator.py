"""Orchestration of the end-to-end shipment inspection workflow."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from .client import DitatClient
from .config import Config
from .extractors import KeyExtractor
from .services import DocumentService, ShipmentService

log = logging.getLogger("ditat")


class Orchestrator:
    """Orchestrate the end-to-end shipment inspection workflow."""

    def __init__(self, config: Config):
        self.config = config
        self.client = DitatClient(
            config.base_url,
            config.account_id,
            config.client_id,
            config.client_secret,
        )
        self.shipment_service = ShipmentService(self.client)
        self.document_service = DocumentService(self.client)
        self.key_extractor = KeyExtractor()

    def run(
        self,
        since_days: int = 7,
        all_shipments: bool = False,
        specific_key: Optional[str] = None,
        max_shipments: Optional[int] = None,
        diagnose: bool = False,
        filter_column: str = "updatedOn",
    ) -> int:
        """Execute the full shipment inspection workflow."""
        if max_shipments is None:
            max_shipments = self.config.max_shipments

        # --- Diagnostic mode: probe filter columns and exit ---
        if diagnose:
            self.shipment_service.diagnose()
            return 0

        # --- Step 1: List shipments ---
        if specific_key:
            shipments = [{"Key": specific_key}]
        else:
            since = None if all_shipments else datetime.now(timezone.utc) - timedelta(days=since_days)
            shipments = self.shipment_service.list_shipments(since=since, filter_column=filter_column)

        if not shipments:
            log.warning("No shipments returned — nothing to inspect")
            return 0

        # --- Steps 2-4: Details, documents, downloads ---
        for s in shipments[: max_shipments]:
            key = self.key_extractor.extract(s, "shipment")
            if not key:
                log.warning("Could not determine key field on shipment: %s", list(s.keys()))
                continue

            try:
                details = self.shipment_service.get_details(key)
            except Exception as e:
                log.error("Detail fetch failed for %s: %s", key, e)
                continue

            docs = self.document_service.list_documents(details)

            ship_out_dir = self.config.download_dir / str(key)
            for doc in docs:
                self.document_service.download(doc, shipment_key=key, out_dir=ship_out_dir)

        log.info("Done.")
        return 0
