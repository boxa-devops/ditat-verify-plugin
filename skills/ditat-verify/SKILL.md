---
name: ditat-verify
description: Pull unprocessed shipments from Ditat TMS, download their documents (BOL/POD/Rate Confirmation), and cross-check BOL+POD+Ditat shipment fields against the Rate Confirmation. Produces a per-shipment markdown report and marks the shipment processed. Trigger when user says "verify ditat shipments", "check ditat", "run ditat verification", "/ditat-verify", or asks to reconcile shipment documents against rate cons.
---

# Ditat Shipment Verification

Goal: for every shipment not yet processed in `state.db`, read its BOL, POD, and Rate Confirmation PDFs, then flag any field that disagrees between (a) BOL ↔ Rate Con, (b) POD ↔ Rate Con, and (c) Ditat shipment record ↔ Rate Con. Write one per-shipment markdown report (audit trail), mark shipment processed in `state.db`, then bundle the batch into a single Word doc the user can review or forward.

## Paths

- **Helper script** lives inside the plugin: `${CLAUDE_PLUGIN_ROOT}/scripts/ditat_verify.py`. Always invoke with the full `${CLAUDE_PLUGIN_ROOT}` path so it works no matter where the user's project is.
- **State (`state.db`, `reports/`, `downloads/`, token cache, `.env`)** lives in the user's project: `${CLAUDE_PROJECT_DIR}`. The script auto-resolves these from `$CLAUDE_PROJECT_DIR` so plugin updates never wipe operational data.
- **`.env`** must exist at `${CLAUDE_PROJECT_DIR}/.env` with `DITAT_BASE_URL`, `DITAT_ACCOUNT_ID`, `DITAT_CLIENT_ID`, `DITAT_CLIENT_SECRET`. See `${CLAUDE_PLUGIN_ROOT}/.env.example` for the template.

Run commands from the user's current shell — `cwd` is `${CLAUDE_PROJECT_DIR}` so relative downloads (`./downloads`) land in the right place.

## Flow

### 0. Preflight (first run of session only)

PowerShell:
```
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" check-env
```
Bash:
```
python "$CLAUDE_PLUGIN_ROOT/scripts/ditat_verify.py" check-env
```

If `ok: false`, tell user to copy `${CLAUDE_PLUGIN_ROOT}/.env.example` to `${CLAUDE_PROJECT_DIR}/.env` and fill credentials. Skip on repeat invocations in same session.

### 1. Fetch unprocessed shipments
```
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" fetch --limit 5
```
Optional flags: `--since-days N` (default 30), `--all` (no date filter), `--include-processed` (re-verify).

Helper downloads docs to `${CLAUDE_PROJECT_DIR}/downloads/<shipment_key>/` and prints JSON on stdout:
```json
{
  "count": N,
  "shipments": [
    {
      "shipment_key": "9536",
      "shipment_id": "SH-0000009584",
      "ditat_fields": { "bol_number": ..., "load_number": ..., "total_weight_lbs": ..., "pickup": {...}, "delivery": {...}, ... },
      "documents": [
        { "doc_key": "...", "file_name": "...", "file_type": "...", "path": "<absolute path>" }
      ]
    }
  ]
}
```

If `count == 0`: tell user "no unprocessed shipments" and stop.

### 2. Per shipment — read + classify docs

For each shipment in the JSON, read each `documents[].path` with the Read tool. Classify each into:

- **RC** (Rate Confirmation) — carrier/agreed rate, load number, equipment, pickup/delivery scheduled dates
- **BOL** (Bill of Lading) — bol number, shipper/consignee, weight, pieces, commodity, PO numbers
- **POD** (Proof of Delivery) — signed-by, delivery date/time, pieces received, weight received, damages
- **OTHER** — invoice, photos, accessorial — log type only, no field diff

Classify by document content. Filename hints help but are not authoritative.

Extract these fields from each:

| RC                   | BOL                  | POD                  |
|----------------------|----------------------|----------------------|
| load_number          | bol_number           | bol_number           |
| agreed_rate          | shipper (name/city/state) | delivery_date    |
| pickup_date          | consignee (name/city/state) | delivery_time |
| delivery_date        | pickup_date          | signed_by            |
| equipment_type       | delivery_date        | pieces_received      |
| pickup_location      | weight_lbs           | weight_received_lbs  |
| delivery_location    | pieces               | damages_notes        |
| commodity            | commodity            |                      |
|                      | po_numbers           |                      |
|                      | hazmat               |                      |

If a shipment has **no RC**, cannot diff against rate confirmation — still produce a report stating "RC missing; can only do BOL↔POD↔Ditat cross-check." If only one doc, note it and skip cross-checks that need the other.

### 3. Diff & severity

RC is contractual source of truth; Ditat is system-of-record snapshot.

