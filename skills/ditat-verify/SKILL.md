---
name: ditat-verify
description: Pull unprocessed shipments from Ditat TMS, download their documents (BOL/POD/Rate Confirmation), and cross-check BOL+POD+Ditat shipment fields against the Rate Confirmation. Produces ONE anomalies-only Word doc (.docx) of problematic shipments and marks every processed shipment in state.db. Trigger when user says "verify ditat shipments", "check ditat", "run ditat verification", "/ditat-verify", or asks to reconcile shipment documents against rate cons.
---

# Ditat Shipment Verification

## What it does

For every shipment in a time window that has not been processed yet:
1. Downloads its PDFs (BOL / POD / Rate Confirmation).
2. Extracts key fields from each PDF (RC is the source of truth).
3. Cross-checks BOL, POD, and the Ditat shipment record against the RC.
4. Writes **one anomalies-only Word doc** at `${CLAUDE_PROJECT_DIR}/reports/ditat-verify-<stamp>.docx` containing:
   - counts header (OK / WARN / ISSUES / RC MISSING),
   - detail section for **problematic shipments only** — clean shipments are omitted.
5. Marks every shipment processed in `state.db` so it won't re-run.

The user only cares about the .docx path. No per-shipment markdown files.

## Triggers

- `verify ditat shipments`, `check ditat`, `run ditat verification`, `/ditat-verify`
- `verify last week` / `verify last month` / `verify last N days`
- `verify shipment <KEY>` / `re-verify shipment <KEY>` (single-shipment retry)
- `ditat env check` / `ditat verify status`
- "reconcile shipment docs against rate cons" or semantic equivalent

Inputs: time window (defaults to last month). Credentials from `${CLAUDE_PROJECT_DIR}/.env`.

## Paths

- **Helper script:** `${CLAUDE_PLUGIN_ROOT}/scripts/ditat_verify.py` — always invoke with the full `${CLAUDE_PLUGIN_ROOT}` path.
- **State** (`state.db`, `reports/`, `downloads/`, token cache, `.env`, `.ditat_batch.json`, `.ditat_findings.json`) lives in `${CLAUDE_PROJECT_DIR}`.
- **`.env`** at `${CLAUDE_PROJECT_DIR}/.env` with `DITAT_BASE_URL`, `DITAT_ACCOUNT_ID`, `DITAT_CLIENT_ID`, `DITAT_CLIENT_SECRET`. Template at `${CLAUDE_PLUGIN_ROOT}/.env.example`.

Run from the user's current shell — `cwd` is `${CLAUDE_PROJECT_DIR}`.

## The flow — 4 steps

### Step 1 — Preflight (first run of session only)

**1a. Project directory.**
- If `$CLAUDE_PROJECT_DIR` is set and exists → use it.
- If set but missing → create it (`New-Item -ItemType Directory -Force` / `mkdir -p`), `cd` in.
- If unset → ask user where to keep state (suggest `~/ditat-verify`), `mkdir -p`, `cd` in, set `$env:CLAUDE_PROJECT_DIR` for the session. Do NOT dump state into the plugin dir.

**1b. Python launcher.** Windows: prefer `py` (Python.org launcher); `python` is often the MS Store shim. macOS/Linux: `python3`.

**1c. Env check:**

PowerShell (Windows):
```
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" check-env
```
Bash:
```
python3 "$CLAUDE_PLUGIN_ROOT/scripts/ditat_verify.py" check-env
```

If `ok: false`, copy `${CLAUDE_PLUGIN_ROOT}/.env.example` to `${CLAUDE_PROJECT_DIR}/.env` and fill credentials. Skip preflight on repeat invocations in the same session.

### Step 2 — Fetch (one helper call)

```
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" fetch --last-month
```

Flags:
- `--last-week` / `--last-month` — presets (7 / 30 days)
- `--since-days N` — custom window
- `--limit N` — cap (default 500)
- `--all` — no date filter
- `--include-processed` — re-verify already-marked shipments
- `--workers N` — across-shipment parallelism (default 5)
- `--doc-workers N` — per-shipment doc-download parallelism (default 3)

Default mapping:
- "verify last week" → `--last-week`
- "verify last month" or unspecified → `--last-month`
- "verify next N shipments" → `--limit N`
- Single-shipment retry → use `verify-one <KEY>` (same envelope, batch of 1).

