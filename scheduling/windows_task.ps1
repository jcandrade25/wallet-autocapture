<#
.SYNOPSIS
    Register a daily Windows Scheduled Task that runs wallet-autocapture headless.

.DESCRIPTION
    Creates (or updates, -Force) a Scheduled Task that runs `python -m src.run_daily`
    from the repo root every day, using pythonw.exe so no console window appears.

    The task runs only when the user is logged on (LogonType Interactive), so it
    never stores your password and it inherits your logged-on session — which is
    what you want when the repo and its secrets live in a synced folder
    (OneDrive / iCloud / Dropbox) that is only mounted while you are signed in.
    StartWhenAvailable makes it catch up if the machine was off at the scheduled
    time. run_daily degrades gracefully (no credentials / no Ollama -> it logs and
    exits, it does not crash), so a missed dependency will not wedge the task.

    Run once in a normal (non-admin) PowerShell:
        powershell -ExecutionPolicy Bypass -File scheduling\windows_task.ps1

    Re-running it updates the task in place (idempotent).

.PARAMETER RepoDir
    Absolute path to the repo root (the folder that contains the `src` package).
    Defaults to the parent of this script's folder, so the defaults work when the
    script lives in <repo>\scheduling\.

.PARAMETER PythonwPath
    Path to pythonw.exe. If omitted, the script tries to derive it from the
    `python` on PATH (skipping the Microsoft Store shim under WindowsApps, which
    fails under Task Scheduler).

.PARAMETER Time
    Daily run time, HH:mm. Default 07:30.

.PARAMETER TaskName
    Scheduled task name. Default 'WalletAutocapture'.

.PARAMETER TaskPath
    Scheduled task folder. Default '\'.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scheduling\windows_task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scheduling\windows_task.ps1 `
        -RepoDir 'D:\code\wallet-autocapture' -Time 08:00 -TaskName 'WalletCapture'
#>
[CmdletBinding()]
param(
    [string]$RepoDir,
    [string]$PythonwPath,
    [string]$Time = '07:30',
    [string]$TaskName = 'WalletAutocapture',
    [string]$TaskPath = '\'
)

$ErrorActionPreference = 'Stop'

# --- Resolve the repo root (default: the folder ABOVE this script). ---
if (-not $RepoDir) {
    $RepoDir = Split-Path -Parent $PSScriptRoot
}
$RepoDir = (Resolve-Path -LiteralPath $RepoDir).Path
if (-not (Test-Path (Join-Path $RepoDir 'src'))) {
    throw "No 'src' package found under '$RepoDir'. Pass -RepoDir <repo root>."
}

# --- Resolve pythonw.exe (headless Python). ---
if (-not $PythonwPath) {
    $cand = Get-Command python.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike '*WindowsApps*' } |
        Select-Object -First 1
    if ($cand) {
        $PythonwPath = $cand.Source -replace 'python\.exe$', 'pythonw.exe'
    }
}
if (-not $PythonwPath -or -not (Test-Path $PythonwPath)) {
    throw "Could not locate pythonw.exe. Pass it explicitly with -PythonwPath 'C:\path\to\pythonw.exe'."
}

Write-Host "repo   : $RepoDir"
Write-Host "pythonw: $PythonwPath"
Write-Host "time   : $Time  (daily)"

# Run the package entry point from the repo root so `-m src.run_daily` resolves.
$action = New-ScheduledTaskAction -Execute $PythonwPath `
    -Argument '-m src.run_daily' `
    -WorkingDirectory $RepoDir

$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]$Time)

# Interactive logon => no stored password; runs inside the user's session.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "wallet-autocapture: daily headless capture of bank-alert transactions into Wallet (runs 'python -m src.run_daily')." `
    -Force | Out-Null

Write-Host ""
Write-Host "[OK] Scheduled task '$TaskName' registered (daily at $Time)." -ForegroundColor Green
Write-Host ""
Write-Host "Run now:    Start-ScheduledTask -TaskName '$TaskName' -TaskPath '$TaskPath'"
Write-Host "Status:     Get-ScheduledTask -TaskName '$TaskName' -TaskPath '$TaskPath' | Get-ScheduledTaskInfo | Select LastRunTime,LastTaskResult,NextRunTime"
Write-Host "Logs:       Get-Content '$RepoDir\logs\run_daily.log' -Tail 40"
Write-Host "Remove:     Unregister-ScheduledTask -TaskName '$TaskName' -TaskPath '$TaskPath' -Confirm:`$false"
Write-Host ""
Write-Host "Before the first real run:"
Write-Host "  1) Create config.json (run: python setup_wizard.py) and fill in identity/email."
Write-Host "  2) Provide the IMAP secret via the env var or password_file named in config.json email{}."
Write-Host "  3) If the repo lives in a cloud-synced folder, keep it available offline so the task can read it while you are logged on."
