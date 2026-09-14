#!/usr/bin/env python3
"""setup-gateway.py —— 网关侧配置（网页版，默认）。

用法：
  python setup-gateway.py                 # 起本地配置页 http://127.0.0.1:8790 并自动开浏览器
  python setup-gateway.py --cli           # 终端问答模式（无浏览器的服务器/SSH 用）
  python setup-gateway.py --page-port 8791 --no-browser

配置四项：① ZCode 远控配对链接 link.txt ② 访问令牌 ③ 监听地址/端口 ④ 语音(ASR/TTS)。
写入本目录：link.txt / gateway-config.json / asr.env。仅用标准库，不联网；配置页只绑回环，
密钥不会离开本机。配置完用 start-gateway.bat(Windows) 或 ./start-gateway.sh 启动网关。
"""
from __future__ import annotations

import argparse
import html
import json
import secrets
import socket
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LINK = HERE / "link.txt"
CONF = HERE / "gateway-config.json"
ASR_ENV = HERE / "asr.env"

# 与 gateway.py 的默认配置保持同一份 schema（缺字段时网关会自行回落默认值）。
CONF_DEFAULTS = {
    "link_file": "link.txt", "workspace": None, "task": "auto",
    "origin": "https://zcode.z.ai", "bind": "0.0.0.0", "port": 8787,
    "token": "", "asr_url": "", "asr_model": "SenseVoiceSmall", "asr_key": "",
    "asr_tencent_id": "", "asr_tencent_key": "", "asr_tencent_region": "ap-shanghai",
    "asr_env_file": "asr.env", "tts_voice": "zh-CN-XiaoxiaoNeural", "ask_timeout": 180,
}