`fetch` does THREE things in one call:
1. Downloads docs to `${CLAUDE_PROJECT_DIR}/downloads/<key>/`.
2. Writes `.ditat_batch.json` (full Ditat record per shipment).
3. **Writes `.ditat_findings.json` skeleton** — every shipment pre-populated with `extracted: {}` and `docs_missing` pre-computed (so invoice-only / partial shipments need NO PDF reading).

Stdout JSON:
```json
{
  "count": N,
  "batch_sidecar": ".../.ditat_batch.json",
  "findings_file": ".../.ditat_findings.json",
  "shipments": [
    {
      "shipment_key": "9536",
      "shipment_id":  "SH-0000009584",
      "ditat_fields": { "bol_number": ..., "load_number": ..., "total_weight_lbs": ..., "pickup": {...}, "delivery": {...}, ... },
      "documents": [
        { "classification": "RC|BOL|POD|UNKNOWN", "file_name": "...", "path": "<absolute>" }
      ]
    }
  ]
}
```

If `count == 0`: tell user "no unprocessed shipments" and stop. **Do not call `finalize`.**

### Step 3 — Read PDFs + extract (big parallel chunks, one merge per chunk)

PDF extraction is done by Claude via the Read tool — Python's role is I/O + diff + docx only. Most carrier PDFs are scanned images, so OCR-grade vision is required and that's what the Read tool gives you.

**Chunk size: 10 shipments per turn.** Each chunk = 1 message with up to 30 parallel Read calls (RC+BOL+POD × 10).

- ≤ 10 shipments → ONE turn, every Read fires in parallel.
- 11–50 shipments → chunks of 10 per turn.
- 50+ shipments → still chunks of 10. Don't shrink to 3-5.

**Skip shipments that don't need reading.** The findings skeleton already lists shipments with `docs_missing: ["RC","BOL","POD"]` (invoice-only) — those need NO Read calls. Only read shipments with at least one of RC/BOL/POD present.

**After each chunk, append the chunk's records via the helper — never write ad-hoc Python.**

Write the chunk records to a temp JSON file then call:
```
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" append-findings <chunk-file.json>
```

Chunk file schema (list form):
```json
[
  {
    "shipment_key": "9605",
    "extracted": {
      "rc":  { "load_number": "...", "agreed_rate": 1500.00, "pickup_date": "2026-05-01",
               "delivery_date": "2026-05-03", "equipment_type": "Reefer",
               "pickup_location":   { "city": "...", "state": "..." },
               "delivery_location": { "city": "...", "state": "..." },
               "commodity": "...", "weight_lbs": 42000, "pieces": 24 },
      "bol": { "bol_number": "...", "weight_lbs": 42000, "pieces": 24,
               "pickup_date": "...", "delivery_date": "...",
               "shipper":   { "city": "...", "state": "..." },
               "consignee": { "city": "...", "state": "..." },
               "commodity": "..." },
      "pod": { "bol_number": "...", "delivery_date": "...", "signed_by": "...",
               "pieces_received": 24, "weight_received_lbs": 41950,
               "damages_notes": null }
    },
    "docs_missing": []
  }
]
```

The helper merges atomically (last-write-wins per shipment_key). The skeleton's `docs_missing` is preserved unless you explicitly override.

**Fields per doc type:**

| RC                              | BOL                            | POD                  |
|---------------------------------|--------------------------------|----------------------|
| load_number                     | bol_number                     | bol_number           |
| agreed_rate                     | shipper {city, state}          | delivery_date        |
| pickup_date                     | consignee {city, state}        | signed_by            |
| delivery_date                   | pickup_date                    | pieces_received      |
| equipment_type                  | delivery_date                  | weight_received_lbs  |
| pickup_location {city, state}   | weight_lbs                     | damages_notes        |
| delivery_location {city, state} | pieces                         |                      |
| commodity                       | commodity, po_numbers, hazmat  |                      |
| weight_lbs, pieces              |                                |                      |

Rules:
- **Do NOT diff in your head.** `finalize` runs the deterministic diff in Python. Just extract cleanly.
- If a doc is missing/unreadable, omit that key from `extracted` and add the type to `docs_missing` (e.g. `["RC"]`). Don't retry unreadable PDFs.
- ISO dates (`YYYY-MM-DD`) where possible.
- Read only page 1 of each PDF unless it's clearly multi-page (RC sometimes splits).

### Step 4 — Finalize (one helper call)

```
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" finalize
```

Defaults: reads `.ditat_findings.json` and `.ditat_batch.json` from project dir; renders **anomalies-only** docx (counts header + problem shipments only).

