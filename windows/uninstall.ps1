<#
.SYNOPSIS
  Remove codex-desktop-auto-continue for the current Windows user.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Name = 'codex-desktop-auto-continue'
$InstallDir = Join-Path $env:LOCALAPPDATA $Name
$Watcher = Join-Path $InstallDir 'codex_desktop_auto_continue.py'

try { Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction Stop } catch { }
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name $Name -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($Watcher) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500
if (Test-Path -LiteralPath $InstallDir) { Remove-Item -LiteralPath $InstallDir -Recurse -Force }

Write-Host "Removed codex-desktop-auto-continue and $InstallDir"
