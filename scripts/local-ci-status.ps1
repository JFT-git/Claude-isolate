# Print PASS, FAIL, or RUNNING for the detached local CI log.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$Log = 'C:\ClaudeIsolateTest\agent\local-ci.log'
if (Test-Path -LiteralPath $Log) {
    $tail = @(Get-Content -LiteralPath $Log -Tail 12 -ErrorAction SilentlyContinue)
    $text = $tail -join "`n"
    if ($text -match 'LOCAL CI PASS') { Write-Output 'PASS'; exit 0 }
    if ($text -match 'LOCAL CI FAIL') { Write-Output 'FAIL'; exit 0 }
    $line = if ($tail.Count) { ($tail[-1] -replace '\s+', ' ').Trim() } else { 'empty' }
    if ($line.Length -gt 180) { $line = $line.Substring($line.Length - 180) }
    Write-Output "RUNNING $line"
    exit 0
}
$task = Get-ScheduledTask -TaskName 'ClaudeIsolateLocalCI' -ErrorAction SilentlyContinue
if ($task) { Write-Output "RUNNING task=$($task.State)" } else { Write-Output 'RUNNING no-log' }
exit 0
