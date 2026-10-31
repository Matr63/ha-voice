@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "OUT=%~dp0ha-voice-kokoro-models.zip"
set "WORK=%~dp0ha-voice-models"
set "ENDPOINT="

if /I "%~1"=="mirror" set "ENDPOINT=https://hf-mirror.com"
if /I "%~1"=="--mirror" set "ENDPOINT=https://hf-mirror.com"
if /I "%~1"=="--endpoint" set "ENDPOINT=%~2"

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python not found. Install Python 3 and retry.
    exit /b 1
  )
  set "PY=python"
)

echo Installing huggingface_hub ...
%PY% -m pip install -q --upgrade huggingface_hub
if errorlevel 1 exit /b 1

echo.
echo Work: %WORK%
echo Out:  %OUT%
if defined ENDPOINT echo Mirror: %ENDPOINT%
echo.

if defined ENDPOINT (
  %PY% "%~dp0kokoro_models.py" --work "%WORK%" --out "%OUT%" --endpoint "%ENDPOINT%"
) else (
  %PY% "%~dp0kokoro_models.py" --work "%WORK%" --out "%OUT%"
)
set "RC=%ERRORLEVEL%"
echo.
if %RC%==0 (
  echo Done. Upload zip: HA Voice -^> Озвучка -^> Загрузить архив с ПК.
) else (
  echo Failed with code %RC%.
  echo Tip: retry with mirror:  %~nx0 mirror
)
exit /b %RC%
