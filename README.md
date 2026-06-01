# ditat-verify (Claude Code plugin + cloud server)

Pulls shipments from a Ditat verification **server**, downloads BOL/POD/Rate-Confirmation PDFs, cross-checks field values between the documents and the Ditat shipment record, and produces **one Word document** listing only the problematic shipments — ready to forward to ops or the customer.

The skill collapses the whole pipeline into ~4 sequential tool turns regardless of batch size.

## Architecture — two parts

```
  server/   (cloud)            scripts/ + skills/  (this plugin, local)
  ─────────────────            ────────────────────────────────────────
  holds Ditat credentials      calls the server for a manifest
  lists shipments              downloads each doc URL
  classifies documents   ──►   Claude extracts PDF fields
  streams PDF binaries         diffs against rules.yaml
                               renders the .docx
```

- **Server** ([server/](server/)) is deployed to the cloud (Cloud Run / Lambda). It is the only thing that knows the Ditat API credentials. See [server/README.md](server/README.md) for deploy steps.
- **Plugin** (this repo's root, `scripts/` + `skills/`) runs locally inside Claude Code. It only needs the server's URL (+ an API key). It holds **no Ditat secrets and no state DB** — every run verifies the full window the server returns.
- **Rules** ([scripts/rules.yaml](scripts/rules.yaml)) externalize every threshold and the accessorial policy, so ops can tune what counts as a problem without touching code.

---

## What the customer gets

A single `.docx` per run at `<your project>/reports/ditat-verify-<YYYY-MM-DD-HHMM>.docx`:
- a counts header (OK / WARN / ISSUES / RC MISSING),
- a detail section for **only** the problematic shipments — route, dates, mismatched fields.

Plus a short summary printed in the chat (verdicts + docx path). No per-shipment files, no manual reconciliation.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Claude Code** | Desktop app, VS Code extension, or CLI. |
| **Python 3.10+** | `py --version` (Windows) / `python3 --version`. Plugin auto-installs its deps on first activation. |
| **A deployed verification server** | See [server/README.md](server/README.md). Whoever deploys it sets the Ditat credentials there. You need its URL + API key. |

The Ditat API user (configured on the **server**, not here) needs **View** role on Shipment **and** on shipment Documents — without the Documents role, doc lists come back empty.

---

## Installation (plugin side)

### 1. Add the marketplace and install

```
/plugin marketplace add boxa-devops/ditat-verify-plugin
/plugin install ditat-verify@ditat-tools
```

A `SessionStart` hook runs `pip install -r scripts/requirements.txt` automatically (re-installs only when that file changes).

### 2. Point the plugin at your server

In your project directory, create a `.env`:

PowerShell:
```powershell
Copy-Item "$env:CLAUDE_PLUGIN_ROOT\.env.example" .env
notepad .env
```
Bash:
```bash
cp "$CLAUDE_PLUGIN_ROOT/.env.example" .env
$EDITOR .env
```

Fill in:
```
DITAT_SERVER_URL=https://your-server.example.run.app
DITAT_SERVER_API_KEY=<the shared key the server expects>
```

`.env` is git-ignored — never commit it.

### 3. Verify

```
ditat server check
```

Or directly. **On Windows use `py`** (not `python` — Windows ships a Store shim that silently fails):

```powershell
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" check-server
```
macOS / Linux:
```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/ditat_verify.py" check-server
```

Expected: `"ok": true` and a `health` block from the server.

### Project directory

All per-run state lives under `$CLAUDE_PROJECT_DIR` (or cwd if unset), never inside the plugin:

PowerShell:
```powershell
New-Item -ItemType Directory -Force "$HOME\ditat-verify" | Out-Null
Set-Location "$HOME\ditat-verify"
$env:CLAUDE_PROJECT_DIR = (Get-Location).Path
```
Bash:
```bash
mkdir -p "$HOME/ditat-verify"; cd "$HOME/ditat-verify"; export CLAUDE_PROJECT_DIR="$PWD"
```

Place `.env` here. `downloads/` and `reports/` are created on first run.

---

## Daily / weekly use

Type one of these into Claude Code:

| User says | What happens |
|---|---|
| `verify ditat shipments` | Last 30 days → docx |
| `verify last week` | Last 7 days (filtered on delivery date) |
| `verify last month` | Last 30 days |
| `verify last 14 days` | Custom window |
| `verify next 5 shipments` | First 5 in default window |
| `ditat server check` | Server preflight |

The skill prints the docx path at the end. Open it in Word / Google Docs / LibreOffice.

---

## How the verdicts work

Cross-checks run in Python with tolerances from [scripts/rules.yaml](scripts/rules.yaml) — same answer every time, and editable without touching code.

| Field type | Critical (`ISSUES`) | Warning (`WARN`) |
|---|---|---|
| Weight (Ditat↔RC) | Δ > 5 % | 1–5 % |
| Weight/pieces (BOL↔RC) | BOL over RC by ≥ 10 % | — |
| Dates | Δ > 1 day | 0 < Δ ≤ 1 day |
| Money (rate vs revenue) | Δ > $1.00 or > 1 % | — |
| BOL / load numbers | Any mismatch | Missing on one side |
| String fields (commodity, equipment, cities) | — | Mismatch after normalization |
| Accessorial policy (detention/layover) | RC worse than policy | RC silent on a term |
| RC missing entirely | `RC MISSING` (or `OK` for allow-listed customers) | — |

Cross-checks: **BOL ↔ RC**, **POD ↔ RC**, **Ditat ↔ RC**, **BOL ↔ POD**, plus an **RC-only accessorial policy** check.

Edit `rules.yaml` to change any threshold, the accessorial defaults, the per-term severities, or the `rc_missing_ok_customers` allow-list. Omitted keys fall back to built-in defaults.

---

## Where everything lives

All state is under `$CLAUDE_PROJECT_DIR`, never inside the plugin — plugin updates never wipe data.

| Path | Purpose |
|---|---|
| `.env` | Server URL + API key (git-ignore) |
| `.ditat_batch.json` | Transient handoff between `pull` and `finalize` |
| `.ditat_findings.json` | Skeleton from `pull`, filled by the agent via `append-findings` |
| `downloads/<key>/` | PDFs downloaded from the server |
| `reports/ditat-verify-*.docx` | The deliverables |

No `state.db` — verification is stateless.

---

## Scheduling (run it automatically every week)

Windows Task Scheduler runs the skill locally, no manual Claude Code session. One-time setup:

1. Plugin installed, `.env` set, `ditat server check` returns `ok: true`.
2. `cd "$env:CLAUDE_PLUGIN_ROOT\scripts\scheduling"`
3. Optionally edit `run-ditat-weekly.ps1` (project dir / prompt) and `register-weekly-task.ps1` (day/time, default Monday 09:00).
4. `.\register-weekly-task.ps1` → `Task registered: DitatVerify-Weekly`.

Every Monday 09:00 it sets `$CLAUDE_PROJECT_DIR`, launches `claude --print --dangerously-skip-permissions "verify last week"`, runs the skill, and appends a line to `<project>\.scheduled-runs.log`.

Test: `Start-ScheduledTask -TaskName "DitatVerify-Weekly"`. Remove: `.\unregister-weekly-task.ps1`.

Limitation: the task fires only while the user is logged on. If asleep at 9:00 it fires on wake (`StartWhenAvailable=true`).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `check-server` `ok: false`, `DITAT_SERVER_URL` missing | Set it in `.env`. |
| `check-server` `health_status: 401` | Set `DITAT_SERVER_API_KEY` to match the server. |
| `check-server` `health` says `misconfigured` | The server is missing Ditat creds — fix on the server, not here. |
| `pull` → `server /batch returned 503` | Server can't reach Ditat. Server-side issue. |
| `python-docx` / `PyYAML` import error | `pip install -r "$env:CLAUDE_PLUGIN_ROOT\scripts\requirements.txt"`. |
| OCR-only PDFs unreadable | Skill lists them in `docs_missing`. Re-scan or attach a text-based version. |
| Want different thresholds | Edit `scripts/rules.yaml`. |

---

## Sub-commands (advanced / scripting)

Each prints JSON on stdout, logs on stderr:

```
py scripts/ditat_verify.py check-server
py scripts/ditat_verify.py pull --last-week [--limit 50]
py scripts/ditat_verify.py append-findings <chunk.json>
py scripts/ditat_verify.py finalize [--rules-file scripts/rules.yaml] [--full-report]
```

`pull` produces a slim JSON manifest + `.ditat_batch.json` sidecar + `.ditat_findings.json` skeleton. The Claude skill fills the findings via `append-findings`, then calls `finalize`. `ditat.diff` and `ditat.docx_report` are import-safe for embedding.

---

## Update / uninstall

```
/plugin marketplace update ditat-tools
/plugin update ditat-verify@ditat-tools
/plugin uninstall ditat-verify
```

State (`reports/`, `downloads/`, `.env`) lives in your project directory and survives plugin updates/uninstalls. Delete manually if desired.