**Tolerances**
- Weight: critical if delta > 5%, warn if 1–5%, info if <1%
- Dates: critical if delta > 1 day, warn if delta > 0 but ≤ 1 day
- Money: critical if delta > $1.00 or > 1%
- Strings: normalize (lowercase, strip, collapse whitespace) before compare; warn on mismatch
- Missing on one side: warn

**Cross-checks per shipment**
1. **BOL ↔ RC**: bol_number presence, pickup_date, delivery_date, weight, pieces, commodity, locations (pickup city/state, delivery city/state)
2. **POD ↔ RC**: delivery_date (POD actual vs RC scheduled), weight_received vs RC/BOL weight, pieces_received vs BOL pieces, bol_number match
3. **Ditat ↔ RC**: bol_number, load_number, total_weight_lbs, total_pieces, equipment_type, pickup city/state, delivery city/state, agreed_rate vs total_revenue
4. **BOL ↔ POD**: bol_number match, weight delivered vs weight shipped, pieces delivered vs pieces shipped

### 4. Write report

Write to `${CLAUDE_PROJECT_DIR}/reports/<shipment_key>.md`:

```markdown
# Shipment <shipment_id> (key=<shipment_key>) — Verification Report
Generated: <UTC timestamp>

## Documents
- RC:  <filename> (doc_key=...)
- BOL: <filename> (doc_key=...)
- POD: <filename> (doc_key=...)
- OTHER: <filename> — <type>

## Summary
- Critical: N
- Warnings: N
- Info:     N

## Findings

### CRITICAL
- **[BOL↔RC] weight_lbs**: BOL=42000, RC=24000 → delta 75% > 5% tolerance.
- **[POD↔RC] delivery_date**: POD=2026-05-10, RC=2026-05-08 → 2-day gap.

### WARN
- **[Ditat↔RC] equipment_type**: Ditat="Reefer", RC="Refrigerated Van" — likely same, normalize.
- **[BOL↔RC] po_numbers**: BOL has PO 12345 not on RC.

### INFO
- **[BOL↔POD] pieces**: 24 vs 24 — match.

## Extracted fields
<one block per doc with all extracted fields, for audit>

## Ditat shipment snapshot
<the ditat_fields object from helper, pretty-printed>
```

### 5. Mark processed
```
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" mark <shipment_key> --shipment-id "<id>" --report-path "reports\<shipment_key>.md" --critical <N> --warn <N>
```
Mark only after report exists. If docs were missing and diffs skipped, still mark with `--critical 0 --warn 0` so the shipment isn't reprocessed every run.

### 6. Bundle batch into one Word doc

After the batch finishes (all marks done), package the freshly written reports into a single `.docx`:

PowerShell:
```
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" build-docx --keys <key1,key2,...>
```

Use `--keys` with the comma-separated shipment_keys you just processed so the doc matches *this* batch. Other flags:
- `--since-days N` → include every report processed in the last N days (for retro bundling, e.g. weekly digest)
- `--output <path>` → custom output path; default is `${CLAUDE_PROJECT_DIR}/reports/ditat-verify-<YYYY-MM-DD>.docx`

Helper prints JSON `{ "docx": "<absolute path>", "shipments": N, "sources": [...] }`.

### 7. Roll up to user

Print a short table plus the docx path:

```
shipment_id        critical  warn   report
SH-0000009584      2         3      reports\9536.md
SH-0000009585      0         1      reports\9537.md

→ Bundle: reports\ditat-verify-2026-05-13.docx
```

## Sub-commands user may invoke directly

- `verify next N shipments` → `fetch --limit N`, full flow
- `verify shipment <KEY>` / `re-verify shipment <KEY>` → `verify-one <KEY>`, full flow. `mark` is INSERT-OR-REPLACE, no need for `reset`.
- `ditat verify status` → `status` subcommand
- `ditat env check` → `check-env`
- `bundle last N days` / `weekly digest` → `build-docx --since-days N` (no API calls, just packages existing reports)

## Operational notes

- **Token budget**: Ditat enforces 12 token-fetches/hour. Helper reuses cached token at `${CLAUDE_PROJECT_DIR}/.ditat_token_*.json`. Don't loop `fetch` rapidly.
- **Permission gap**: Ditat user may lack `documents` View role → docs list empty even when shipment has files. Helper logs warning; report says "no documents fetched (role may be missing)".
- **Large PDF**: read first ~10 pages; BOL/POD/RC fields almost always sit on page 1.
- **State location**: `state.db` lives in `${CLAUDE_PROJECT_DIR}`. Run `status` to inspect. Run `reset <key>` to force re-verification.

## What this skill does NOT do

- Write back to Ditat (project is read-only by design).
- Send Slack/email alerts.
- OCR image-only TIFFs (Read may fail; log "needs OCR" finding and move on).
