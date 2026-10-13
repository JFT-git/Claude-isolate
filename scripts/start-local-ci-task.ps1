# Register and start local CI outside the SSH session so a network blip
# cannot kill QEMU. Does not stop Chrome, Firefox, Edge, or Outlook.
param(
    [string]$GitCommit = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Name = 'ClaudeIsolateLocalCI'
$Root = 'C:\ClaudeIsolateTest\build\Claude-isolate'
$Script = Join-Path $Root 'scripts\local-ci-windows.ps1'
$Log = 'C:\ClaudeIsolateTest\agent\local-ci.log'
if (-not (Test-Path -LiteralPath $Script)) { throw "Missing $Script" }
New-Item -ItemType Directory -Force -Path 'C:\ClaudeIsolateTest\agent' | Out-Null
Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Log -ErrorAction SilentlyContinue
$arg = "-NoProfile -ExecutionPolicy Bypass -File `"$Script`""
if ($GitCommit) { $arg += " -GitCommit $GitCommit" }
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddDays(300))
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew
# SSH sets USERDOMAIN to WORKGROUP, which is not a logon account.
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $Name
$deadline = (Get-Date).AddSeconds(45)
do {
    Start-Sleep -Seconds 2
    $task = Get-ScheduledTask -TaskName $Name
    if ($task.State -eq 'Running') { break }
} while ((Get-Date) -lt $deadline)
$task = Get-ScheduledTask -TaskName $Name
$info = Get-ScheduledTaskInfo -TaskName $Name
Write-Output ("TASK {0} state={1} result={2}" -f $Name, $task.State, $info.LastTaskResult)
if ($task.State -ne 'Running' -and -not (Test-Path -LiteralPath $Log)) {
    throw "Scheduled task $Name did not start (state=$($task.State) result=$($info.LastTaskResult))"
}
