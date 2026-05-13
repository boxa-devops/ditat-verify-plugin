# ditat-verify (Claude Code plugin)

Pulls unprocessed shipments from Ditat TMS, downloads BOL/POD/Rate-Confirmation PDFs, cross-checks fields between documents and the Ditat shipment record, and writes a per-shipment markdown report.

## Install

This repo is a self-marketplace. Two-step install:

```
/plugin marketplace add boxa-devops/ditat-verify-plugin
/plugin install ditat-verify@ditat-tools
```

For local testing before push:
```
/plugin marketplace add C:\Users\imoma\OneDrive\Рабочий стол\ditat-verify-plugin
/plugin install ditat-verify@ditat-tools
```

A `SessionStart` hook will `pip install -r scripts/requirements.txt` on first activation. Re-installs only when `requirements.txt` changes.

## Update

```
/plugin marketplace update ditat-tools
/plugin update ditat-verify@ditat-tools
```

## Setup (per user)

1. In your working project directory, copy the env template:
   - PowerShell: `Copy-Item "$env:CLAUDE_PLUGIN_ROOT\.env.example" .env`
   - Bash:       `cp "$CLAUDE_PLUGIN_ROOT/.env.example" .env`
2. Fill in `DITAT_ACCOUNT_ID`, `DITAT_CLIENT_ID`, `DITAT_CLIENT_SECRET` (issued by Ditat admin).
3. Verify:
   ```
   python "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" check-env
   ```
   Expect `"ok": true`.

## Usage in Claude Code

Trigger the skill by typing any of:

- `verify ditat shipments` — process next batch of unprocessed shipments
- `verify shipment <KEY>` — process one specific shipment (re-runs if already marked)
- `ditat verify status` — show last 20 processed shipments
- `ditat env check` — credential preflight

## Where data lives

All state is under your project dir (`$CLAUDE_PROJECT_DIR`), never inside the plugin:

| File / dir                       | Purpose                                  |
|----------------------------------|------------------------------------------|
| `.env`                           | Ditat credentials (gitignore this)       |
| `.ditat_token_*.json`            | Cached OAuth token (12-fetch/hr limit)   |
| `state.db`                       | SQLite — processed-shipment ledger       |
| `downloads/<key>/`               | Original PDFs pulled from Ditat          |
| `reports/<key>.md`               | Per-shipment verification report         |

## Privilege requirements

The Ditat API user (`DITAT_CLIENT_ID`) needs:

- **View** role on Shipment
- **View** role on shipment Documents (sub-permission) — without it, document list comes back empty
- Standard rate-limit tier (12 token fetches/hour is enough for ≤4 sweeps/day)

If the documents View role is missing, the helper logs a warning and the report records "no documents fetched".

## Updating

`git pull` in the plugin install dir (or re-install). State + reports live in your project dir and are untouched by updates.

## Uninstall

`/plugin uninstall ditat-verify`. State files in your project dir remain — delete them manually if no longer needed.
