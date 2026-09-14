#!/usr/bin/env bash
# ZCode 工牌网关一键启动：建 venv + 装依赖 + 依赖自检 + 运行 gateway.py
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python

if [ ! -x "$PY" ]; then
  echo "[setup] 创建虚拟环境并安装依赖..."
  python3 -m venv .venv
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r requirements.txt
fi

# 依赖自检：缺 websocket-client 时网关能起来、端口也在监听，但永远配不上 ZCode
# （日志刷 ModuleNotFoundError: No module named 'websocket'，/ui/workspaces 恒为 0）
if ! "$PY" -c "import websocket, certifi, miniaudio, edge_tts" >/dev/null 2>&1; then
  echo "[setup] 依赖不全，补装 requirements.txt ..."
  "$PY" -m pip install -r requirements.txt
  "$PY" -c "import websocket, certifi, miniaudio, edge_tts" >/dev/null 2>&1 || {
    echo "[error] 依赖仍缺失，请手动运行：$PY -m pip install -r requirements.txt"
    exit 1
  }
fi

if [ ! -f gateway-config.json ]; then
  echo "[setup] 还没配置过：请先运行   python3 setup-gateway.py"
  echo "        它会打开网页配置页，四项填完点保存后再运行本脚本。详见 QUICKSTART.md"
  echo "        （无图形界面的服务器：python3 setup-gateway.py --cli）"
  exit 1
fi

# Linux 若开了防火墙，需放行网关端口（默认 8787）：
#   sudo ufw allow 8787/tcp        或        sudo firewall-cmd --add-port=8787/tcp --permanent

echo "[run] 启动网关（Ctrl+C 退出）..."
exec "$PY" gateway.py
