param([string]$Workspace = "")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"

# Standard library only: do not modify any existing program environment.
$tokenizerCandidates = @(
    (Join-Path $PSScriptRoot ".venv-reviewer\Scripts\python.exe"),
    (Join-Path $PSScriptRoot ".venv-recognizer\Scripts\python.exe"),
    (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
)
$tokenizerCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($tokenizerCommand) { $tokenizerCandidates += $tokenizerCommand.Source }
$tokenizerPython = $null
foreach ($tokenizerCandidate in $tokenizerCandidates) {
    if (-not (Test-Path -LiteralPath $tokenizerCandidate)) { continue }
    try {
        & $tokenizerCandidate -c "import tkinter,sys; assert sys.version_info >= (3,10)" 2>$null
        if ($LASTEXITCODE -eq 0) { $tokenizerPython = $tokenizerCandidate; break }
    } catch { continue }
}
if (-not $tokenizerPython) { throw "需要包含 Tkinter 的 Python 3.10 或更新版本。" }
$tokenizerArguments = @((Join-Path $PSScriptRoot "tokenize_scores.py"))
if ($Workspace) { $tokenizerArguments += @("--workspace", $Workspace) }
& $tokenizerPython @tokenizerArguments
if ($LASTEXITCODE -ne 0) { throw "Token 化工具啟動失敗。" }
