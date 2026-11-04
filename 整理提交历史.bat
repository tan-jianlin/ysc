@echo off
cd /d "%~dp0"
echo ============================================================
echo   Squash git history into ONE commit
echo ============================================================
echo.
echo  Current commits:
git rev-list --count HEAD
echo.
echo   What this does:
echo     1. backup .git to ..\.git-bak-^<timestamp^>
echo     2. fold ALL commits into a single baseline commit
echo     3. force push to Gitee
echo     4. rewrite GitHub master, keeping every file that is on GitHub today
echo.
echo   Only history is folded; no file is deleted. GitHub-side files that have
echo   no local copy (refresh_config.yml, refresh_config.py, source_pool.json)
echo   are re-attached by blob SHA, then verified path by path. If the remote
echo   file list cannot be read, step 4 aborts instead of guessing.
echo.
echo   Playback on TV is not affected at all.
echo.
echo   Preview plan (dry run):
python squash_history.py --dry-run
echo.
set /p GO="Really squash and force push? [y/N]: "
if /i not "%GO%"=="y" goto END

echo.
python squash_history.py
if errorlevel 1 (
  echo.
  echo [!] Stopped with error code %errorlevel%.
  echo     If it failed at branch rename, run: python squash_history.py --resume
)

:END
echo.
pause
