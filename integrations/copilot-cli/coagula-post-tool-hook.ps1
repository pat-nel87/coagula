# GitHub Copilot CLI postToolUse universal interceptor (PowerShell port).
#
# Mirrors coagula-post-tool-hook.sh. Catches the result of any tool above a
# token threshold and replaces it via modifiedResult so the model only sees
# the funneled version.
#
# AUTO-DETECT: if Git Bash is available AND the .sh sibling exists, defers
# to the bash implementation (single source of truth). Otherwise does the
# work natively in PowerShell.
#
# Wire it up via ~/.copilot/hooks/coagula.json with BOTH bash and powershell
# fields — the Copilot CLI host auto-picks per platform:
#
#   {
#     "version": 1,
#     "hooks": {
#       "postToolUse": [
#         {
#           "type": "command",
#           "bash":       "/path/to/coagula-post-tool-hook.sh",
#           "powershell": "powershell -NoProfile -File C:\\path\\to\\coagula-post-tool-hook.ps1"
#         }
#       ]
#     }
#   }
#
# Env knobs match the bash version: COAGULA_QUERY, COAGULA_BUDGET, COAGULA_KEEP,
# COAGULA_THRESHOLD, COAGULA_SKIP_TOOLS, COAGULA_DISABLE.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# ---- Auto-detect: defer to bash if it's a Windows-path-aware bash ---------
# `Get-Command bash` on Windows can resolve to:
#   - C:\Windows\System32\bash.exe       (WSL launcher, classic)
#   - %LOCALAPPDATA%\Microsoft\WindowsApps\bash.exe   (WSL Store stub, Win11 default)
# Both run Linux which cannot read C:\... paths — calling our .sh sibling
# through them silently fails. Defer only when bash is a Windows-native
# flavor (Git Bash MINGW / Cygwin / MSYS). On macOS/Linux any bash is fine.
function Test-SafeBash {
    param([string]$BashPath)
    if (-not $BashPath) { return $false }
    # Hard reject known WSL launcher locations before invocation. The
    # WindowsApps reject is critical on Win11 — that's the default Store
    # stub location and a uname probe there can hang or prompt.
    if ($BashPath -match '\\System32\\(bash|wsl)\.exe$') { return $false }
    if ($BashPath -match '\\WindowsApps\\') { return $false }
    $onWindows = [System.Environment]::OSVersion.Platform -eq 'Win32NT'
    if (-not $onWindows) { return $true }
    # Secondary safety on Windows: a bash binary in some unexpected
    # location that turns out to be WSL/cmder/etc. -> uname -s tells us.
    try {
        $u = & $BashPath -c 'uname -s' 2>$null
        return ($u -match '^(MINGW|CYGWIN|MSYS)')
    } catch { return $false }
}

$siblingSh = Join-Path $PSScriptRoot 'coagula-post-tool-hook.sh'
$bashExe   = Get-Command bash -ErrorAction SilentlyContinue
if ($bashExe -and (Test-Path $siblingSh) -and (Test-SafeBash $bashExe.Source)) {
    # Read stdin, forward to the bash impl, return its stdout verbatim.
    # Convert backslashes -> forward slashes; Git Bash handles C:/... but
    # backslashes get interpreted as escapes and silently strip the path.
    $stdin = [Console]::In.ReadToEnd()
    $siblingForBash = $siblingSh -replace '\\','/'
    $stdin | & $bashExe.Source $siblingForBash
    exit $LASTEXITCODE
}

# ---- Native PowerShell implementation -------------------------------------

# Read stdin payload from Copilot CLI.
$raw = [Console]::In.ReadToEnd()

function Out-NoOp { Write-Output '{}'; exit 0 }

if ($env:COAGULA_DISABLE -eq '1') { Out-NoOp }
if (-not (Get-Command coagula -ErrorAction SilentlyContinue)) { Out-NoOp }

try {
    $payload = $raw | ConvertFrom-Json -ErrorAction Stop
} catch {
    Out-NoOp
}

$toolName   = [string]$payload.toolName
$resultType = [string]$payload.toolResult.resultType
$resultText = [string]$payload.toolResult.textResultForLlm

if ([string]::IsNullOrEmpty($toolName) -or
    [string]::IsNullOrEmpty($resultText) -or
    $resultType -ne 'success') { Out-NoOp }

# Always-skipped internal bookkeeping tools (match the bash impl).
$alwaysSkip = @('report_intent', 'sql', 'notification')
if ($alwaysSkip -contains $toolName) { Out-NoOp }
if ($toolName -like 'todo_*') { Out-NoOp }

if ($env:COAGULA_SKIP_TOOLS) {
    $extra = $env:COAGULA_SKIP_TOOLS -split ',' | ForEach-Object { $_.Trim() }
    if ($extra -contains $toolName) { Out-NoOp }
}

# Threshold check (chars / 4 token estimate — matches coagula.tokens fallback).
$threshold = if ($env:COAGULA_THRESHOLD) { [int]$env:COAGULA_THRESHOLD } else { 2000 }
$resultChars  = $resultText.Length
$resultTokens = [int]($resultChars / 4)
if ($resultTokens -lt $threshold) { Out-NoOp }

