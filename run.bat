@echo off
setlocal EnableExtensions
rem ===========================================================================
rem  Bridge Brief - one-command launcher for Windows
rem
rem    run.bat           start the web app on port 8765 and open the browser
rem    run.bat 9000      same, on another port
rem    run.bat build     build the data index from the raw downloads, then start
rem    run.bat test      run the test suite instead of the app
rem
rem  What it does, in order:
rem    1. finds Python 3.10 or newer
rem    2. installs the Python packages, first run only
rem    3. loads settings such as ANTHROPIC_API_KEY from a .env file, if present
rem    4. offers to build the data index if the raw downloads are there but
rem       have not been ingested yet
rem    5. starts the server and opens http://127.0.0.1:PORT in your browser
rem
rem  It never downloads a dataset and never writes to data\raw.
rem  Full instructions: HOW_TO_RUN.md and HOW_TO_SETUP.md
rem ===========================================================================

cd /d "%~dp0"
title Bridge Brief

set "PORT=8765"
set "MODE=serve"
if /i "%~1"=="test" set "MODE=test"
if /i "%~1"=="build" set "MODE=build"
if "%MODE%"=="serve" if not "%~1"=="" set "PORT=%~1"

rem ---- 1. Python -------------------------------------------------------------
call :find_python
if errorlevel 1 goto :no_python
for /f "delims=" %%V in ('%PY% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%V"
echo [ok]     Python %PYVER%, run as: %PY%

rem ---- 2. Packages -----------------------------------------------------------
%PY% -c "import PIL, anthropic, pytest" >nul 2>nul
if not errorlevel 1 goto :packages_ok
echo [setup]  Installing Python packages. First run only, takes about a minute.
%PY% -m pip install --disable-pip-version-check -e ".[dev,imagery,llm]"
if errorlevel 1 goto :pip_failed
:packages_ok
echo [ok]     Python packages installed

rem ---- 3. Settings from .env -------------------------------------------------
if not exist ".env" goto :env_done
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do set "%%A=%%B"
echo [ok]     Settings loaded from .env
:env_done

set "DATA=%~dp0data"
if defined BRIDGE_BRIEF_DATA set "DATA=%BRIDGE_BRIEF_DATA%"
set "INDEX=%DATA%\derived\assets.sqlite"

if "%MODE%"=="test" goto :run_tests
if "%MODE%"=="build" goto :build

rem ---- 4. Data index ---------------------------------------------------------
call :index_has_data
if not errorlevel 1 goto :index_ok
if exist "%DATA%\raw\nbi" goto :offer_build
echo.
echo [note]   No data index found at %INDEX%
echo          The app will still open, but no bridges will be searchable and
echo          no report can be generated. HOW_TO_SETUP.md, step 4, explains
echo          which public datasets to download and where to put them.
echo.
goto :serve

:offer_build
echo.
echo [note]   The raw datasets are in %DATA%\raw but have not been ingested yet.
choice /c YN /m "         Build the data index now? About 5 minutes"
if errorlevel 2 goto :serve
goto :build

:index_ok
echo [ok]     Data index found: %INDEX%
goto :serve

rem ---- Build the index from the raw downloads --------------------------------
:build
echo.
echo [build]  Building the data index from %DATA%\raw
echo          Every step is resumable: if it is interrupted, run it again.
echo.
echo [build]  1/5  National Bridge Inventory, 2023 and 2025
%PY% -m src.ingest.nbi --year 2023 --year 2025
if errorlevel 1 goto :build_failed
echo [build]  2/5  Element-level condition data, 2023 and 2025
%PY% -m src.ingest.nbe --year 2023 --year 2025
if errorlevel 1 goto :build_failed
echo [build]  3/5  Coverage table and missing-evidence findings
%PY% -m src.catalog --record-missing
if errorlevel 1 goto :build_failed
echo [build]  4/5  Contradiction engine over 2023
%PY% -m src.analysis.contradictions --year 2023
if errorlevel 1 goto :build_failed
if not exist "%DATA%\raw\dacl10k" goto :skip_dacl10k
echo [build]  5/5  dacl10k reference imagery
%PY% -m src.ingest.dacl10k
if errorlevel 1 goto :build_failed
goto :build_done
:skip_dacl10k
echo [build]  5/5  dacl10k not found in %DATA%\raw\dacl10k, skipped. It is optional.
:build_done
echo.
echo [ok]     Data index built.
goto :serve

rem ---- 5. Start the web app --------------------------------------------------
:serve
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul
if errorlevel 1 goto :port_free
echo.
echo [note]   Something is already running on port %PORT%, probably Bridge Brief.
echo          Opening it in the browser. To start a second copy: run.bat 8766
start "" "http://127.0.0.1:%PORT%/"
goto :end

:port_free
if defined ANTHROPIC_API_KEY goto :vision_on
echo [note]   No ANTHROPIC_API_KEY: photographs will be read by the classical
echo          baseline detector. See HOW_TO_RUN.md to switch on Claude vision.
goto :launch
:vision_on
echo [ok]     ANTHROPIC_API_KEY is set: photographs will be read by Claude vision.

:launch
echo.
echo ===========================================================================
echo   Bridge Brief is starting at  http://127.0.0.1:%PORT%/
echo   The browser opens by itself. Keep this window open while you use it.
echo   Press Ctrl+C in this window to stop the server.
echo ===========================================================================
echo.
start "" /b %PY% -c "import time, webbrowser; time.sleep(2); webbrowser.open('http://127.0.0.1:%PORT%/')"
%PY% -m src.ui.server --port %PORT%
if errorlevel 1 pause
goto :end

rem ---- Tests -----------------------------------------------------------------
:run_tests
%PY% -m pytest -q
if errorlevel 1 pause
goto :end

rem ---- Helpers ---------------------------------------------------------------
:find_python
set "PY="
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    exit /b 0
)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 (
    set "PY=py -3"
    exit /b 0
)
exit /b 1

:index_has_data
if not exist "%INDEX%" exit /b 1
%PY% -c "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); sys.exit(0 if c.execute('SELECT 1 FROM structures LIMIT 1').fetchone() else 1)" "%INDEX%" >nul 2>nul
exit /b %errorlevel%

rem ---- Failures --------------------------------------------------------------
:no_python
echo.
echo [error]  Python 3.10 or newer was not found.
echo          Install it from https://www.python.org/downloads/ and tick
echo          "Add python.exe to PATH" in the installer, then run this again.
echo.
pause
exit /b 1

:pip_failed
echo.
echo [error]  Installing the Python packages failed. The pip output above says why.
echo          Most often this is no internet connection, or a very new Python
echo          that a package has no wheel for yet. Python 3.10 to 3.12 is safest.
echo.
pause
exit /b 1

:build_failed
echo.
echo [error]  Building the index stopped. The message above names the missing
echo          or unreadable file. HOW_TO_SETUP.md, step 4, shows where each
echo          download goes. Fix it and run:  run.bat build
echo.
pause
exit /b 1

:end
endlocal
