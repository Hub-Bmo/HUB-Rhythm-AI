param([switch]$Resume)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"
$gmd12Python = Join-Path $env:USERPROFILE "AppData\Local\Programs\Python\Python313\python.exe"
$gmd12Run = Join-Path $PSScriptRoot "outputs\guoyue_gmd12_ft_v1"
New-Item -ItemType Directory -Force -Path $gmd12Run | Out-Null
$gmd12Launch = Join-Path $gmd12Run "launch.json"
if (Test-Path -LiteralPath $gmd12Launch) {
    $gmd12Previous = Get-Content -LiteralPath $gmd12Launch -Raw | ConvertFrom-Json
    $gmd12Active = Get-Process -Id $gmd12Previous.pid -ErrorAction SilentlyContinue
    if ($gmd12Active -and $gmd12Active.ProcessName -like "python*") { throw "The mapped experiment is already running." }
}
$gmd12Args = @("-u", "-m", "rhythm_transformer.train", "--data", "outputs/guoyue_gmd12_tokens_v1/sequences.jsonl", "--vocab", "outputs/guoyue_gmd12_tokens_v1/vocab.json", "--run", "outputs/guoyue_gmd12_ft_v1", "--epochs", "60", "--batch-size", "8", "--lr", "0.00003", "--warmup", "20", "--patience", "10", "--model-name", "guoyue_gmd12_finetuned_v1.pt", "--no-samples")
if ($Resume) {
    $gmd12Args += @("--resume", "outputs/guoyue_gmd12_ft_v1/last.pt")
} else {
    if (Test-Path -LiteralPath (Join-Path $gmd12Run "last.pt")) { throw "Existing checkpoint found. Use -Resume." }
    $gmd12Args += @("--init-from", "outputs/gmd_transformer_v1/gmd_transformer_pretrained.pt")
}
$gmd12Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$gmd12Log = Join-Path $gmd12Run ("train-" + $gmd12Timestamp + ".log")
$gmd12Error = Join-Path $gmd12Run ("error-" + $gmd12Timestamp + ".log")
$gmd12Process = Start-Process -FilePath $gmd12Python -ArgumentList $gmd12Args -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $gmd12Log -RedirectStandardError $gmd12Error -PassThru
@{pid=$gmd12Process.Id; started_at=(Get-Date).ToUniversalTime().ToString("o"); log=$gmd12Log; error_log=$gmd12Error} | ConvertTo-Json | Set-Content -LiteralPath $gmd12Launch -Encoding UTF8
Write-Output "Mapped guoyue fine-tuning started from the original GMD model. PID: $($gmd12Process.Id)"
