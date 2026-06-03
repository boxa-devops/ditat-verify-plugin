#!/usr/bin/env python3
"""Ditat shipment verification — plugin-side CLI entrypoint.

The credentialed half (talking to Ditat, downloading PDFs) now lives in the
cloud server (see server/). This CLI drives the LOCAL half:

  check-server     — validate server config + ping /health.
  pull             — POST the server's /batch, download every doc to disk, emit
                     slim JSON + persist `.ditat_batch.json` sidecar AND
                     pre-build `.ditat_findings.json` skeleton for `finalize`.
  append-findings  — merge a chunk's extracted records into
                     `.ditat_findings.json` atomically.
  finalize         — consume agent findings + sidecar → diff (rules.yaml) → ONE
                     batch .docx.

No state DB — every run verifies the full window the server returns.
Stdout is JSON, stderr is logs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from ditat import diff as diff_mod
from ditat import rules as rules_mod
from ditat.docx_report import build_batch_docx
from ditat.logging_utils import setup_logging
from ditat.remote import ServerConfig, download_docs, fetch_batch

log = logging.getLogger("ditat")


# ---------------------------------------------------------------- paths

def _state_root() -> Path:
    return Path(
        os.getenv("DITAT_STATE_DIR")
        or os.getenv("CLAUDE_PROJECT_DIR")
        or Path.cwd()
    ).resolve()


REPORTS_DIR = Path(os.getenv("DITAT_REPORTS_DIR") or (_state_root() / "reports"))
DOWNLOAD_DIR = Path(os.getenv("DITAT_DOWNLOAD_DIR") or (_state_root() / "downloads"))
BATCH_SIDECAR = _state_root() / ".ditat_batch.json"
FINDINGS_FILE = _state_root() / ".ditat_findings.json"

MAX_FINDINGS_BYTES = 50 * 1024 * 1024  # 50 MB defensive cap


# ---------------------------------------------------------------- sidecar + findings

def _findings_skeleton(batch_records: list[dict]) -> dict:
    """Build the initial findings file from downloaded batch records.

    Each shipment gets an empty `extracted` dict (the agent fills it after
    reading PDFs) and the server-computed `docs_missing` list.
    """
    shipments = [{
        "shipment_key": r.get("shipment_key"),
        "shipment_id":  r.get("shipment_id"),
        "extracted":    {},
        "docs_missing": r.get("docs_missing") or [],
    } for r in batch_records]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shipments": shipments,
    }


def _write_findings(doc: dict) -> None:
    FINDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    FINDINGS_FILE.write_text(json.dumps(doc, default=str, indent=2), encoding="utf-8")


def _cleanup_intermediates(batch_path: Path, findings_path: Path) -> list[str]:
    """Delete every per-run intermediate, leaving only the reports/ folder.

    Removes the batch sidecar, findings file, the downloads/ tree, and any agent
    chunk files. The .env (config) and reports/ (deliverables) are never touched.
    """
    removed: list[str] = []
    for p in (batch_path, findings_path):
        try:
            if p.exists():
                p.unlink()
                removed.append(str(p))
        except OSError:
            pass
    if DOWNLOAD_DIR.exists():
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        removed.append(str(DOWNLOAD_DIR))
    root = _state_root()
    for pattern in (".ditat_chunk_*.json", "chunk*.json"):
        for cp in root.glob(pattern):
            try:
                cp.unlink()
                removed.append(str(cp))
            except OSError:
                pass
    if removed:
        log.info("Cleaned %d intermediate(s): %s", len(removed), ", ".join(removed))
    return removed


def _write_sidecar(records: list[dict]) -> None:
    BATCH_SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    BATCH_SIDECAR.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "shipments": records,
    }, default=str, indent=2), encoding="utf-8")


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


# ---------------------------------------------------------------- commands

def _plugin_root() -> Path:
    return Path(os.getenv("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)


def _env_template() -> str:
    example = _plugin_root() / ".env.example"
    if example.is_file():
        return example.read_text(encoding="utf-8")
    return ("DITAT_SERVER_URL=\n"
            "DITAT_SERVER_API_KEY=\n")


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a project directory + .env for onboarding a new client."""
    target = Path(args.path).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    (target / "reports").mkdir(exist_ok=True)

    env_path = target / ".env"
    created = False
    if not env_path.exists():
        env_path.write_text(_env_template(), encoding="utf-8")
        created = True

    # Which required vars are still blank/placeholder?
    text = env_path.read_text(encoding="utf-8")
    needs = []
    for var in ("DITAT_SERVER_URL", "DITAT_SERVER_API_KEY"):
        val = ""
        for line in text.splitlines():
            if line.strip().startswith(f"{var}="):
                val = line.split("=", 1)[1].strip()
                break
        if not val or val.startswith(("your_", "https://your-")):
            needs.append(var)

    print(json.dumps({
        "project_dir": str(target),
        "env_path": str(env_path),
        "env_created": created,
        "needs_filling": needs,
        "next": "Fill the needs_filling vars in .env, then run check-server.",
    }, indent=2))
    return 0


