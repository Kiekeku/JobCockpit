[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9-]+$')]
    [string]$TaskKey,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$UvArguments,

    # Finish cleanup and write the summary before the existing two-hour task limit.
    [ValidateRange(1, 6900)]
    [int]$MaxRuntimeSeconds = 6900
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$nodePath = $null
$launcherArguments = $null
$taskLogDirectory = Join-Path (Join-Path $repositoryRoot "var\scheduled-tasks") $TaskKey
$runId = "{0}-{1}" -f (Get-Date).ToString("yyyyMMdd-HHmmss-fff"), $PID
$stdoutPath = Join-Path $taskLogDirectory "$runId.stdout.log"
$stderrPath = Join-Path $taskLogDirectory "$runId.stderr.log"
$summaryPath = Join-Path $taskLogDirectory "$runId.summary.json"
$latestSummaryPath = Join-Path $taskLogDirectory "latest.summary.json"
$startedAt = Get-Date
$exitCode = 1
$launchError = $null
$process = $null

New-Item -ItemType Directory -Path $taskLogDirectory -Force | Out-Null

try {
    # Retain the existing scheduled-task parameter contract, but do not let a
    # routine ingestion run reinstall a package used by another process.
    if ($UvArguments -notmatch '^run\s+jobcockpit(?:\s+|$)') {
        throw "Expected UvArguments to begin with 'run jobcockpit'"
    }
    $cliArguments = $UvArguments -replace '^run\s+jobcockpit\s*', ''
    $nodePath = (Get-Command node.exe -ErrorAction Stop).Source
    $launcher = Join-Path $repositoryRoot "scripts\cli.mjs"
    $launcherArguments = '"' + $launcher + '" ' + $cliArguments

    $process = Start-Process `
        -FilePath $nodePath `
        -ArgumentList $launcherArguments `
        -WorkingDirectory $repositoryRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru
    # Retain a process handle so Windows PowerShell preserves its native exit code.
    $null = $process.Handle
    if ($process.WaitForExit($MaxRuntimeSeconds * 1000)) {
        $process.WaitForExit()
        $exitCode = $process.ExitCode
    } else {
        & taskkill.exe /PID $process.Id /T /F *> $null
        $null = $process.WaitForExit(10000)
        $exitCode = 124
        $launchError = "Ingestion exceeded the $MaxRuntimeSeconds second runtime limit"
        $launchError | Add-Content -LiteralPath $stderrPath -Encoding utf8
    }
}
catch {
    $launchError = $_.Exception.Message
    if ($null -ne $process -and -not $process.HasExited) {
        & taskkill.exe /PID $process.Id /T /F *> $null
        $null = $process.WaitForExit(10000)
    }
    $launchError | Set-Content -LiteralPath $stderrPath -Encoding utf8
}
finally {
    if ($null -ne $process) { $process.Dispose() }
}

$completedAt = Get-Date
$summary = [ordered]@{
    task_key = $TaskKey
    run_id = $runId
    started_at = $startedAt.ToString("o")
    completed_at = $completedAt.ToString("o")
    duration_seconds = [Math]::Round(($completedAt - $startedAt).TotalSeconds, 3)
    max_runtime_seconds = $MaxRuntimeSeconds
    exit_code = $exitCode
    succeeded = ($exitCode -eq 0)
    executable = $nodePath
    arguments = $launcherArguments
    requested_arguments = $UvArguments
    working_directory = $repositoryRoot
    stdout_log = $stdoutPath
    stderr_log = $stderrPath
    launch_error = $launchError
}
$summaryJson = $summary | ConvertTo-Json -Depth 3
$summaryJson | Set-Content -LiteralPath $summaryPath -Encoding utf8
$summaryJson | Set-Content -LiteralPath $latestSummaryPath -Encoding utf8

exit $exitCode
