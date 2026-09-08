@echo off
rem OpenTelegramStorage installer for Windows (no Docker, no Python needed).
rem Copies this package to %LOCALAPPDATA%\OpenTelegramStorage, adds Start Menu
rem shortcuts, optionally starts it at login, then launches it.
setlocal EnableDelayedExpansion
set "SRC=%~dp0"
set "DEST=%LOCALAPPDATA%\OpenTelegramStorage"
echo.
echo  OpenTelegramStorage - Windows installer
echo  ----------------------------------------
echo  Install location: %DEST%
echo.
if /i "%SRC%"=="%DEST%\" (
    echo Already running from the install location.
) else (
    echo Copying files (this takes a moment)...
    robocopy "%SRC%." "%DEST%" /E /NFL /NDL /NJH /NJS /XD data >nul
    if errorlevel 8 ( echo Copy failed. & pause & exit /b 1 )
)
set "SM=%APPDATA%\Microsoft\Windows\Start Menu\Programs\OpenTelegramStorage"
if not exist "%SM%" mkdir "%SM%"
powershell -NoProfile -Command ^
  "$w=New-Object -ComObject WScript.Shell;" ^
  "$s=$w.CreateShortcut('%SM%\OpenTelegramStorage.lnk');$s.TargetPath='%DEST%\OpenTelegramStorage.cmd';$s.WorkingDirectory='%DEST%';$s.Description='Start OpenTelegramStorage';$s.Save();" ^
  "$s=$w.CreateShortcut('%SM%\Open in browser.lnk');$s.TargetPath='http://localhost:8080';$s.Save();" ^
  "$s=$w.CreateShortcut('%SM%\Uninstall OpenTelegramStorage.lnk');$s.TargetPath='%DEST%\uninstall.cmd';$s.WorkingDirectory='%DEST%';$s.Save();" ^
  "$s=$w.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\OpenTelegramStorage.lnk');$s.TargetPath='%DEST%\OpenTelegramStorage.cmd';$s.WorkingDirectory='%DEST%';$s.Save()"
echo Start Menu and Desktop shortcuts created.
echo.
choice /C YN /M "Start OpenTelegramStorage automatically when you log in (runs in the background)"
if errorlevel 2 goto :nostartup
powershell -NoProfile -Command ^
  "$w=New-Object -ComObject WScript.Shell;$s=$w.CreateShortcut([Environment]::GetFolderPath('Startup')+'\OpenTelegramStorage.lnk');$s.TargetPath='wscript.exe';$s.Arguments='\"%DEST%\start-hidden.vbs\"';$s.WorkingDirectory='%DEST%';$s.Save()"
echo Autostart enabled.
:nostartup
echo.
echo Installed. Starting now...
start "" "%DEST%\OpenTelegramStorage.cmd"
echo A browser tab opens at http://localhost:8080 - follow the setup wizard there.
echo.
pause
