# windows-smoke.ps1 — self-contained diagnostic + smoke-test for the
# coagula Windows install. Run on a clean (or troubled) Windows box to
# verify each layer of the stack independently. Each check is colored,
# numbered, and tells you exactly what to do if it fails.
#
# Usage from the cloned coagula repo:
#   .\integrations\windows-smoke.ps1
#   .\integrations\windows-smoke.ps1 -SkipLiveSession   # skip the real `copilot -p` call
#
# What it checks (in order):
#   1. Python on PATH + version
#   2. coagula CLI on PATH and runs --help
#   3. Optional: jq on PATH (only needed if you'll use the bash hooks)
#   4. coagula CLI funnels a known noisy log (>=99% reduction expected)
#   5. PS hook scripts parse cleanly
#   6. PS hook scripts produce expected JSON on synthetic stdin
#   7. ~/.copilot/hooks/coagula.json exists and has dual-field config
#   8. Optional: GitHub Copilot CLI on PATH
#   9. Optional: live `copilot -p "cat noisy.log"` session fires the hook
# 10. Optional: Azure OpenAI env vars present + ping succeeds

[CmdletBinding()]
param(
    [switch]$SkipLiveSession
)

$ErrorActionPreference = 'Continue'

$script:passed = 0
$script:failed = 0
$script:warned = 0

function Pass { param($Msg) Write-Host "  PASS  $Msg" -ForegroundColor Green; $script:passed++ }
function Fail { param($Msg, $Hint = $null) Write-Host "  FAIL  $Msg" -ForegroundColor Red; if ($Hint) { Write-Host "        $Hint" -ForegroundColor DarkYellow }; $script:failed++ }
function Warn { param($Msg, $Hint = $null) Write-Host "  WARN  $Msg" -ForegroundColor Yellow; if ($Hint) { Write-Host "        $Hint" -ForegroundColor DarkYellow }; $script:warned++ }
function Section { param($N, $Title) Write-Host ""; Write-Host "$N. $Title" -ForegroundColor Cyan }

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Fixture  = Join-Path $RepoRoot 'tests\fixtures\crashloop.log'
$HookCli  = Join-Path $RepoRoot 'integrations\copilot-cli'
$HookCC   = Join-Path $RepoRoot 'integrations\claude-code'

Write-Host "coagula Windows smoke-test" -ForegroundColor White
Write-Host "Repo root: $RepoRoot"

# ---------------------------------------------------------------------------
Section 1 "Python on PATH"
# ---------------------------------------------------------------------------
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if ($python) {
    $ver = & $python.Source --version 2>&1
    if ($ver -match '3\.(1[0-9]|[2-9][0-9])') { Pass "$ver at $($python.Source)" }
    else { Fail "Python is too old ($ver)" "coagula requires Python >= 3.10. Run: winget install Python.Python.3.13" }
} else {
    Fail "Python not on PATH" "Install: winget install Python.Python.3.13 (then restart PowerShell)"
}

# ---------------------------------------------------------------------------
Section 2 "coagula CLI on PATH"
# ---------------------------------------------------------------------------
$coagula = Get-Command coagula -ErrorAction SilentlyContinue
if ($coagula) {
    try {
        $out = & coagula --help 2>&1
        if ($LASTEXITCODE -eq 0 -and ($out -join "`n") -match '--query') {
            Pass "coagula --help works ($($coagula.Source))"
        } else {
            Fail "coagula --help returned unexpected output" "Try: pip install --force-reinstall coagula"
        }
    } catch {
        Fail "coagula errored on --help: $_" "Try: pip install --force-reinstall coagula"
    }
} else {
    Fail "coagula not on PATH" @"
Install with one of:
  pip install https://github.com/pat-nel87/coagula/releases/download/v0.3.0/coagula-0.3.0-py3-none-any.whl
  pip install -e .                          # from the cloned repo
After install, add Python's Scripts\ to PATH if needed (Settings -> Environment Variables).
"@
}

# ---------------------------------------------------------------------------
Section 3 "jq (only required for the bash hook path)"
# ---------------------------------------------------------------------------
if (Get-Command jq -ErrorAction SilentlyContinue) {
    Pass "jq on PATH"
} else {
    Warn "jq not on PATH" "Only needed if you run the .sh hook (Git Bash). The PowerShell hook doesn't need it. Install: winget install jqlang.jq"
}

# ---------------------------------------------------------------------------
Section 4 "coagula CLI funnels the crashloop fixture"
# ---------------------------------------------------------------------------
if (-not (Test-Path $Fixture)) {
    Fail "Fixture missing: $Fixture" "Re-clone the repo or run from the repo root."
} elseif (-not $coagula) {
    Warn "Skipping — coagula not on PATH (see check 2)."
} else {
    try {
        $inText = Get-Content -Raw -LiteralPath $Fixture
        $inLen = $inText.Length
        $outText = $inText | coagula --query "why connection refused" --profile passthrough --budget 2000 --keep 5
        $outLen = ($outText -join "`n").Length
        $reduction = [math]::Round((1 - $outLen / $inLen) * 100, 1)
        if ($outLen -gt 0 -and $reduction -ge 95.0 -and ($outText -join '') -match 'connection refused') {
            Pass "Reduced $inLen -> $outLen chars ($reduction%) with signal preserved"
        } else {
            Fail "Reduction was $reduction% or signal lost" "If signal is missing, check tests/fixtures/crashloop.log isn't truncated."
        }
    } catch {
        Fail "coagula CLI errored: $_"
    }
}