def cmd_check_server(_args: argparse.Namespace) -> int:
    config = ServerConfig()
    ok, missing = config.validate()
    result = {
        "ok": ok,
        "missing": missing,
        "server_url": config.base_url,
        "auth": "api-key" if config.api_key else "none",
    }
    if ok:
        import requests
        try:
            r = requests.get(f"{config.base_url}/health", headers=config._headers(), timeout=30)
            result["health_status"] = r.status_code
            result["health"] = r.json() if "json" in r.headers.get("Content-Type", "") else r.text[:200]
        except Exception as e:  # noqa: BLE001
            result["ok"] = False
            result["health_error"] = str(e)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 2


def cmd_pull(args: argparse.Namespace) -> int:
    config = ServerConfig()
    ok, missing = config.validate()
    if not ok:
        print(json.dumps({"error": f"missing server config: {missing}"}), file=sys.stderr)
        return 2

    since_days = 30 if args.last_month else args.since_days
    manifest = fetch_batch(
        config,
        since_days=since_days,
        last_week=args.last_week,
        all_time=args.all,
        filter_column=args.filter_column,
        limit=args.limit,
        page_size=args.page_size,
        require_docs=args.require_docs,
    )

    log.info("Manifest: %d shipments — downloading docs", manifest.get("count", 0))
    records = download_docs(config, manifest, DOWNLOAD_DIR)

    _write_sidecar(records)
    _write_findings(_findings_skeleton(records))

    print(json.dumps({
        "count": len(records),
        "batch_sidecar": str(BATCH_SIDECAR.resolve()),
        "findings_file": str(FINDINGS_FILE.resolve()),
        "shipments": [_slim_for_stdout(r) for r in records],
    }, default=str, indent=2))
    return 0


