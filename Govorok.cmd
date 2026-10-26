@echo off
rem Govorok - single launcher for both the sources and the portable package.
rem
rem   Govorok.cmd                      settings window (+ OBS dock panel)
rem   Govorok.cmd dock -c ch --port N  headless: bot + dock only (for autostart)
rem   Govorok.cmd test [--only stt]    self tests (no network, no keys needed)
rem   Govorok.cmd check [mic/mem/unload]  microphone and subtitles diagnostics
rem   Govorok.cmd models               pre-download faster-whisper weights
rem   Govorok.cmd pip install ...       pip inside the resolved environment
rem   Govorok.cmd --help               application help
rem
rem Python resolution: bundled .\python.exe (portable package) -> .venv -> PATH.
rem ASCII on purpose: cmd reads .cmd in the OEM code page, so non-ASCII text turns
rem into garbage there. Sub-commands read %~2, %3 - no shift: in cmd %* is expanded
rem before an inline shift takes effect, which silently leaked the word "dock"/"test"
rem into the application command line.
setlocal EnableExtensions
cd /d "%~dp0"
set "ROOT=%CD%"

set "PACKAGED="
if exist "%ROOT%\python.exe" (
    set "PYTHON=%ROOT%\python.exe"
    set "PYW=%ROOT%\pythonw.exe"
    set "PACKAGED=1"
    goto :dispatch
)
if exist "%ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
    set "PYW=%ROOT%\.venv\Scripts\pythonw.exe"
    goto :dispatch
)
where python >nul 2>nul
if errorlevel 1 goto :nopython
set "PYTHON=python"
set "PYW=pythonw"

:dispatch
if "%~1"==""      goto :gui
if /i "%~1"=="dock"    goto :dock
if /i "%~1"=="test"    goto :test
if /i "%~1"=="check"   goto :check
if /i "%~1"=="diag"    goto :check
if /i "%~1"=="models"  goto :models
if /i "%~1"=="pip"     goto :pip
if /i "%~1"=="gui"     goto :gui_args
if /i "%~1"=="window"  goto :gui_args
rem first word is not a sub-command: hand everything to main.py as is
%PYTHON% main.py %*
exit /b %ERRORLEVEL%

:dock
%PYTHON% main.py --dock %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

:test
%PYTHON% selftest.py %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

:models
%PYTHON% tools\fetch_models.py %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

:pip
%PYTHON% -m pip %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

rem diagnostics: live_check by default, a sub-command picks a narrower tool
:check
if /i "%~2"=="mic"    ( %PYTHON% tools\mic_probe.py %3 & exit /b %ERRORLEVEL% )
if /i "%~2"=="mem"    ( %PYTHON% tools\mem_probe.py %3 & exit /b %ERRORLEVEL% )
if /i "%~2"=="unload" ( %PYTHON% tools\unload_check.py %3 & exit /b %ERRORLEVEL% )
%PYTHON% tools\live_check.py %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

rem window with explicit arguments (mostly "gui --help")
:gui_args
%PYTHON% main.py %2 %3 %4 %5 %6 %7 %8 %9
exit /b %ERRORLEVEL%

rem in the package the window starts without a console; if it does not appear,
rem run "Govorok.cmd gui --help" or plain python to see the error text
:gui
if "%PACKAGED%"=="1" (
    start "" "%PYW%" main.py
    exit /b 0
)
%PYTHON% main.py
exit /b %ERRORLEVEL%

:nopython
echo Python was not found.
echo  Portable package: unpack the WHOLE archive, it ships its own python.exe.
echo  Sources:  python -m venv .venv  ^&^&  .venv\Scripts\pip install -r requirements.txt
pause
exit /b 1
