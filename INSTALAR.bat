@echo off
setlocal
title Lightroom Assistant - Instalacao
cd /d "%~dp0"

echo ============================================
echo   Lightroom Assistant - Instalacao
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERRO] Python nao foi encontrado no PATH.
    echo Instale o Python 3.10+ de https://www.python.org/downloads/
    echo e marque a opcao "Add Python to PATH" durante a instalacao.
    pause
    exit /b 1
)

echo Criando ambiente virtual em .venv ...
python -m venv .venv
if errorlevel 1 (
    echo [ERRO] Falha ao criar o ambiente virtual.
    pause
    exit /b 1
)

echo Instalando dependencias (PySide6, Pillow, numpy)...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

if errorlevel 1 (
    echo [ERRO] Falha ao instalar dependencias.
    pause
    exit /b 1
)

echo.
echo Instalacao concluida com sucesso!
echo Use o arquivo INICIAR.bat para abrir o programa.
pause
