@echo off
setlocal
set "DEST=%LOCALAPPDATA%\OpenTelegramStorage"
echo This removes the program files and shortcuts. Your data folder is kept:
echo   %DEST%\data
echo (delete it yourself if you also want the index, keys and staging gone)
echo.
choice /C YN /M "Continue"
if errorlevel 2 exit /b 0
taskkill /F /IM python.exe /FI "WINDOWTITLE eq OpenTelegramStorage*" >nul 2>&1
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\OpenTelegramStorage.lnk" 2>nul
rmdir /S /Q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\OpenTelegramStorage" 2>nul
del "%USERPROFILE%\Desktop\OpenTelegramStorage.lnk" 2>nul
for /d %%D in ("%DEST%\*") do if /i not "%%~nxD"=="data" rmdir /S /Q "%%D"
for %%F in ("%DEST%\*") do del /Q "%%F"
echo Done. Data kept at %DEST%\data
pause