def lan_ips():
    """本机局域网 IP（工牌要填的那个）。借 UDP connect 取默认路由出口 IP，不发包。"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def load_conf():
    conf = dict(CONF_DEFAULTS)
    if CONF.exists():
        try:
            conf.update(json.loads(CONF.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return conf


def asr_env_has_keys():
    if not ASR_ENV.exists():
        return False
    txt = ASR_ENV.read_text(encoding="utf-8", errors="replace")
    return "TENCENT_CLOUD_SECRET_ID" in txt and "TENCENT_CLOUD_SECRET_KEY" in txt


def save_config(form):
    """把表单写进 link.txt / gateway-config.json / asr.env，返回写好的 conf。"""
    conf = load_conf()
    link = (form.get("link") or "").strip()
    if link:
        LINK.write_text(link + "\n", encoding="utf-8")

    conf["token"] = (form.get("token") or "").strip() or secrets.token_hex(16)
    conf["bind"] = (form.get("bind") or "").strip() or "0.0.0.0"
    for key, default in (("port", 8787), ("ask_timeout", 180)):
        try:
            conf[key] = int((form.get(key) or "").strip() or default)
        except ValueError:
            conf[key] = default
    conf["tts_voice"] = (form.get("voice") or "").strip() or "zh-CN-XiaoxiaoNeural"
    conf.setdefault("link_file", "link.txt")

    mode = form.get("asr_mode") or "skip"
    if mode == "tencent":
        conf["asr_env_file"] = "asr.env"
        tid = (form.get("tid") or "").strip()
        tkey = (form.get("tkey") or "").strip()
        if tid and tkey:                       # 空=保留上次写的 asr.env
            ASR_ENV.write_text(
                "TENCENT_CLOUD_SECRET_ID=%s\nTENCENT_CLOUD_SECRET_KEY=%s\n" % (tid, tkey),
                encoding="utf-8")
            conf["asr_url"] = ""
    elif mode == "funasr":
        conf["asr_url"] = (form.get("asr_url") or "").strip()
        conf["asr_model"] = (form.get("asr_model") or "").strip() or "SenseVoiceSmall"
        conf["asr_key"] = (form.get("asr_key") or "").strip()
        conf["asr_tencent_id"] = ""            # 清掉腾讯优先项，否则网关会继续走腾讯
        conf["asr_tencent_key"] = ""
        conf["asr_env_file"] = ""
    # mode == "skip"：原样保留已有 asr.env / asr_url

    CONF.write_text(json.dumps(conf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return conf


# ---------------- 配置页 HTML ----------------

_STYLE = """
:root{--bg:#0e1726;--card:#16233a;--line:#26374f;--fg:#e8eef7;--mut:#94a7c0;--acc:#3ba7e0}
*{box-sizing:border-box}
body{margin:0;padding:28px 16px;background:var(--bg);color:var(--fg);
  font:15px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:680px;margin:0 auto}
h1{font-size:21px;margin:0 0 4px}
.sub{color:var(--mut);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:18px 20px;margin-bottom:14px}
.card h2{font-size:15px;margin:0 0 4px;color:var(--acc)}
.hint{color:var(--mut);font-size:12.5px;margin:0 0 10px}
label{display:block;font-size:13px;color:var(--mut);margin:10px 0 4px}
input,textarea,select{width:100%;padding:9px 11px;border-radius:8px;
  border:1px solid var(--line);background:#0b1422;color:var(--fg);font:14px/1.5 inherit}
textarea{min-height:64px;resize:vertical;word-break:break-all}
input:focus,textarea:focus,select:focus{outline:0;border-color:var(--acc)}
.row{display:flex;gap:10px}
.row>div{flex:1}
button{cursor:pointer;border:1px solid var(--line);border-radius:8px;
  background:#1d2f4a;color:var(--fg);padding:8px 14px;font:14px inherit}
button:hover{border-color:var(--acc)}
.go{width:100%;margin-top:16px;background:var(--acc);border-color:var(--acc);
  color:#04121e;font-weight:600;padding:11px}
.ip{color:var(--acc);font-weight:600}
.ok{background:#12312a;border-color:#1f5a45;color:#8ff0c6}
.err{background:#3a1620;border-color:#6d2436;color:#ffb4c4}
code{background:#0b1422;padding:2px 6px;border-radius:5px;font-size:13px}
"""


def render_page(conf, page_nonce, note=""):
    ips = lan_ips()
    ip_txt = ", ".join("<span class='ip'>%s</span>" % html.escape(i) for i in ips) or "（未检测到，用 ipconfig/ifconfig 自查）"
    link_txt = LINK.read_text(encoding="utf-8", errors="replace").strip() if LINK.exists() else ""
    has_env = asr_env_has_keys()
    tencent_checked = " selected" if (has_env or conf.get("asr_tencent_id")) else ""
    funasr_checked = " selected" if conf.get("asr_url") else ""
    skip_checked = "" if (tencent_checked or funasr_checked) else " selected"
    tkey_ph = "留空=保留已存密钥" if has_env else "粘贴 SecretKey"
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZCode 工牌网关 · 配置</title><style>%(style)s</style></head><body><div class="wrap">
<h1>ZCode 工牌网关 · 配置</h1>
<div class="sub">四项填完点底部保存，会写入本目录的 <code>link.txt</code> /
<code>gateway-config.json</code> / <code>asr.env</code>。本页只绑回环，密钥不外传。</div>
%(note)s
<form method="post" action="/save">
<input type="hidden" name="nonce" value="%(nonce)s">

<div class="card"><h2>① ZCode 远控配对链接</h2>
<p class="hint">在 ZCode 桌面端生成一条“网页远控”链接，给网关专用（别和手机共用，否则互踢）。</p>
<textarea name="link" placeholder="https://zcode.z.ai/remote/v4?sid=...&amp;hash=...&amp;mid=...">%(link)s</textarea></div>

<div class="card"><h2>② 访问令牌</h2>
<p class="hint">工牌配网时填同一个值。已自动生成随机令牌，可改可重生成。</p>
<div class="row"><div><input id="token" name="token" value="%(token)s"></div>
<div style="flex:0 0 auto"><button type="button" onclick="regen()">重新生成</button></div></div></div>

<div class="card"><h2>③ 监听地址 / 端口</h2>
<p class="hint">本机局域网 IP：%(ip)s　—— 工牌“网关地址”要填 <code>http://&lt;上面的IP&gt;:端口</code>。</p>
<div class="row">
<div><label>监听地址</label><input name="bind" value="%(bind)s"></div>
<div><label>端口</label><input name="port" value="%(port)s"></div></div></div>

<div class="card"><h2>④ 语音（ASR / TTS）</h2>
<p class="hint">录音固定 24kHz，网关自动重采样 16kHz 送 ASR。依赖 miniaudio+edge-tts（start-gateway 自动装）。</p>
<label>ASR 方式</label>
<select name="asr_mode" id="asr_mode" onchange="toggleAsr()">
<option value="tencent"%(tencent_sel)s>腾讯云一句话识别（推荐，需 SecretId/Key）</option>
<option value="funasr"%(funasr_sel)s>自建 FunASR（OpenAI 兼容端点）</option>
<option value="skip"%(skip_sel)s>暂不配置（只显示文本，不出语音）</option>
</select>
<div id="box_tencent"><label>腾讯云 SecretId</label><input name="tid" placeholder="AKID...">
<label>腾讯云 SecretKey</label><input name="tkey" type="password" placeholder="%(tkey_ph)s"></div>
<div id="box_funasr" style="display:none">
<label>asr_url</label><input name="asr_url" value="%(asr_url)s" placeholder="http://funasr主机:8000">
<label>模型名</label><input name="asr_model" value="%(asr_model)s">
<label>asr_key（可空）</label><input name="asr_key" value="%(asr_key)s"></div>
<label>TTS 音色（edge-tts）</label><input name="voice" value="%(voice)s"></div>

<button class="go" type="submit">保存配置</button>
</form>
<div class="sub" style="margin-top:16px">保存后：Windows 双击 <code>start-gateway.bat</code>；
macOS/Linux <code>./start-gateway.sh</code>。然后工牌上 长按OK → 设置 → 配网填地址与令牌。</div>
</div>
<script>
function regen(){var a=new Uint8Array(16);crypto.getRandomValues(a);
var t=[].map.call(a,function(b){return ('0'+b.toString(16)).slice(-2)}).join('');
document.getElementById('token').value=t;}
function toggleAsr(){var m=document.getElementById('asr_mode').value;
document.getElementById('box_tencent').style.display=(m==='tencent')?'':'none';
document.getElementById('box_funasr').style.display=(m==='funasr')?'':'none';}
toggleAsr();
</script></body></html>""" % {
        "style": _STYLE, "nonce": html.escape(page_nonce), "note": note,
        "ip": ip_txt, "link": html.escape(link_txt), "token": html.escape(conf.get("token") or ""),
        "bind": html.escape(str(conf.get("bind") or "0.0.0.0")),
        "port": html.escape(str(conf.get("port") or 8787)),
        "voice": html.escape(str(conf.get("tts_voice") or "zh-CN-XiaoxiaoNeural")),
        "asr_url": html.escape(str(conf.get("asr_url") or "")),
        "asr_model": html.escape(str(conf.get("asr_model") or "SenseVoiceSmall")),
        "asr_key": html.escape(str(conf.get("asr_key") or "")),
        "tkey_ph": tkey_ph, "tencent_sel": tencent_checked,
        "funasr_sel": funasr_checked, "skip_sel": skip_checked,
    }


def render_done(conf):
    ips = lan_ips()
    ip = ips[0] if ips else "<本机局域网IP>"
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>配置完成</title><style>%(style)s</style></head><body><div class="wrap">
<div class="card ok"><h2>✓ 配置已保存</h2>
<p>已写入 <code>link.txt</code> / <code>gateway-config.json</code> / <code>asr.env</code>（在本 <code>gateway/</code> 目录）。</p></div>
<div class="card"><h2>下一步</h2>
<p><b>1. 启动网关</b>：Windows 双击 <code>start-gateway.bat</code>；macOS/Linux 运行 <code>./start-gateway.sh</code>。</p>
<p><b>2. 工牌配网</b>：长按 OK → 设置 → 配网设置 → 连热点 <code>ZCode-Badge-Setup</code> → 开 <code>http://192.168.4.1</code>，填：</p>
<p>网关地址 <code>http://%(ip)s:%(port)s</code>　令牌 <code>%(token)s</code></p>
<p><b>3.</b> 保存重启工牌，即可看到工作区/会话。</p></div>
</div></body></html>""" % {"style": _STYLE, "ip": html.escape(ip),
                          "port": html.escape(str(conf.get("port"))),
                          "token": html.escape(str(conf.get("token")))}


class Handler(BaseHTTPRequestHandler):
    page_nonce = ""

    def log_message(self, fmt, *args):        # 静音访问日志
        pass

    def _send(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path == "/":
            self._send(render_page(load_conf(), self.page_nonce))
        else:
            self._send("<h1>404</h1>", 404)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/save":
            self._send("<h1>404</h1>", 404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in
                urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8")).items()}
        if form.get("nonce") != self.page_nonce:      # 只认本页发出的提交
            self._send("<h1>403</h1><p>页面已过期，请重新打开配置页。</p>", 403)
            return
        conf = save_config(form)
        self._send(render_done(conf))


# ---------------- 终端问答模式（无浏览器的服务器用） ----------------

def cli_main():
    print("== ZCode 工牌网关配置向导（终端模式）==")
    ips = lan_ips()
    print("本机局域网 IP（工牌要填的就是它）：",
          ", ".join(ips) if ips else "（未检测到，请用 ipconfig/ifconfig 自查）")
    print()
    print("【1/4 远控配对链接】在 ZCode 桌面端生成一条『网页远控/配对』链接（给网关专用，别和手机共用）。")
    print("  已存在 link.txt，回车保留，或粘贴新链接覆盖：" if LINK.exists() else
          "  粘贴链接（形如 https://zcode.z.ai/remote/v4?sid=...）：")
    link = input("  > ").strip()
    if link:
        LINK.write_text(link + "\n", encoding="utf-8")
        print("  已写入 link.txt")
    else:
        print("  保留现有 link.txt" if LINK.exists() else "  ! 未提供链接，网关将无法配对；稍后可重跑本向导。")
    print()
    print("【2/4 访问令牌】工牌配网时要填同一个令牌。回车=自动生成一个随机令牌。")
    tok = input("  > ").strip() or secrets.token_hex(16)
    print()
    print("【3/4 监听】默认 0.0.0.0:8787（0.0.0.0 表示允许局域网/工牌访问）。")
    port = input("  端口（回车=8787）> ").strip() or "8787"
    bind = input("  监听地址（回车=0.0.0.0）> ").strip() or "0.0.0.0"
    print()
    print("【4/4 语音】ASR=腾讯云一句话识别（密钥放 asr.env）；TTS=edge-tts（免密钥）。")
    sid = input("  腾讯云 SecretId（回车=跳过，语音提问不可用）> ").strip()
    skey = input("  腾讯云 SecretKey（回车=跳过）> ").strip() if sid else ""
    if sid and skey:
        ASR_ENV.write_text(
            "TENCENT_CLOUD_SECRET_ID=%s\nTENCENT_CLOUD_SECRET_KEY=%s\n" % (sid, skey),
            encoding="utf-8")
        print("  已写入 asr.env")
    else:
        print("  跳过 ASR 密钥；需要语音提问时补 asr.env（见 QUICKSTART『语音配置』）。")
    voice = input("  TTS 音色（回车=zh-CN-XiaoxiaoNeural）> ").strip() or "zh-CN-XiaoxiaoNeural"
    conf = load_conf()
    conf.update({"bind": bind, "port": int(port), "token": tok, "tts_voice": voice})
    CONF.write_text(json.dumps(conf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n已写入 gateway-config.json\n== 下一步 ==\n"
          " 1) 启动网关：Windows 双击 start-gateway.bat；macOS/Linux ./start-gateway.sh\n"
          " 2) 工牌：长按OK→设置→配网设置→连热点 ZCode-Badge-Setup→开 http://192.168.4.1")
    print("    网关地址填：http://%s:%s   令牌填：%s" % (ips[0] if ips else "<本机局域网IP>", port, tok))
    return 0


def main():
    ap = argparse.ArgumentParser(description="ZCode 工牌网关配置（网页版）")
    ap.add_argument("--cli", action="store_true", help="终端问答模式（无浏览器的服务器用）")
    ap.add_argument("--page-port", type=int, default=8790, help="配置页端口（默认 8790，只绑回环）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()
    if args.cli:
        return cli_main()

    Handler.page_nonce = secrets.token_hex(16)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.page_port), Handler)
    except OSError as e:
        print("配置页端口 %d 被占用（%s）。换一个：python setup-gateway.py --page-port 8791"
              % (args.page_port, e), file=sys.stderr)
        return 1
    url = "http://127.0.0.1:%d/" % args.page_port
    print("ZCode 工牌网关配置页：%s" % url)
    print("在浏览器里填完点『保存配置』，然后按页面提示启动网关。（Ctrl+C 退出本页）")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n配置页已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())