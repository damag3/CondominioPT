@echo off
chcp 65001 >nul
echo === Condominio PT: criar .exe ===
py -m pip install --upgrade pyinstaller
py -m PyInstaller --noconsole --onefile --name CondominioPT condominio.py
echo.
echo Pronto: dist\CondominioPT.exe
pause
