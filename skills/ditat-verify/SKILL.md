---
name: ditat-verify
description: Pull unprocessed shipments from Ditat TMS, download their documents (BOL/POD/Rate Confirmation), and cross-check BOL+POD+Ditat shipment fields against the Rate Confirmation. Produces ONE batch Word doc (.docx) listing only problematic shipments and marks every processed shipment in state.db. Trigger when user says "verify ditat shipments", "check ditat", "run ditat verification", "/ditat-verify", or asks to reconcile shipment documents against rate cons.
---

# Ditat Shipment Verification

## How to use this skill

**What it does.** For every shipment in a time window that hasn't already been processed, downloads its PDFs (BOL / POD / Rate Confirmation), extracts the key fields from each, cross-checks them against the Ditat shipment record AND against the Rate Confirmation, and emits **one Word document** with:
- a summary table of all shipments in the batch (verdict per row), and
- a detail section for **problematic shipments only** (critical findings, warnings, or RC missing).

Every shipment processed is marked in `state.db` so it won't be re-checked on the next run.

**The output the user cares about.** A single `.docx` file at `${CLAUDE_PROJECT_DIR}/reports/ditat-verify-<stamp>.docx`. The skill prints the absolute path at the end. **No per-shipment `.md` files** are produced — the docx is the deliverable.

**Triggers.** Run this skill when the user says any of:
- `verify ditat shipments`, `check ditat`, `run ditat verification`, `/ditat-verify`
- `verify last week` / `verify last month` / `verify last N days`
- `verify shipment <KEY>` / `re-verify shipment <KEY>` (single-shipment retry)
- `ditat env check` / `ditat verify status`
- "reconcile shipment docs against rate cons" or anything semantically equivalent

**Inputs.** Just a time window (defaults to last month if user said nothing). Credentials come from `${CLAUDE_PROJECT_DIR}/.env`.

**Outputs.**
1. `${CLAUDE_PROJECT_DIR}/reports/ditat-verify-<YYYY-MM-DD-HHMM>.docx` — the deliverable.
2. Updated `state.db` rows for every shipment processed.
3. A short table printed in chat: shipment_id, verdict, critical_count, warn_count → docx path.

**Reusable as a building block.** Each helper sub-command (`check-env`, `fetch`, `verify-one`, `finalize`, `status`, `reset`, `mark`) prints JSON on stdout and logs on stderr, so it can be piped or invoked from another skill. The Python modules `ditat.diff` and `ditat.docx_report` are import-safe and stateless.

## Paths

- **Helper script:** `${CLAUDE_PLUGIN_ROOT}/scripts/ditat_verify.py` — always invoke with the full `${CLAUDE_PLUGIN_ROOT}` path so plugin updates don't break it.
- **State** (`state.db`, `reports/`, `downloads/`, token cache, `.env`, `.ditat_batch.json`) lives in `${CLAUDE_PROJECT_DIR}` and is auto-resolved by the helper.
- **`.env`** must exist at `${CLAUDE_PROJECT_DIR}/.env` with `DITAT_BASE_URL`, `DITAT_ACCOUNT_ID`, `DITAT_CLIENT_ID`, `DITAT_CLIENT_SECRET`. Template at `${CLAUDE_PLUGIN_ROOT}/.env.example`.

Run from the user's current shell — `cwd` is `${CLAUDE_PROJECT_DIR}`.

## The flow — 4 sequential turns total

The skill collapses the old per-shipment loop into a fixed 4-step pipeline regardless of batch size:

### Step 1 — Preflight (first run of session only)

**1a. Ensure a working project directory exists.** The skill writes `state.db`, `downloads/`, `reports/`, and the token cache into whichever directory is `$CLAUDE_PROJECT_DIR` (falls back to current working directory). On the customer's first run this folder usually does not exist yet. Before any helper call:

- If `$CLAUDE_PROJECT_DIR` is set and the path exists → use it.
- If it's set but missing → create it (`New-Item -ItemType Directory -Force <path>` / `mkdir -p <path>`), then `cd` into it.
- If unset → ask the user where they want shipment state stored (suggest `~/ditat-verify` or similar), `mkdir -p` it, `cd` into it, and `$env:CLAUDE_PROJECT_DIR = <abs path>` (PowerShell) or `export CLAUDE_PROJECT_DIR=<abs path>` (Bash) for the session. Do **not** dump state into the plugin directory.

**1b. Pick the right Python launcher.** On Windows, `python` is often a Microsoft Store shim that fails silently. Prefer `py` (the Python launcher installed with the standard Python.org distribution). On macOS/Linux use `python3`. If both `python` and `py` are missing, the customer needs to install Python 3.10+ first.

**1c. Run the env check:**

PowerShell (Windows):
```
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" check-env
```
Bash:
```
python3 "$CLAUDE_PLUGIN_ROOT/scripts/ditat_verify.py" check-env
```

If `ok: false`, tell the user to copy `${CLAUDE_PLUGIN_ROOT}/.env.example` to `${CLAUDE_PROJECT_DIR}/.env` and fill credentials. Skip preflight on repeat invocations in the same session.

### Step 2 — Fetch (single helper call)

