---
name: ditat-setup
description: First-time onboarding for the Ditat verification plugin. Guides a new client through choosing where to store the project, naming it, scaffolding the folder + .env, filling the server URL and API key, and validating the connection. Trigger when the user says "set up ditat", "ditat onboarding", "configure ditat", "first time setup", "/ditat-setup", "get me started with ditat", or runs the plugin with no project configured yet.
---

# Ditat Verification — Onboarding

Walk a brand-new client from nothing to a working, validated setup in a few
guided steps. Be friendly and concrete — they may be non-technical ops staff.

Helper script: `${CLAUDE_PLUGIN_ROOT}/scripts/ditat_verify.py` (Windows: `py`,
macOS/Linux: `python3`).

## Step 1 — Where to store the project

Ask the user where to keep their Ditat workspace. Use AskUserQuestion with these
options (resolve the actual paths for their OS first):

- **Desktop** (recommended) — `~/Desktop` / `%USERPROFILE%\Desktop`
- **Home folder** — `~`
- **Documents** — `~/Documents`
- (the user can pick "Other" and type a path)

Confirm the Desktop path actually exists; if not, fall back to the home folder.

## Step 2 — Name the project

Ask for a project name. Default / recommended: **`ditat-verify`**. Offer it as
the first option and let them choose "Other" for a custom name. Sanitize to a
safe folder name (letters, digits, dashes, underscores).

The project directory is `<location>/<name>`.

## Step 3 — Scaffold the folder + .env

Run the helper to create the directory, a `reports/` folder, and a `.env` from
the template:

PowerShell:
```
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" init "C:\Users\<you>\Desktop\ditat-verify"
```
Bash:
```
python3 "$CLAUDE_PLUGIN_ROOT/scripts/ditat_verify.py" init "$HOME/Desktop/ditat-verify"
```

Then `cd` into it and set the project dir for this session:
```
$env:CLAUDE_PROJECT_DIR = "C:\Users\<you>\Desktop\ditat-verify"   # PowerShell
export CLAUDE_PROJECT_DIR="$HOME/Desktop/ditat-verify"            # Bash
```

The JSON output lists `env_path` and `needs_filling` (which vars are still blank).

## Step 4 — Fill the .env

The client needs two values from whoever runs the verification **server**:
- `DITAT_SERVER_URL` — e.g. `https://ditat-verify-server-production.up.railway.app`
- `DITAT_SERVER_API_KEY` — the shared key the server expects

Two ways — let the user choose:
1. **They paste the values here** → you write them into the `.env` with Edit.
   (The API key is a secret; if they'd rather not paste it in chat, use option 2.)
2. **They edit the file** → open `<project>/.env` for them (e.g. `notepad`,
   `open`, or just tell them the path) and have them fill the two values, then
   say "done".

Only `DITAT_SERVER_URL` and `DITAT_SERVER_API_KEY` are required. Leave the
optional vars as-is.

## Step 5 — Validate

```
py "$env:CLAUDE_PLUGIN_ROOT\scripts\ditat_verify.py" check-server
```

- `ok: true` + a `health` block → setup complete. 🎉
- `missing: ["DITAT_SERVER_URL"]` → not filled yet; back to Step 4.
- `health_status: 401` → the API key doesn't match the server's; fix it.
- `health: misconfigured` → the SERVER is missing its Ditat credentials; that's
  the server operator's job, not the client's.

## Step 6 — Hand off

Once validated, tell the user they're ready and how to run it day-to-day:
- "verify last week" / "verify last month" / "verify ditat shipments"

Remind them: this folder (`$CLAUDE_PROJECT_DIR`) holds their `.env` and the
`reports/` deliverables. For automated weekly runs, point them at
`${CLAUDE_PLUGIN_ROOT}/scripts/scheduling/` (Windows Task Scheduler).

## Notes

- **Persisting `$CLAUDE_PROJECT_DIR`.** Setting it as above lasts for the current
  session only. If they want it permanent, add it to their shell profile or the
  scheduled-task script — don't block onboarding on this.
- **Don't dump state in the plugin folder.** Always create a customer-owned
  directory (Step 1), never under `$CLAUDE_PLUGIN_ROOT`.
- After onboarding, the normal verification flow lives in the **ditat-verify**
  skill.
