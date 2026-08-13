@echo off
REM Lightweight wrapper to run Jeeves CLI on Windows
REM Usage: add the project folder to PATH or place this file somewhere on PATH
set SCRIPT_DIR=%~dp0
python "%SCRIPT_DIR%\main.py" %*
