[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$jobCockpit = Join-Path $repositoryRoot "scripts\cli.mjs"
$nodePath = (Get-Command node.exe -ErrorAction Stop).Source
if (-not (Test-Path -LiteralPath $jobCockpit -PathType Leaf)) {
    throw "JobCockpit CLI was not found at $jobCockpit"
}
$logDirectory = Join-Path $repositoryRoot "var\logs\publication"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$runName = "worker-{0}-{1}" -f (Get-Date -Format "yyyyMMdd-HHmmss"), $PID
$process = Start-Process -FilePath $nodePath `
    -ArgumentList ('"{0}" worker publication' -f $jobCockpit) `
    -WorkingDirectory $repositoryRoot -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDirectory "$runName.stdout.log") `
    -RedirectStandardError (Join-Path $logDirectory "$runName.stderr.log") `
    -Wait -PassThru
exit $process.ExitCode
