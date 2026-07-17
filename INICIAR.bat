@echo off
setlocal
title Lightroom Assistant
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] Ambiente nao encontrado. Execute INSTALAR.bat primeiro.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" app.py
if errorlevel 1 (
    echo.
    echo O programa foi encerrado com um erro. Veja a pasta logs para detalhes.
    pause
)
