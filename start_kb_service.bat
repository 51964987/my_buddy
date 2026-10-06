@echo off
REM UTF-8 encoding (no BOM) with CRLF line endings.
REM NOTE: keep this help block ASCII only - cmd pre-reads the file head using the
REM system ANSI codepage, so Chinese text here can be mis-parsed into commands.
chcp 65001 >nul
setlocal
cd /d %~dp0

REM Do NOT inherit an external PYTHONPATH. IDE / editor tooling may put a shim dir
REM on it; this project bootstraps sys.path itself and never needs PYTHONPATH.
set "PYTHONPATH="

REM ============================================================
REM  kb buddy service launcher
REM  usage: start_kb_service.bat [buildweb]
REM
REM  - listen host/port come from kbserver.config.json (default 127.0.0.1:8765)
REM  - localhost only by design (safety rule: LAN listening requires a token,
REM    edit kbserver.config.json yourself if you really need it)
REM  - stops any previous instance holding the same port before starting
REM  - optional arg "buildweb": rebuild the webui (Vue3) before starting,
REM    output goes to kbserver/static (served at /app). Requires Node.js.
REM    Without the arg the committed build output is used as-is.
REM ============================================================

REM ---------------- optional: rebuild webui (/app) ----------------
if /i not "%~1"=="buildweb" goto :afterbuild
echo [buildweb] rebuilding webui ...
pushd webui
if not exist node_modules (
  echo [buildweb] installing npm dependencies ...
  call npm install
  if errorlevel 1 (
    echo [buildweb] npm install FAILED - keeping existing kbserver/static and starting anyway
    popd
    goto :afterbuild
  )
)
call npm run build
if errorlevel 1 (
  echo [buildweb] npm run build FAILED - keeping existing kbserver/static and starting anyway
) else (
  echo [buildweb] webui rebuilt into kbserver/static
)
popd

:afterbuild
REM ---------------- interpreter: fixed venv312 (Python 3.12, tech-stack baseline) ----------------
REM 绝对路径固定解释器，避免 PATH 上的 python 漂移到其他环境（v0.36 曾因解释器漂移缺依赖 500）
set "PY=D:\venvs\312\Scripts\python.exe"

REM ---------------- read port from config (fallback 8765) ----------------
set "PORT=8765"
for /f %%i in ('"%PY%" -X utf8 -c "import json,os;print(json.load(open('kbserver.config.json', encoding='utf-8')).get('port', 8765)) if os.path.exists('kbserver.config.json') else print(8765)"') do set "PORT=%%i"

echo ============================================
echo   kb buddy 服务
echo   监听地址以 kbserver.config.json 为准，默认 127.0.0.1:%PORT%
echo   工作台：http://127.0.0.1:%PORT%/app/
echo   重建前端后启动：start_kb_service.bat buildweb
echo   停止服务：在本窗口按 Ctrl+C，或直接关闭窗口
echo ============================================

REM ---------------- stop previous instance holding the port ----------------
set "OLD_PID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do set "OLD_PID=%%a"
if not defined OLD_PID goto :nokill
echo 检测到旧服务占用端口 %PORT%，PID=%OLD_PID%，正在结束...
taskkill /PID %OLD_PID% /F >nul 2>&1
timeout /t 3 /nobreak >nul
echo 旧服务已结束
goto :afterkill

:nokill
echo 端口 %PORT% 无旧服务占用

:afterkill
echo 启动服务...
"%PY%" -X utf8 -m kbserver
echo.
echo 服务已退出
pause
