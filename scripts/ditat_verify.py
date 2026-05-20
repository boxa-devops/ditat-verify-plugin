#!/usr/bin/env python3
"""
Ditat shipment verification helper — drives the /ditat-verify Claude skill.

Subcommands:
  check-env  — validate .env / credentials without making API calls.
  fetch      — list unprocessed shipments, fetch details + download docs in
               parallel, emit slim JSON descriptor on stdout, persist a batch
               sidecar so `finalize` can render the docx without re-fetching.
  verify-one — fetch + download for a single specific shipment (re-verify).
  finalize   — consume the agent's findings JSON, run deterministic diffs,
               mark every shipment processed, build ONE batch .docx with a
               summary table + detail section for problematic shipments only.
  mark       — record a single shipment as processed (used by verify-one).
  status     — show recent processed shipments.
  reset      — clear processed flag for a shipment.

Logs go to STDERR. Stdout is JSON, pipeable.

State stored in state.db (SQLite, project root). Schema:
  processed_shipments(shipment_key PK, shipment_id, processed_at, report_path,
                      critical_count, warn_count, verdict)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ditat import diff as diff_mod
from ditat.client import DitatClient
from ditat.config import Config
from ditat.docx_report import build_batch_docx
from ditat.extractors import KeyExtractor
from ditat.logging_utils import setup_logging
from ditat.response import ResponseParser
from ditat.services import DocumentService, ShipmentService

log = logging.getLogger("ditat")


# ---------------------------------------------------------------- paths

def _state_root() -> Path:
    """State directory: env override → CLAUDE_PROJECT_DIR → cwd."""
    return Path(
        os.getenv("DITAT_STATE_DIR")
        or os.getenv("CLAUDE_PROJECT_DIR")
        or Path.cwd()
    ).resolve()


STATE_DB = _state_root() / os.getenv("DITAT_STATE_DB_NAME", "state.db")
REPORTS_DIR = Path(os.getenv("DITAT_REPORTS_DIR") or (_state_root() / "reports"))
BATCH_SIDECAR = _state_root() / ".ditat_batch.json"


# ---------------------------------------------------------------- state.db

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(STATE_DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
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
    )
    # idempotent migrations
    cols = {row[1] for row in conn.execute("PRAGMA table_info(processed_shipments)")}
    if "verdict" not in cols:
        conn.execute("ALTER TABLE processed_shipments ADD COLUMN verdict TEXT")
    return conn


# ---------------------------------------------------------------- ditat slim

def _slim_ditat(details: dict) -> dict:
    """Compact Ditat record — only the fields used in diff / docx header."""
    eg = ResponseParser._ci_get(details, "EntityGraph") or details or {}
    if not isinstance(eg, dict):
        return {}
    pick = lambda *keys: ResponseParser._ci_get(eg, *keys)
    stops = pick("dspShipmentStops") or []
    items = pick("dspShipmentItems") or []
    revenues = pick("rnpShipmentRevenues") or []

    def stop_summary(*needles: str) -> dict:
        if not isinstance(stops, list):
            return {}
        for s in stops:
            if not isinstance(s, dict):
                continue
            stype = str(ResponseParser._ci_get(s, "stopType", "type") or "").lower()
            if any(n in stype for n in needles):
                return {
                    "city":  ResponseParser._ci_get(s, "city"),
                    "state": ResponseParser._ci_get(s, "state"),
                    "appointment_from": ResponseParser._ci_get(s, "appointmentFrom", "scheduledFrom"),
                    "appointment_to":   ResponseParser._ci_get(s, "appointmentTo", "scheduledTo"),
                }
        return {}

    total_weight = 0.0
    total_pieces = 0
    commodities: list[str] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        w = ResponseParser._ci_get(it, "weight", "weightLbs")
        p = ResponseParser._ci_get(it, "pieces", "quantity")
        c = ResponseParser._ci_get(it, "commodity", "description")
        try:
            total_weight += float(w) if w is not None else 0.0
        except (TypeError, ValueError):
            pass
        try:
            total_pieces += int(p) if p is not None else 0
        except (TypeError, ValueError):
            pass
        if c:
            commodities.append(c)

    total_revenue = 0.0
    for r in revenues if isinstance(revenues, list) else []:
        if not isinstance(r, dict):
            continue
        amt = ResponseParser._ci_get(r, "amount", "total", "totalAmount")
        try:
            total_revenue += float(amt) if amt is not None else 0.0
        except (TypeError, ValueError):
            pass

    return {
        "bol_number":     pick("bolNumber", "bol"),
        "load_number":    pick("loadNumber", "loadId"),
        "equipment_type": pick("equipmentType", "trailerType"),
        "total_weight_lbs": total_weight or None,
        "total_pieces":   total_pieces or None,
        "total_revenue":  total_revenue or None,
        "commodity":      (commodities[0] if commodities else None),
        "pickup":         stop_summary("pickup", "origin"),
        "delivery":       stop_summary("delivery", "destination", "consignee"),
    }


# ---------------------------------------------------------------- per-shipment

def _build_services(config: Config):
    client = DitatClient(config.base_url, config.account_id, config.client_id, config.client_secret)
    return client, ShipmentService(client), DocumentService(client), KeyExtractor()


def _classify(file_name: str) -> str:
    """Quick filename-based hint. Agent re-classifies authoritatively from content."""
    n = (file_name or "").lower()
    if any(k in n for k in ("rate-con", "ratecon", "rate confirmation", "rate_con", "ratecnf")):
        return "RC"
    if any(k in n for k in ("bol", "bill of lading", "bill-of-lading")):
        return "BOL"
    if any(k in n for k in ("pod", "proof of delivery", "delivery-receipt", "delivery_receipt")):
        return "POD"
    return "UNKNOWN"


def _process_one(shipment_obj: dict, details: dict, config: Config,
                 document_svc: DocumentService, key_extractor: KeyExtractor,
                 download_workers: int = 3) -> dict | None:
    """Build the per-shipment slim record. Downloads docs in parallel within
    a shipment (default 3 workers per shipment)."""
    key = key_extractor.extract(shipment_obj, "shipment")
    if not key:
        return None
    docs = document_svc.list_documents(details)
    out_dir = config.download_dir / str(key)

    def _dl(d: dict) -> dict | None:
        path = document_svc.download(d, shipment_key=key, out_dir=out_dir)
        if not path:
            return None
        file_name = d.get("fileName") or d.get("FileName") or path.name
        return {
            "doc_key":   key_extractor.extract(d, "document"),
            "file_name": file_name,
            "file_type": d.get("fileType") or d.get("FileType"),
            "classification": _classify(file_name),
            "path":      str(path.resolve()),
        }

    doc_records: list[dict] = []
    if docs:
        workers = max(1, min(download_workers, len(docs)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for rec in ex.map(_dl, docs):
                if rec is not None:
                    doc_records.append(rec)

    return {
        "shipment_key": key,
        "shipment_id":  ResponseParser._ci_get(shipment_obj, "id", "shipmentId"),
        "ditat_fields": _slim_ditat(details),
        "documents":    doc_records,
    }


# ---------------------------------------------------------------- commands

def cmd_check_env(_args: argparse.Namespace) -> int:
    config = Config()
    ok, missing = config.validate()
    print(json.dumps({
        "ok": ok,
        "missing": missing,
        "base_url": config.base_url,
        "download_dir": str(config.download_dir.resolve()),
        "state_db": str(STATE_DB.resolve()),
        "batch_sidecar": str(BATCH_SIDECAR.resolve()),
    }, indent=2))
    return 0 if ok else 2


def cmd_fetch(args: argparse.Namespace) -> int:
    config = Config()
    ok, missing = config.validate()
    if not ok:
        print(json.dumps({"error": f"missing env vars: {missing}"}), file=sys.stderr)
        return 2

    _, shipment_svc, document_svc, key_extractor = _build_services(config)

    if args.last_week:
        args.since_days = 7
    elif args.last_month:
        args.since_days = 30
    since = None if args.all else datetime.now(timezone.utc) - timedelta(days=args.since_days)
    shipments = shipment_svc.list_shipments(since=since, filter_column=args.filter_column)

    conn = db_connect()
    processed_keys = {row[0] for row in conn.execute("SELECT shipment_key FROM processed_shipments")}
    conn.close()

    # Pre-filter shipments before paying detail/download cost
    pending: list[tuple[str, dict]] = []
    for s in shipments:
        if len(pending) >= args.limit:
            break
        k = key_extractor.extract(s, "shipment")
        if not k:
            continue
        if not args.include_processed and k in processed_keys:
            continue
        pending.append((k, s))

    log.info("Processing %d shipments with %d workers", len(pending), args.workers)

    def _one(item: tuple[str, dict]) -> dict | None:
        k, s = item
        try:
            details = shipment_svc.get_details(k)
        except Exception as e:
            log.error("detail fetch failed for %s: %s", k, e)
            return None
        return _process_one(s, details, config, document_svc, key_extractor,
                            download_workers=args.doc_workers)

    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for rec in ex.map(_one, pending):
            if rec is None:
                continue
            if args.require_docs and not rec["documents"]:
                continue
            out.append(rec)

    # Persist batch sidecar so finalize doesn't have to re-fetch
    BATCH_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    BATCH_SIDECAR.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(out),
        "shipments": out,
    }, default=str, indent=2), encoding="utf-8")

    # Slim stdout: agent only needs key, id, ditat_fields, document paths+classification
    print(json.dumps({
        "count": len(out),
        "batch_sidecar": str(BATCH_SIDECAR.resolve()),
        "shipments": [{
            "shipment_key": r["shipment_key"],
            "shipment_id":  r["shipment_id"],
            "ditat_fields": r["ditat_fields"],
            "documents": [{
                "classification": d["classification"],
                "file_name":      d["file_name"],
                "path":           d["path"],
            } for d in r["documents"]],
        } for r in out],
    }, default=str, indent=2))
    return 0


def cmd_verify_one(args: argparse.Namespace) -> int:
    config = Config()
    ok, missing = config.validate()
    if not ok:
        print(json.dumps({"error": f"missing env vars: {missing}"}), file=sys.stderr)
        return 2

    _, shipment_svc, document_svc, key_extractor = _build_services(config)
    try:
        details = shipment_svc.get_details(args.shipment_key)
    except Exception as e:
        print(json.dumps({"error": f"detail fetch failed: {e}"}), file=sys.stderr)
        return 1

    stub = {"Key": args.shipment_key}
    rec = _process_one(stub, details, config, document_svc, key_extractor)
    if not rec:
        print(json.dumps({"error": "could not extract shipment key"}), file=sys.stderr)
        return 1

    # Persist a 1-shipment batch sidecar so finalize works the same way
    BATCH_SIDECAR.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": 1,
        "shipments": [rec],
    }, default=str, indent=2), encoding="utf-8")

    print(json.dumps({
        "count": 1,
        "batch_sidecar": str(BATCH_SIDECAR.resolve()),
        "shipments": [{
            "shipment_key": rec["shipment_key"],
            "shipment_id":  rec["shipment_id"],
            "ditat_fields": rec["ditat_fields"],
            "documents": [{
                "classification": d["classification"],
                "file_name":      d["file_name"],
                "path":           d["path"],
            } for d in rec["documents"]],
        }],
    }, default=str, indent=2))
    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    """Consume agent findings + sidecar batch → diff + mark + docx."""
    findings_path = Path(args.findings_file)
    if not findings_path.is_absolute():
        findings_path = _state_root() / findings_path
    if not findings_path.exists():
        print(json.dumps({"error": f"findings file not found: {findings_path}"}), file=sys.stderr)
        return 1

    batch_path = Path(args.batch_file) if args.batch_file else BATCH_SIDECAR
    if not batch_path.is_absolute():
        batch_path = _state_root() / batch_path
    if not batch_path.exists():
        print(json.dumps({"error": f"batch sidecar not found: {batch_path} "
                                   f"(run `fetch` first)"}), file=sys.stderr)
        return 1

    findings_doc = json.loads(findings_path.read_text(encoding="utf-8"))
    batch_doc = json.loads(batch_path.read_text(encoding="utf-8"))
    batch_records: list[dict] = batch_doc.get("shipments") or []
    findings_list: list[dict] = findings_doc.get("shipments") or []

    findings_index: dict[str, dict] = {}
    for f in findings_list:
        key = str(f.get("shipment_key"))
        if not key:
            continue
        findings_index[key] = f.get("extracted") or {}
        findings_index[key]["docs_missing"] = f.get("docs_missing") or []

    diff_index: dict[str, dict] = {}
    for entry in batch_records:
        key = str(entry.get("shipment_key"))
        ditat = entry.get("ditat_fields") or {}
        extracted = findings_index.get(key) or {}
        diff_index[key] = diff_mod.run_diff(ditat, extracted)

    # Output path
    if args.output:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = _state_root() / out_path
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
        out_path = REPORTS_DIR / f"ditat-verify-{stamp}.docx"

    counters = build_batch_docx(batch_records, findings_index, diff_index, out_path)

    # Mark every shipment processed in one transaction
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = db_connect()
    try:
        with conn:
            for entry in batch_records:
                key = entry.get("shipment_key")
                if not key:
                    continue
                d = diff_index.get(str(key)) or {}
                conn.execute(
                    "INSERT OR REPLACE INTO processed_shipments "
                    "(shipment_key, shipment_id, processed_at, report_path, "
                    " critical_count, warn_count, verdict) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        entry.get("shipment_id"),
                        now_iso,
                        str(out_path),
                        d.get("critical_count", 0),
                        d.get("warn_count", 0),
                        d.get("verdict"),
                    ),
                )
    finally:
        conn.close()

    # Compact summary for agent: only problematic shipments listed individually
    problem_rows = []
    for entry in batch_records:
        key = entry.get("shipment_key")
        d = diff_index.get(str(key)) or {}
        if d.get("verdict") in {"ISSUES", "WARN", "RC MISSING"}:
            problem_rows.append({
                "shipment_key": key,
                "shipment_id":  entry.get("shipment_id"),
                "verdict":      d.get("verdict"),
                "critical":     d.get("critical_count", 0),
                "warn":         d.get("warn_count", 0),
            })

    print(json.dumps({
        "docx": str(out_path.resolve()),
        "processed": counters["shipments"],
        "problematic": counters["problematic"],
        "verdicts": counters["verdicts"],
        "problem_shipments": problem_rows,
    }, indent=2))

    # Clean up sidecar unless asked to keep
    if not args.keep_batch:
        try:
            batch_path.unlink()
        except OSError:
            pass

    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    conn = db_connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO processed_shipments "
            "(shipment_key, shipment_id, processed_at, report_path, "
            " critical_count, warn_count, verdict) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                args.shipment_key,
                args.shipment_id,
                datetime.now(timezone.utc).isoformat(),
                args.report_path,
                args.critical,
                args.warn,
                args.verdict,
            ),
        )
    conn.close()
    print(json.dumps({"marked": args.shipment_key}))
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    conn = db_connect()
    cur = conn.execute(
        "SELECT shipment_key, shipment_id, processed_at, verdict, "
        "critical_count, warn_count, report_path "
        "FROM processed_shipments ORDER BY processed_at DESC LIMIT 20"
    )
    rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
    total = conn.execute("SELECT COUNT(*) FROM processed_shipments").fetchone()[0]
    conn.close()
    print(json.dumps({"total_processed": total, "recent": rows}, indent=2))
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    conn = db_connect()
    with conn:
        conn.execute("DELETE FROM processed_shipments WHERE shipment_key=?", (args.shipment_key,))
    conn.close()
    print(json.dumps({"reset": args.shipment_key}))
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description="Ditat shipment verification helper")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("check-env", help="Validate .env / credentials without API calls")
    e.set_defaults(func=cmd_check_env)

    f = sub.add_parser("fetch", help="List unprocessed shipments + download docs (parallel)")
    f.add_argument("--limit", type=int, default=500,
                   help="Max shipments to process this run (default: 500)")
    f.add_argument("--since-days", type=int, default=30,
                   help="Window in days (default: 30 = last month)")
    f.add_argument("--last-week", action="store_true", help="Shortcut: --since-days 7")
    f.add_argument("--last-month", action="store_true", help="Shortcut: --since-days 30")
    f.add_argument("--all", action="store_true", help="Ignore since-days, list all shipments")
    f.add_argument("--filter-column", default="updatedOn")
    f.add_argument("--include-processed", action="store_true",
                   help="Include shipments already in state.db (re-verify)")
    f.add_argument("--require-docs", action="store_true", default=True,
                   help="Skip shipments with zero downloadable docs")
    f.add_argument("--workers", type=int, default=5,
                   help="Parallel workers across shipments (default: 5)")
    f.add_argument("--doc-workers", type=int, default=3,
                   help="Parallel doc downloads per shipment (default: 3)")
    f.set_defaults(func=cmd_fetch)

    v = sub.add_parser("verify-one", help="Fetch + download docs for one shipment")
    v.add_argument("shipment_key")
    v.set_defaults(func=cmd_verify_one)

    fz = sub.add_parser("finalize",
                        help="Diff + mark + render docx from agent findings + batch sidecar")
    fz.add_argument("--findings-file", required=True,
                    help="JSON written by agent (shipments[].extracted)")
    fz.add_argument("--batch-file", default=None,
                    help="Override batch sidecar path (default: .ditat_batch.json)")
    fz.add_argument("--output", default=None,
                    help="Output docx path (default: reports/ditat-verify-<stamp>.docx)")
    fz.add_argument("--keep-batch", action="store_true",
                    help="Keep .ditat_batch.json after finalize (default: delete)")
    fz.set_defaults(func=cmd_finalize)

    m = sub.add_parser("mark", help="Mark a single shipment processed (used by verify-one flow)")
    m.add_argument("shipment_key")
    m.add_argument("--shipment-id", default=None)
    m.add_argument("--report-path", default=None)
    m.add_argument("--critical", type=int, default=0)
    m.add_argument("--warn", type=int, default=0)
    m.add_argument("--verdict", default=None)
    m.set_defaults(func=cmd_mark)

    s = sub.add_parser("status", help="Show recent processed-shipment status")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("reset", help="Clear processed flag for a shipment")
    r.add_argument("shipment_key")
    r.set_defaults(func=cmd_reset)

    args = p.parse_args()
    setup_logging(args.verbose)
    REPORTS_DIR.mkdir(exist_ok=True, parents=True)
    try:
        return args.func(args)
    except ImportError as e:
        print(json.dumps({
            "error": f"missing dependency: {e}. Run: pip install -r scripts/requirements.txt"
        }), file=sys.stderr)
        return 2
    except Exception as e:
        log.exception("helper failed")
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
