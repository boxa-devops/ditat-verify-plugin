---
name: ditat-verify
description: Pull shipments from the Ditat verification server, download their documents (BOL/POD/Rate Confirmation), and cross-check BOL+POD+Ditat shipment fields against the Rate Confirmation. Produces ONE anomalies-only Word doc (.docx) of problematic shipments. Trigger when user says "verify ditat shipments", "check ditat", "run ditat verification", "/ditat-verify", or asks to reconcile shipment documents against rate cons.
---

# Ditat Shipment Verification

## Architecture (split: server + plugin)

The credentialed half lives in a **cloud server** (see `server/`): it holds the
Ditat API credentials, lists shipments, classifies docs, and streams PDF
binaries. This **plugin** is the local half: it calls the server, downloads the
PDFs, has Claude extract fields, runs the deterministic diff against
`rules.yaml`, and renders the docx.

The plugin never talks to Ditat directly and stores **no state DB** — every run
verifies the full window the server returns.

## What it does

For every shipment the server returns in a time window:
1. Downloads its PDFs (BOL / POD / Rate Confirmation) from the server.
2. Extracts key fields from each PDF (RC is the source of truth).
3. Cross-checks BOL, POD, and the Ditat shipment record against the RC, using
   the thresholds in `rules.yaml`.
4. Writes **one anomalies-only Word doc** at `${CLAUDE_PROJECT_DIR}/reports/ditat-verify-<stamp>.docx`:
   - counts header (OK / WARN / ISSUES / RC MISSING),
   - detail section for **problematic shipments only** — clean shipments omitted.

The user only cares about the .docx path. No per-shipment markdown files.

## Triggers

- `verify ditat shipments`, `check ditat`, `run ditat verification`, `/ditat-verify`
- `verify last week` / `verify last month` / `verify last N days`
- `ditat server check`
- "reconcile shipment docs against rate cons" or semantic equivalent

Inputs: time window (defaults to last month). Server config from `${CLAUDE_PROJECT_DIR}/.env`.

## Paths

- **Helper script:** `${CLAUDE_PLUGIN_ROOT}/scripts/ditat_verify.py` — always invoke with the full `${CLAUDE_PLUGIN_ROOT}` path.
- **State** (`reports/`, `downloads/`, `.env`, `.ditat_batch.json`, `.ditat_findings.json`) lives in `${CLAUDE_PROJECT_DIR}`. No `state.db`.
- **`.env`** at `${CLAUDE_PROJECT_DIR}/.env` with `DITAT_SERVER_URL` and (optionally) `DITAT_SERVER_API_KEY`. Template at `${CLAUDE_PLUGIN_ROOT}/.env.example`.
- **`rules.yaml`** at `${CLAUDE_PLUGIN_ROOT}/scripts/rules.yaml` — editable thresholds + accessorial policy. Override path with `--rules-file` or `$DITAT_RULES_PATH`.

Run from the user's current shell — `cwd` is `${CLAUDE_PROJECT_DIR}`.

## The flow — 4 steps

### Step 1 — Preflight (first run of session only)

**1a. Project directory.**
- If `$CLAUDE_PROJECT_DIR` is set and exists → use it.
- If set but missing → create it (`New-Item -ItemType Directory -Force` / `mkdir -p`), `cd` in.
- If unset → ask user where to keep state (suggest `~/ditat-verify`), `mkdir -p`, `cd` in, set `$env:CLAUDE_PROJECT_DIR` for the session. Do NOT dump state into the plugin dir.

**1b. Python launcher.** Windows: prefer `py` (Python.org launcher); `python` is often the MS Store shim. macOS/Linux: `python3`.

**1c. Server check:**

PowerShell (Windows):
```
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" check-server
```
Bash:
```
python3 "$CLAUDE_PLUGIN_ROOT/scripts/ditat_verify.py" check-server
```

If `ok: false`, copy `${CLAUDE_PLUGIN_ROOT}/.env.example` to `${CLAUDE_PROJECT_DIR}/.env` and set `DITAT_SERVER_URL` (+ `DITAT_SERVER_API_KEY` if the server requires auth). Skip preflight on repeat invocations in the same session.

