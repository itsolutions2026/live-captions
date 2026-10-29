param(
    [switch]$Stop,
    [switch]$Restart,
    [switch]$Console
)

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonwPath = Join-Path $scriptRoot ".venv\Scripts\pythonw.exe"
$pythonPath = Join-Path $scriptRoot ".venv\Scripts\python.exe"
$captureScript = Join-Path $scriptRoot "capture_live_captions.py"

if ($Console -and (Test-Path $pythonPath)) {
    $pythonExe = $pythonPath
} elseif (Test-Path $pythonwPath) {
    $pythonExe = $pythonwPath
} elseif (Test-Path $pythonPath) {
    $pythonExe = $pythonPath
} else {
    Write-Error "Python environment not found. Expected .venv\Scripts\python.exe"
    exit 1
}

if (-not (Test-Path $captureScript)) {
    Write-Error "Capture script not found: $captureScript"
    exit 1
}

$existingProcesses = @(Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -match "^pythonw?\.exe$" -and
        $_.CommandLine -like "*capture_live_captions.py*"
    })

if ($Stop) {
    if (-not $existingProcesses) {
        Write-Host "Capture helper is not running."
        exit 0
    }

    foreach ($process in $existingProcesses) {
        Stop-Process -Id $process.ProcessId -Force
        Write-Host "Stopped caption capture PID: $($process.ProcessId)"
    }

    exit 0
}

if ($existingProcesses) {
    if (-not $Restart) {
        Write-Host "Capture helper is already running."
        Write-Host "PID: $($existingProcesses[0].ProcessId)"
        Write-Host "Use -Restart to stop and start it again, or -Stop to end it."
        exit 0
    }

    foreach ($process in $existingProcesses) {
        Stop-Process -Id $process.ProcessId -Force
        Write-Host "Stopped caption capture PID: $($process.ProcessId)"
    }

    Start-Sleep -Seconds 2
}

$transcriptFiles = @(
    "live_captions.txt",
    "live_captions_recent.txt",
    "live_captions_background.txt",
    "live_captions.txt.tmp"
)
foreach ($name in $transcriptFiles) {
    $path = Join-Path $scriptRoot $name
    $ok = $false
    for ($attempt = 1; $attempt -le 8; $attempt++) {
        try {
            $dir = Split-Path -Parent $path
            if (-not (Test-Path -LiteralPath $dir)) {
                New-Item -ItemType Directory -Path $dir -Force | Out-Null
            }
            if (Test-Path -LiteralPath $path) {
                $item = Get-Item -LiteralPath $path -ErrorAction Stop
                $item.Attributes = $item.Attributes -band (-bnot [System.IO.FileAttributes]::ReadOnly)
            }
            [System.IO.File]::WriteAllText($path, "")
            $ok = $true
            break
        } catch {
            Start-Sleep -Milliseconds 400
        }
    }
    if (-not $ok) {
        Write-Warning "Could not clear transcript file after retries: $path (close the file in your editor if it is open, then restart)."
    }
}
Write-Host "Transcript files cleared for a new session."
Write-Host "If Cursor still shows old text, reload the file from disk (cached buffer)."

if ($Console) {
    Write-Host "Starting caption capture in this PowerShell window."
    Write-Host "Press Ctrl+C to stop."
    & $pythonExe $captureScript
    exit $LASTEXITCODE
}

$process = Start-Process `
    -FilePath $pythonExe `
    -ArgumentList @($captureScript) `
    -WorkingDirectory $scriptRoot `
    -WindowStyle Minimized `
    -PassThru

Write-Host "Started caption capture."
Write-Host "PID: $($process.Id)"
Write-Host "Output file: $(Join-Path $scriptRoot 'live_captions.txt')"
