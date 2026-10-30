param([switch]$Resume)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"
$fineTunePython = Join-Path $env:USERPROFILE "AppData\Local\Programs\Python\Python313\python.exe"
$fineTuneRun = Join-Path $PSScriptRoot "outputs\guoyue_transformer_ft_v1"
New-Item -ItemType Directory -Force -Path $fineTuneRun | Out-Null
$fineTuneLaunch = Join-Path $fineTuneRun "launch.json"
if (Test-Path -LiteralPath $fineTuneLaunch) {
    $fineTunePrevious = Get-Content -LiteralPath $fineTuneLaunch -Raw | ConvertFrom-Json
    $fineTuneActive = Get-Process -Id $fineTunePrevious.pid -ErrorAction SilentlyContinue
    if ($fineTuneActive -and $fineTuneActive.ProcessName -like "python*") { throw "Fine-tuning is already running." }
}
$fineTuneArgs = @("-u", "-m", "rhythm_transformer.train", "--data", "projects/guoyue_transformer_ft_v1/data/sequences.jsonl", "--vocab", "projects/guoyue_transformer_ft_v1/data/vocab.json", "--run", "outputs/guoyue_transformer_ft_v1", "--epochs", "60", "--batch-size", "8", "--lr", "0.00003", "--warmup", "20", "--patience", "10", "--model-name", "guoyue_transformer_finetuned_v1.pt", "--no-samples")
if ($Resume) {
    $fineTuneArgs += @("--resume", "outputs/guoyue_transformer_ft_v1/last.pt")
} else {
    if (Test-Path -LiteralPath (Join-Path $fineTuneRun "last.pt")) { throw "Existing checkpoint found. Use -Resume." }
    $fineTuneArgs += @("--init-from", "outputs/gmd_transformer_v1/gmd_transformer_pretrained.pt")
}
$fineTuneTimestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$fineTuneLog = Join-Path $fineTuneRun ("train-" + $fineTuneTimestamp + ".log")
$fineTuneError = Join-Path $fineTuneRun ("error-" + $fineTuneTimestamp + ".log")
$fineTuneProcess = Start-Process -FilePath $fineTunePython -ArgumentList $fineTuneArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $fineTuneLog -RedirectStandardError $fineTuneError -PassThru
@{pid=$fineTuneProcess.Id; started_at=(Get-Date).ToUniversalTime().ToString("o"); log=$fineTuneLog; error_log=$fineTuneError} | ConvertTo-Json | Set-Content -LiteralPath $fineTuneLaunch -Encoding UTF8
Write-Output "Guoyue fine-tuning started. PID: $($fineTuneProcess.Id)"
Write-Output "Progress: $fineTuneRun\status.json"
