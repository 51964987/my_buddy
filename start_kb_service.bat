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
REM  usage: start_kb_service.bat
REM
REM  - listen host/port come from kbserver.config.json (default 127.0.0.1:8765)
REM  - localhost only by design (safety rule: LAN listening requires a token,
REM    edit kbserver.config.json yourself if you really need it)
REM  - stops any previous instance holding the same port before starting
REM ============================================================

REM ---------------- read port from config (fallback 8765) ----------------
set "PORT=8765"
for /f %%i in ('python -X utf8 -c "import json,os;print(json.load(open('kbserver.config.json', encoding='utf-8')).get('port', 8765)) if os.path.exists('kbserver.config.json') else print(8765)"') do set "PORT=%%i"

echo ============================================
echo   kb buddy 服务
echo   监听地址以 kbserver.config.json 为准，默认 127.0.0.1:%PORT%
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
python -X utf8 -m kbserver
echo.
echo 服务已退出
pause
