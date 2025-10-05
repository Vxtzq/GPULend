@echo off
REM =====================================================
REM Install Python dependencies and run app.py with dialog
REM =====================================================

REM Set Python executable
SET PYTHON_EXEC=python

REM Launch a PowerShell popup to show "Installing libraries..."
powershell -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Installing required Python libraries...','Please wait')"

REM Upgrade pip
%PYTHON_EXEC% -m pip install --upgrade pip

REM Install required packages
%PYTHON_EXEC% -m pip install httpx psutil GPUtil PyQt6

REM Run the app
%PYTHON_EXEC% app.py

REM Optional: notify installation done
powershell -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Libraries installed! App is launching.','Done')"

REM Pause so user can see any terminal output
pause
