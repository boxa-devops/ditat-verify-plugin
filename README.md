# ditat-verify (Claude Code plugin)

Pulls unprocessed shipments from Ditat TMS, downloads BOL/POD/Rate-Confirmation PDFs, cross-checks field values between the documents and the Ditat shipment record, and produces **one Word document** listing only the problematic shipments — ready to forward to ops or the customer.

The skill collapses the whole pipeline into ~4 sequential tool turns regardless of batch size, so processing a month's worth of shipments takes minutes, not hours.

---

## What the customer gets

1. A single `.docx` per run, saved at `<your project>/reports/ditat-verify-<YYYY-MM-DD-HHMM>.docx`. It contains:
   - A summary table — every shipment in the batch (OK / WARN / ISSUES / RC MISSING) with critical & warning counts and a doc-presence label (RC ✓ · BOL ✓ · POD ✗).
   - A detail section — **only** the problematic shipments, with route, dates, and the list of mismatched fields.
2. An updated `state.db` so the same shipments are not re-processed on the next run.
3. A short summary printed in the chat (verdicts + path to the docx).

That is it. No per-shipment files, no manual reconciliation.

---

## Prerequisites (one-time on the customer's machine)

| Requirement | Notes |
|---|---|
| **Claude Code** | Desktop app, VS Code extension, or CLI. Any current build. |
| **Python 3.10+** | `python --version`. Plugin auto-installs its Python deps on first activation. |
| **A Ditat API user** with credentials (`AccountID`, `ClientID`, `ClientSecret`) | Provisioned by the Ditat admin. The user must have **View** role on Shipment **and** on shipment Documents (sub-permission). Without the Documents role, doc lists come back empty. |

---

## Installation (customer's first time)

### 1. Add the plugin marketplace and install

In Claude Code, run:

```
/plugin marketplace add boxa-devops/ditat-verify-plugin
/plugin install ditat-verify@ditat-tools
```

A `SessionStart` hook will `pip install -r scripts/requirements.txt` automatically on first activation. It only re-installs when `requirements.txt` changes.

To update later:

```
/plugin marketplace update ditat-tools
/plugin update ditat-verify@ditat-tools
```

### 2. Configure credentials

In whichever folder the customer plans to run the skill from (their "project directory"), create a `.env` file:

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

Fill in the values issued by the Ditat admin:

```
DITAT_BASE_URL=https://tmsapi01.ditat.net
DITAT_ACCOUNT_ID=<your account id>
DITAT_CLIENT_ID=<your client id>
DITAT_CLIENT_SECRET=<your client secret>
```

`.env` should be git-ignored — never commit it.

### 3. Verify

In a Claude Code session inside that project directory:

```
ditat env check
```

Or directly. **On Windows use `py`** (not `python` — Windows ships a Microsoft Store shim that silently fails):

```powershell
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" check-env
```

macOS / Linux:
```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/ditat_verify.py" check-env
```

Expected: `"ok": true`. If `false`, the `missing` list tells you which env var still has a placeholder value.

### Project directory

The skill stores all per-customer state under whichever folder is `$CLAUDE_PROJECT_DIR` (or the current working directory if unset). First-time customers usually need to create this folder. Pick any directory you control, then:

PowerShell:
```powershell
New-Item -ItemType Directory -Force "$HOME\ditat-verify" | Out-Null
Set-Location "$HOME\ditat-verify"
$env:CLAUDE_PROJECT_DIR = (Get-Location).Path
```

Bash:
```bash
mkdir -p "$HOME/ditat-verify"
cd "$HOME/ditat-verify"
export CLAUDE_PROJECT_DIR="$PWD"
```

Then place `.env` in that directory. `state.db`, `downloads/`, and `reports/` will be created here on first run.

---

## Daily / weekly use

Just type one of these into Claude Code. The skill handles the rest end-to-end.

