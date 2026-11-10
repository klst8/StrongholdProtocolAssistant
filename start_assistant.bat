@echo off
cd /d "%~dp0"
python assistant.py
if errorlevel 1 pause
