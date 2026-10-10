# AirDictate supervisor: restarts voice_remote.py if it exits unexpectedly.
# An intentional stop (stop.bat or tray "Exit") creates stopped.flag, then the supervisor stops too.
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$flag = Join-Path $dir "stopped.flag"
$log = Join-Path $dir "voice_remote.log"

# one supervisor only
$created = $false
$mutex = New-Object System.Threading.Mutex($true, "Global\AirDictateSupervisor", [ref]$created)
if (-not $created) { exit }

$py = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $py) {
    $pyExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($pyExe) { $py = Join-Path (Split-Path $pyExe) "pythonw.exe" }
}
if (-not $py) { exit }

$fast = 0
Add-Content -Path $log -Value ((Get-Date).ToString("HH:mm:ss") + " supervisor: started") -Encoding UTF8
trap { Add-Content -Path $log -Value ((Get-Date).ToString("HH:mm:ss") + " supervisor: error " + $_) -Encoding UTF8; break }
while (-not (Test-Path $flag)) {
    $started = Get-Date
    $p = Start-Process $py -ArgumentList ('"' + (Join-Path $dir "voice_remote.py") + '"') -PassThru -WindowStyle Hidden
    $p.WaitForExit()
    if (Test-Path $flag) { break }
    $lived = ((Get-Date) - $started).TotalSeconds
    # a very short run usually means another copy already holds the instance lock: wait longer, do not spam
    if ($lived -lt 8) { $fast++ } else { $fast = 0 }
    $pause = if ($fast -ge 3) { 60 } else { 3 }
    Add-Content -Path $log -Value ((Get-Date).ToString("HH:mm:ss") + " supervisor: program exited, restarting in $pause s") -Encoding UTF8
    Start-Sleep -Seconds $pause
}
Add-Content -Path $log -Value ((Get-Date).ToString("HH:mm:ss") + " supervisor: stopped by stopped.flag") -Encoding UTF8
