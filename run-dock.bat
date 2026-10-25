@echo off
rem Govorok без окна: чат и перевод идут только в док-панель OBS.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [Govorok] создаю виртуальное окружение...
    python -m venv .venv || goto :error
    echo [Govorok] устанавливаю зависимости...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)

".venv\Scripts\python.exe" main.py --dock %*
pause
goto :eof

:error
echo.
echo [Govorok] Не удалось подготовить окружение. Проверьте, что Python 3.10+ в PATH.
pause
