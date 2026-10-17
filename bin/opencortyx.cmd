@echo off
powershell -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0opencortyx.ps1" %*
