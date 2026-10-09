@echo off
REM claude-note - bir marta bosib o'rnatish.
REM Ikki marta bosing. Boshqa hech narsa qilish shart emas.

chcp 65001 >nul 2>&1
title claude-note - o'rnatish
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 goto nopython

python "%~dp0ornat.py" %*
echo.
pause
exit /b 0

:nopython
echo.
echo    ============================================================
echo     Python topilmadi.
echo    ============================================================
echo.
echo    Python kerak. Ikkita yo'l bor:
echo.
echo      1^) Shu buyruqni ishga tushiring:
echo           winget install -e --id Python.Python.3.12
echo.
echo      2^) Yoki https://www.python.org/downloads/ dan yuklab oling.
echo         O'rnatishda "Add python.exe to PATH" ni BELGILANG.
echo.
echo    Keyin shu faylni qayta ikki marta bosing.
echo.
pause
exit /b 1
