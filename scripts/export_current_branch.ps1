# File name: export_current_branch.ps1
# Exports tracked bot code from the current Git branch to a target bot folder.
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetPath
)

$ErrorActionPreference = "Stop"

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    $output = & git @Args
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Args -join ' ') failed"
    }
    return $output
}

$repoRoot = (& git rev-parse --show-toplevel 2>$null | Out-String).Trim()
if (-not $repoRoot) {
    throw "Run this script inside the QuantBot Git repository."
}

$target = Resolve-Path -LiteralPath $TargetPath -ErrorAction Stop
$branch = (& git branch --show-current | Out-String).Trim()

Set-Location $repoRoot
$files = Invoke-Git ls-files
$excluded = @(
    ".gitignore",
    ".vscode/tasks.json",
    "scripts/git_record.ps1",
    "scripts/export_current_branch.ps1"
)

foreach ($file in $files) {
    if ($excluded -contains $file) {
        continue
    }
    if ($file -like "config.py" -or $file -like "config_backup.py") {
        continue
    }
    $source = Join-Path $repoRoot $file
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        continue
    }
    $destination = Join-Path $target $file
    $destinationDir = Split-Path -Parent $destination
    if ($destinationDir -and -not (Test-Path -LiteralPath $destinationDir)) {
        New-Item -ItemType Directory -Path $destinationDir | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination $destination -Force
}

Write-Host "Exported branch '$branch' to $target"
Write-Host "Local config.py/config_backup.py were intentionally preserved."
