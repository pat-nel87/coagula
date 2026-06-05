# GitHub Copilot CLI postToolUse universal interceptor (PowerShell port).
#
# Mirrors coagula-post-tool-hook.sh. Catches the result of any tool above
# the per-tool token threshold (or session cumulative threshold) and
# replaces it via modifiedResult so the model only sees the funneled
# version.
#
# AUTO-DETECT: if Git Bash is available AND the .sh sibling exists, defers
# to the bash implementation (single source of truth). Otherwise does the
# work natively in PowerShell.
#
# Env knobs match the bash version: COAGULA_QUERY, COAGULA_BUDGET,
# COAGULA_KEEP, COAGULA_THRESHOLD (overrides per-tool defaults),
# COAGULA_CUMULATIVE_THRESHOLD, COAGULA_SKIP_TOOLS, COAGULA_DISABLE,
# COAGULA_DEBUG_LOG.

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
    if ($BashPath -match '\\System32\\(bash|wsl)\.exe$') { return $false }
    if ($BashPath -match '\\WindowsApps\\') { return $false }
    $onWindows = [System.Environment]::OSVersion.Platform -eq 'Win32NT'
    if (-not $onWindows) { return $true }
    try {
        $u = & $BashPath -c 'uname -s' 2>$null
        return ($u -match '^(MINGW|CYGWIN|MSYS)')
    } catch { return $false }
}

$siblingSh = Join-Path $PSScriptRoot 'coagula-post-tool-hook.sh'
$bashExe   = Get-Command bash -ErrorAction SilentlyContinue
if ($bashExe -and (Test-Path $siblingSh) -and (Test-SafeBash $bashExe.Source)) {
    $stdin = [Console]::In.ReadToEnd()
    $siblingForBash = $siblingSh -replace '\\','/'
    $stdin | & $bashExe.Source $siblingForBash
    exit $LASTEXITCODE
}

# ---- Native PowerShell implementation -------------------------------------

$raw = [Console]::In.ReadToEnd()

# ---- Logging — every decision exit goes through Write-Decision ------------
$homeDir = if ($env:USERPROFILE) { $env:USERPROFILE } elseif ($env:HOME) { $env:HOME } else { '' }
$script:LogPath = if ($env:COAGULA_DEBUG_LOG) {
    $env:COAGULA_DEBUG_LOG
} elseif ($homeDir) {
    Join-Path $homeDir '.copilot/coagula-debug.log'
} else {
    ''
}

function Write-Decision {
    param([string]$Message)
    if (-not $script:LogPath) { return }
    if (@('off','OFF','disabled','DISABLED','0') -contains $script:LogPath) { return }
    $line = "$(Get-Date -Format 'o') [post-tool] $Message"
    Add-Content -LiteralPath $script:LogPath -Value $line -ErrorAction SilentlyContinue
}

function Out-NoOp {
    param([string]$LogMessage = '')
    if ($LogMessage) { Write-Decision -Message $LogMessage }
    Write-Output '{}'
    exit 0
}

# ---- Per-tool threshold table — matches bash impl -------------------------
function Get-ThresholdForTool {
    param([string]$Tool)
    if ($env:COAGULA_THRESHOLD) { return [int]$env:COAGULA_THRESHOLD }
    switch -Regex ($Tool) {
        '^(bash|shell|powershell|read_powershell)$' { return 2000 }
        '^(view|read|read_file|str_replace_based_edit_tool)$' { return 500 }
        '^(mcp:|.*__)' { return 1000 }
        default        { return 1500 }
    }
}

# ---- Session state — per-PPID cumulative token counter --------------------
# Stored as a JSON file keyed on parent process ID (the copilot CLI process).
# Atomic writes via tmp + Move-Item to avoid torn reads.
$sessionDir  = if ($homeDir) { Join-Path $homeDir '.copilot/coagula-session-state' } else { $null }
$ppid        = $PID  # for this PS process; we'll resolve parent below
try {
    $parent = (Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$PID" -ErrorAction SilentlyContinue).ParentProcessId
    if ($parent) { $ppid = $parent }
} catch { }
$sessionFile = if ($sessionDir) { Join-Path $sessionDir "$ppid.json" } else { $null }
$cumulativeThreshold = if ($env:COAGULA_CUMULATIVE_THRESHOLD) {
    [int]$env:COAGULA_CUMULATIVE_THRESHOLD
} else { 8000 }

function Get-SessionTotal {
    if (-not $sessionFile -or -not (Test-Path -LiteralPath $sessionFile)) { return 0 }
    try {
        $obj = Get-Content -LiteralPath $sessionFile -Raw | ConvertFrom-Json
        $now = [int][double]::Parse((Get-Date -UFormat %s))
        if (-not $obj.updated_at -or ($now - [int]$obj.updated_at) -gt 3600) { return 0 }
        return [int]$obj.total_tokens
    } catch { return 0 }
}

function Add-SessionTokens {
    param([int]$Tokens)
    if (-not $sessionFile -or -not $sessionDir) { return 0 }
    if (-not (Test-Path -LiteralPath $sessionDir)) {
        New-Item -ItemType Directory -Force -Path $sessionDir -ErrorAction SilentlyContinue | Out-Null
    }
    $prev = Get-SessionTotal
    $total = $prev + $Tokens
    $now   = [int][double]::Parse((Get-Date -UFormat %s))
    $tmp   = "$sessionFile.tmp.$PID"
    try {
        @{ updated_at = $now; total_tokens = $total } |
            ConvertTo-Json -Compress |
            Set-Content -LiteralPath $tmp -Encoding UTF8 -ErrorAction Stop
        Move-Item -LiteralPath $tmp -Destination $sessionFile -Force -ErrorAction Stop
    } catch {
        Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
    }
    return $total
}

# Best-effort cleanup of stale session files (~2% of calls).
if ((Get-Random -Maximum 50) -eq 0 -and $sessionDir -and (Test-Path -LiteralPath $sessionDir)) {
    Get-ChildItem -LiteralPath $sessionDir -Filter '*.json' -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddHours(-1) } |
        Remove-Item -ErrorAction SilentlyContinue
}

