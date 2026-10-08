@echo off
REM Builds MarioKartNitro.exe (C# / WPF, one standalone file, .NET runtime included).
REM Run ON WINDOWS from this folder. Needs the .NET 8 SDK installed on THIS PC only
REM (players need nothing): https://dotnet.microsoft.com/download/dotnet/8.0

cd /d "%~dp0"
where dotnet >nul 2>nul
if errorlevel 1 (
  echo The .NET 8 SDK is not installed. Get it from https://dotnet.microsoft.com/download/dotnet/8.0
  goto failed
)

if exist dist\MarioKartNitro.exe del /f /q dist\MarioKartNitro.exe

echo Building MarioKartNitro.exe ...
dotnet publish NitroLauncher\NitroLauncher.csproj -c Release -o dist
if errorlevel 1 goto failed

echo.
echo Build complete: dist\MarioKartNitro.exe
echo Upload that file to the GitHub release (asset name MarioKartNitro.exe).
goto end

:failed
echo.
echo Build failed - copy the error text above.
:end
pause