| User says | What happens |
|---|---|
| `verify ditat shipments` | Last 30 days of unprocessed shipments → docx |
| `verify last week` | Last 7 days |
| `verify last month` | Last 30 days |
| `verify last 14 days` | Custom window |
| `verify next 5 shipments` | First 5 unprocessed in default window |
| `verify shipment 9536` | Re-process one specific shipment (overrides processed flag) |
| `ditat verify status` | Last 20 processed shipments + verdict counts |
| `ditat env check` | Credential preflight |

The skill prints the docx path at the end. Open it in Word (or Google Docs, LibreOffice — standard `.docx`).

### How long does a run take?

- Downloads + cross-checks: ~1–2 seconds per shipment with default 5-worker parallelism.
- The single agent turn that reads PDFs in parallel: a few seconds per shipment of PDF content.
- A typical 50-shipment weekly run: well under 2 minutes.

If the Ditat API is slow or large PDFs need to be downloaded, allow more time. The token cache (12 fetches/hour limit) survives across runs.

---

## Scheduling (run it automatically every week)

For ops staff who want the docx waiting in their inbox every Monday morning without ever touching Claude Code manually. Uses **Windows Task Scheduler** — runs locally on the customer's machine, no cloud credentials, no extra services.

### Setup (one-time)

1. Make sure the prerequisites in §Installation are done — plugin installed, `.env` filled, `ditat env check` returns `ok: true`.
2. Open PowerShell. Navigate to the plugin's scheduling folder:
   ```powershell
   cd "$env:CLAUDE_PLUGIN_ROOT\scripts\scheduling"
   ```
3. Optionally edit `run-ditat-weekly.ps1` to change the project directory or the prompt (e.g. `verify last month` instead of `verify last week`).
4. Optionally edit `register-weekly-task.ps1` to change the day/time (default: Monday 09:00).
5. Run the registrar:
   ```powershell
   .\register-weekly-task.ps1
   ```
   Output should say `Task registered: DitatVerify-Weekly`.

That's it. Every Monday at 9:00 the task will:

1. Set `$CLAUDE_PROJECT_DIR` to the chosen folder.
2. Launch Claude Code headless (`claude --print --dangerously-skip-permissions "verify last week"`).
3. Run the skill end-to-end. The new `.docx` appears in `reports/`.
4. Append a one-line summary to `<project>\.scheduled-runs.log`.

### Verify it works

Trigger a manual test run:

```powershell
Start-ScheduledTask -TaskName "DitatVerify-Weekly"
```

Then check the log:

```powershell
Get-Content "$HOME\ditat-verify\.scheduled-runs.log" -Tail 10
```

Expected lines like:

```
2026-05-26 09:00:14  START prompt='verify last week' project=C:\Users\you\ditat-verify
2026-05-26 09:01:47  OK docx=C:\Users\you\ditat-verify\reports\ditat-verify-2026-05-26-0901.docx
```

### Change schedule or remove

To change day/time: edit `register-weekly-task.ps1` then re-run it. The script is idempotent (removes the old task first).

To remove entirely:

```powershell
.\unregister-weekly-task.ps1
```

### Limitations

- The task fires only **while the user is logged on** (so Claude Code can launch normally). It will not fire if the laptop is shut down or the user is signed out. To run when logged off, the task would need to store the user's password — not recommended.
- If the laptop is asleep at 9:00, the task fires when it wakes up (`StartWhenAvailable=true`).
- The customer must approve the very first `--dangerously-skip-permissions` flag once per session. Inside a scheduled headless run this is automatic.

### Optional: email the docx after each run

