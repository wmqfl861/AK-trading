@echo off
rem response-only-zcode.cmd -- EXACT LAUNCH RECIPE (r005) for the response-only
rem native headless prompt entry. Mirrors the installed %USERPROFILE%\.local\bin\zcode.cmd
rem byte-for-byte except for the single inserted `--require` preload argument.
rem ZRO117_CAPTURE_FILE (absolute fresh path) is MANDATORY for this invocation.
rem Task-local modified invocation code per author017/018/019; the installed
rem bundle and persistent configuration are never modified. Supported invocation
rem = native headless prompt entry via the installed launcher; the protocol /
rem workspace construction route is excluded and fails closed at the guard.
setlocal
set "ZCODE_DATA_BASE_DIR=%USERPROFILE%\.zcode-cli-home"
set "ZCODE_OLD_CP="
for /f "tokens=2 delims=:" %%c in ('chcp') do set "ZCODE_OLD_CP=%%c"
chcp 65001 >nul
node --require "C:\AKM5C019\native-entry-adapter-018\revisions\r005\response-only-entry.cjs" "%USERPROFILE%\.zcode\runtime\current\bin\zcode.mjs" %*
set "ZCODE_RC=%ERRORLEVEL%"
if defined ZCODE_OLD_CP chcp %ZCODE_OLD_CP% >nul
exit /b %ZCODE_RC%
