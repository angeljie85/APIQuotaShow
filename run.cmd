@echo off
rem Kimi Usage Widget - launcher
rem Picks the first pythonw.exe on PATH that actually has tkinter.
rem A machine can have several Pythons, and the first one is not always usable.
rem Optional, for the tray icon:  pip install pystray pillow

setlocal enabledelayedexpansion
set "SCRIPT=%~dp0kimi_usage.pyw"
set "FOUND="

if not exist "%SCRIPT%" (
    echo Cannot find kimi_usage.pyw next to this script.
    pause
    exit /b 1
)

for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do (
    if not defined FOUND (
        "%%P" -c "import tkinter" >nul 2>nul
        if !errorlevel! equ 0 set "FOUND=%%P"
    )
)

if defined FOUND (
    start "" "!FOUND!" "%SCRIPT%"
    exit /b 0
)

rem Fall back to python.exe (opens a console window, but still works)
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
    if not defined FOUND (
        "%%P" -c "import tkinter" >nul 2>nul
        if !errorlevel! equ 0 set "FOUND=%%P"
    )
)

if defined FOUND (
    start "" "!FOUND!" "%SCRIPT%"
    exit /b 0
)

echo No Python with tkinter was found on this machine.
echo Install Python 3.8+ from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
pause
exit /b 1