Add this block to the bottom of `run-ditat-weekly.ps1` if you want the docx emailed to ops automatically (uses Windows' built-in `Send-MailMessage` — SMTP creds required):

```powershell
if ($docx -and (Test-Path $docx)) {
    Send-MailMessage `
        -From "ops@yourcompany.com" `
        -To   "ops-team@yourcompany.com" `
        -Subject "Ditat weekly verification ($(Get-Date -Format 'yyyy-MM-dd'))" `
        -Body  "Attached: $docx" `
        -Attachments $docx `
        -SmtpServer "smtp.yourcompany.com" `
        -UseSsl
}
```

---

## How the verdicts work

Cross-checks are performed in Python with fixed tolerances — same answer every time.

| Field type | Critical (`ISSUES`) | Warning (`WARN`) |
|---|---|---|
| Weight | Δ > 5 % | 1–5 % |
| Dates | Δ > 1 day | 0 < Δ ≤ 1 day |
| Money (rate vs revenue) | Δ > $1.00 or > 1 % | — |
| BOL / load numbers | Any mismatch | Missing on one side |
| String fields (commodity, equipment, cities) | — | Mismatch after normalization |
| RC missing entirely | Verdict = `RC MISSING` (cannot cross-check) | — |

Cross-checks performed: **BOL ↔ Rate Confirmation**, **POD ↔ Rate Confirmation**, **Ditat record ↔ Rate Confirmation**, **BOL ↔ POD**.

Only shipments with verdict ISSUES, WARN, or RC MISSING appear in the detail section of the docx. OK shipments appear in the summary row only.

---

## Where everything lives

All state is under the customer's project directory (`$CLAUDE_PROJECT_DIR`), never inside the plugin — so plugin updates never wipe operational data.

| Path | Purpose |
|---|---|
| `.env` | Ditat credentials (git-ignore) |
| `.ditat_token_*.json` | Cached OAuth token (respects 12-fetch/hour limit) |
| `.ditat_batch.json` | Transient handoff between `fetch` and `finalize` (auto-deleted) |
| `state.db` | SQLite — processed-shipment ledger (key, id, verdict, counts, report path) |
| `downloads/<key>/` | Original PDFs pulled from Ditat |
| `reports/ditat-verify-*.docx` | The deliverables |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `check-env` reports `ok: false` | Missing or placeholder values in `.env`. Re-edit and rerun. |
| Empty document lists, helper logs "Documents View role not granted" | Ask Ditat admin to grant the **Documents View** sub-permission on the API user. |
| `429` rate-limit errors during a run | The helper backs off automatically. If it persists, wait an hour (token-fetch sliding window) or reduce `--workers`. |
| `python-docx` import error | First `SessionStart` hook may have failed. Run manually: `pip install -r "$env:CLAUDE_PLUGIN_ROOT\scripts\requirements.txt"`. |
| OCR-only PDFs unreadable | The skill lists them in `docs_missing`. Re-scan or manually attach a text-based version. |
| Want to re-process a shipment that was already marked | `verify shipment <KEY>` (overrides the processed flag for that one). |
| Need to wipe and start over | Delete `state.db` (and optionally `downloads/`, `reports/`). Credentials and token cache survive. |

---

## Privilege requirements (Ditat side)

The API user (`DITAT_CLIENT_ID`) needs:

- **View** role on **Shipment**
- **View** role on shipment **Documents** sub-permission — without it, document lists are empty
- Standard rate-limit tier (12 token fetches/hour covers ≤4 sweeps/day with the on-disk token cache)

The skill is **read-only against Ditat** by design — it does not write back, send Slack/email, or modify shipment records.

---

## Sub-commands (advanced / scripting)

The Python helper is fully usable standalone — each sub-command prints JSON on stdout, logs on stderr:

```
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" check-env
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" fetch --last-week [--limit 50] [--workers 5]
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" verify-one <SHIPMENT_KEY>
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" finalize --findings-file .ditat_findings.json
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" status
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" reset <SHIPMENT_KEY>
python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" mark <SHIPMENT_KEY> [--shipment-id ...] [--verdict ...] [--critical N] [--warn N]
```

`fetch` produces a slim JSON manifest plus a `.ditat_batch.json` sidecar. The Claude skill consumes the manifest, writes a `.ditat_findings.json` with extracted PDF fields, then calls `finalize`. The Python modules `ditat.diff` and `ditat.docx_report` are import-safe if you want to embed the logic in another tool.

---

## Update / uninstall

Update:
```
/plugin marketplace update ditat-tools
/plugin update ditat-verify@ditat-tools
```

Uninstall:
```
/plugin uninstall ditat-verify
```

State (`state.db`, `reports/`, `downloads/`, `.env`) lives in the customer's project directory and survives plugin updates and uninstalls. Delete those files manually if they should also be removed.
