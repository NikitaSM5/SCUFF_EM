@echo off
cd /d "%~dp0.."
py -3.11 gui\app.py
if errorlevel 1 pause
