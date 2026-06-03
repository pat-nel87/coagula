# Install the coagula hooks for GitHub Copilot CLI on Windows.
#
# - Copies both .sh AND .ps1 hook scripts to ~/.copilot/hooks-bin/
#   (the .sh files are bundled so Git Bash users get the bash impl too)
# - Writes ~/.copilot/hooks/coagula.json with both `bash` and `powershell`
#   command fields — the Copilot CLI host auto-picks per platform.
# - Refuses to clobber existing coagula.json unless -Force.
#
# Usage from PowerShell:
#   .\integrations\copilot-cli\install.ps1
#   .\integrations\copilot-cli\install.ps1 -Force

[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Sanity: required source files.
$preBash = Join-Path $ScriptDir 'coagula-pre-bash-hook.sh'
$prePs   = Join-Path $ScriptDir 'coagula-pre-bash-hook.ps1'
$postBash = Join-Path $ScriptDir 'coagula-post-tool-hook.sh'
$postPs   = Join-Path $ScriptDir 'coagula-post-tool-hook.ps1'

foreach ($f in $preBash, $prePs, $postBash, $postPs) {
    if (-not (Test-Path -LiteralPath $f)) {
        Write-Error "Hook source missing: $f"
        exit 1
    }
}

# Warn-only checks for runtime deps.
foreach ($cmd in 'copilot', 'coagula') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Warning "'$cmd' not on PATH. The hook will be installed but won't fire until '$cmd' is available."
    }
}

# Resolve dest dirs.
$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { Join-Path $env:USERPROFILE '.copilot' }
$hookDir   = Join-Path $copilotHome 'hooks'
$binDir    = Join-Path $copilotHome 'hooks-bin'
$config    = Join-Path $hookDir 'coagula.json'

New-Item -ItemType Directory -Path $hookDir -Force | Out-Null
New-Item -ItemType Directory -Path $binDir  -Force | Out-Null

Copy-Item $preBash, $prePs, $postBash, $postPs $binDir -Force
Write-Host "Hooks -> $binDir" -ForegroundColor Green

if ((Test-Path -LiteralPath $config) -and -not $Force) {
    Write-Host ""
    Write-Host "Existing $config found. Re-run with -Force to overwrite, or merge:" -ForegroundColor Yellow
    Write-Host ""
    $printOnly = $true
} else {
    $printOnly = $false
}

# Build a config with BOTH bash and powershell command fields. Copilot CLI
# auto-picks `powershell` on Windows, `bash` on macOS/Linux, and the .ps1
# itself defers to Git Bash automatically when available.
$installedPreBash  = Join-Path $binDir 'coagula-pre-bash-hook.sh'
$installedPrePs    = Join-Path $binDir 'coagula-pre-bash-hook.ps1'
$installedPostBash = Join-Path $binDir 'coagula-post-tool-hook.sh'
$installedPostPs   = Join-Path $binDir 'coagula-post-tool-hook.ps1'

$psInvocation = "powershell -NoProfile -ExecutionPolicy Bypass -File"

$configObj = @{
    version = 1
    hooks = @{
        preToolUse = @(
            @{
                type       = 'command'
                bash       = $installedPreBash.Replace('\','/')
                powershell = "$psInvocation `"$installedPrePs`""
            }
        )
        postToolUse = @(
            @{
                type       = 'command'
                bash       = $installedPostBash.Replace('\','/')
                powershell = "$psInvocation `"$installedPostPs`""
            }
        )
    }
}

$json = $configObj | ConvertTo-Json -Depth 5

if ($printOnly) {
    Write-Host $json
} else {
    Set-Content -LiteralPath $config -Value $json
    Write-Host "Config -> $config" -ForegroundColor Green
}

Write-Host ""
Write-Host "Optional env knobs (set in PowerShell profile or per session):"
Write-Host "  `$env:COAGULA_QUERY      - query for relevance ranking"
Write-Host "  `$env:COAGULA_BUDGET     - token budget for cleaned output (default 2000)"
Write-Host "  `$env:COAGULA_KEEP       - top-K kept by relevance (default 5)"
Write-Host "  `$env:COAGULA_THRESHOLD  - postToolUse skips outputs under this (default 2000)"
Write-Host "  `$env:COAGULA_SKIP_TOOLS - extra comma-separated tools to bypass"
Write-Host "  `$env:COAGULA_DISABLE    - set to 1 to bypass entirely"
Write-Host ""
Write-Host "Verify with: copilot -p `"cat <some-big-file>`" --allow-all-tools --allow-all-paths"
