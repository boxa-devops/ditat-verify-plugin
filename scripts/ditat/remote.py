"""Client for the Ditat verification server.

The plugin no longer talks to Ditat directly — it calls our own server, which
holds the Ditat credentials. This module: (1) loads server config from env,
(2) POSTs /batch for a manifest, (3) downloads each per-doc URL to disk.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

log = logging.getLogger("ditat")

_DOWNLOAD_RETRIES = 2
_DOC_WORKERS = 4


class ServerConfig:
    """Server URL + API key, loaded from .env / environment."""

    def __init__(self) -> None:
        _load_dotenv()
        self.base_url = (os.getenv("DITAT_SERVER_URL") or "").rstrip("/")
        self.api_key = os.getenv("DITAT_SERVER_API_KEY")

    def validate(self) -> tuple[bool, list[str]]:
        missing = []
        if not self.base_url:
            missing.append("DITAT_SERVER_URL")
        # api_key may be intentionally empty for an open dev server, so it's not required.
        return len(missing) == 0, missing

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key} if self.api_key else {}


def _load_dotenv() -> None:
    explicit = os.getenv("DITAT_DOTENV_PATH")
    if explicit and Path(explicit).is_file():
        load_dotenv(explicit)
        return
    project = os.getenv("CLAUDE_PROJECT_DIR")
    if project and (Path(project) / ".env").is_file():
        load_dotenv(Path(project) / ".env")
        return
    load_dotenv()


def fetch_batch(config: ServerConfig, *, since_days: int = 30, last_week: bool = False,
                all_time: bool = False, filter_column: Optional[str] = None,
                limit: int = 500, page_size: int = 1000,
                require_docs: bool = True) -> dict:
    """POST /batch and return the manifest dict."""
    params = {
        "since_days": since_days,
        "last_week": str(last_week).lower(),
        "all": str(all_time).lower(),
        "limit": limit,
        "page_size": page_size,
        "require_docs": str(require_docs).lower(),
    }
    if filter_column:
        params["filter_column"] = filter_column

    url = f"{config.base_url}/batch"
    log.info("POST %s params=%s", url, params)
    resp = requests.post(url, params=params, headers=config._headers(), timeout=300)
    if resp.status_code != 200:
        raise RuntimeError(f"server /batch returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def download_docs(config: ServerConfig, manifest: dict, out_root: Path) -> list[dict]:
    """Download every doc in the manifest to disk; return enriched batch records.

    Each record mirrors the old fetch shape: ditat_fields + documents with a
    local `path`. Docs that fail to download are dropped (logged).
    """
    records: list[dict] = []
    for ship in manifest.get("shipments", []):
        key = str(ship.get("shipment_key") or "")
        out_dir = out_root / key
        doc_records = []
        for d in ship.get("documents", []):
            path = _download_one(config, d.get("url"), out_dir, d.get("file_name"),
                                 d.get("doc_key"))
            if path is None:
                continue
            doc_records.append({
                "doc_key":        d.get("doc_key"),
                "file_name":      path.name,
                "file_type":      d.get("file_type"),
                "classification": d.get("classification"),
                "path":           str(path.resolve()),
            })
        records.append({
            "shipment_key": key,
            "shipment_id":  ship.get("shipment_id"),
            "ditat_fields": ship.get("ditat_fields") or {},
            "docs_missing": ship.get("docs_missing") or [],
            "documents":    doc_records,
        })
    return records


# Ditat filenames often arrive with NO extension (e.g. "UNIT 565042 160112 RC").
# The Read tool needs a real extension to OCR a PDF as an image — without it the
# file is treated as text and the agent gets raw PDF bytes. Derive one from the
# response Content-Type.
_CT_EXT = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
}


def _ensure_extension(filename: str, content_type: str) -> str:
    if Path(filename).suffix:
        return filename
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return filename + _CT_EXT.get(ct, ".pdf")  # carrier docs are overwhelmingly PDF


def _download_one(config: ServerConfig, url: Optional[str], out_dir: Path,
                  file_name: Optional[str], doc_key: Optional[str]) -> Optional[Path]:
    if not url:
        return None
    base_name = file_name or f"document_{doc_key or 'unknown'}"
    for attempt in range(1, _DOWNLOAD_RETRIES + 1):
        try:
            resp = requests.get(url, headers=config._headers(), stream=True, timeout=120)
        except Exception as e:  # noqa: BLE001
            log.warning("doc download failed (%d/%d) %s: %s", attempt, _DOWNLOAD_RETRIES, url, e)
            continue
        if resp.status_code != 200:
            log.warning("doc download HTTP %s for %s", resp.status_code, url)
            resp.close()
            return None
        out_path = out_dir / _ensure_extension(base_name, resp.headers.get("Content-Type", ""))
        out_dir.mkdir(parents=True, exist_ok=True)
        total = 0
        try:
            with out_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)
                        total += len(chunk)
        except Exception as e:  # noqa: BLE001
            out_path.unlink(missing_ok=True)
            log.warning("doc stream interrupted (%d/%d) %s: %s",
                        attempt, _DOWNLOAD_RETRIES, url, e)
            continue
        finally:
            resp.close()
        log.info("Downloaded %d bytes → %s", total, out_path)
        return out_path
    return None
