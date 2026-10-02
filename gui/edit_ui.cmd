@echo off
if exist "C:\Qt\5.15.2\mingw81_64\bin\designer.exe" (
    start "" "C:\Qt\5.15.2\mingw81_64\bin\designer.exe" "%~dp0antenna.ui"
) else (
    echo Open antenna.ui in Qt Designer 5.15.2.
    pause
)
