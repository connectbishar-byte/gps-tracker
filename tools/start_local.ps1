# Starts the TCP receiver and the web server in two PowerShell windows (Windows, local trial).
# Run from the project folder after: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
Push-Location $root
& $py manage.py migrate
& $py manage.py ensure_admin
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root'; & '$py' manage.py run_receiver --host 0.0.0.0 --port 5027"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root'; & '$py' manage.py runserver 127.0.0.1:8000"
Start-Sleep -Seconds 4
Test-NetConnection -ComputerName 127.0.0.1 -Port 5027 | Select-Object ComputerName, RemotePort, TcpTestSucceeded
Start-Process "http://127.0.0.1:8000"
Pop-Location