```
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" fetch --last-month
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
- Single-shipment retry → use `verify-one <KEY>` instead (same envelope, batch of 1).

Helper downloads docs in parallel and prints slim JSON on stdout:
```json
{
  "count": N,
  "batch_sidecar": ".../.ditat_batch.json",
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

If `count == 0`: tell the user "no unprocessed shipments" and stop. **Do not call `finalize`.**

### Step 3 — Read PDFs + write findings (parallel Reads, chunked if batch big)

PDF extraction is done by **Claude via the Read tool** — not in Python. Python's role stays I/O + diff + docx. Reading PDFs with Claude tolerates the format variation across carriers/brokers far better than rule-based parsers, which is why this step lives in the agent.

**Chunking policy** (important for performance and context):

- **≤ 10 shipments** (≈30 PDFs): fire every `Read` in a single message, parallel. One turn, one findings file, done.
- **11–25 shipments**: process in chunks of ~8 shipments per turn. Each turn = one message with all that chunk's Reads in parallel, then append that chunk's records to `${CLAUDE_PROJECT_DIR}/.ditat_findings.json`.
- **> 25 shipments** (e.g. 54-shipment weekly run = ~162 PDFs): chunks of 5–8 shipments per turn. Each chunk is its own parallel-Read message.

Within a chunk: fire **all** that chunk's PDF paths as parallel `Read` calls in a **single message** — do not loop one Read per turn. The whole point of the optimization is parallel reads inside one turn.

After every chunk, write/update `${CLAUDE_PROJECT_DIR}/.ditat_findings.json` so progress is durable across context resets. Use the same `{ "shipments": [...] }` schema across chunks — re-read the file, append the new shipments' records, write it back.

The `classification` hint in the fetch JSON tells you which doc is RC vs BOL vs POD, but re-classify from content if the hint is `UNKNOWN` or looks wrong.

Extract these fields per doc type:

| RC                   | BOL                       | POD                  |
|----------------------|---------------------------|----------------------|
| load_number          | bol_number                | bol_number           |
| agreed_rate          | shipper {city, state}     | delivery_date        |
| pickup_date          | consignee {city, state}   | signed_by            |
| delivery_date        | pickup_date               | pieces_received      |
| equipment_type       | delivery_date             | weight_received_lbs  |
| pickup_location {city, state} | weight_lbs       | damages_notes        |
| delivery_location {city, state} | pieces        |                      |
| commodity            | commodity                 |                      |
|                      | po_numbers, hazmat        |                      |

After all chunks are done, the combined findings file at `${CLAUDE_PROJECT_DIR}/.ditat_findings.json` should look like:

```json
{
  "shipments": [
    {
      "shipment_key": "9536",
      "shipment_id":  "SH-0000009584",
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
}
```

Rules for the agent:
- **Do NOT diff in your head.** The helper's `finalize` step runs the deterministic diff in Python. Just extract the fields cleanly.
- If a doc is missing or unreadable, omit that key from `extracted` and list the type in `docs_missing` (e.g. `["RC"]`).
- Use ISO dates (`YYYY-MM-DD`) where possible. The diff module accepts common variants but ISO is safest.
- For large PDFs, the first ~10 pages are almost always enough.

### Step 4 — Finalize (single helper call)

```
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" finalize --findings-file .ditat_findings.json
```

The helper does, in one transaction:
1. Runs cross-checks in Python (BOL↔RC, POD↔RC, Ditat↔RC, BOL↔POD) with tolerances (weight Δ>5% critical, dates Δ>1d critical, money Δ>$1 critical, normalized string compare).
2. Marks every shipment processed in `state.db`.
3. Builds **one `.docx`** with a summary table + detail section for problematic shipments only.
4. Deletes the batch sidecar.

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
- `--keep-batch` — keep `.ditat_batch.json` (default: delete)
- `--batch-file <path>` — override sidecar path (advanced)

### Step 5 — Roll up to user

Print a compact table (verdict-first ordering) and the docx path:

```
shipment_id        verdict       critical  warn
SH-0000009584      ISSUES        2         3
SH-0000009586      RC MISSING    0         1
SH-0000009585      OK            0         0  ← not in detail section

→ reports\ditat-verify-2026-05-13-1530.docx
```

**Net sequential tool turns per batch:**
- Tiny batch (≤10 shipments): ~4 turns (preflight + fetch + 1 parallel-Read turn + finalize).
- Big batch (e.g. 54 shipments): ~10–14 turns (preflight + fetch + 7–11 parallel-Read chunks + finalize). Still dramatically fewer than the old per-shipment loop (~3N+2 = 164+ calls for 54 shipments).

## Sub-commands user may invoke directly

- `verify last week` → `fetch --last-week`, full flow
- `verify last month` / `verify ditat shipments` → `fetch --last-month`, full flow
- `verify next N shipments` → `fetch --limit N`, full flow
- `verify shipment <KEY>` / `re-verify shipment <KEY>` → `verify-one <KEY>`, then `finalize` as a batch of 1. `mark` is INSERT-OR-REPLACE — no need to `reset` first.
- `ditat verify status` → `status` subcommand
- `ditat env check` → `check-env`

## Operational notes

- **Token budget.** Ditat enforces 12 token-fetches/hour. Helper reuses cached token at `${CLAUDE_PROJECT_DIR}/.ditat_token_*.json`. Workers share the same session, so concurrency does not multiply token fetches.
- **Permission gap.** If the Ditat user lacks the `documents` View role, the docs list is empty even when files exist. Helper logs a warning; affected shipments will have empty `documents` and the docx will show them with the relevant doc marked `✗`.
- **Large PDFs.** Stream first ~10 pages; BOL/POD/RC fields almost always live on page 1.
- **Concurrent runs.** State.db uses WAL + busy_timeout=5s; two overlapping invocations won't corrupt state.
- **`.ditat_batch.json`** is the bridge between `fetch` and `finalize`. It carries the full Ditat record for each shipment so `finalize` can run diffs without re-hitting the API. Delete it manually if a fetch was aborted.

## What this skill does NOT do

- Write back to Ditat (read-only by design).
- Send Slack/email alerts.
- OCR image-only PDFs — Read tool may fail on those; list the doc in `docs_missing` and move on.
- Produce per-shipment `.md` files anymore. The docx is the only persistent report artifact.
