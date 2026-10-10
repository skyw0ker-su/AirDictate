# Сборка AirDictate.exe (папка dist\AirDictate) и архива для релиза.
# Нужен Python 3.12+ с пакетами из requirements.txt и PyInstaller.
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# модель распознавания речи (нужна для голосового управления Chrome)
$model = Join-Path $root "assets\vosk-model-small-ru"
if (-not (Test-Path (Join-Path $model "am\encoder.onnx"))) {
    python -m pip install huggingface_hub | Out-Null
    $env:HF_HUB_DISABLE_XET = "1"
    python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='alphacep/vosk-model-small-ru', local_dir=r'$model')"
}

python -m PyInstaller --noconfirm --clean --onedir --noconsole --name AirDictate `
    --add-data "$root\assets\remote.png;assets" `
    --add-data "$model\am;assets\vosk-model-small-ru\am" `
    --add-data "$model\lang;assets\vosk-model-small-ru\lang" `
    --collect-all hid --collect-all sherpa_onnx `
    --exclude-module torch --exclude-module transformers --exclude-module matplotlib `
    --exclude-module scipy --exclude-module pandas `
    --distpath "$root\dist" --workpath "$root\build" --specpath "$root\build" `
    airdictate_main.py

Copy-Item (Join-Path $root "README.md") (Join-Path $root "dist\AirDictate\README.md") -Force
"готово: dist\AirDictate\AirDictate.exe"
