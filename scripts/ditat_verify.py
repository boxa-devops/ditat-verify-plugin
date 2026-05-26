#!/usr/bin/env python3
"""Ditat shipment verification — CLI entrypoint.

Sub-commands:
  check-env  — validate .env / credentials without making API calls.
  fetch      — list unprocessed shipments, download docs in parallel, emit
               slim JSON, persist `.ditat_batch.json` sidecar for `finalize`.
  verify-one — same shape as `fetch` but for a single shipment_key.
  finalize   — consume agent findings + sidecar → diff + mark + ONE batch
               .docx. Single transaction.
  mark       — mark a single shipment processed (used by verify-one retry).
  status     — show recent processed-shipment rows.
  reset      — clear processed flag for a shipment.

Stdout is JSON, stderr is logs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ditat import db as state_db
from ditat import diff as diff_mod
from ditat.api import (
    DitatApiError,
    DocumentService,
    ShipmentService,
    ci_get,
    document_key,
    shipment_key,
)
from ditat.client import DitatClient
from ditat.config import Config
from ditat.ditat_record import slim_ditat
from ditat.docx_report import build_batch_docx
from ditat.logging_utils import setup_logging

log = logging.getLogger("ditat")


# ---------------------------------------------------------------- paths

def _state_root() -> Path:
    return Path(
        os.getenv("DITAT_STATE_DIR")
        or os.getenv("CLAUDE_PROJECT_DIR")
        or Path.cwd()
    ).resolve()


STATE_DB = _state_root() / os.getenv("DITAT_STATE_DB_NAME", "state.db")
REPORTS_DIR = Path(os.getenv("DITAT_REPORTS_DIR") or (_state_root() / "reports"))
BATCH_SIDECAR = _state_root() / ".ditat_batch.json"

MAX_FINDINGS_BYTES = 50 * 1024 * 1024  # 50 MB defensive cap


# ---------------------------------------------------------------- helpers

def _build_services(config: Config):
    client = DitatClient(config.base_url, config.account_id, config.client_id, config.client_secret)
    return client, ShipmentService(client), DocumentService(client)


def _classify(file_name: str) -> str:
    n = (file_name or "").lower()
    # Order matters: POD's substrings can collide with BOL; check POD first.
    if any(k in n for k in ("rate-con", "ratecon", "rate confirmation", "rate_con", "ratecnf")):
        return "RC"
    if any(k in n for k in ("pod", "proof of delivery", "delivery-receipt", "delivery_receipt")):
        return "POD"
    if any(k in n for k in ("bol", "bill of lading", "bill-of-lading", "bill_of_lading")):
        return "BOL"
    return "UNKNOWN"


def _process_one(shipment_obj: dict, details: dict, config: Config,
                 document_svc: DocumentService, download_workers: int = 3) -> dict | None:
    key = shipment_key(shipment_obj)
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
            "doc_key":   document_key(d),
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
        "shipment_id":  ci_get(shipment_obj, "id", "shipmentId"),
        "ditat_fields": slim_ditat(details),
        "documents":    doc_records,
    }


def _slim_for_stdout(rec: dict) -> dict:
    return {
        "shipment_key": rec["shipment_key"],
        "shipment_id":  rec["shipment_id"],
        "ditat_fields": rec["ditat_fields"],
        "documents": [{
            "classification": d["classification"],
            "file_name":      d["file_name"],
            "path":           d["path"],
        } for d in rec["documents"]],
    }


def _write_sidecar(records: list[dict]) -> None:
    BATCH_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    BATCH_SIDECAR.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "shipments": records,
    }, default=str, indent=2), encoding="utf-8")


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

    _, shipment_svc, document_svc = _build_services(config)

    if args.last_week:
        args.since_days = 7
    elif args.last_month:
        args.since_days = 30
    since = None if args.all else datetime.now(timezone.utc) - timedelta(days=args.since_days)
    shipments = shipment_svc.list_shipments(
        since=since,
        filter_column=args.filter_column,
        page_size=args.page_size,
    )

    processed = state_db.processed_keys(STATE_DB)

    pending: list[tuple[str, dict]] = []
    for s in shipments:
        if len(pending) >= args.limit:
            break
        k = shipment_key(s)
        if not k:
            continue
        if not args.include_processed and k in processed:
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
        return _process_one(s, details, config, document_svc, download_workers=args.doc_workers)

    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        for rec in ex.map(_one, pending):
            if rec is None:
                continue
            if args.require_docs and not rec["documents"]:
                continue
            out.append(rec)

    _write_sidecar(out)

    print(json.dumps({
        "count": len(out),
        "batch_sidecar": str(BATCH_SIDECAR.resolve()),
        "shipments": [_slim_for_stdout(r) for r in out],
    }, default=str, indent=2))
    return 0


def cmd_verify_one(args: argparse.Namespace) -> int:
    config = Config()
    ok, missing = config.validate()
    if not ok:
        print(json.dumps({"error": f"missing env vars: {missing}"}), file=sys.stderr)
        return 2

    if not args.shipment_key.isdigit():
        log.warning("verify-one key '%s' is non-numeric — Ditat keys are usually numeric strings.",
                    args.shipment_key)

    _, shipment_svc, document_svc = _build_services(config)
    try:
        details = shipment_svc.get_details(args.shipment_key)
    except DitatApiError as e:
        print(json.dumps({"error": f"detail fetch failed: {e}"}), file=sys.stderr)
        return 1

    stub = {"Key": args.shipment_key}
    rec = _process_one(stub, details, config, document_svc)
    if not rec:
        print(json.dumps({"error": "could not extract shipment key"}), file=sys.stderr)
        return 1

    _write_sidecar([rec])
    print(json.dumps({
        "count": 1,
        "batch_sidecar": str(BATCH_SIDECAR.resolve()),
        "shipments": [_slim_for_stdout(rec)],
    }, default=str, indent=2))
    return 0


def cmd_finalize(args: argparse.Namespace) -> int:
    findings_path = Path(args.findings_file)
    if not findings_path.is_absolute():
        findings_path = _state_root() / findings_path
    if not findings_path.exists():
        print(json.dumps({"error": f"findings file not found: {findings_path}"}), file=sys.stderr)
        return 1
    if findings_path.stat().st_size > MAX_FINDINGS_BYTES:
        print(json.dumps({
            "error": f"findings file too large ({findings_path.stat().st_size} bytes, "
                     f"cap {MAX_FINDINGS_BYTES})"
        }), file=sys.stderr)
        return 1

    batch_path = Path(args.batch_file) if args.batch_file else BATCH_SIDECAR
    if not batch_path.is_absolute():
        batch_path = _state_root() / batch_path
    if not batch_path.exists():
        print(json.dumps({
            "error": f"batch sidecar not found: {batch_path} (run `fetch` or `verify-one` first)"
        }), file=sys.stderr)
        return 1

    try:
        findings_doc = json.loads(findings_path.read_text(encoding="utf-8"))
        batch_doc = json.loads(batch_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(json.dumps({
            "error": f"invalid JSON in input: {e}",
            "hint":  "findings.json must match the schema in SKILL.md Step 3.",
        }), file=sys.stderr)
        return 1

    batch_records: list[dict] = batch_doc.get("shipments") or []
    findings_list: list[dict] = findings_doc.get("shipments") or []

    findings_index: dict[str, dict] = {}
    for f in findings_list:
        key = str(f.get("shipment_key") or "")
        if not key:
            continue
        extracted = f.get("extracted") or {}
        extracted["docs_missing"] = f.get("docs_missing") or []
        findings_index[key] = extracted

    diff_index: dict[str, dict] = {}
    for entry in batch_records:
        key = str(entry.get("shipment_key") or "")
        ditat = entry.get("ditat_fields") or {}
        extracted = findings_index.get(key) or {}
        diff_index[key] = diff_mod.run_diff(ditat, extracted)

    if args.output:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = _state_root() / out_path
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
        out_path = REPORTS_DIR / f"ditat-verify-{stamp}.docx"

    counters = build_batch_docx(batch_records, findings_index, diff_index, out_path)

    rows = []
    for entry in batch_records:
        key = entry.get("shipment_key")
        d = diff_index.get(str(key)) or {}
        rows.append({
            "shipment_key": key,
            "shipment_id":  entry.get("shipment_id"),
            "report_path":  str(out_path),
            "critical":     d.get("critical_count", 0),
            "warn":         d.get("warn_count", 0),
            "verdict":      d.get("verdict"),
        })
    state_db.mark_batch(STATE_DB, rows)

    problem_rows = [{
        "shipment_key": r["shipment_key"],
        "shipment_id":  r["shipment_id"],
        "verdict":      r["verdict"],
        "critical":     r["critical"],
        "warn":         r["warn"],
    } for r in rows if r["verdict"] in {"ISSUES", "WARN", "RC MISSING"}]

    print(json.dumps({
        "docx": str(out_path.resolve()),
        "processed": counters["shipments"],
        "problematic": counters["problematic"],
        "verdicts": counters["verdicts"],
        "problem_shipments": problem_rows,
    }, indent=2))

    if args.cleanup:
        try:
            batch_path.unlink()
        except OSError:
            pass
        try:
            findings_path.unlink()
        except OSError:
            pass

    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    state_db.mark_one(
        STATE_DB,
        shipment_key=args.shipment_key,
        shipment_id=args.shipment_id,
        report_path=args.report_path,
        critical=args.critical,
        warn=args.warn,
        verdict=args.verdict,
    )
    print(json.dumps({"marked": args.shipment_key}))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(state_db.recent_status(STATE_DB, limit=args.limit), indent=2))
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    state_db.reset(STATE_DB, args.shipment_key)
    print(json.dumps({"reset": args.shipment_key}))
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description="Ditat shipment verification helper")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("check-env", help="Validate .env / credentials")
    e.set_defaults(func=cmd_check_env)

    f = sub.add_parser("fetch", help="List + download unprocessed shipments (parallel)")
    f.add_argument("--limit", type=int, default=500)
    f.add_argument("--since-days", type=int, default=30)
    f.add_argument("--last-week", action="store_true")
    f.add_argument("--last-month", action="store_true")
    f.add_argument("--all", action="store_true")
    f.add_argument("--filter-column", default="updatedOn")
    f.add_argument("--include-processed", action="store_true")
    f.add_argument("--require-docs", action="store_true", default=True)
    f.add_argument("--workers", type=int, default=5)
    f.add_argument("--doc-workers", type=int, default=3)
    f.add_argument("--page-size", type=int, default=1000,
                   help="Server-side page size for list_shipments (default: 1000)")
    f.set_defaults(func=cmd_fetch)

    v = sub.add_parser("verify-one", help="Fetch + download docs for one shipment")
    v.add_argument("shipment_key")
    v.set_defaults(func=cmd_verify_one)

    fz = sub.add_parser("finalize",
                        help="Diff + mark + render docx from agent findings + batch sidecar")
    fz.add_argument("--findings-file", required=True)
    fz.add_argument("--batch-file", default=None)
    fz.add_argument("--output", default=None)
    fz.add_argument("--cleanup", action="store_true",
                    help="Delete sidecar + findings files after success (default: keep)")
    fz.set_defaults(func=cmd_finalize)

    m = sub.add_parser("mark", help="Mark one shipment processed (verify-one path)")
    m.add_argument("shipment_key")
    m.add_argument("--shipment-id", default=None)
    m.add_argument("--report-path", default=None)
    m.add_argument("--critical", type=int, default=0)
    m.add_argument("--warn", type=int, default=0)
    m.add_argument("--verdict", default=None)
    m.set_defaults(func=cmd_mark)

    s = sub.add_parser("status", help="Show recent processed-shipment status")
    s.add_argument("--limit", type=int, default=20)
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
