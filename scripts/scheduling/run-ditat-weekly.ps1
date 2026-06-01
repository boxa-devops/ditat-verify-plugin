<#
.SYNOPSIS
Headless weekly run of /ditat-verify in Claude Code.

.DESCRIPTION
Invoked by Windows Task Scheduler. Sets the Claude Code working directory,
fires the skill non-interactively, captures the docx path from stdout, and
appends a one-line summary to a rolling log file.

.NOTES
This script is meant to be configured ONCE by editing the variables in the
"User settings" block below. After that, register-weekly-task.ps1 wires it
into Task Scheduler and it runs on its own forever.
#>

# ------------------------------------------------------------------ User settings

# Where the customer's `.env`, `reports/`, `downloads/` live.
# This is the same folder you cd into when running the skill manually.
$ProjectDir = "$HOME\ditat-verify"

# Prompt that triggers the skill. Defaults to last week. Change to
# "verify last month" if you prefer a wider window.
$Prompt = "verify last week"

# Path to claude.exe. Usually auto-discovered, but set explicitly if you
# installed Claude Code to a custom location.
$ClaudeExe = ""

# Log file (rolling, plain text, ~1 line per run).
$LogFile = "$ProjectDir\.scheduled-runs.log"

# ------------------------------------------------------------------ Implementation

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING   = "utf-8"
$env:CLAUDE_PROJECT_DIR = $ProjectDir

function Write-Log([string]$Message) {
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    "$stamp  $Message" | Out-File -Append -Encoding utf8 -FilePath $LogFile
}

if (-not (Test-Path $ProjectDir)) {
    New-Item -ItemType Directory -Force -Path $ProjectDir | Out-Null
}

# Auto-discover claude.exe if not pinned
if (-not $ClaudeExe -or -not (Test-Path $ClaudeExe)) {
    $cmd = Get-Command claude -ErrorAction SilentlyContinue
    if ($cmd) { $ClaudeExe = $cmd.Source }
}
if (-not $ClaudeExe -or -not (Test-Path $ClaudeExe)) {
    Write-Log "FAIL: claude.exe not found on PATH. Set `$ClaudeExe at the top of run-ditat-weekly.ps1."
    exit 2
}

Write-Log "START prompt='$Prompt' project=$ProjectDir"

# Run Claude Code headless. --print exits after one turn cycle.
# --dangerously-skip-permissions: scheduled task pre-trusts this invocation;
# all permission prompts auto-approve. Without this flag the task would hang
# on the first tool call.
Set-Location $ProjectDir
$raw = & $ClaudeExe --print --dangerously-skip-permissions $Prompt 2>&1
$exitCode = $LASTEXITCODE

# Extract the docx path if the skill printed one (matches our finalize output).
$docx = $null
foreach ($line in ($raw -split "`n")) {
    if ($line -match '"docx"\s*:\s*"([^"]+)"') {
        $docx = $Matches[1] -replace '\\\\', '\'
        break
    }
    elseif ($line -match '(reports[\\/].+?\.docx)') {
        $docx = $Matches[1]
    }
}

if ($exitCode -eq 0 -and $docx) {
    Write-Log "OK docx=$docx"
    exit 0
}
elseif ($exitCode -eq 0) {
    Write-Log "OK (no docx in output — likely 'no unprocessed shipments')"
    exit 0
}
else {
    Write-Log "FAIL exit=$exitCode — see transcript below"
    Write-Log ("------ claude stdout/stderr ------`n" + ($raw -join "`n"))
    exit $exitCode
}
