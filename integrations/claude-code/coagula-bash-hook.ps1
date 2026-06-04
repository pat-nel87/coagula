# Claude Code PreToolUse Bash hook (PowerShell port).
#
# Mirrors coagula-bash-hook.sh. Rewrites noisy diagnostic commands the model
# is about to run via Claude Code's Bash tool, piping them through coagula
# before they execute. Same logic also fires in VSCode Copilot Chat (which
# reads .claude/settings.json).
#
# AUTO-DETECT: if Git Bash is available and the .sh sibling exists, defers
# to the bash implementation. Otherwise does the work natively.
#
# Wire it up in ~/.claude/settings.json with OS-specific commands:
#
#   {
#     "hooks": {
#       "PreToolUse": [{
#         "matcher": "Bash",
#         "hooks": [{
#           "type": "command",
#           "command": "powershell -NoProfile -File C:\\Users\\you\\.claude\\coagula-bash-hook.ps1"
#         }]
#       }]
#     }
#   }
#
# (On macOS/Linux, use coagula-bash-hook.sh in the same `command` field.)

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

# Defer to bash only when it can actually read Windows paths. WSL bash
# (uname -s == Linux) silently fails on C:\... siblings — skip it.
# WindowsApps reject covers Win11 default Store stubs.
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

$siblingSh = Join-Path $PSScriptRoot 'coagula-bash-hook.sh'
$bashExe   = Get-Command bash -ErrorAction SilentlyContinue
if ($bashExe -and (Test-Path $siblingSh) -and (Test-SafeBash $bashExe.Source)) {
    $stdin = [Console]::In.ReadToEnd()
    $siblingForBash = $siblingSh -replace '\\','/'
    $stdin | & $bashExe.Source $siblingForBash
    exit $LASTEXITCODE
}

# ---- Native PowerShell implementation -------------------------------------

$raw = [Console]::In.ReadToEnd()

function Out-Silent { exit 0 }

if ($env:COAGULA_DISABLE -eq '1') { Out-Silent }
if (-not (Get-Command coagula -ErrorAction SilentlyContinue)) { Out-Silent }

try {
    $payload = $raw | ConvertFrom-Json -ErrorAction Stop
} catch {
    Out-Silent
}

# Claude Code field shape: tool_name + tool_input (snake_case).
$command = [string]$payload.tool_input.command
if ([string]::IsNullOrEmpty($command)) { Out-Silent }

# Don't double-wrap.
if ($command -match '\|\s?coagula(\s|$)') { Out-Silent }

# Don't re-funnel reads of Copilot's spill files (escape hatch).
if ($command -match 'copilot-tool-output-[\w-]+\.txt') { Out-Silent }

$defaultPattern = '^(kubectl|oc|helm|psql|mysql|sqlite3|az|gcloud|aws|curl|wget|Invoke-WebRequest|Invoke-RestMethod|iwr|irm|gh api|journalctl|dmesg|ps |netstat|lsof|iptables|systemctl|docker (ps|inspect|logs)|terraform (show|plan))\s'
$extraPattern   = $env:COAGULA_NOISY_PATTERNS

$stripped = $command.TrimStart()
$matched = $false
if ($stripped -imatch $defaultPattern) { $matched = $true }
elseif ($extraPattern -and $stripped -imatch ('^(' + $extraPattern + ')\s')) { $matched = $true }
if (-not $matched) { Out-Silent }

# Profile selection.
$profile = 'passthrough'
switch -Regex ($stripped) {
    '^(kubectl|oc|helm)\s'     { $profile = 'k8s'; break }
    '^(psql|mysql|sqlite3)\s'  { $profile = 'postgres'; break }
    '^(az|gcloud|aws)\s'       { $profile = 'azure'; break }
}

# Query derivation: env first, then transcript (last user message), else fallback.
$query = $env:COAGULA_QUERY
if ([string]::IsNullOrEmpty($query) -and $payload.transcript_path -and (Test-Path $payload.transcript_path)) {
    try {
        $lastUserText = Get-Content -LiteralPath $payload.transcript_path |
            ForEach-Object {
                try { $_ | ConvertFrom-Json -ErrorAction Stop } catch { $null }
            } |
            Where-Object { $_ -and $_.role -eq 'user' } |
            ForEach-Object {
                if ($_.content -is [array] -and $_.content[0].text) { $_.content[0].text }
                elseif ($_.content -is [string]) { $_.content }
                else { $null }
            } |
            Where-Object { $_ } |
            Select-Object -Last 1
        if ($lastUserText) {
            $query = ($lastUserText -replace "`r?`n", ' ').Substring(0, [Math]::Min(200, $lastUserText.Length))
        }
    } catch {}
}
# No fallback string — empty query triggers lite mode in the CLI.

$budget = if ($env:COAGULA_BUDGET) { [int]$env:COAGULA_BUDGET } else { 2000 }
$keep   = if ($env:COAGULA_KEEP)   { [int]$env:COAGULA_KEEP }   else { 5 }

if ([string]::IsNullOrEmpty($query)) {
    $rewritten = "( $command ) 2>&1 | coagula --profile $profile --budget $budget --keep $keep"
} else {
    $escaped = $query -replace "'", "'\''"
    $quotedQuery = "'$escaped'"
    $rewritten = "( $command ) 2>&1 | coagula --query $quotedQuery --profile $profile --budget $budget --keep $keep"
}

# Debug log — transform-only. Default ~/.copilot/coagula-debug.log; the
# claude-code hook logs to the same file so all hook activity is in one
# tail. Override path with COAGULA_DEBUG_LOG=<path>, disable with =off.
$homeDir = if ($env:USERPROFILE) { $env:USERPROFILE } elseif ($env:HOME) { $env:HOME } else { '' }
$logPath = if ($env:COAGULA_DEBUG_LOG) {
    $env:COAGULA_DEBUG_LOG
} elseif ($homeDir) {
    Join-Path $homeDir '.copilot/coagula-debug.log'
} else {
    ''
}
if (@('off','OFF','disabled','DISABLED','0') -notcontains $logPath) {
    $msg = "$(Get-Date -Format 'o') [claude-pre-bash] rewrote (profile=$profile): $command"
    Add-Content -LiteralPath $logPath -Value $msg -ErrorAction SilentlyContinue
}

$response = @{
    hookSpecificOutput = @{
        hookEventName     = 'PreToolUse'
        permissionDecision = 'allow'
        updatedInput       = @{ command = $rewritten }
        additionalContext  = "[coagula:pre-bash:$profile]"
    }
} | ConvertTo-Json -Depth 5 -Compress

Write-Output $response
