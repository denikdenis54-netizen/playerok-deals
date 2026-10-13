@echo off
chcp 65001 >nul
title Playerok Deals
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
pause