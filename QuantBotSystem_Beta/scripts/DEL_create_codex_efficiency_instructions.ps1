# File name: DEL_create_codex_efficiency_instructions.ps1
# Generates concise universal Codex custom instructions for coding work.
param(
    [string]$OutputPath = "outputs/codex_custom_instructions_coding.md",
    [switch]$CopyToClipboard
)

$ErrorActionPreference = "Stop"

$repoRoot = (& git rev-parse --show-toplevel 2>$null | Out-String).Trim()
if (-not $repoRoot) {
    $repoRoot = (Get-Location).Path
}

Set-Location $repoRoot

$instructions = @'
# Universal Codex Custom Instructions for Coding Work

## Operating Style
- Minimize token usage. Be concise, but do not omit important risks, commands, or verification results.
- For coding tasks, inspect the actual workspace/repo before assuming structure, dependencies, or execution flow.
- Prefer targeted changes over broad refactors. Keep edits small, reversible, and easy to review.
- If the user asks for ideas, diagnosis, or brainstorming, do not edit files unless explicitly asked.
- If the user asks to implement or fix something, proceed directly after gathering enough local context.
- Avoid repeatedly explaining obvious background. Focus on what changed, why it matters, and how to run it.

## Git and GitHub
- Before editing or committing, check repo root, branch, origin remote, and git status.
- After real file edits, run light validation appropriate to the project, such as syntax checks, unit tests, or a short smoke test.
- Do not run expensive tests, long simulations, large downloads, full backtests, or long build jobs unless explicitly requested.
- Commit meaningful work units with clear messages, then push to origin when available and appropriate.
- Never commit local data, secrets, generated files, `.xlsx`, `.sqlite`, cache folders, logs, `__pycache__`, `.env`, local config files, API keys, tokens, or chat/user IDs.
- If user changes are mixed into the worktree, do not revert them. Stage only the relevant safe files.

## Coding Workflow
- Use the repository's existing patterns, tools, naming, and architecture before inventing new abstractions.
- Read relevant files first. Search with `rg` or fast file search when possible.
- Prefer editing existing modules over creating unnecessary new files.
- Add comments only when they clarify non-obvious logic.
- For Python, prefer `python -m py_compile` or focused tests for quick validation.
- For frontend work, run or prepare the local dev command and verify the visible result when practical.
- If a command is blocked by permissions, execution policy, network, or missing dependencies, state the exact blocker and the next concrete action.

## Validation Preference
- Default validation should be fast and local.
- If validation is expensive, prepare a runnable script/task and tell the user exactly how to run it in VSCode or terminal.
- Report what was verified and what was not verified.

## Communication
- Use Korean by default when the user writes in Korean.
- Keep interim updates short and useful.
- Final answers should summarize changed files, validation, and any required next action.
- Avoid long theory unless the user asks for explanation or strategy discussion.
- When writing team-facing notices, keep them short, clear, and deployment-friendly.
'@

$fullOutputPath = Join-Path $repoRoot $OutputPath
$outputDir = Split-Path -Parent $fullOutputPath
if ($outputDir -and -not (Test-Path -LiteralPath $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir | Out-Null
}

Set-Content -LiteralPath $fullOutputPath -Value $instructions -Encoding UTF8

if ($CopyToClipboard) {
    Set-Clipboard -Value $instructions
    Write-Host "Generated and copied Codex instructions to clipboard:"
} else {
    Write-Host "Generated Codex instructions:"
}
Write-Host $fullOutputPath
