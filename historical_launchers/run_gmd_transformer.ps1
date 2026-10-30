param([switch]$Resume)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"
$trainerPython = Join-Path $env:USERPROFILE "AppData\Local\Programs\Python\Python313\python.exe"
if (-not (Test-Path -LiteralPath $trainerPython)) { throw "The existing CUDA Python environment was not found." }
$trainerRun = Join-Path $PSScriptRoot "outputs\gmd_transformer_v1"
New-Item -ItemType Directory -Force -Path $trainerRun | Out-Null
$trainerLaunchFile = Join-Path $trainerRun "launch.json"
if (Test-Path -LiteralPath $trainerLaunchFile) {
    $trainerPrevious = Get-Content -LiteralPath $trainerLaunchFile -Raw | ConvertFrom-Json
    $trainerActive = Get-Process -Id $trainerPrevious.pid -ErrorAction SilentlyContinue
    if ($trainerActive -and $trainerActive.ProcessName -like "python*") {
        throw "Training is already running. Open outputs/gmd_transformer_v1/status.json for progress."
    }
}
$trainerArgs = @("-u", "-m", "rhythm_transformer.train", "--run", "outputs/gmd_transformer_v1")
if ($Resume) {
    if (-not (Test-Path -LiteralPath (Join-Path $trainerRun "last.pt"))) { throw "No resumable checkpoint exists." }
    $trainerArgs += @("--resume", "outputs/gmd_transformer_v1/last.pt")
} elseif (Test-Path -LiteralPath (Join-Path $trainerRun "last.pt")) {
    throw "This run already has a checkpoint. Use the resume launcher."
}
$trainerTimestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$trainerLog = Join-Path $trainerRun ("train-" + $trainerTimestamp + ".log")
$trainerErrorLog = Join-Path $trainerRun ("error-" + $trainerTimestamp + ".log")
$trainerProcess = Start-Process -FilePath $trainerPython -ArgumentList $trainerArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $trainerLog -RedirectStandardError $trainerErrorLog -PassThru
@{pid=$trainerProcess.Id; started_at=(Get-Date).ToUniversalTime().ToString("o"); log=$trainerLog; error_log=$trainerErrorLog} | ConvertTo-Json | Set-Content -LiteralPath $trainerLaunchFile -Encoding UTF8
Write-Output "Training started in the background. PID: $($trainerProcess.Id)"
Write-Output "Progress: $trainerRun\status.json"
Write-Output "Log: $trainerLog"
