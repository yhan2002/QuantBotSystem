# QuantBot

## Scope

- `QuantBotSystem/` is the deployment line: do not edit it without explicit approval.
- Make strategy and UI changes in `QuantBotSystem_Beta/` first.
- Preserve user changes; inspect `git status` before editing or committing.

## Workflow

- Use Korean for user-facing messages.
- Keep changes small. Read relevant files before editing.
- For Python, run focused `unittest` and `py_compile`; do not run long backtests, downloads, or bot delivery without request.
- Signals use information available at the close and may enter no earlier than the next open. Do not introduce lookahead.

## Data and Git

- Never commit secrets, `config.local.py`, `local_settings.py`, `.env`, SQLite, caches, logs, generated CSV/JSON, or Excel files.
- Stage only task-relevant files. Commit clear work units; push only when requested.
- `CHANGELOG.md` is the project change record. Keep meeting-thread notes concise and link back to it.

## AGENTS Maintenance

- Update this file when a durable scope, safety, validation, or release convention changes.
- Do not add weekly history, experiment results, or implementation detail; record those in `CHANGELOG.md`.
- Keep this file concise and remove superseded rules when adding a replacement.

## Model Selection

- Sol: architecture, security review, multi-file refactors
- Terra: default for features, fixes, and tests
- Luna: documentation, formatting, repetitive subagent work
