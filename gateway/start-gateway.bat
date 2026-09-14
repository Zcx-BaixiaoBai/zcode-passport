@echo off
setlocal
cd /d "%~dp0"
set PY=.venv\Scripts\python.exe

REM ---- 1. 虚拟环境 + 依赖 ----
if not exist %PY% (
    echo [setup] 创建虚拟环境并安装依赖（首次约 1 分钟）...
    python -m venv .venv || goto :err
    %PY% -m pip install --upgrade pip
    %PY% -m pip install -r requirements.txt || goto :err
)

REM 依赖自检：缺 websocket-client 时网关能起来、8787 也在监听，但永远配不上 ZCode
REM （日志刷 ModuleNotFoundError: No module named 'websocket'，工作区恒为 0）
%PY% -c "import websocket, certifi, miniaudio, edge_tts" >nul 2>&1
if errorlevel 1 (
    echo [setup] 依赖不全，正在补装 requirements.txt ...
    %PY% -m pip install -r requirements.txt || goto :err
    %PY% -c "import websocket, certifi, miniaudio, edge_tts" >nul 2>&1 || goto :deperr
)

REM ---- 2. 配置检查 ----
if not exist gateway-config.json (
    echo [setup] 还没配置过：请先运行   python setup-gateway.py
    echo         它会打开网页配置页，四项填完点保存后再双击本脚本。详见 QUICKSTART.md
    pause
    exit /b 1
)

REM ---- 3. Windows 防火墙 ----
REM 入站规则按“程序路径”匹配：系统 python 可能早有规则，但 venv 里的 python.exe 是另一个路径，
REM 没规则的话本机 curl 通、工牌从局域网却连不进来。添加需管理员权限，失败只警告不中断。
netsh advfirewall firewall show rule name="ZCode Badge Gateway" 2>nul | find "ZCode Badge Gateway" >nul
if errorlevel 1 (
    echo [setup] 添加防火墙入站规则 "ZCode Badge Gateway" ...
    netsh advfirewall firewall add rule name="ZCode Badge Gateway" dir=in action=allow protocol=TCP program="%~dp0.venv\Scripts\python.exe" >nul 2>&1
    if errorlevel 1 echo   ! 添加失败（需管理员权限）。若工牌连不上，请用管理员身份重跑本脚本。
)

echo [run] 启动网关（Ctrl+C 退出；本窗口别关，关了工牌就断）...
%PY% gateway.py
goto :eof

:deperr
echo [error] 依赖仍缺失。请手动运行： %PY% -m pip install -r requirements.txt
pause
exit /b 1

:err
echo [error] 初始化失败：请确认已装 Python 3.10+ 且网络可用。
pause
exit /b 1
