$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = 'C:/Users/qasd1/.pyenv/pyenv-win/versions/3.11.9/python.exe'
$taskPrior = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*launch.py*--port 8766*' }
foreach ($taskProcess in $taskPrior) {
    & taskkill.exe /PID $taskProcess.ProcessId /T /F
}
Start-Process -FilePath $taskPython -ArgumentList @('launch.py','--no-browser','--port','8766') -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput "$taskRoot/.local/logs/server.stdout.log" -RedirectStandardError "$taskRoot/.local/logs/server.stderr.log"
