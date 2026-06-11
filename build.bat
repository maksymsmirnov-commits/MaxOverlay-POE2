@echo off
REM Build MaxOverlay-POE2.exe (Windows). Requires Python 3.10+ on PATH.
setlocal

echo === Creating / using virtual environment ===
if not exist .venv (
    python -m venv .venv
)
call .venv\Scripts\activate.bat

echo === Installing dependencies ===
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-build.txt
if errorlevel 1 goto :error

echo === Building the executable ===
pyinstaller --noconfirm --clean maxoverlay.spec
if errorlevel 1 goto :error

echo.
echo === Done ===
echo The executable is at:  dist\MaxOverlay-POE2.exe
echo.
goto :eof

:error
echo.
echo BUILD FAILED. See the messages above.
exit /b 1

