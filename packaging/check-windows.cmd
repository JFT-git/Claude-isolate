@echo off
cd /d "%~dp0\.."
echo Windows development preview: unit tests and command planning only.
echo Live VM networking has not been validated on Windows.
py -3 -m unittest discover -s tests -v
exit /b %ERRORLEVEL%