### Step 2 — Pull (one helper call)

```
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" pull --last-month
```

Flags:
- `--last-week` / `--last-month` — presets (7 / 30 days). `--last-week` filters on **delivery date** (delivered last week), not `updatedOn`.
- `--filter-column COL` — Ditat lookup column for the window. Override if the server rejects the name.
- `--since-days N` — custom window
- `--limit N` — cap (default 500)
- `--all` — no date filter
- `--page-size N` — server-side page size (default 1000)

Default mapping:
- "verify last week" → `--last-week`
- "verify last month" or unspecified → `--last-month`
- "verify next N shipments" → `--limit N`

`pull` does THREE things in one call:
1. POSTs the server's `/batch` and downloads each doc to `${CLAUDE_PROJECT_DIR}/downloads/<key>/`.
2. Writes `.ditat_batch.json` (full Ditat record per shipment).
3. **Writes `.ditat_findings.json` skeleton** — every shipment pre-populated with `extracted: {}` and `docs_missing` pre-computed by the server (so invoice-only / partial shipments need NO PDF reading).

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

**Only finished loads are returned.** The server filters to shipment status **Completed** or **Cancelled** (excludes Invoiced and in-progress; override via the `statuses` query / `DITAT_VERIFY_STATUSES`). Status is authoritative; for loads without a status it falls back to "delivery date has passed". `finalize` also drops pending defensively and reports `skipped_pending`.

**Cancelled loads** are verified but **exempt from doc-completeness** — they were never delivered, so missing BOL/POD is expected, not a critical.

If `count == 0`: tell user "no delivered shipments in this window" and stop. **Do not call `finalize`.**

### Step 3 — Read PDFs + extract (big parallel chunks, one merge per chunk)

PDF extraction is done by Claude via the Read tool — Python's role is I/O + diff + docx only. Most carrier PDFs are scanned images, so OCR-grade vision is required and that's what the Read tool gives you.

**Chunk size: 10 shipments per turn.** Each chunk = 1 message with up to 30 parallel Read calls (RC+BOL+POD × 10).

- ≤ 10 shipments → ONE turn, every Read fires in parallel.
- 11–50 shipments → chunks of 10 per turn.
- 50+ shipments → still chunks of 10. Don't shrink to 3-5.

**Skip shipments that don't need reading.** The findings skeleton already lists shipments with `docs_missing: ["RC","BOL","POD"]` (invoice-only) — those need NO Read calls. Only read shipments with at least one of RC/BOL/POD present.

**After each chunk, append the chunk's records via the helper — never write ad-hoc Python.**

Write the chunk records to a file named `.ditat_chunk_<n>.json` in the project dir, then call:
```
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" append-findings .ditat_chunk_1.json
```
(Use the `.ditat_chunk_*.json` name so `finalize` auto-cleans it afterward.)

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
               "commodity": "...", "pages_present": 11, "pages_expected": 11 },
      "pod": { "bol_number": "...", "delivery_date": "...", "signed_by": "...",
               "pieces_received": 24, "weight_received_lbs": 41950,
               "damages_notes": null,
               "arrival_time": "08:00", "departure_time": "12:30",
               "pages_present": 1, "pages_expected": 1 }
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
| pickup_location {city, state}   | delivery_date                  | weight_received_lbs  |
| delivery_location {city, state} | weight_lbs                     | damages_notes        |
| commodity                       | pieces                         | **arrival_time**     |
| weight_lbs, pieces              | commodity, po_numbers, hazmat  | **departure_time**   |
| detention_rate ($/hr)           | **pages_present/expected**     | **pages_present/expected** |
| detention_free_hrs              |                                |                      |
| detention_max_hrs               |                                |                      |
| layover_rate ($/24h)            |                                |                      |
| layover_threshold_hrs           |                                |                      |

**Pickup / delivery dates — source priority: POD → BOL → Ditat trip.** Take the
actual pickup/delivery date from the POD if present, else the BOL, else fall back
to the Ditat trip (shipment) dates. For SH-…688-type cases where the BOL/POD scan
is unreadable, use the Ditat dates rather than leaving them blank.
- These dates often appear **inline inside the Shipper/Consignee block**, e.g.
  `Pickup: Jun 2, 2026 · 08:00-15:00` and `Delivery: Jun 3, 2026 · 08:00` — not as
  a labeled column. Capture them.
