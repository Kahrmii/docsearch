@echo off
rem Removes the stale socket files that keep Docker Desktop from starting (see fix_docker.py). Docker must not be running.
cd /d "%~dp0"
python fix_docker.py
pause
