@echo off
rem Native-batch fake gh for Windows test runs.
rem
rem The `gh` shim shells out through bash (and, for one endpoint, python inside that
rem bash). Each extra hop is a real CreateProcess call, and collect_repo makes dozens of
rem `gh` calls per pass (one per PR's head-sha run query, plus one per pending run's
rem jobs fetch, all mostly sequential). Routing every call through bash pushes a single
rem fake pass well past what the polling tests wait for, so instead: substring matching
rem via pure batch variable expansion (`%VAR:needle=%` -- if the replace changes the
rem string, the needle was present), which spawns nothing, plus `type` (a cmd.exe
rem builtin, not a process) for every endpoint, including `/jobs?`. `/jobs?` needs
rem {"jobs": [...]} unwrapped to a bare array; rather than shelling out to python per
rem call, jobs_array.json below is that unwrapped array pre-extracted once. Regenerate
rem it if tests/fixtures/jobs_queued.json ever changes:
rem   python -c "import json; d=json.load(open('tests/fixtures/jobs_queued.json')); open('tests/fake_gh/jobs_array.json','w').write(json.dumps(d['jobs']))"
rem Behavior mirrors tests/fake_gh/gh (the POSIX shim used on non-Windows runners)
rem exactly; keep the two in sync.
setlocal enabledelayedexpansion
set "ARGS=%*"
set "F=%~dp0..\fixtures"
if "%FAKE_GH_DELAY%"=="" set "FAKE_GH_DELAY=0"
if not "%FAKE_GH_DELAY%"=="0" timeout /t %FAKE_GH_DELAY% /nobreak >nul

if "%ARGS:~0,11%"=="auth status" exit /b 0

set "chk=%ARGS:rate_limit=%"
if not "%chk%"=="%ARGS%" (
  type "%F%\rate_limit.json"
  exit /b 0
)

set "chk=%ARGS:/actions/runners=%"
if not "%chk%"=="%ARGS%" (
  type "%F%\runners.json"
  exit /b 0
)

set "chk=%ARGS:/pulls?=%"
if not "%chk%"=="%ARGS%" (
  type "%F%\pulls.json"
  exit /b 0
)

set "chk=%ARGS:/jobs?=%"
if not "%chk%"=="%ARGS%" (
  type "%~dp0jobs_array.json"
  exit /b 0
)

set "chk=%ARGS:/actions/runs?=%"
if not "%chk%"=="%ARGS%" (
  type "%F%\runs.json"
  exit /b 0
)

set "chk=%ARGS:rerun-failed-jobs=%"
if not "%chk%"=="%ARGS%" (
  echo {}
  exit /b 0
)

echo fake gh: unhandled %ARGS% 1>&2
exit /b 1
