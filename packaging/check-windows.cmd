@echo off
cd /d "%~dp0\.."
echo Building the Windows app and installer.
py -3.12 -m pip install --require-hashes -r requirements-windows.txt
if errorlevel 1 exit /b %ERRORLEVEL%
py -3 -m unittest discover -s tests -v
if errorlevel 1 exit /b %ERRORLEVEL%
py -3.12 windows\build.py
exit /b %ERRORLEVEL%