- **YOU normalize the date — always emit ISO `YYYY-MM-DD`.** Convert at extraction
  time: `Jun 3, 2026 · 08:00` → `"2026-06-03"`, `06/03/2026` → `"2026-06-03"`.
  Don't pass the raw string through; the LLM (you) owns this, not Python.

**Page completeness (`pages_present` / `pages_expected`) — per BOL and POD.**
If the document says "Page 1 of 11", set `pages_expected: 11`. Set
`pages_present` to how many pages the uploaded PDF actually has. The diff flags a
**critical** when `pages_expected > pages_present` (e.g. an 11-page BOL uploaded
as 1 page — all pages must be present). Omit both keys if there's no "of N" marker.

**POD in/out times (drive accessorial detection):**
- `arrival_time` / `departure_time` — the in/out (check-in / check-out) times stamped on the delivery receipt. Accept `HH:MM`, `H:MM AM/PM`, or full ISO datetime. Omit if the POD doesn't show them.
- The diff computes wait = departure − arrival. If wait exceeds the default free hours and the **RC is silent** on detention, that's flagged (see RC-policy rules). Without these times, no accessorial occurrence can be detected.

**RC accessorial extraction notes (the RC governs):**
- `detention_rate` — dollars per hour the carrier is paid for detention.
- `detention_free_hrs` — free hours before detention starts (typical RC phrasing: "after N free hours").
- `detention_max_hrs` — cap on detention hours (omit if RC says no cap).
- `layover_rate` — dollars per 24-hour layover period.
- `layover_threshold_hrs` — hours of waiting before layover triggers.
- Extract whatever terms the RC states — **when the RC states a detention/layover term it is the agreed contract and is NOT flagged**, even if below company defaults. If the RC is silent on a term, omit the key; it's only a problem when the POD shows the accessorial actually occurred.

Rules:
- **Do NOT diff in your head.** `finalize` runs the deterministic diff in Python. Just extract cleanly.
- If a doc is missing/unreadable, omit that key from `extracted` and add the type to `docs_missing` (e.g. `["RC"]`). Don't retry unreadable PDFs.
- **Dates: always ISO `YYYY-MM-DD`.** You convert messy formats during extraction
  (see the pickup/delivery note above) — never emit raw "Jun 3, 2026 · 08:00".
- Read only page 1 of each PDF unless it's clearly multi-page (RC sometimes splits).

### Step 4 — Finalize (one helper call)

```
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" finalize
```

Defaults: reads `.ditat_findings.json` and `.ditat_batch.json` from project dir; loads `rules.yaml`; renders **anomalies-only** docx (counts header + problem shipments only).

The helper:
1. Loads thresholds from `rules.yaml` (falls back to built-in defaults if absent).
2. Runs cross-checks with the rules below.
3. Builds **one `.docx`** with counts header + detail section for problematic shipments only.
4. Cleans every per-run intermediate (downloads/, sidecar, findings, chunk files), leaving only `reports/`. Pass `--keep-intermediates` to retain them.

**Cross-check rules** (defaults — all tunable in `rules.yaml`):

