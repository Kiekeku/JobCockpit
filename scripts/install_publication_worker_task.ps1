[CmdletBinding()]
param([string]$TaskName = "JobCockpit Publication Worker")

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "run_publication_worker.ps1"
$jobCockpit = Join-Path $repositoryRoot "scripts\cli.mjs"
if (-not (Test-Path -LiteralPath $jobCockpit -PathType Leaf)) {
    throw "JobCockpit CLI was not found at $jobCockpit"
}
$nodePath = (Get-Command node.exe -ErrorAction Stop).Source
& $nodePath $jobCockpit --help | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "JobCockpit CLI runtime preflight failed; prepare it with pnpm dev"
}
$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$userName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $powerShell `
    -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $runner) `
    -WorkingDirectory $repositoryRoot
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $userName),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$principal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$task = New-ScheduledTask -Action $action -Trigger $triggers -Principal $principal `
    -Settings $settings -Description "Process optional JobCockpit publications and status updates while signed in."
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName
