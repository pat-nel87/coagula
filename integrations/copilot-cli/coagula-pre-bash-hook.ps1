# GitHub Copilot CLI preToolUse Bash rewriter (PowerShell port).
#
# Mirrors coagula-pre-bash-hook.sh. Rewrites noisy diagnostic Bash commands
# (kubectl, psql, az, etc.) to pipe through coagula before they execute.
#
# AUTO-DETECT: if Git Bash is available and the .sh sibling exists, defers
# to the bash implementation. Otherwise does the work natively.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# Defer to bash only when it can actually read Windows paths. WSL bash
# (uname -s == Linux) silently fails on C:\... siblings — skip it.
# Path-reject known WSL launcher locations before invocation; the
# WindowsApps reject covers Win11 default Store stubs which may hang
# or prompt on uname.
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

$siblingSh = Join-Path $PSScriptRoot 'coagula-pre-bash-hook.sh'
$bashExe   = Get-Command bash -ErrorAction SilentlyContinue
if ($bashExe -and (Test-Path $siblingSh) -and (Test-SafeBash $bashExe.Source)) {
    $stdin = [Console]::In.ReadToEnd()
    # Forward slashes — Git Bash handles C:/...; backslashes get escaped away.
    $siblingForBash = $siblingSh -replace '\\','/'
    $stdin | & $bashExe.Source $siblingForBash
    exit $LASTEXITCODE
}

# ---- Native PowerShell implementation -------------------------------------

$raw = [Console]::In.ReadToEnd()

# Default "allow unchanged" passthrough for any bail path.
$allowPassthrough = '{"permissionDecision":"allow"}'
function Out-Passthrough { Write-Output $allowPassthrough; exit 0 }

if ($env:COAGULA_DISABLE -eq '1') { Out-Passthrough }
if (-not (Get-Command coagula -ErrorAction SilentlyContinue)) { Out-Passthrough }

try {
    $payload = $raw | ConvertFrom-Json -ErrorAction Stop
} catch {
    Out-Passthrough
}

if ([string]$payload.toolName -ne 'bash') { Out-Passthrough }

# toolArgs is a JSON string in preToolUse (per empirical probe of Copilot CLI).
$argsObj = $null
if ($payload.toolArgs -is [string]) {
    try { $argsObj = $payload.toolArgs | ConvertFrom-Json -ErrorAction Stop } catch { Out-Passthrough }
} else {
    $argsObj = $payload.toolArgs
}

$command = if ($argsObj) { [string]$argsObj.command } else { '' }
if ([string]::IsNullOrEmpty($command)) { Out-Passthrough }

# Already piped through coagula? Don't double-wrap.
if ($command -match '\|\s?coagula(\s|$)') { Out-Passthrough }

# Match against built-in noisy command list.
$defaultPattern = '^(kubectl|oc|helm|psql|mysql|sqlite3|az|gcloud|aws|curl|wget|Invoke-WebRequest|Invoke-RestMethod|iwr|irm|gh api|journalctl|dmesg|ps |netstat|lsof|iptables|systemctl|docker (ps|inspect|logs)|terraform (show|plan))\s'
$extraPattern   = $env:COAGULA_NOISY_PATTERNS

$stripped = $command.TrimStart()
$matched = $false
if ($stripped -imatch $defaultPattern) { $matched = $true }
elseif ($extraPattern -and $stripped -imatch ('^(' + $extraPattern + ')\s')) { $matched = $true }

if (-not $matched) { Out-Passthrough }

# Profile selection.
$profile = 'passthrough'
switch -Regex ($stripped) {
    '^(kubectl|oc|helm)\s'     { $profile = 'k8s'; break }
    '^(psql|mysql|sqlite3)\s'  { $profile = 'postgres'; break }
    '^(az|gcloud|aws)\s'       { $profile = 'azure'; break }
}

# Query derivation — empty means lite mode (omit --query so the CLI
# skips Relevance + Summarize against a meaningless string).
$query = $env:COAGULA_QUERY
if ([string]::IsNullOrEmpty($query)) { $query = $env:COAGULA_TASK }

$budget = if ($env:COAGULA_BUDGET) { [int]$env:COAGULA_BUDGET } else { 2000 }
$keep   = if ($env:COAGULA_KEEP)   { [int]$env:COAGULA_KEEP }   else { 5 }

if ([string]::IsNullOrEmpty($query)) {
    $rewritten = "( $command ) 2>&1 | coagula --profile $profile --budget $budget --keep $keep"
} else {
    $escaped = $query -replace "'", "'\''"
    $quotedQuery = "'$escaped'"
    $rewritten = "( $command ) 2>&1 | coagula --query $quotedQuery --profile $profile --budget $budget --keep $keep"
}

# Merge into the original args object so other fields (description, initial_wait, …) survive.
$newArgs = @{}
foreach ($p in $argsObj.PSObject.Properties) { $newArgs[$p.Name] = $p.Value }
$newArgs.command = $rewritten

# Debug log — append-only, transform-only entries. See post-tool hook
# for env var details.
$logPath = if ($env:COAGULA_DEBUG_LOG) { $env:COAGULA_DEBUG_LOG } `
           else { Join-Path $env:USERPROFILE '.copilot\coagula-debug.log' }
if (@('off','OFF','disabled','DISABLED','0') -notcontains $logPath) {
    $msg = "$(Get-Date -Format 'o') [pre-bash] rewrote (profile=$profile): $command"
    Add-Content -LiteralPath $logPath -Value $msg -ErrorAction SilentlyContinue
}

$response = @{
    permissionDecision = 'allow'
    modifiedArgs       = $newArgs
    additionalContext  = "[coagula] funneled output of: $command"
} | ConvertTo-Json -Depth 5 -Compress

Write-Output $response