# ---- Early-exit guards ----------------------------------------------------
if ($env:COAGULA_DISABLE -eq '1') { Out-NoOp 'disabled (COAGULA_DISABLE=1)' }
if (-not (Get-Command coagula -ErrorAction SilentlyContinue)) {
    Out-NoOp 'coagula-missing (not on PATH — hooks are no-ops, install coagula globally to fix)'
}

try {
    $payload = $raw | ConvertFrom-Json -ErrorAction Stop
} catch {
    # No log — too noisy for non-tool events.
    Write-Output '{}'; exit 0
}

$toolName   = [string]$payload.toolName
$resultType = [string]$payload.toolResult.resultType
$resultText = [string]$payload.toolResult.textResultForLlm

if ([string]::IsNullOrEmpty($toolName) -or
    [string]::IsNullOrEmpty($resultText) -or
    $resultType -ne 'success') {
    Write-Output '{}'; exit 0
}

# Always-skipped internal bookkeeping tools.
$alwaysSkip = @('report_intent', 'sql', 'notification')
if ($alwaysSkip -contains $toolName -or $toolName -like 'todo_*') {
    Out-NoOp "skipped (tool=$toolName reason=internal-bookkeeping)"
}

# User-configured extra skips.
if ($env:COAGULA_SKIP_TOOLS) {
    $extra = $env:COAGULA_SKIP_TOOLS -split ',' | ForEach-Object { $_.Trim() }
    if ($extra -contains $toolName) {
        Out-NoOp "skipped (tool=$toolName reason=user-skip-list)"
    }
}

$resultChars  = $resultText.Length
$resultTokens = [int]($resultChars / 4)
$perCallThreshold = Get-ThresholdForTool -Tool $toolName

# Idempotency guard: skip if the result already looks like coagula's output.
$head = if ($resultChars -ge 200) { $resultText.Substring(0, 200) } else { $resultText }
if ($head -match '(?m)^### ') {
    Out-NoOp "skipped (tool=$toolName reason=already-funneled)"
}

# ---- Threshold decision: per-call OR cumulative-trigger -------------------
$sessionTotal  = Get-SessionTotal
$triggerReason = $null

if ($resultTokens -ge $perCallThreshold) {
    $triggerReason = "per-call($resultTokens>=$perCallThreshold)"
} elseif ($cumulativeThreshold -gt 0 -and ($sessionTotal + $resultTokens) -ge $cumulativeThreshold) {
    $triggerReason = "cumulative($sessionTotal+$resultTokens>=$cumulativeThreshold)"
} else {
    $newTotal = Add-SessionTokens -Tokens $resultTokens
    Out-NoOp "under-threshold (tool=$toolName tokens=$resultTokens per_call=$perCallThreshold cumulative=$newTotal/$cumulativeThreshold)"
}

# ---- Profile auto-detection for shell-family tools ------------------------
$query = $env:COAGULA_QUERY
if ([string]::IsNullOrEmpty($query)) { $query = $env:COAGULA_TASK }

$profile = 'passthrough'
if (@('bash','shell','powershell') -contains $toolName) {
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

# ---- Funnel via the coagula CLI ------------------------------------------
try {
    if ([string]::IsNullOrEmpty($query)) {
        $cleanedRaw = $resultText | & coagula --profile $profile --budget $budget --keep $keep 2>$null
    } else {
        $cleanedRaw = $resultText | & coagula --query $query --profile $profile --budget $budget --keep $keep 2>$null
    }
    if ($LASTEXITCODE -ne 0) {
        Out-NoOp "funnel-error (tool=$toolName exit=$LASTEXITCODE) — passing through unchanged"
    }
} catch {
    Out-NoOp "funnel-exception (tool=$toolName) — passing through unchanged"
}

# PowerShell captures multi-line external-command stdout as an Object[]
# (one element per line) — NOT as a single string. Always -join so
# downstream char-count guards work on real characters not line counts.
if ($null -eq $cleanedRaw) {
    Out-NoOp "funnel-empty (tool=$toolName)"
}
$cleaned = if ($cleanedRaw -is [array]) { $cleanedRaw -join "`n" } else { [string]$cleanedRaw }
if ([string]::IsNullOrEmpty($cleaned)) {
    Out-NoOp "funnel-empty (tool=$toolName)"
}

if ($cleaned.Length -ge $resultChars) {
    Out-NoOp "funnel-noop (tool=$toolName reason=no-shrink in=$resultChars`c out=$($cleaned.Length)c)"
}

$cleanedTokens = [int]($cleaned.Length / 4)
$newTotal = Add-SessionTokens -Tokens $cleanedTokens

$finalText = "[coagula: $resultTokens -> $cleanedTokens tok | tool=$toolName profile=$profile]`n$cleaned"

Write-Decision -Message "fired (tool=$toolName profile=$profile in=$resultTokens out=$cleanedTokens reason=$triggerReason cumulative=$newTotal)"

$response = @{
    modifiedResult = @{
        resultType       = 'success'
        textResultForLlm = $finalText
    }
} | ConvertTo-Json -Depth 5 -Compress

Write-Output $response
