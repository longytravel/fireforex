#
# Pre-PR ritual — run three reviewers on the current branch's diff vs main.
# Usage (in Claude Code chat, NOT a raw PowerShell prompt):
#   /simplify          # Claude runs the simplify skill
#   /code-review       # Claude runs the code-review skill
#   Then this script:
#   .\scripts\pre-pr.ps1
#
# This script launches Codex mini as a read-only reviewer on the diff.
# Output goes to artifacts/review-codex-mini-<timestamp>.md for pasting into the PR.

param(
  [string]$BaseBranch = "main",
  [string]$Model = "gpt-5.4-mini",
  [string]$ReasoningEffort = "high"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# Collect the diff
$diffFile = Join-Path $repoRoot "artifacts\_pre_pr_diff.patch"
New-Item -ItemType Directory -Force -Path (Split-Path $diffFile) | Out-Null
git diff "$BaseBranch...HEAD" | Set-Content -Path $diffFile -Encoding utf8

if ((Get-Content $diffFile -Raw).Length -lt 10) {
  Write-Host "No diff vs $BaseBranch. Nothing to review."
  exit 0
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outputFile = Join-Path $repoRoot "artifacts\review-codex-mini-$timestamp.md"

$prompt = @"
You are a code reviewer for the Fire Forex project (forex backtest + live trading).
Review the diff in artifacts/_pre_pr_diff.patch against these priorities, in order:

1. Signal-variant ID resolution — does this change variant IDs, lookback steps, or
   the signal library? If yes, are the 7 deployed VPS configs migration-safe?
2. Live-trading correctness — broker time vs UTC, forming candles, phantom positions,
   retry-induced duplicate signals, MT5 deal history gaps.
3. Rust<->Python contract — if core/src/ changed, is the pyo3 boundary still stable,
   and is there a pinned NPZ reference test?
4. Float equality — any == on floats? Flag them.
5. Silent-no-op risk — any new knob that might not be wired through to the Rust engine?
   (See .claude/rules/testing.md for the add-forex-knob / validate-forex-knob pattern.)
6. Ordinary bugs — off-by-one, error swallowing, resource leaks.

Output a terse markdown report with sections:
- Must fix before merge
- Should fix
- Nitpicks
- Overall verdict (ship / changes requested)

Keep the total output under 300 words. Do not repeat the diff.
"@

Write-Host "Running Codex $Model (reasoning=$ReasoningEffort) on diff vs $BaseBranch..."
Write-Host "Output -> $outputFile"

$cmd = @(
  "codex", "exec",
  "--skip-git-repo-check",
  "-m", $Model,
  "--config", "model_reasoning_effort=`"$ReasoningEffort`"",
  "--sandbox", "read-only",
  "`"$prompt`""
) -join " "

# Run and capture stdout; suppress stderr (thinking tokens)
cmd /c "$cmd 2>NUL" | Tee-Object -FilePath $outputFile

Write-Host ""
Write-Host "Done. Paste the contents of $outputFile into the PR body under 'Review outputs'."