| Pair          | Field                          | Rule (default)                                                         |
|---------------|--------------------------------|------------------------------------------------------------------------|
| Docs          | RC / BOL / POD                 | **delivered** shipment missing any of RC/BOL/POD → critical (RC exempt for `rc_missing_ok_customers`). Pending + `skip_customers` (Amazon) excluded upstream. |
| Docs          | pages                          | BOL/POD `pages_expected > pages_present` (e.g. "1 of 11" but 1 uploaded) → critical |
| RC-policy     | detention                      | RC states detention terms → accepted (no flag). RC silent **and** POD in/out wait > 2h free → critical |
| RC-policy     | layover                        | RC states layover terms → accepted (no flag). RC silent **and** POD in/out wait ≥ 5h → critical |
| BOL↔RC        | weight_lbs                     | bol ≤ rc → OK; bol > rc by ≥10% → critical; below 10% → info           |
| BOL↔RC        | pieces                         | bol ≤ rc → OK; bol > rc by ≥10% → critical; below 10% → info           |
| Dates         | pickup_date, delivery_date     | resolved date **POD → BOL → Ditat trip** vs RC; Δ > 1d → critical; Δ = 1d → warn. Both sides must have a date (one-sided absence = no flag). |
| BOL↔RC        | commodity                      | **lenient "like" compare** — match if one contains the other or they share a meaningful word; only fully unrelated → warn |
| BOL↔RC        | locations                      | normalized string compare; mismatch → warn (fuzzy → info)              |
| POD↔RC        | bol_number                     | **skipped when BOL doc present** — BOL↔POD covers it                   |
| POD↔RC        | weight_received, pieces_received | **dropped** — POD quantities diverge on partial deliveries           |
| POD↔RC        | damages_notes                  | any damages → warn                                                     |
| Ditat↔RC      | total_weight_lbs               | weight Δ > 5% → critical; ≥1% → warn (Ditat 0/empty → warn "not entered") |
| Ditat↔RC      | total_pieces                   | any diff → critical (Ditat 0/empty → warn "not entered")              |
| Ditat↔RC      | load_number                    | id mismatch → critical                                                 |
| Ditat↔RC      | pickup_location, delivery_location | city + state only via normalized compare                          |
| Ditat↔RC      | revenue_vs_rate                | money Δ > $1 → critical                                                |
| BOL↔POD       | bol_number                     | id mismatch → critical (weight + pieces dropped — POD unreliable)      |

**Not compared** (intentionally removed — produced noise, no value):
- BOL↔RC `bol_number` — the RC carries no BOL number.
- Ditat↔RC `bol_number`, `equipment_type` — RC has no BOL number; "53Van" vs "Dry Van 53'" is the same trailer.
- **Amazon loads** (`skip_customers`) — excluded entirely; not our process.

**Special-case verdict:**
- RC missing **and** customer name matches an entry in `rc_missing_ok_customers` (default: `amazon`) → verdict downgraded from `RC MISSING` to `OK`.

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
- `--rules-file <path>` — override rules.yaml path
- `--keep-intermediates` — keep downloads/, sidecar, findings, chunk files (default: deleted, leaving only reports/)
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
- ≤10 shipments: ~3 turns (preflight optional + pull + 1 parallel-Read turn + finalize).
- 45 shipments: preflight + pull + ~4-5 chunked parallel-Read+append turns + finalize ≈ 7-8 turns.
- 100 shipments: ~12 turns total.

## Sub-commands user may invoke directly

- `verify last week` → `pull --last-week`, full flow
- `verify last month` / `verify ditat shipments` → `pull --last-month`, full flow
- `verify next N shipments` → `pull --limit N`, full flow
- `ditat server check` → `check-server`

## Operational notes

- **Server holds the credentials.** The plugin only needs `DITAT_SERVER_URL` (+ API key). Ditat's token budget, permissions, and retries are the server's concern.
- **No state DB.** Every run verifies the whole window. There is no processed-ledger and no `mark`/`status`/`reset`.
- **`.ditat_batch.json`** carries the full Ditat record + local doc paths so `finalize` can diff without re-hitting the server. Delete manually if a pull was aborted.
- **`.ditat_findings.json`** is the skeleton populated by `pull` and filled in by the agent via `append-findings`. Don't hand-edit; use the CLI.
- **`rules.yaml`** is the single place to tune thresholds. A partial file is fine — omitted keys fall back to defaults.

## Anti-patterns — DO NOT do these

These break the pipeline. Reject the impulse:

