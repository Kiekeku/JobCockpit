[CmdletBinding()]
param(
    [string]$TaskName = "JobCockpit Status Reconciliation",
    [string]$AfternoonTaskName = "",
    [ValidateRange(1, 10000)]
    [int]$MidnightLimit = 1000,
    [ValidateRange(0, 10000)]
    [int]$MidnightRenderedLimit = 1000,
    [ValidateRange(1, 7200)]
    [int]$MidnightMaxRuntimeSeconds = 7200,
    [ValidateRange(1, 10000)]
    [int]$AfternoonLimit = 100,
    [ValidateRange(0, 10000)]
    [int]$AfternoonRenderedLimit = 30,
    [ValidateRange(1, 7200)]
    [int]$AfternoonMaxRuntimeSeconds = 2700
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $repositoryRoot "scripts\run_job_status_reconciliation.ps1"
$jobCockpit = Join-Path $repositoryRoot "scripts\cli.mjs"

if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "Status reconciliation runner was not found at $runner"
}

if (-not (Test-Path -LiteralPath $jobCockpit -PathType Leaf)) {
    throw "JobCockpit CLI was not found at $jobCockpit"
}
$nodePath = (Get-Command node.exe -ErrorAction Stop).Source
& $nodePath $jobCockpit --help | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "JobCockpit CLI runtime preflight failed; prepare it with pnpm dev"
}

$powerShell = Join-Path $PSHOME "powershell.exe"
if (-not (Test-Path -LiteralPath $powerShell -PathType Leaf)) {
    $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
}

if ([string]::IsNullOrWhiteSpace($AfternoonTaskName)) {
    $AfternoonTaskName = "$TaskName - 2 PM"
}
if ($AfternoonTaskName -eq $TaskName) {
    throw "Midnight and afternoon task names must be different"
}

function Register-ReconciliationTask {
    param(
        [string]$Name,
        [string]$RunTime,
        [string]$Description,
        [int]$Limit,
        [int]$RenderedLimit,
        [int]$MaxRuntimeSeconds
    )

    $actionArguments = (
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" ' +
        "-Limit {1} -RenderedLimit {2} -MaxRuntimeSeconds {3}"
    ) -f $runner, $Limit, $RenderedLimit, $MaxRuntimeSeconds
    $action = New-ScheduledTaskAction `
        -Execute $powerShell `
        -Argument $actionArguments `
        -WorkingDirectory $repositoryRoot
    $timeOfDay = [TimeSpan]::ParseExact(
        $RunTime, 'hh\:mm', [Globalization.CultureInfo]::InvariantCulture
    )
    $trigger = New-ScheduledTaskTrigger `
        -Daily `
        -At ([DateTime]::Today.Add($timeOfDay))
    $principal = New-ScheduledTaskPrincipal `
        -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive `
        -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Seconds ($MaxRuntimeSeconds + 600)) `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable
    $task = New-ScheduledTask `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description $Description

    Register-ScheduledTask -TaskName $Name -InputObject $task -Force | Out-Null
}

Register-ReconciliationTask `
    -Name $TaskName `
    -RunTime "00:00" `
    -Description "High-throughput midnight JobCockpit status reconciliation." `
    -Limit $MidnightLimit `
    -RenderedLimit $MidnightRenderedLimit `
    -MaxRuntimeSeconds $MidnightMaxRuntimeSeconds

Register-ReconciliationTask `
    -Name $AfternoonTaskName `
    -RunTime "14:00" `
    -Description "Bounded 2 PM JobCockpit status reconciliation." `
    -Limit $AfternoonLimit `
    -RenderedLimit $AfternoonRenderedLimit `
    -MaxRuntimeSeconds $AfternoonMaxRuntimeSeconds

Get-ScheduledTask -TaskName @($TaskName, $AfternoonTaskName)
