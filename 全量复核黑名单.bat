@echo off
cd /d "%~dp0"
echo ============================================================
echo   Recheck ALL blocklisted sources
echo   (blacklist cooldown is 30 days; this forces an early pass)
echo ============================================================
echo.
echo This runs the full daily job, so it takes 5-10 minutes.
echo Log file: update_log.txt
echo.
set /p PUSH="Push the result to remote (GitHub/Gitee)? [y/N]: "
if /i "%PUSH%"=="y" goto WITHPUSH
echo.
echo [mode] dry run: local files only, nothing pushed
python update_sources.py --no-push --recheck-blocklist
goto END

:WITHPUSH
echo.
echo [mode] full run: will commit and push to remote
python update_sources.py --recheck-blocklist

:END
echo.
echo ---- finished, see update_log.txt ----
pause