- **Do NOT write custom Python scripts to init/append/stub findings.** Use `pull` (writes skeleton) and `append-findings <chunk.json>` (merges chunks). One-off scripts are forbidden — the helper CLI covers every step.
- **Do NOT shrink chunks to 3-5 shipments "to be safe".** Default is 10 shipments × 3 PDFs = 30 parallel Reads per turn. This is the entire performance optimization.
- **Do NOT read PDFs for shipments where the skeleton already says `docs_missing: ["RC","BOL","POD"]`.** That shipment is invoice-only; `finalize` handles it with verdict `RC MISSING`. Skip it.
- **Do NOT diff in your head.** Just extract fields. The deterministic diff is in `ditat/diff.py`, thresholds in `rules.yaml`.
- **Do NOT write per-shipment `reports/<key>.md` files.** The deliverable is the one batch `.docx`.
- **Do NOT call Ditat directly from the plugin.** The server owns that. The plugin only knows the server URL.
- **Do NOT write helper output into the plugin directory** (`$CLAUDE_PLUGIN_ROOT`). All state lives in `$CLAUDE_PROJECT_DIR`.
- **Do NOT retry the same failing Read.** Record the doc type in `docs_missing` and move on.

If you find yourself writing Python to work around a step, STOP and re-read this file. The helper CLI already covers it.

## If something is missing or wrong — guide the user

Translate every failure into the next action; never dump errors silently.

### Environment / install

| Condition | What to do |
|---|---|
| `python` resolves to MS Store shim (exit 49) | Re-run with `py` instead of `python`. Tell user once. |
| Neither `py`, `python`, nor `python3` works | Tell user to install Python 3.10+ from python.org. Stop. |
| `python-docx` / `requests` / `python-dotenv` / `PyYAML` import error | Run `py -m pip install -r "$env:CLAUDE_PLUGIN_ROOT\scripts\requirements.txt"`. The `SessionStart` hook normally handles this. |
| `$CLAUDE_PLUGIN_ROOT` empty | Plugin not installed/active. Tell user to run `/plugin install ditat-verify@ditat-tools` and restart session. |

### Project directory

| Condition | What to do |
|---|---|
| `$CLAUDE_PROJECT_DIR` unset | Ask user (suggest `~/ditat-verify`); `mkdir -p`, `cd`, set for session. |
| Set but folder missing | Create with `mkdir -p` / `New-Item -ItemType Directory -Force`. Continue. |
| cwd is the plugin folder | Refuse. cd out to a customer-owned folder first; plugin updates would wipe state. |

### Server

| Condition | What to do |
|---|---|
| `check-server` returns `ok: false` with `DITAT_SERVER_URL` missing | Set `DITAT_SERVER_URL` in `${CLAUDE_PROJECT_DIR}/.env`. Don't proceed to pull. |
| `check-server` `health_status: 401` | Server requires auth. Set `DITAT_SERVER_API_KEY` to match the server's key. |
| `check-server` `health` shows `misconfigured` | The SERVER is missing Ditat creds. Whoever runs the server must set them. |
| `pull` returns `server /batch returned 503` | Server can't reach Ditat / missing creds. Server-side fix needed. |
| `pull` returns `server /batch returned 5xx` | Transient server/Ditat error. Retry once; if it persists, check server logs. |

### Data flow

| Condition | What to do |
|---|---|
| `pull` returns `count: 0` | "No shipments in this window." Stop. Don't call `finalize`. Offer a wider window. |
| One shipment has fewer docs than expected | Normal — `pull` pre-fills `docs_missing` for you. Skip PDF reads for it. |
| PDF Read returns empty/truncated | Don't retry. Add the doc type to `docs_missing` for that shipment. |
| `finalize` says `batch sidecar not found` | Re-run `pull`. |
| `finalize` says `findings file not found` | Re-run `pull` (writes skeleton). |

### Output

| Condition | What to do |
|---|---|
| `finalize` succeeds with `problematic: 0` | "N shipments verified, 0 problematic." Still print docx path (has counts header). |
| Docx unreadable in Word | Confirm path is absolute. Valid .docx is a zip — try LibreOffice as cross-check. |

**General rule:** every failure ends with one of: (1) "Here is what I'll run next" (auto-recoverable), (2) "Please do X then retry" (needs user input), (3) "Stopping here — Y reason" (hard fail).

## What this skill does NOT do

- Write back to Ditat (read-only by design).
- Send Slack/email alerts.
- OCR image-only PDFs locally — Read tool handles OCR via vision; if it still fails, the doc is recorded as missing.
- Produce per-shipment `.md` files. The docx is the only persistent report artifact.