The helper in one transaction:
1. Runs cross-checks (BOL↔RC, POD↔RC, Ditat↔RC, BOL↔POD) with tolerances (weight Δ>5% critical, dates Δ>1d critical, money Δ>$1 critical, normalized string compare).
2. Marks every shipment processed in `state.db`.
3. Builds **one `.docx`** with counts header + detail section for problematic shipments only.
4. Optionally deletes the sidecar + findings.

Output JSON:
```json
{
  "docx": "<abs path>",
  "processed": N,
  "problematic": M,
  "verdicts": { "OK": ..., "WARN": ..., "ISSUES": ..., "RC MISSING": ... },
  "problem_shipments": [ { "shipment_id": "...", "verdict": "ISSUES", "critical": 2, "warn": 1 }, ... ]
}
```

Flags:
- `--output <path>` — override docx output location
- `--findings-file <path>` — override findings path (default: `.ditat_findings.json`)
- `--batch-file <path>` — override sidecar path
- `--cleanup` — delete sidecar + findings after success
- `--full-report` — include all-shipments summary table in docx (default omits it)

### Step 5 — Roll up to user

Print a compact table (problematic only) and the docx path:

```
shipment_id        verdict       critical  warn
SH-0000009584      ISSUES        2         3
SH-0000009586      RC MISSING    0         1

→ reports\ditat-verify-2026-05-26-1900.docx
```

**Net turns per batch:**
- ≤10 shipments: ~3 turns (preflight optional + fetch + 1 parallel-Read turn + finalize). With finalize that's still 3 distinct CLI calls, but the heavy lifting is one big parallel-Read.
- 45 shipments: preflight + fetch + ~4-5 chunked parallel-Read+append turns + finalize ≈ 7-8 turns.
- 100 shipments: ~12 turns total.

## Sub-commands user may invoke directly

- `verify last week` → `fetch --last-week`, full flow
- `verify last month` / `verify ditat shipments` → `fetch --last-month`, full flow
- `verify next N shipments` → `fetch --limit N`, full flow
- `verify shipment <KEY>` → `verify-one <KEY>`, then `finalize` as a batch of 1
- `ditat verify status` → `status` subcommand
- `ditat env check` → `check-env`

## Operational notes

- **Token budget.** Ditat enforces 12 token-fetches/hour. Helper reuses cached token at `${CLAUDE_PROJECT_DIR}/.ditat_token_*.json`.
- **Permission gap.** If the Ditat user lacks the `documents` View role, docs list is empty even when files exist. Helper logs a warning; affected shipments get `RC MISSING` verdict.
- **Concurrent runs.** State.db uses WAL + busy_timeout=5s; two overlapping invocations won't corrupt state.
- **`.ditat_batch.json`** carries the full Ditat record for each shipment so `finalize` can diff without re-hitting the API. Delete manually if a fetch was aborted.
- **`.ditat_findings.json`** is the skeleton populated by `fetch` and filled in by the agent via `append-findings`. Don't hand-edit; use the CLI.

## Anti-patterns — DO NOT do these

These break the pipeline. Reject the impulse:

- **Do NOT write custom Python scripts to init/append/stub findings.** Use `fetch` (writes skeleton) and `append-findings <chunk.json>` (merges chunks). One-off scripts like `_init_findings.py`, `_append_findings.py`, `_chunk_records.json` are forbidden — the helper CLI covers every step.
- **Do NOT shrink chunks to 3-5 shipments "to be safe".** Default is 10 shipments × 3 PDFs = 30 parallel Reads per turn. This is the entire performance optimization. Shrinking it doubles or triples session time.
- **Do NOT read PDFs for shipments where the skeleton already says `docs_missing: ["RC","BOL","POD"]`.** That shipment is invoice-only / no docs; `finalize` handles it with verdict `RC MISSING`. Skip it entirely.
- **Do NOT diff in your head.** Just extract fields. The deterministic diff is in `ditat/diff.py`.
- **Do NOT write per-shipment `reports/<key>.md` files.** The deliverable is the one batch `.docx`.
- **Do NOT call `mark` from the agent during a batch run.** `finalize` marks every shipment in one transaction.
- **Do NOT skip `finalize` because "some shipments are incomplete".** It works with whatever is in findings.json; missing docs are fine.
- **Do NOT write helper output into the plugin directory** (`$CLAUDE_PLUGIN_ROOT`). All state lives in `$CLAUDE_PROJECT_DIR`.
- **Do NOT retry the same failing Read.** Record the doc type in `docs_missing` and move on. Likely scanned PDF with bad OCR — move on.

