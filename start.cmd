@echo off
setlocal
cd /d "%~dp0"
set "_pattern_probe=import sys; assert sys.version_info >= (3,11); import tkinter; r=tkinter.Tk(); r.withdraw(); r.destroy()"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "%_pattern_probe%" >nul 2>nul
  if not errorlevel 1 (
    py -3 "%~dp0app.py" %*
    goto :finished
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  python -c "%_pattern_probe%" >nul 2>nul
  if not errorlevel 1 (
    python "%~dp0app.py" %*
    goto :finished
  )
)
set "_pattern_python=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%_pattern_python%" (
  "%_pattern_python%" -c "%_pattern_probe%" >nul 2>nul
  if not errorlevel 1 (
    "%_pattern_python%" "%~dp0app.py" %*
    goto :finished
  )
)
echo Python 3.11 or newer with the Tcl/Tk component is required.
echo Install standard Python for Windows with Tcl/Tk enabled, then try again.
echo See README.md for setup and the separate data ZIP instructions.
pause
exit /b 1

:finished
set "_pattern_exit=%errorlevel%"
if not "%_pattern_exit%"=="0" pause
exit /b %_pattern_exit%