# ---------------------------------------------------------------------------
Section 5 "PowerShell hook scripts parse cleanly"
# ---------------------------------------------------------------------------
$psHooks = @(
    Join-Path $HookCli 'coagula-pre-bash-hook.ps1'
    Join-Path $HookCli 'coagula-post-tool-hook.ps1'
    Join-Path $HookCC  'coagula-bash-hook.ps1'
)
foreach ($h in $psHooks) {
    if (-not (Test-Path $h)) { Fail "Missing: $h"; continue }
    $tokens = $null; $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($h, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors -and $errors.Count -gt 0) {
        $msg = ($errors | ForEach-Object { "  $($_.Message) (line $($_.Extent.StartLineNumber))" }) -join "`n"
        Fail "Parse errors in $h`n$msg"
    } else {
        Pass "Parsed: $(Split-Path -Leaf $h)"
    }
}

# ---------------------------------------------------------------------------
Section 6 "PS hooks emit correct JSON on synthetic stdin"
# ---------------------------------------------------------------------------
if (-not $coagula) {
    Warn "Skipping — coagula not on PATH."
} else {
    # 6a. preToolUse kubectl rewrite
    $payload = '{"sessionId":"x","timestamp":1,"cwd":"/tmp","toolName":"bash","toolArgs":"{\"command\":\"kubectl get pod -o json\",\"description\":\"x\",\"initial_wait\":30}"}'
    $env:COAGULA_QUERY = "why is pod crashing"
    $shSibling = Join-Path $HookCli 'coagula-pre-bash-hook.sh'
    $movedSh = $false
    if (Test-Path $shSibling) { Rename-Item $shSibling "$shSibling.bak"; $movedSh = $true }
    try {
        $out = $payload | pwsh -NoProfile -File (Join-Path $HookCli 'coagula-pre-bash-hook.ps1')
        $obj = $out | ConvertFrom-Json
        if ($obj.modifiedArgs.command -match 'coagula --query.*--profile k8s') {
            Pass "preToolUse rewrites kubectl with --profile k8s"
        } else {
            Fail "preToolUse output unexpected: $($obj | ConvertTo-Json -Compress)"
        }
    } catch {
        Fail "preToolUse hook errored: $_"
    } finally {
        if ($movedSh) { Rename-Item "$shSibling.bak" $shSibling }
    }

    # 6b. postToolUse big-input funnel
    $shSibling = Join-Path $HookCli 'coagula-post-tool-hook.sh'
    $movedSh = $false
    if (Test-Path $shSibling) { Rename-Item $shSibling "$shSibling.bak"; $movedSh = $true }
    try {
        $big = Get-Content -Raw -LiteralPath $Fixture
        $payload = @{
            sessionId = "x"; timestamp = 1; cwd = "/tmp"
            toolName  = "bash"
            toolArgs  = @{ command = "journalctl -u svc" }
            toolResult = @{ resultType = "success"; textResultForLlm = $big }
        } | ConvertTo-Json -Depth 6 -Compress
        $env:COAGULA_QUERY = "why connection refused"
        $out = $payload | pwsh -NoProfile -File (Join-Path $HookCli 'coagula-post-tool-hook.ps1')
        $obj = $out | ConvertFrom-Json
        $cleaned = $obj.modifiedResult.textResultForLlm
        if ($cleaned -and $cleaned.Length -lt 1000) {
            Pass "postToolUse funneled $($big.Length) -> $($cleaned.Length) chars"
        } else {
            Fail "postToolUse did not funnel large input (output length: $($cleaned.Length))"
        }
    } catch {
        Fail "postToolUse hook errored: $_"
    } finally {
        if ($movedSh) { Rename-Item "$shSibling.bak" $shSibling }
    }
}

