@echo off
REM Launch BB Fab Browser
cd /d "%~dp0"
python bb_fab_browser.py
if errorlevel 1 pause
