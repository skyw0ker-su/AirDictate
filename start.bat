@echo off
rem AirDictate: start in the background under the supervisor (auto-restart if it exits unexpectedly)
del "%~dp0stopped.flag" 2>nul
start "" /b powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0supervisor.ps1"
