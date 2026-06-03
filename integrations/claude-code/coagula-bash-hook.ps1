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

$siblingSh = Join-Path $PSScriptRoot 'coagula-bash-hook.sh'
$bashExe   = Get-Command bash -ErrorAction SilentlyContinue
if ($bashExe -and (Test-Path $siblingSh)) {
    $stdin = [Console]::In.ReadToEnd()
    $stdin | & $bashExe.Source $siblingSh
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

$defaultPattern = '^(kubectl|oc|helm|psql|mysql|sqlite3|az|gcloud|aws|gh api|journalctl|dmesg|ps |netstat|lsof|iptables|systemctl|docker (ps|inspect|logs)|terraform (show|plan))\s'
$extraPattern   = $env:COAGULA_NOISY_PATTERNS

$stripped = $command.TrimStart()
$matched = $false
if ($stripped -cmatch $defaultPattern) { $matched = $true }
elseif ($extraPattern -and $stripped -cmatch ('^(' + $extraPattern + ')\s')) { $matched = $true }
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
if ([string]::IsNullOrEmpty($query)) { $query = 'general diagnostic query' }

$budget = if ($env:COAGULA_BUDGET) { [int]$env:COAGULA_BUDGET } else { 2000 }
$keep   = if ($env:COAGULA_KEEP)   { [int]$env:COAGULA_KEEP }   else { 5 }

$escaped = $query -replace "'", "'\''"
$quotedQuery = "'$escaped'"
$rewritten = "( $command ) 2>&1 | coagula --query $quotedQuery --profile $profile --budget $budget --keep $keep"

$response = @{
    hookSpecificOutput = @{
        hookEventName     = 'PreToolUse'
        permissionDecision = 'allow'
        updatedInput       = @{ command = $rewritten }
        additionalContext  = "[coagula] funneled output of: $command"
    }
} | ConvertTo-Json -Depth 5 -Compress

Write-Output $response
