@echo off
rem Govorok: окно настроек + док-панель для OBS.
rem Чат показывается в доке OBS (Виды - Доки - Пользовательские доки браузера),
rem ссылка на него есть в окне настроек.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [Govorok] создаю виртуальное окружение...
    python -m venv .venv || goto :error
    echo [Govorok] устанавливаю зависимости...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)

echo [Govorok] запускаю окно настроек и док-панель...
".venv\Scripts\python.exe" main.py
goto :eof

:error
echo.
echo [Govorok] Не удалось подготовить окружение. Проверьте, что Python 3.10+ в PATH.
pause
