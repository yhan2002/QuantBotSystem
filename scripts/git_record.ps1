# File name: git_record.ps1
# Commits current work, pushes it to origin, and opens a GitHub PR when possible.
param(
    [string]$Message = "Codex update",
    [string]$OriginUrl = "",
    [string]$BaseBranch = "main",
    [switch]$NoPr
)

$ErrorActionPreference = "Stop"

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & git @Args
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Args -join ' ') failed"
    }
}

function Get-GitOutput {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    $output = & git @Args 2>$null
    if ($LASTEXITCODE -ne 0) {
        return ""
    }
    return ($output | Out-String).Trim()
}

$repoRoot = Get-GitOutput rev-parse --show-toplevel
if (-not $repoRoot) {
    throw "This folder is not a Git repository. Run git init first."
}
Set-Location $repoRoot

if (-not (Get-GitOutput config user.name)) {
    Invoke-Git config user.name "Codex Automation"
}
if (-not (Get-GitOutput config user.email)) {
    Invoke-Git config user.email "codex@example.local"
}

$origin = Get-GitOutput remote get-url origin
if ($OriginUrl) {
    if ($origin) {
        Invoke-Git remote set-url origin $OriginUrl
    } else {
        Invoke-Git remote add origin $OriginUrl
    }
    $origin = Get-GitOutput remote get-url origin
}

if (-not $origin) {
    Write-Host "No origin remote is configured."
    Write-Host "Run one of these:"
    Write-Host "  git remote add origin https://github.com/OWNER/REPO.git"
    Write-Host "  .\scripts\git_record.ps1 -OriginUrl https://github.com/OWNER/REPO.git -Message `"your message`""
    exit 2
}

$branch = Get-GitOutput branch --show-current
if (-not $branch) {
    $branch = $BaseBranch
    Invoke-Git checkout -B $branch
}

$protectedBranches = @($BaseBranch, "main", "master") | Select-Object -Unique
if ($protectedBranches -contains $branch) {
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $branch = "codex/auto-$timestamp"
    Invoke-Git checkout -B $branch
}

Invoke-Git add -A
$pending = Get-GitOutput status --porcelain
if ($pending) {
    Invoke-Git commit -m $Message
} else {
    Write-Host "No file changes to commit."
}

Invoke-Git push -u origin $branch

if ($NoPr -or $branch -eq $BaseBranch) {
    Write-Host "Push complete. PR creation skipped."
    exit 0
}

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    Write-Host "Push complete. GitHub CLI is not installed, so create the PR manually:"
    Write-Host "  gh pr create --base $BaseBranch --head $branch --fill"
    exit 0
}

$existingPr = & gh pr view $branch --json url --jq .url 2>$null
if ($LASTEXITCODE -eq 0 -and $existingPr) {
    Write-Host "PR already exists: $existingPr"
    exit 0
}

& gh pr create --base $BaseBranch --head $branch --fill
if ($LASTEXITCODE -ne 0) {
    throw "gh pr create failed"
}

