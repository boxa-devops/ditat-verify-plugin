"""SQLite state store: processed-shipment ledger.

Single table, single migration. Open/close per call — SQLite is fine for that
volume and it avoids long-lived connections holding the WAL open.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_shipments (
    shipment_key   TEXT PRIMARY KEY,
    shipment_id    TEXT,
    processed_at   TEXT NOT NULL,
    report_path    TEXT,
    critical_count INTEGER DEFAULT 0,
    warn_count     INTEGER DEFAULT 0,
    verdict        TEXT
)
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(_SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(processed_shipments)")}
    if "verdict" not in cols:
        conn.execute("ALTER TABLE processed_shipments ADD COLUMN verdict TEXT")
    return conn


def processed_keys(db_path: Path) -> set[str]:
    conn = connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT shipment_key FROM processed_shipments")}
    finally:
        conn.close()


def mark_batch(db_path: Path, rows: Iterable[dict]) -> int:
    """Insert/replace every row in one transaction. Returns count written.

    Each `row` is `{shipment_key, shipment_id, report_path, critical, warn, verdict}`.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    written = 0
    conn = connect(db_path)
    try:
        with conn:
            for r in rows:
                key = r.get("shipment_key")
                if not key:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO processed_shipments "
                    "(shipment_key, shipment_id, processed_at, report_path, "
                    " critical_count, warn_count, verdict) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        r.get("shipment_id"),
                        now_iso,
                        r.get("report_path"),
                        int(r.get("critical") or 0),
                        int(r.get("warn") or 0),
                        r.get("verdict"),
                    ),
                )
                written += 1
    finally:
        conn.close()
    return written


def mark_one(db_path: Path, *, shipment_key: str, shipment_id: str | None,
             report_path: str | None, critical: int, warn: int,
             verdict: str | None) -> None:
    mark_batch(db_path, [{
        "shipment_key": shipment_key,
        "shipment_id": shipment_id,
        "report_path": report_path,
        "critical": critical,
        "warn": warn,
        "verdict": verdict,
    }])


def recent_status(db_path: Path, limit: int = 20) -> dict:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "SELECT shipment_key, shipment_id, processed_at, verdict, "
            "critical_count, warn_count, report_path "
            "FROM processed_shipments ORDER BY processed_at DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
        total = conn.execute("SELECT COUNT(*) FROM processed_shipments").fetchone()[0]
        return {"total_processed": total, "recent": rows}
    finally:
        conn.close()


def reset(db_path: Path, shipment_key: str) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                "DELETE FROM processed_shipments WHERE shipment_key=?",
                (shipment_key,),
            )
    finally:
        conn.close()