If you find yourself writing Python to work around a step, STOP and re-read this file. The helper CLI already covers it.

## If something is missing or wrong — guide the user

Translate every failure into the next action; never dump errors silently.

### Environment / install

| Condition | What to do |
|---|---|
| `python` resolves to MS Store shim (exit 49) | Re-run with `py` instead of `python`. Tell user once. |
| Neither `py`, `python`, nor `python3` works | Tell user to install Python 3.10+ from python.org. Stop. |
| `python-docx` / `requests` / `python-dotenv` import error | Run `py -m pip install -r "$env:CLAUDE_PLUGIN_ROOT\scripts\requirements.txt"`. The `SessionStart` hook normally handles this. |
| `$CLAUDE_PLUGIN_ROOT` empty | Plugin not installed/active. Tell user to run `/plugin install ditat-verify@ditat-tools` and restart session. |
| `UnicodeEncodeError` on ad-hoc Python (Cyrillic paths) | Set `$env:PYTHONIOENCODING = "utf-8"` before the call. Better: avoid ad-hoc one-liners; use the CLI. |

### Project directory

| Condition | What to do |
|---|---|
| `$CLAUDE_PROJECT_DIR` unset | Ask user (suggest `~/ditat-verify`); `mkdir -p`, `cd`, set for session. |
| Set but folder missing | Create with `mkdir -p` / `New-Item -ItemType Directory -Force`. Continue. |
| cwd is the plugin folder | Refuse. cd out to a customer-owned folder first; plugin updates would wipe state. |

### Credentials

| Condition | What to do |
|---|---|
| `check-env` returns `ok: false` | Tell user which vars (in JSON). Offer to copy `.env.example`. Don't proceed to fetch. |
| HTTP 401 / "invalid_client" on first fetch | Creds wrong. Confirm with Ditat admin. Don't retry — burns 12/hr budget. |
| `TokenFetchLimitExceeded` | Hit 3-per-process cap. Wait, fix creds, re-run. |

### Ditat permissions

| Condition | What to do |
|---|---|
| API returns code 900 on `includeDocuments=true` | Helper auto-retries without the flag; docs list will be empty. Tell admin to grant Documents View. Continue — affected shipments get `RC MISSING`. |
| Same for notes | Warn once, continue. |

### Data flow

| Condition | What to do |
|---|---|
| `fetch` returns `count: 0` | "No unprocessed shipments in this window." Stop. Don't call `finalize`. Offer wider window or `--include-processed`. |
| Every shipment has `documents: []` | Likely permission gap. Warn; let `finalize` run for Ditat-only checks. |
| One shipment has fewer docs than expected | Normal — `fetch` pre-fills `docs_missing` for you. Skip PDF reads for it. |
| PDF Read returns empty/truncated | Don't retry. Add the doc type to `docs_missing` for that shipment. |
| `finalize` says `batch sidecar not found` | Re-run `fetch` or `verify-one`. |
| `finalize` says `findings file not found` | Re-run `fetch` (writes skeleton). |
| `verify-one <KEY>` returns `detail fetch failed` | Keys are numeric strings (e.g. `9536`), not `SH-...` IDs. |

### Rate limiting / network

| Condition | What to do |
|---|---|
| `429 rate-limited` warnings | Helper backs off. If still fails, reduce `--workers`. |
| `ConnectTimeout` | Check user can reach `https://tmsapi01.ditat.net`. Retry once. |
| Many 429s + `TokenFetchLimitExceeded` | Stop. Wait for sliding-hour reset. Token cache survives. |

### Output

| Condition | What to do |
|---|---|
| `finalize` succeeds with `problematic: 0` | "N shipments verified, 0 problematic." Still print docx path (has counts header). |
| Docx unreadable in Word | Confirm path is absolute. Valid .docx is a zip — try LibreOffice as cross-check. |

**General rule:** every failure ends with one of: (1) "Here is what I'll run next" (auto-recoverable), (2) "Please do X then retry" (needs user input), (3) "Stopping here — Y reason" (hard fail, don't burn budget).

## What this skill does NOT do

- Write back to Ditat (read-only by design).
- Send Slack/email alerts.
- OCR image-only PDFs locally — Read tool handles OCR via vision; if it still fails, the doc is recorded as missing.
- Produce per-shipment `.md` files. The docx is the only persistent report artifact.
