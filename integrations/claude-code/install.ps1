# Install the coagula Claude Code Bash hook on Windows.
#
# - Copies coagula-bash-hook.ps1 (and the .sh sibling if present) to
#   $env:USERPROFILE\.claude\.
# - Prints the settings.json snippet (or with -AutoUpdateSettings, patches
#   the file via JSON merge).
#
# Usage from PowerShell:
#   .\integrations\claude-code\install.ps1
#   .\integrations\claude-code\install.ps1 -AutoUpdateSettings

[CmdletBinding()]
param(
    [switch]$AutoUpdateSettings
)

$ErrorActionPreference = 'Stop'

# Refresh PATH from the user + machine registry into the current process,
# so a `pip install` done in the same terminal is visible to Get-Command
# below. Otherwise the sanity-check section false-warns "coagula not on
# PATH" even when it's already installed. Windows-only; no-op elsewhere.
function Update-PathFromRegistry {
    if ([System.Environment]::OSVersion.Platform -ne 'Win32NT') { return }
    $machinePath = [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath    = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $combined = @($machinePath, $userPath) | Where-Object { $_ } | ForEach-Object { $_.TrimEnd(';') }
    $env:Path = ($combined -join ';')
}
Update-PathFromRegistry

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SrcPs = Join-Path $ScriptDir 'coagula-bash-hook.ps1'
$SrcSh = Join-Path $ScriptDir 'coagula-bash-hook.sh'
$DestDir = Join-Path $env:USERPROFILE '.claude'
$DestPs = Join-Path $DestDir 'coagula-bash-hook.ps1'
$DestSh = Join-Path $DestDir 'coagula-bash-hook.sh'
$Settings = Join-Path $DestDir 'settings.json'

if (-not (Test-Path $SrcPs)) { Write-Error "Hook source missing: $SrcPs"; exit 1 }

foreach ($cmd in 'coagula', 'copilot') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Warning "'$cmd' not on PATH. (Only 'coagula' is required to run the hook; 'copilot' is unrelated and listed just as a sanity check.)"
    }
}

New-Item -ItemType Directory -Path $DestDir -Force | Out-Null
Copy-Item $SrcPs $DestPs -Force
Write-Host "Installed hook -> $DestPs" -ForegroundColor Green
if (Test-Path $SrcSh) {
    Copy-Item $SrcSh $DestSh -Force
    Write-Host "Bundled bash sibling -> $DestSh" -ForegroundColor Gray
}

# Build the hook entry. The .ps1 auto-defers to Git Bash when available, so a
# single PowerShell entry covers both "Git Bash installed" and "PS-only".
$hookCommand = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$DestPs`""
$snippet = @{
    hooks = @{
        PreToolUse = @(
            @{
                matcher = 'Bash'
                hooks = @(
                    @{
                        type = 'command'
                        command = $hookCommand
                    }
                )
            }
        )
    }
} | ConvertTo-Json -Depth 6

if (-not $AutoUpdateSettings) {
    Write-Host ""
    Write-Host "Add the following to $Settings (or merge with existing hooks)."
    Write-Host "Re-run with -AutoUpdateSettings to do this automatically."
    Write-Host ""
    Write-Host $snippet
} else {
    # Merge: read existing settings (or start fresh), back up, add the Bash
    # matcher only if not already present.
    if (Test-Path $Settings) {
        $backup = "$Settings.bak.$([int][double]::Parse((Get-Date -UFormat %s)))"
        Copy-Item $Settings $backup
        Write-Host "Backed up existing settings -> $backup"
        $existing = Get-Content -Raw -LiteralPath $Settings | ConvertFrom-Json
    } else {
        $existing = [pscustomobject]@{}
    }

    # Ensure .hooks exists.
    if (-not ($existing.PSObject.Properties.Name -contains 'hooks')) {
        Add-Member -InputObject $existing -NotePropertyName 'hooks' -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    if (-not ($existing.hooks.PSObject.Properties.Name -contains 'PreToolUse')) {
        Add-Member -InputObject $existing.hooks -NotePropertyName 'PreToolUse' -NotePropertyValue @() -Force
    }

    $newEntry = [pscustomobject]@{
        matcher = 'Bash'
        hooks = @(
            [pscustomobject]@{ type = 'command'; command = $hookCommand }
        )
    }

    $bashMatcherIdx = -1
    for ($i = 0; $i -lt $existing.hooks.PreToolUse.Count; $i++) {
        if ($existing.hooks.PreToolUse[$i].matcher -eq 'Bash') { $bashMatcherIdx = $i; break }
    }

    if ($bashMatcherIdx -ge 0) {
        # Append our hook to the existing Bash matcher (dedup by command).
        $existingCmds = @($existing.hooks.PreToolUse[$bashMatcherIdx].hooks.command)
        if ($existingCmds -notcontains $hookCommand) {
            $existing.hooks.PreToolUse[$bashMatcherIdx].hooks += [pscustomobject]@{ type='command'; command=$hookCommand }
        }
    } else {
        $existing.hooks.PreToolUse += $newEntry
    }

    $existing | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Settings
    Write-Host "Updated $Settings — hook wired to the Bash matcher." -ForegroundColor Green
}

Write-Host ""
Write-Host "Test by starting a Claude Code or VSCode Copilot Chat session and"
Write-Host "asking it to run e.g. 'kubectl get pod -o json'."
