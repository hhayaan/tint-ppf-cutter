@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if not errorlevel 1 (
  python -c "import tkinter" >nul 2>nul
  if not errorlevel 1 (
    python "%~dp0app.py" %*
    goto :finished
  )
)
set "_pattern_python=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%_pattern_python%" (
  "%_pattern_python%" -c "import tkinter" >nul 2>nul
  if not errorlevel 1 (
    "%_pattern_python%" "%~dp0app.py" %*
    goto :finished
  )
)
echo Python 3 with the Tcl/Tk component is required to launch Pattern Explorer.
pause
exit /b 1

:finished
set "_pattern_exit=%errorlevel%"
if not "%_pattern_exit%"=="0" pause
exit /b %_pattern_exit%
