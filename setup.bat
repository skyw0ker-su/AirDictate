@echo off
rem AirDictate: remote setup wizard (it stops the running program while you set up, then starts it again)
start "" pythonw "%~dp0setup_wizard.py" --stop-app
