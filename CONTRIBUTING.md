# Contributing to coagula

Thanks for considering a contribution. coagula is Apache 2.0 licensed; by
contributing you agree your contributions are licensed under the same
terms, and that you have the right to make the contribution under the
Developer Certificate of Origin (see below).

## Getting started

```bash
git clone https://github.com/pat-nel87/coagula.git
cd coagula
python -m venv .venv && source .venv/bin/activate   # or .\.venv\Scripts\Activate.ps1 on Windows
pip install -e ".[dev,mcp]"
pytest                                              # 82+ tests should pass
```

For end-to-end checks involving optional backends:

```bash
RUN_OLLAMA_TESTS=1 pytest tests/test_models_ollama.py      # needs `ollama serve`
RUN_AZURE_TESTS=1  pytest tests/test_models_azure_openai.py # needs AZURE_OPENAI_* env vars
```

## Developer Certificate of Origin (DCO)

Every commit must be signed off, certifying you wrote the code (or have
the right to submit it under the project's license). This is enforced on
PRs.

Add the sign-off automatically with `git commit -s` — it appends:

```
Signed-off-by: Your Name <you@example.com>
```

This is the standard
[Developer Certificate of Origin v1.1](https://developercertificate.org/);
no CLA, no paperwork, just a one-line trailer per commit.

If you've already committed without `-s`, amend with:

```bash
git commit --amend -s --no-edit
git push --force-with-lease
```

## What good PRs look like

- **Scoped.** One logical change per PR. A new stage, a bug fix, a doc
  update — not all three together.
- **Tested.** New behavior gets unit tests under `tests/`. Bug fixes get
  a regression test that fails on `main` and passes on your branch.
- **CI clean.** `pytest`, `shellcheck integrations/**/*.sh`, and the
  PowerShell parse step all need to pass. They run automatically on PR.
- **Docs updated.** If you change a public API, env var, or install
  step, update `README.md` and the relevant `integrations/*/README.md`.

## Areas where contributions are particularly welcome

- **More denylist profiles** in `coagula/config.py` (Terraform state,
  Helm releases, GitHub Actions logs, etc.)
- **New backend adapters** in `coagula/models/` (Anthropic API,
  Bedrock, local LM Studio, etc.) — mirror the
  `azure_openai.py` shape.
- **More host integrations** in `integrations/` (Cursor, JetBrains AI,
  any host that grows hook-equivalent APIs).
- **Smoke scripts** for non-Windows platforms (macOS / Linux flavor of
  `windows-smoke.ps1`).

## Code style

- Python: type hints throughout, dataclasses for data, one stage per
  module under `stages/`, no required runtime dependencies on the
  default path. Optional deps go behind extras.
- Bash: shellcheck clean. `set -euo pipefail` at top of scripts.
- PowerShell: `$ErrorActionPreference = 'Stop'`, scripts parse with
  `[System.Management.Automation.Language.Parser]::ParseFile`.

## Reporting bugs / asking questions

Open a GitHub issue. For security issues, see [SECURITY.md](./SECURITY.md).
