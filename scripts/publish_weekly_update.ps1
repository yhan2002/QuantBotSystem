param(
    [string]$Message = "chore: weekly QuantBot update"
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

git add -- `
    .gitignore `
    AGENTS.md `
    README.md `
    CHANGELOG.md `
    scripts `
    .vscode `
    RUN_quantbot_version_switch.py `
    test_RUN_quantbot_version_switch.py `
    QuantBotSystem `
    QuantBotSystem_Beta `
    QuantBotSystem_Portfolio_Beta

$staged = git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host 'No safe source changes to publish.'
    exit 0
}

git commit -m $Message

$remotes = git remote
if ($remotes -contains 'origin') {
    $branch = git branch --show-current
    git push origin $branch
    exit $LASTEXITCODE
}

Write-Host 'Committed locally. Add an origin remote to enable GitHub publishing.'
