@echo off
py -3.11 "%~dp0run_test.py" %*
set "result=%errorlevel%"
if not "%result%"=="0" pause
exit /b %result%
