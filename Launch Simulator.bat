@echo off
REM ---------------------------------------------------------------------
REM  Well Control Simulator - choke panel
REM
REM  Double-click this file to start the simulator. It opens in your
REM  browser at http://localhost:8020 and keeps running until you close
REM  this window.
REM
REM  This is the practice tool: you line the rig up, bring the pump on,
REM  and work the choke, and the well responds to what you actually do.
REM  The Kill Planner is the separate tool for designing the kill.
REM
REM  First run takes a minute while it builds the environment.
REM ---------------------------------------------------------------------

cd /d "%~dp0"
title Well Control Simulator

echo.
echo   Well Control Simulator
echo   ======================
echo.

REM -- Find a Python to build the environment with ----------------------
set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY%" (
    where py >nul 2>&1 && set "PY=py"
)
if not exist "%PY%" if not "%PY%"=="py" (
    echo   Python was not found.
    echo.
    echo   Install it with this command, then run this file again:
    echo       winget install Python.Python.3.13 --scope user
    echo.
    pause
    exit /b 1
)

REM -- Build the virtual environment on first run ------------------------
if not exist ".venv\Scripts\python.exe" (
    echo   First run - setting up. This takes a minute...
    echo.
    "%PY%" -m venv .venv
    if errorlevel 1 goto failed
    ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    ".venv\Scripts\python.exe" -m pip install --quiet -e ".[sim]"
    if errorlevel 1 goto failed
    echo   Setup complete.
    echo.
)

REM -- The web front end is an optional extra; add it if it is missing ---
".venv\Scripts\python.exe" -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo   Adding the simulator's web front end...
    ".venv\Scripts\python.exe" -m pip install --quiet -e ".[sim]"
    if errorlevel 1 goto failed
)

echo   Starting at http://localhost:8020
echo.
echo     /             the rig screens - practise on your own, no login
echo     /instructor   instructor station - run a class
echo     /join         where a student enters a class code
echo.
echo   To run a class for other people, deploy with Docker instead:
echo   this launcher is bound to localhost and nobody else can reach it.
echo.
echo   Close this window to stop the simulator.
echo.

start "" "http://localhost:8020/"

REM  Bound to localhost only: this is a training tool on your machine,
REM  not something to expose on a network.
".venv\Scripts\python.exe" -m uvicorn wellcontrol.sim.api:app ^
    --host 127.0.0.1 ^
    --port 8020

goto :eof

:failed
echo.
echo   Setup failed. See the messages above.
echo.
pause
exit /b 1
