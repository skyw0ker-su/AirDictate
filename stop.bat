@echo off
rem AirDictate: full stop. The flag file tells the supervisor not to restart the program.
echo stop> "%~dp0stopped.flag"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and ($_.CommandLine -like '*supervisor.ps1*' -or $_.CommandLine -like '*voice_remote.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Add-Type -Namespace U -Name M -MemberDefinition '[DllImport(\"user32.dll\")] public static extern short GetAsyncKeyState(int k); [DllImport(\"user32.dll\")] public static extern void mouse_event(int f,int x,int y,int d,int e); [DllImport(\"user32.dll\")] public static extern bool ClipCursor(IntPtr r);'; [U.M]::ClipCursor([IntPtr]::Zero) | Out-Null; if (([U.M]::GetAsyncKeyState(1) -band 0x8000) -ne 0) { [U.M]::mouse_event(4,0,0,0,0) }; if (([U.M]::GetAsyncKeyState(2) -band 0x8000) -ne 0) { [U.M]::mouse_event(16,0,0,0,0) }; Write-Host 'AirDictate stopped'"
pause
