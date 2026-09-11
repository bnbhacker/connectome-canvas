# Keeps the studio alive outside any terminal or browser pane: restarts it if it dies,
# appends everything to studio.log. Configuration comes from ..\.env (see .env.example).
#
#   powershell -ExecutionPolicy Bypass -File tools\studio.ps1            # foreground, Ctrl+C to stop
#   Start-Process powershell -ArgumentList '-ExecutionPolicy','Bypass','-File','tools\studio.ps1' -WindowStyle Hidden
#
# Stop it for good by creating the file studio.stop next to run.py.

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$log = Join-Path $root "studio.log"
$stopFile = Join-Path $root "studio.stop"
if (Test-Path $stopFile) { Remove-Item $stopFile }

while ($true) {
    "$(Get-Date -Format s) studio starting" | Add-Content $log
    & py -3 run.py serve --port 4660 --tunnel --autopublish 2>&1 | Add-Content $log
    "$(Get-Date -Format s) studio exited" | Add-Content $log
    if (Test-Path $stopFile) { "$(Get-Date -Format s) stop file found, not restarting" | Add-Content $log; break }
    Start-Sleep -Seconds 15
}
