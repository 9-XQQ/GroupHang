param(
    [ValidateSet('Install', 'Status', 'Restart', 'Uninstall')]
    [string]$Mode = 'Status',
    [string]$PythonPath
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$workerPath = Join-Path $projectRoot 'scripts\cleanup_agent_state.py'
$statusPath = Join-Path $projectRoot 'logs\agent_retention_status.json'
$hasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $digest = $hasher.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant()))
    $suffix = ([System.BitConverter]::ToString($digest)).Replace('-', '').Substring(0, 12)
} finally {
    $hasher.Dispose()
}
$taskName = "TripPlanner-AgentRetention-$suffix"
$description = "TripPlanner retention worker for $projectRoot"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
# Refuse to replace/remove a task unless it is owned by this checkout.
if ($existing -and ($existing.Description -ne $description -or
        @($existing.Actions).Count -ne 1 -or
        $existing.Actions[0].WorkingDirectory -ne $projectRoot)) {
    throw "Task name collision: $taskName is not owned by this checkout."
}
if ($Mode -eq 'Uninstall') {
    if ($existing) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Write-Output "Removed project retention task: $taskName"
    exit 0
}
if ($Mode -eq 'Restart') {
    if (-not $existing) { throw "Retention task is not installed: $taskName" }
    Stop-ScheduledTask -TaskName $taskName
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(10)
    while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
        if ([DateTimeOffset]::UtcNow -ge $deadline) { throw 'Worker did not stop within ten seconds.' }
        Start-Sleep -Milliseconds 100
    }
    Start-ScheduledTask -TaskName $taskName
    Write-Output "Restarted project retention task: $taskName"
    exit 0
}
if ($Mode -eq 'Status') {
    if (-not $existing) {
        Write-Output "Retention task is not installed: $taskName"
        exit 1
    }
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    [PSCustomObject]@{
        TaskName = $taskName
        State = $existing.State
        LastRunTime = $info.LastRunTime
        LastTaskResult = $info.LastTaskResult
        WorkingDirectory = $existing.Actions[0].WorkingDirectory
        StatusFile = $statusPath
    } | Format-List
    if (Test-Path -LiteralPath $statusPath) {
        $heartbeat = Get-Content -LiteralPath $statusPath -Encoding UTF8 -Raw | ConvertFrom-Json
        if (-not $heartbeat.last_success_at) {
            Write-Output 'Worker has not completed a successful sweep.'
            exit 1
        }
        $age = ([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($heartbeat.last_success_at)).TotalSeconds
        $healthy = $age -ge 0 -and $age -le 30 -and $heartbeat.consecutive_failures -eq 0
        [PSCustomObject]@{
            Healthy = $healthy
            LastSuccessAt = $heartbeat.last_success_at
            SuccessAgeSeconds = [Math]::Round($age, 1)
            ConsecutiveFailures = $heartbeat.consecutive_failures
            LastErrorType = $heartbeat.last_error_type
        } | Format-List
        if (-not $healthy) { exit 1 }
    } else {
        Write-Output 'No worker heartbeat has been written.'
        exit 1
    }
    exit 0
}
if (-not $PythonPath) {
    throw 'Install requires -PythonPath pointing to the route_plan python.exe.'
}
$resolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path
if ([System.IO.Path]::GetFileName($resolvedPython) -ne 'python.exe') {
    throw 'PythonPath must point to python.exe; the installer uses its pythonw.exe sibling.'
}
$hiddenPython = Join-Path (Split-Path -Parent $resolvedPython) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $hiddenPython)) {
    throw "Hidden Python interpreter not found: $hiddenPython"
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$action = New-ScheduledTaskAction -Execute $hiddenPython -WorkingDirectory $projectRoot -Argument (
    '"{0}" --watch --status-file "{1}"' -f $workerPath, $statusPath
)
$triggers = @(
    New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
    New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
)
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$task = New-ScheduledTask -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description $description
Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output "Installed and started: $taskName"
Write-Output "Runs independently while the current Windows user is logged in; no password is stored."
Write-Output "Check: python scripts/cleanup_agent_state.py --status"
