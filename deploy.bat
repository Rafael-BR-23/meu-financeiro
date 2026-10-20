@echo off
chcp 65001 >nul
echo Iniciando deploy para o servidor ZimaOS...
if exist venv\Scripts\python.exe (
    venv\Scripts\python.exe deploy.py %*
) else (
    python deploy.py %*
)
pause
