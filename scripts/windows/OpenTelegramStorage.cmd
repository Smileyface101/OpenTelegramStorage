@echo off
rem Start OpenTelegramStorage (portable: everything is inside this folder).
setlocal
set "HERE=%~dp0"
set "OTS_DATA_DIR=%HERE%data"
set "OTS_FRONTEND_DIST=%HERE%app\frontend\dist"
if "%OTS_IMPORT_DIR%"=="" set "OTS_IMPORT_DIR=%HERE%data\import"
if "%OTS_PORT%"=="" set "OTS_PORT=8080"
if not exist "%OTS_DATA_DIR%" mkdir "%OTS_DATA_DIR%"
cd /d "%HERE%app\backend"
echo Applying database migrations...
"%HERE%python\python.exe" -m alembic upgrade head || goto :fail
echo.
echo OpenTelegramStorage is running at http://localhost:%OTS_PORT%
echo Data folder: %OTS_DATA_DIR%
echo Close this window (or press Ctrl+C) to stop the server.
start "" "http://localhost:%OTS_PORT%"
"%HERE%python\python.exe" -m uvicorn main:app --host 127.0.0.1 --port %OTS_PORT%
goto :eof
:fail
echo.
echo Something went wrong. Scroll up for the error.
pause