def cmd_append_findings(args: argparse.Namespace) -> int:
    """Merge a chunk's extracted records into `.ditat_findings.json`.

    Input JSON shape (accepts either form):
      A) [ {"shipment_key": "...", "extracted": {...}, "docs_missing": [...]}, ... ]
      B) {"shipments": [ ...same as A... ]}

    Atomic merge: last-write-wins per shipment_key, missing keys preserved.
    """
    src = Path(args.input)
    if not src.is_absolute():
        src = _state_root() / src
    if not src.exists():
        print(json.dumps({"error": f"chunk file not found: {src}"}), file=sys.stderr)
        return 1
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON: {e}"}), file=sys.stderr)
        return 1

    chunk = payload.get("shipments") if isinstance(payload, dict) else payload
    if not isinstance(chunk, list):
        print(json.dumps({"error": "chunk must be a list of shipment records"}), file=sys.stderr)
        return 1

    if not FINDINGS_FILE.exists():
        print(json.dumps({
            "error": f"findings file not found: {FINDINGS_FILE} (run `pull` first)"
        }), file=sys.stderr)
        return 1

    findings = json.loads(FINDINGS_FILE.read_text(encoding="utf-8"))
    by_key = {str(s.get("shipment_key")): s for s in findings.get("shipments", [])}

    merged, skipped = 0, []
    for rec in chunk:
        key = str(rec.get("shipment_key") or "")
        if not key or key not in by_key:
            skipped.append(key)
            continue
        if "extracted" in rec:
            by_key[key]["extracted"] = rec["extracted"]
        if "docs_missing" in rec:
            by_key[key]["docs_missing"] = rec["docs_missing"]
        merged += 1

    _write_findings(findings)
    print(json.dumps({
        "merged": merged,
        "skipped_keys": skipped,
        "findings_file": str(FINDINGS_FILE.resolve()),
    }, indent=2))
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
            "error": f"batch sidecar not found: {batch_path} (run `pull` first)"
        }), file=sys.stderr)
        return 1

    try:
        findings_doc = json.loads(findings_path.read_text(encoding="utf-8"))
        batch_doc = json.loads(batch_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(json.dumps({
            "error": f"invalid JSON in input: {e}",
            "hint":  "findings.json must match the schema in SKILL.md.",
        }), file=sys.stderr)
        return 1

    rules = rules_mod.load_rules(args.rules_file)
    as_of = datetime.now(timezone.utc).date()

    all_records: list[dict] = batch_doc.get("shipments") or []
    findings_list: list[dict] = findings_doc.get("shipments") or []

    # We verify DELIVERED, non-skipped shipments only. Drop future-delivery loads
    # (not done yet) and skip-list customers (e.g. Amazon — not our process).
    batch_records: list[dict] = []
    skipped_pending = 0
    skipped_customer = 0
    for entry in all_records:
        ditat = entry.get("ditat_fields") or {}
        if diff_mod.is_skipped_customer(ditat, rules):
            skipped_customer += 1
            continue
        if diff_mod.is_pending(ditat, as_of):
            skipped_pending += 1
            continue
        batch_records.append(entry)

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
        diff_index[key] = diff_mod.run_diff(ditat, extracted, rules, as_of)

    if args.output:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = _state_root() / out_path
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
        out_path = REPORTS_DIR / f"ditat-verify-{stamp}.docx"

    counters = build_batch_docx(
        batch_records, findings_index, diff_index, out_path,
        anomalies_only=getattr(args, "anomalies_only", True),
    )

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

    cleaned = [] if args.keep_intermediates else _cleanup_intermediates(batch_path, findings_path)

    print(json.dumps({
        "docx": str(out_path.resolve()),
        "processed": counters["shipments"],
        "skipped_pending": skipped_pending,
        "skipped_customer": skipped_customer,
        "problematic": counters["problematic"],
        "verdicts": counters["verdicts"],
        "problem_shipments": problem_rows,
        "cleaned": len(cleaned),
    }, indent=2))

    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description="Ditat shipment verification helper (plugin side)")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    ini = sub.add_parser("init", help="Scaffold a project dir + .env (onboarding)")
    ini.add_argument("path", help="Full path to the project directory to create")
    ini.set_defaults(func=cmd_init)

    c = sub.add_parser("check-server", help="Validate server config + ping /health")
    c.set_defaults(func=cmd_check_server)

    pl = sub.add_parser("pull", help="Fetch manifest from server + download docs")
    pl.add_argument("--limit", type=int, default=500)
    pl.add_argument("--since-days", type=int, default=30)
    pl.add_argument("--last-week", action="store_true")
    pl.add_argument("--last-month", action="store_true")
    pl.add_argument("--all", action="store_true")
    pl.add_argument("--filter-column", default=None)
    pl.add_argument("--require-docs", action="store_true", default=True)
    pl.add_argument("--page-size", type=int, default=1000)
    pl.set_defaults(func=cmd_pull)

    fz = sub.add_parser("finalize",
                        help="Diff + render docx from agent findings + batch sidecar")
    fz.add_argument("--findings-file", default=str(FINDINGS_FILE))
    fz.add_argument("--batch-file", default=None)
    fz.add_argument("--rules-file", default=None,
                    help="Path to rules.yaml. Default: auto-discover (scripts/rules.yaml).")
    fz.add_argument("--output", default=None)
    fz.add_argument("--keep-intermediates", action="store_true",
                    help="Keep downloads/, sidecar, findings, and chunk files. "
                         "Default: delete them after success, leaving only reports/.")
    fz.add_argument("--anomalies-only", action="store_true", default=True,
                    help="Docx contains only problematic shipments + counts header. Default: on.")
    fz.add_argument("--full-report", dest="anomalies_only", action="store_false",
                    help="Include full summary table of every shipment in docx.")
    fz.set_defaults(func=cmd_finalize)

    af = sub.add_parser("append-findings", help="Merge chunk records into .ditat_findings.json")
    af.add_argument("input", help="Path to chunk JSON file (list or {shipments:[...]})")
    af.set_defaults(func=cmd_append_findings)

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
    except Exception as e:  # noqa: BLE001
        log.exception("helper failed")
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
