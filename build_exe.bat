@echo off
setlocal EnableDelayedExpansion
title BB Fab Browser - Build
cd /d "%~dp0"

echo.
echo  ================================================
echo   BB Fab Browser  --  Build Script
echo  ================================================
echo.

:: -- Check Python --------------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found.
    echo  Please install Python 3.10+ from https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during installation.
    pause & exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo  [OK] Python %PYVER% found.
echo.

:: -- Install dependencies ------------------------------------------------------
echo  Installing / verifying dependencies...
echo.
python -m pip install --upgrade pip -q
python -m pip install -r requirements.txt pyinstaller -q
if errorlevel 1 (
    echo  [ERROR] pip install failed. Check your internet connection.
    pause & exit /b 1
)
echo  [OK] Dependencies ready.
echo.

:: -- Optional drag-and-drop support ---------------------------------------------
set DND_ARGS=
set TKDND_DIR=
for /f "delims=" %%p in ('python -c "import tkinterdnd2,os; print(os.path.dirname(tkinterdnd2.__file__))" 2^>nul') do set TKDND_DIR=%%p
if defined TKDND_DIR (
    echo  [OK] tkinterdnd2 found, drag-and-drop will be included.
    set DND_ARGS=--add-data "!TKDND_DIR!\tkdnd;tkinterdnd2/tkdnd" --hidden-import=tkinterdnd2 --collect-all tkinterdnd2
) else (
    echo  [--] tkinterdnd2 not installed, building without drag-and-drop.
)
echo.

:: -- Run the self-test first ---------------------------------------------------
echo  Running self-test...
python test_preview_logic.py >nul
if errorlevel 1 (
    echo  [ERROR] Self-test failed. Run "python test_preview_logic.py" to see why.
    pause & exit /b 1
)
echo  [OK] Self-test passed.
echo.

:: -- Build with PyInstaller ----------------------------------------------------
echo  Building standalone .exe (this takes ~30-90 seconds) ...
echo.

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --noconfirm ^
    --clean ^
    --name "BB_Fab_Browser" ^
    --hidden-import="PIL._tkinter_finder" ^
    %DND_ARGS% ^
    bb_fab_browser.py

if errorlevel 1 (
    echo.
    echo  [ERROR] Build failed. See output above.
    pause & exit /b 1
)

echo.
echo  ================================================
echo   BUILD COMPLETE!
echo  ================================================
echo.
echo  Your app is at:  dist\BB_Fab_Browser.exe
echo.
pause
