#!/usr/bin/env python3
"""demo-gateway.py —— 免 ZCode / 免配对链接的体验用 mock 网关。

审核者或想先试玩的人，无需 ZCode 账号、无需配对链接、无需服务器：
    python demo-gateway.py            # 默认监听 0.0.0.0:8787
然后把工牌配网的“网关地址”填成本机局域网 IP:8787 即可完整走通
“浏览工作区/会话 → 按住说话 → 听到+看到回复”的流程。

它只模拟 gateway.py 的 HTTP 接口（字段与固件 gw_client.c 的解析严格一致），
返回罐头数据；/ask 不做真实 ASR，回复为演示文本 + 演示语音
（装了 edge-tts+miniaudio 时用真语音，否则回落为提示音 WAV）。

安全：默认不设令牌（方便体验）；如需令牌，设环境变量 DEMO_TOKEN 后请求需带
X-Gateway-Token。仅监听，不主动外连（edge-tts 语音合成除外，可选）。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VOICE = os.environ.get("DEMO_TTS_VOICE", "zh-CN-XiaoxiaoNeural")

WORKSPACES = [
    {"path": "demo/workspace-a", "label": "示例工作区 A", "total": 3, "running": 1, "current": True},
    {"path": "demo/workspace-b", "label": "Demo Workspace B", "total": 2, "running": 0, "current": False},
]

SESSIONS = {
    "demo/workspace-a": [
        {"taskId": "demo-sess-1", "title": "帮我写一个问候脚本", "status": "running",
         "phase": "running", "preview": "正在生成…"},
        {"taskId": "demo-sess-2", "title": "解释这段代码在做什么", "status": "completed",
         "phase": "completed", "preview": "这段代码…"},
        {"taskId": "demo-sess-3", "title": "修一个空指针 bug", "status": "completed",
         "phase": "completed", "preview": "已定位…"},
    ],
    "demo/workspace-b": [
        {"taskId": "demo-sess-4", "title": "Plan a weekend trip", "status": "completed",
         "phase": "completed", "preview": "Here is a plan…"},
        {"taskId": "demo-sess-5", "title": "Draft a product intro", "status": "running",
         "phase": "running", "preview": "Drafting…"},
    ],
}

HISTORY = {
    "demo-sess-1": "[1] 你好！这是一个演示会话。\n真实使用时，这里会显示你桌面 ZCode 会话的最近输出。",
    "demo-sess-2": "[1] 这段代码读取配置并初始化界面。\n（演示历史）",
    "demo-sess-3": "[1] 空指针已修复：增加了判空。\n（演示历史）",
    "demo-sess-4": "[1] Here is a sample weekend plan.\n(demo history)",
    "demo-sess-5": "[1] Drafting the product intro…\n(demo history)",
}

DEMO_ANSWER = ("（演示回复）收到你的语音啦！真实使用时，这里会是你桌面 ZCode 会话"
               "针对你提问的回答，并同步语音播报。现在你体验的是免 ZCode 的 demo 网关。")


# ---------- WAV 生成 ----------

def _pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _tone_wav() -> bytes:
    """无 TTS 依赖时的回落：一段上行提示音（16k mono s16）。"""
    import math
    rate = 16000
    notes = [523.25, 659.25, 783.99]      # C5 E5 G5
    frames = bytearray()
    per = rate // 3
    for i, f in enumerate(notes):
        for n in range(per):
            t = n / rate
            env = min(1.0, n / 200) * min(1.0, (per - n) / 400)
            v = int(12000 * env * math.sin(2 * math.pi * f * t))
            frames += struct.pack("<h", v)
    return _pcm_to_wav(bytes(frames), rate)


_wav_cache: dict[str, bytes] = {}
_wav_lock = threading.Lock()


def answer_wav(text: str) -> bytes:
    with _wav_lock:
        if text in _wav_cache:
            return _wav_cache[text]
    wav = None
    try:
        import edge_tts          # 可选：真语音
        import miniaudio         # 可选：mp3→wav

        async def _synth() -> bytes:
            comm = edge_tts.Communicate(text, VOICE)
            mp3 = b""
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    mp3 += chunk["data"]
            return mp3

        mp3 = asyncio.run(_synth())
        dec = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16,
                               nchannels=1, sample_rate=16000)
        wav = _pcm_to_wav(dec.data, 16000)
    except Exception:
        wav = None
    if not wav:
        wav = _tone_wav()
    with _wav_lock:
        _wav_cache[text] = wav
    return wav


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZCodeDemoGateway/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def _authed(self) -> bool:
        token = os.environ.get("DEMO_TOKEN", "")
        if not token:
            return True
        return self.headers.get("X-Gateway-Token") == token

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            data = bytearray()
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    while True:
                        t = self.rfile.readline()
                        if t in (b"\r\n", b"\n", b""):
                            break
                    break
                data += self.rfile.read(size)
                self.rfile.readline()
            return bytes(data)
        cl = self.headers.get("Content-Length")
        return self.rfile.read(int(cl)) if cl else b""

    def do_GET(self):
        if not self._authed():
            self._json(401, {"error": "invalid or missing X-Gateway-Token"})
            return
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = parse_qs(u.query)
        if path == "/health":
            self._json(200, {"ok": True, "status": "online", "demo": True})
        elif path == "/ui/workspaces":
            self._json(200, {"workspaces": WORKSPACES})
        elif path == "/ui/sessions":
            ws = (qs.get("workspace") or [""])[0]
            sess = SESSIONS.get(ws) or SESSIONS[WORKSPACES[0]["path"]]
            self._json(200, {"sessions": sess})
        elif path == "/ui/history":
            task = (qs.get("task") or [""])[0]
            self._json(200, {"ok": True, "taskId": task,
                             "text": HISTORY.get(task, "[1] （演示）该会话暂无历史输出。")})
        elif path.startswith("/audio/"):
            wav = answer_wav(DEMO_ANSWER)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            self._json(401, {"error": "invalid or missing X-Gateway-Token"})
            return
        u = urlparse(self.path)
        if u.path.rstrip("/") == "/ask":
            body = self._read_body()          # 消耗 chunked 音频（demo 不做 ASR）
            question = "（演示：已收到 %d 字节语音）" % len(body)
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "application/json" in ctype:
                try:
                    question = json.loads(body.decode("utf-8")).get("text", question)
                except Exception:
                    pass
            wav = answer_wav(DEMO_ANSWER)
            self._json(200, {"ok": True, "question": question, "answer": DEMO_ANSWER,
                             "audioId": "demo-audio", "audioBytes": len(wav)})
        else:
            self._json(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description="ZCode 工牌免 ZCode 体验网关")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print("ZCode demo-gateway 已启动：http://%s:%d" % (args.host, args.port))
    print("把工牌配网的网关地址填成本机局域网 IP:%d（无需 ZCode/配对链接）。" % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
