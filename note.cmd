@echo off
REM claude-note-win qisqa ishga tushirish
REM   note status          - holat
REM   note reindex         - INDEX.md ni qayta yigish
REM   note backfill 10     - eski 10 ta sessiyani qayta ishlash
REM   note dry <session-id>- yozmasdan korib chiqish
REM   note scan [D:/]     - diskdagi loyihalarni royxatga olish (standart: uy papkasi)
REM   note push           - kod ozgarishlarini GitHub ga yuborish
REM   note telemetry      - jamoa hisoboti holati
REM   note telemetry test - yuboriladigan yozuvni korish (yubormasdan)
REM   note telemetry flush- navbatda qolganlarini yuborish
REM   note telemetry backfill - eski sessiyalarni serverga yuborish
REM   boshqa har qanday argument note.py ga uzatiladi

setlocal
set HERE=%~dp0

if /I "%1"=="status"   ( python "%HERE%install.py" --status & goto :eof )
if /I "%1"=="install"  ( python "%HERE%install.py" & goto :eof )
if /I "%1"=="uninstall" ( python "%HERE%install.py" --uninstall & goto :eof )
if /I "%1"=="reindex"  ( python "%HERE%note.py" --reindex & goto :eof )
if /I "%1"=="costs"    ( python "%HERE%note.py" --costs & goto :eof )
if /I "%1"=="scan"     ( python "%HERE%note.py" --scan %2 & goto :eof )
if /I "%1"=="analyze"  ( python "%HERE%note.py" --analyze --session %2 & goto :eof )
if /I "%1"=="antigravity" ( python "%HERE%note.py" --antigravity & goto :eof )
if /I "%1"=="narx"     ( python "%HERE%note.py" --costs & goto :eof )
if /I "%1"=="backfill" ( python "%HERE%note.py" --backfill --limit %2 & goto :eof )
if /I "%1"=="dry"      ( python "%HERE%note.py" --session %2 --dry-run & goto :eof )
if /I "%1"=="log"      ( type "%HERE%state\note.log" & goto :eof )
if /I "%1"=="paket"  ( python "%HERE%note.py" --paket %2 & goto :eof )
if /I "%1"=="telemetry" ( python "%HERE%note.py" --telemetry %2 & goto :eof )
if /I "%1"=="open"     ( start "" "%HERE%vault" & goto :eof )
if /I "%1"=="push"     ( pushd "%HERE%" & git add -A & git commit -m "update: %DATE% %TIME%" & git push & popd & goto :eof )

python "%HERE%note.py" %*