# ---------------------------------------------------------------------------
Section 7 "Installed hook config at ~/.copilot/hooks/coagula.json"
# ---------------------------------------------------------------------------
$cfg = Join-Path $env:USERPROFILE '.copilot\hooks\coagula.json'
if (-not (Test-Path $cfg)) {
    Warn "Not installed yet" "Run: .\integrations\copilot-cli\install.ps1"
} else {
    try {
        $parsed = Get-Content -Raw -LiteralPath $cfg | ConvertFrom-Json
        $checks = @(
            @{ Path = 'preToolUse.bash';       Value = $parsed.hooks.preToolUse[0].bash }
            @{ Path = 'preToolUse.powershell'; Value = $parsed.hooks.preToolUse[0].powershell }
            @{ Path = 'postToolUse.bash';      Value = $parsed.hooks.postToolUse[0].bash }
            @{ Path = 'postToolUse.powershell';Value = $parsed.hooks.postToolUse[0].powershell }
        )
        foreach ($c in $checks) {
            if ([string]::IsNullOrEmpty($c.Value)) { Fail "Config missing $($c.Path)" "Re-run install.ps1 -Force" }
            else { Pass "Config has $($c.Path)" }
        }
    } catch {
        Fail "Config JSON invalid: $_" "Re-run install.ps1 -Force"
    }
}

# ---------------------------------------------------------------------------
Section 8 "GitHub Copilot CLI on PATH"
# ---------------------------------------------------------------------------
$copilot = Get-Command copilot -ErrorAction SilentlyContinue
if ($copilot) {
    $ver = (& copilot --version 2>&1) -join ' '
    Pass "copilot installed ($ver)"
} else {
    Warn "copilot CLI not installed" @"
This is required for hooks to actually fire in real sessions. Install:
  winget install GitHub.cli
  gh auth login
  gh extension install github/copilot-cli
"@
}

# ---------------------------------------------------------------------------
Section 9 "Live Copilot CLI session smoke-test"
# ---------------------------------------------------------------------------
if ($SkipLiveSession) {
    Warn "Skipped (-SkipLiveSession)"
} elseif (-not $copilot) {
    Warn "Skipped — copilot not installed."
} elseif (-not (Test-Path $cfg)) {
    Warn "Skipped — hook config not installed (run install.ps1 first)."
} else {
    Write-Host "  Running a live session — this will use a Copilot premium request." -ForegroundColor DarkGray
    try {
        $prompt = "Run exactly this command: Get-Content '$Fixture'. Then in one sentence, tell me the dominant error pattern. Do not use grep, head, or any filtering."
        $sessionOutput = & copilot -p $prompt --allow-all-tools --allow-all-paths --no-color 2>&1 | Out-String
        if ($sessionOutput -match 'connection refused') {
            Pass "Live session returned the expected signal"
            if ($sessionOutput -match '\[coagula:') {
                Pass "Coagula prefix detected in session output (hook fired)"
            } else {
                Warn "Session worked but [coagula:] prefix not detected" "Hook may not have fired. Check %USERPROFILE%\.copilot\logs\ for preToolUse/postToolUse entries."
            }
        } else {
            Fail "Live session did not return expected signal" "Output excerpt: $($sessionOutput.Substring(0, [Math]::Min(300, $sessionOutput.Length)))"
        }
    } catch {
        Fail "Live session errored: $_"
    }
}

# ---------------------------------------------------------------------------
Section 10 "Azure OpenAI configuration (optional)"
# ---------------------------------------------------------------------------
if ($env:AZURE_OPENAI_ENDPOINT -and $env:AZURE_OPENAI_API_KEY -and $env:AZURE_OPENAI_LLM_DEPLOYMENT) {
    try {
        & python -c "
import os, sys
from coagula.models.azure_openai import ping
ok = ping(
    os.environ['AZURE_OPENAI_ENDPOINT'],
    os.environ['AZURE_OPENAI_API_KEY'],
    deployment=os.environ['AZURE_OPENAI_LLM_DEPLOYMENT'],
    api_version=os.environ.get('AZURE_OPENAI_API_VERSION', '2024-10-21'),
)
sys.exit(0 if ok else 1)
"
        if ($LASTEXITCODE -eq 0) {
            Pass "Azure OpenAI reachable (deployment: $env:AZURE_OPENAI_LLM_DEPLOYMENT)"
        } else {
            Fail "Azure ping failed" "Check endpoint, key, deployment name, and API version. Inspect %USERPROFILE%\.copilot\logs\."
        }
    } catch {
        Fail "Azure ping errored: $_"
    }
} else {
    Warn "Azure not configured" @"
Optional. To enable cheap-tier summarization through Azure:
  `$env:AZURE_OPENAI_ENDPOINT       = "https://my-resource.openai.azure.com"
  `$env:AZURE_OPENAI_API_KEY        = "..."
  `$env:AZURE_OPENAI_LLM_DEPLOYMENT = "gpt-5.4-nano"
"@
}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Summary" -ForegroundColor White
Write-Host "  Passed:  $script:passed" -ForegroundColor Green
Write-Host "  Warned:  $script:warned" -ForegroundColor Yellow
Write-Host "  Failed:  $script:failed" -ForegroundColor $(if ($script:failed -gt 0) { 'Red' } else { 'Gray' })

if ($script:failed -gt 0) {
    Write-Host ""
    Write-Host "Some checks failed. Read the FAIL/Hint lines above for remediation." -ForegroundColor Yellow
    exit 1
}
Write-Host ""
Write-Host "All critical checks passed." -ForegroundColor Green
exit 0
