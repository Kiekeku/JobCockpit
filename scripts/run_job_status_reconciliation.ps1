[CmdletBinding()]
param(
    [ValidateRange(1, 10000)]
    [int]$Limit = 100,
    [ValidateRange(0, 10000)]
    [int]$RenderedLimit = 30,
    [ValidateRange(1, 7200)]
    [int]$MaxRuntimeSeconds = 5400
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$jobCockpit = Join-Path $repositoryRoot "scripts\cli.mjs"
$logDirectory = Join-Path $repositoryRoot "var\logs\job-status-reconciliation"

function ConvertTo-SafeDiagnostic {
    param([object]$Value)

    $message = [string]$Value
    $message = [regex]::Replace($message, "https?://\S+", "[url]", "IgnoreCase")
    $message = [regex]::Replace($message, "\s+", " ").Trim()
    if ($message.Length -gt 400) {
        return $message.Substring(0, 400)
    }
    return $message
}

function New-FailureResult {
    param(
        [string]$ErrorCode,
        [string]$Message
    )

    return [ordered]@{
        run_id = $null
        status = "failed"
        selected = 0
        checked = 0
        open = 0
        closed = 0
        expired = 0
        inconclusive = 0
        failed = 0
        deferred = 0
        skipped = 0
        error_code = $ErrorCode
        error = ConvertTo-SafeDiagnostic $Message
    }
}

function ConvertFrom-JsonOutput {
    param([string[]]$Streams)

    foreach ($stream in $Streams) {
        if ([string]::IsNullOrWhiteSpace($stream)) {
            continue
        }
        $trimmed = $stream.Trim()
        $candidates = New-Object System.Collections.Generic.List[string]
        $candidates.Add($trimmed)
        $lines = $trimmed -split "\r?\n"
        for ($index = $lines.Count - 1; $index -ge 0; $index--) {
            $line = $lines[$index].Trim()
            if ($line -and $line -ne $trimmed) {
                $candidates.Add($line)
            }
        }
        foreach ($candidate in $candidates) {
            try {
                $payload = $candidate | ConvertFrom-Json -ErrorAction Stop
                if ($null -ne $payload -and -not ($payload -is [string])) {
                    return $payload
                }
            } catch {
                continue
            }
        }
    }
    return $null
}

function Stop-ProcessTree {
    param([System.Diagnostics.Process]$Process)

    if ($null -eq $Process) {
        return
    }
    try {
        if ($Process.HasExited) {
            return
        }
    } catch {
        return
    }
    $treeKilled = $false
    try {
        $taskkillPath = Join-Path $env:SystemRoot "System32\taskkill.exe"
        $killInfo = New-Object System.Diagnostics.ProcessStartInfo
        $killInfo.FileName = $taskkillPath
        $killInfo.Arguments = "/PID $($Process.Id) /T /F"
        $killInfo.UseShellExecute = $false
        $killInfo.CreateNoWindow = $true
        $killInfo.RedirectStandardOutput = $true
        $killInfo.RedirectStandardError = $true
        $killer = [System.Diagnostics.Process]::Start($killInfo)
        if ($null -ne $killer) {
            $null = $killer.StandardOutput.ReadToEnd()
            $null = $killer.StandardError.ReadToEnd()
            $killer.WaitForExit()
            $treeKilled = $killer.ExitCode -eq 0
            $killer.Dispose()
        }
    } catch {
        $treeKilled = $false
    }
    if (-not $treeKilled) {
        try {
            $Process.Kill()
        } catch {
            return
        }
    }
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory ("status-reconciliation-{0}.jsonl" -f (Get-Date -Format "yyyyMMdd"))
$startedAt = (Get-Date).ToUniversalTime()
$exitCode = 1
$result = $null
$process = $null
$stdout = ""
$stderr = ""
$streamsReady = $false

try {
    if (-not (Test-Path -LiteralPath $jobCockpit -PathType Leaf)) {
        $result = New-FailureResult "cli_not_found" "JobCockpit CLI was not found"
    } else {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = (Get-Command node.exe -ErrorAction Stop).Source
        $startInfo.WorkingDirectory = $repositoryRoot
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        # Scheduled Firefox renders run in independently cancellable worker trees.
        $startInfo.EnvironmentVariables["JOBCOCKPIT_POSTING_RENDERED_FALLBACK_PROVIDER"] = "invisible_playwright"
        $startInfo.Arguments = (
            '"{0}" jobs reconcile-status --json --limit {1} --rendered-limit {2} ' +
            "--max-runtime-seconds {3}"
        ) -f $jobCockpit, $Limit, $RenderedLimit, $MaxRuntimeSeconds

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        if (-not $process.Start()) {
            throw "JobCockpit CLI did not start"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $runnerGraceSeconds = 5
        $waitMilliseconds = [Math]::Min(
            [int]::MaxValue,
            ($MaxRuntimeSeconds + $runnerGraceSeconds) * 1000
        )
        $completed = $process.WaitForExit($waitMilliseconds)
        if (-not $completed) {
            Stop-ProcessTree $process
            $terminated = $process.WaitForExit(10000)
            if ($terminated) {
                $process.WaitForExit()
                $streamsReady = $true
            }
            $exitCode = 124
            $result = New-FailureResult (
                "runner_timeout"
            ) (
                "JobCockpit CLI exceeded the runner timeout"
            )
        } else {
            $process.WaitForExit()
            $streamsReady = $true
            $exitCode = $process.ExitCode
        }
        if ($streamsReady) {
            $stdout = $stdoutTask.GetAwaiter().GetResult()
            $stderr = $stderrTask.GetAwaiter().GetResult()
        }

        if ($null -eq $result) {
            $result = ConvertFrom-JsonOutput @($stdout, $stderr)
            if ($null -eq $result) {
                $diagnostic = ConvertTo-SafeDiagnostic (
                    @($stderr, $stdout) -join " "
                )
                if ([string]::IsNullOrWhiteSpace($diagnostic)) {
                    $diagnostic = "JobCockpit CLI returned invalid JSON"
                }
                $result = New-FailureResult "invalid_cli_json" $diagnostic
                if ($exitCode -eq 0) {
                    $exitCode = 1
                }
            }
        }
    }
} catch {
    Stop-ProcessTree $process
    $result = New-FailureResult "runner_start_failed" $_.Exception.Message
    if ($exitCode -eq 0) {
        $exitCode = 1
    }
} finally {
    if ($null -ne $process) {
        $process.Dispose()
    }
}

$record = [ordered]@{
    started_at = $startedAt.ToString("o")
    finished_at = (Get-Date).ToUniversalTime().ToString("o")
    exit_code = $exitCode
    result = $result
}
$record | ConvertTo-Json -Compress -Depth 10 | Add-Content -LiteralPath $logPath -Encoding utf8
exit $exitCode
