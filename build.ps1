# Сборка AirDictate.exe (папка dist\AirDictate) и архива для релиза.
# Нужен Python 3.12+ с пакетами из requirements.txt и PyInstaller.
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

python -m PyInstaller --noconfirm --clean --onedir --noconsole --name AirDictate `
    --add-data "$root\assets\remote.png;assets" `
    --collect-all hid `
    --exclude-module torch --exclude-module transformers --exclude-module matplotlib `
    --exclude-module scipy --exclude-module pandas `
    --distpath "$root\dist" --workpath "$root\build" --specpath "$root\build" `
    airdictate_main.py

Copy-Item (Join-Path $root "README.md") (Join-Path $root "dist\AirDictate\README.md") -Force
"готово: dist\AirDictate\AirDictate.exe"
