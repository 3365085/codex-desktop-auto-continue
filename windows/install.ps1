<#
.SYNOPSIS
  Install codex-desktop-auto-continue for the current Windows user.

.DESCRIPTION
  Copies the watcher to %LOCALAPPDATA%\codex-desktop-auto-continue, registers
  a per-user logon task (or, if Task Scheduler refuses, a HKCU Run entry),
  and starts the watcher immediately under pythonw.exe (no console window).
  No administrator rights are needed and the Codex app is not modified.

.PARAMETER CodexBin
  Codex CLI used for `codex queue` and `codex app-server`.
  Default: $env:CODEX_CLI_PATH, then `codex` on PATH.

.PARAMETER Python
  Python 3.10+ interpreter. Default: py -3, python, python3.

.PARAMETER ExtraArgs
  Extra watcher arguments, e.g. '--desktop-exe MyCodex.exe'.
#>
[CmdletBinding()]
param(
    [string]$CodexBin,
    [string]$Python,
    [string]$ExtraArgs = ''
)

$ErrorActionPreference = 'Stop'
$Name = 'codex-desktop-auto-continue'
$ProjectDir = Split-Path -Parent $PSScriptRoot
$InstallDir = Join-Path $env:LOCALAPPDATA $Name
$Watcher = Join-Path $InstallDir 'codex_desktop_auto_continue.py'
$LogFile = Join-Path $InstallDir 'watcher.log'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'

function Resolve-Python {
    $candidates = @()
    if ($Python) { $candidates += , @($Python) }
    $candidates += , @('py', '-3')
    $candidates += , @('python')
    $candidates += , @('python3')
    foreach ($candidate in $candidates) {
        $exe = $candidate[0]
        $prefix = @($candidate | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = @(& $exe @prefix -c 'import sys; print(sys.version_info >= (3, 10)); print(sys.executable)' 2>$null)
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or $out.Count -lt 2 -or $out[0] -ne 'True') { continue }
        return $out[1].Trim()
    }
    throw 'Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/windows/ or pass -Python <path>.'
}

function Resolve-Codex {
    if ($CodexBin) {
        if (-not (Test-Path -LiteralPath $CodexBin)) { throw "Codex executable not found: $CodexBin" }
        return (Resolve-Path -LiteralPath $CodexBin).Path
    }
    if ($env:CODEX_CLI_PATH -and (Test-Path -LiteralPath $env:CODEX_CLI_PATH)) { return $env:CODEX_CLI_PATH }
    $command = Get-Command codex -CommandType Application -ErrorAction SilentlyContinue |
        Sort-Object { if ($_.Source -like '*.exe') { 0 } else { 1 } } |
        Select-Object -First 1
    if ($command) { return $command.Source }
    throw 'No Codex CLI was found. Install it (npm install -g @openai/codex) or pass -CodexBin <path to codex.exe>.'
}

function Stop-Watcher {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($Watcher) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

$pythonExe = Resolve-Python
$pythonw = Join-Path (Split-Path -Parent $pythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) {
    Write-Warning "pythonw.exe was not found next to $pythonExe; the watcher will run with a console window."
    $pythonw = $pythonExe
}
$codex = Resolve-Codex

# Restart on upgrades so an already-running watcher loads the copied code.
try { Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction Stop } catch { }
Stop-Watcher

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -LiteralPath (Join-Path $ProjectDir 'codex_desktop_auto_continue.py') -Destination $Watcher -Force

$arguments = "`"$Watcher`" --codex-bin `"$codex`" --max-attempts 10000 --goal-desktop-ui --log-file `"$LogFile`""
if ($ExtraArgs) { $arguments = "$arguments $ExtraArgs" }

$mode = $null
try {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'Resume Goals and queue ordinary Codex failures' -Force | Out-Null
    Remove-ItemProperty -Path $RunKey -Name $Name -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $Name
    $mode = "scheduled task '$Name'"
} catch {
    Write-Warning "Task Scheduler registration failed ($($_.Exception.Message)); using the HKCU Run key instead."
    Set-ItemProperty -Path $RunKey -Name $Name -Value "`"$pythonw`" $arguments"
    Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $InstallDir -WindowStyle Hidden
    $mode = "HKCU Run entry '$Name'"
}

Write-Host "Installed and started codex-desktop-auto-continue ($mode)"
Write-Host "Python: $pythonw"
Write-Host "Codex:  $codex"
Write-Host "Logs:   Get-Content -Wait -Tail 50 `"$LogFile`""
