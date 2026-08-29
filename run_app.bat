@echo off
title Turbomachinery Geometry Studio
echo Starting Turbomachinery Algorithmic Studio...
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -3.11 "%~dp0gui.py"
) else (
    python "%~dp0gui.py"
)
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Application exited with error code %ERRORLEVEL%
    pause
)