# Idempotency guard: skip if the result already looks like coagula output.
$head = if ($resultChars -ge 200) { $resultText.Substring(0, 200) } else { $resultText }
if ($head -match '(?m)^### ') { Out-NoOp }

# Derive query — empty means lite mode (omit --query so the CLI skips
# Relevance + Summarize against a meaningless string).
$query = $env:COAGULA_QUERY
if ([string]::IsNullOrEmpty($query)) { $query = $env:COAGULA_TASK }

# Profile auto-detection for shell-family tools. Without this, Windows
# `kubectl` calls (toolName='powershell') would funnel with the empty
# passthrough denylist instead of the k8s denylist that does the actual
# pruning work. Matches the pre-tool hook's toolName allowlist.
$profile = 'passthrough'
if (@('bash','shell','powershell') -contains $toolName) {
    # toolArgs may be either a JSON string or a parsed object.
    $argsObj = $null
    if ($payload.toolArgs -is [string]) {
        try { $argsObj = $payload.toolArgs | ConvertFrom-Json -ErrorAction Stop } catch {}
    } else {
        $argsObj = $payload.toolArgs
    }
    $command = if ($argsObj) { [string]$argsObj.command } else { '' }
    switch -Regex ($command) {
        '^(kubectl|oc|helm)\s'           { $profile = 'k8s'; break }
        '^(psql|mysql|sqlite3)\s'        { $profile = 'postgres'; break }
        '^(az|gcloud|aws)\s'             { $profile = 'azure'; break }
    }
}

$budget = if ($env:COAGULA_BUDGET) { [int]$env:COAGULA_BUDGET } else { 2000 }
$keep   = if ($env:COAGULA_KEEP)   { [int]$env:COAGULA_KEEP }   else { 5 }

# Funnel via the coagula CLI. On any error, fall through to no-op.
# Omit --query when empty so the CLI runs lite mode.
try {
    if ([string]::IsNullOrEmpty($query)) {
        $cleanedRaw = $resultText | & coagula --profile $profile --budget $budget --keep $keep 2>$null
    } else {
        $cleanedRaw = $resultText | & coagula --query $query --profile $profile --budget $budget --keep $keep 2>$null
    }
    if ($LASTEXITCODE -ne 0) { Out-NoOp }
} catch {
    Out-NoOp
}

# PowerShell captures multi-line external-command stdout as an
# Object[] (one element per output line) — NOT as a single string.
# Without an explicit -join, three things break together:
#   1. `$cleaned.Length` returns line count, not char count → reported
#      token counts are off by orders of magnitude (the "X -> 1 tok"
#      log entries every Windows user has been seeing).
#   2. `if ($cleaned.Length -ge $resultChars)` compares line-count to
#      char-count → the not-actually-smaller guard never triggers, so
#      garbled output always gets swapped in.
#   3. `"[coagula: ...]`n$cleaned"` stringifies the array with SPACE
#      separators, collapsing all the `### source\n\ncontent` newlines
#      to spaces. The model sees structural headers inline with content.
# The Bash version is fine — $(cmd) captures as a single string.
# Always -join so downstream code works on real char counts and newlines.
if ($null -eq $cleanedRaw) { Out-NoOp }
$cleaned = if ($cleanedRaw -is [array]) { $cleanedRaw -join "`n" } else { [string]$cleanedRaw }
if ([string]::IsNullOrEmpty($cleaned)) { Out-NoOp }

# Skip the swap if not actually smaller (in chars, not lines).
if ($cleaned.Length -ge $resultChars) { Out-NoOp }

$cleanedTokens = [int]($cleaned.Length / 4)
$finalText = "[coagula: $resultTokens -> $cleanedTokens tok | tool=$toolName profile=$profile]`n$cleaned"

# Debug log — append-only, transform-only entries. Disable with
# COAGULA_DEBUG_LOG=off, override path with COAGULA_DEBUG_LOG=<path>.
# Default: ~/.copilot/coagula-debug.log (silently no-ops if dir missing).
$homeDir = if ($env:USERPROFILE) { $env:USERPROFILE } elseif ($env:HOME) { $env:HOME } else { '' }
$logPath = if ($env:COAGULA_DEBUG_LOG) {
    $env:COAGULA_DEBUG_LOG
} elseif ($homeDir) {
    Join-Path $homeDir '.copilot/coagula-debug.log'
} else {
    ''
}
if (@('off','OFF','disabled','DISABLED','0') -notcontains $logPath) {
    $msg = "$(Get-Date -Format 'o') [post-tool] funneled (tool=$toolName profile=$profile): $resultTokens -> $cleanedTokens tok"
    Add-Content -LiteralPath $logPath -Value $msg -ErrorAction SilentlyContinue
}

$response = @{
    modifiedResult = @{
        resultType       = 'success'
        textResultForLlm = $finalText
    }
} | ConvertTo-Json -Depth 5 -Compress

Write-Output $response
