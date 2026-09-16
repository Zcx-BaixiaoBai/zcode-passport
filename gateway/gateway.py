#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gateway.py — ZCode 工牌网关服务 v1

把 zcode_remote.ZCodeTerminal（已实测打通的 v4 协议连接器）封装成常驻 HTTP API，
供电子工牌 / 小程序 / 任意内网客户端消费。部署在常开服务器上，工牌无需直连
zcode.z.ai（TLS、重连、KICKED、帧聚合都在网关做）。

API（默认端口 8787；配置 token 后除 /health 外均需请求头 X-Gateway-Token）：
  GET  /health                        连接状态/桥/订阅/事件计数
  GET  /workspaces                    工作区列表（bootstrap 快照）
  GET  /tasks                         实时任务列表（workspace-list-updated 推送刷新）
  GET  /sessions                      会话索引（sessions-index 快照）
  GET  /sessions/<taskId>             单会话状态（control/meta/usage 快照）
  POST /prompt  {"text":"...","taskId":"sess_...(可选)"}   发消息/唤醒会话
  GET  /events?since=<seq>&wait=20&taskId=sess_...         长轮询增量事件（供 TTS）
  --- 工牌固件专用（v1.1） ---
  GET  /ui/workspaces                 三级界面第1层：工作区+进度汇总
  GET  /ui/sessions?workspace=<path>  第2层：该工作区会话列表
  POST /ask?taskId=<id>&rate=16000    第3层语音问答闭环：
       body=原始PCM(s16le mono) → ASR → sendPrompt → 等回复 → TTS
       → {"question","answer","audioId","audioBytes"}
       （Content-Type: application/json 时走 {"text","taskId"} 文本模式，跳过 ASR）
  GET  /audio/<audioId>               取回复语音 WAV(16k mono s16)，5 分钟过期
  POST /asr?rate=16000  body=PCM      仅语音转文字 → {"text"}
  POST /tts {"text"}                  仅文字转语音 → WAV 二进制

运行：
  python gateway.py                          # 常驻（读同目录 gateway-config.json）
  python gateway.py --once --duration 30     # 自检：连上、订阅、服务 30s 后退出
配置 gateway-config.json：
  {"link_file":"link.txt", "workspace":"ai card", "task":"auto",
   "origin":"https://zcode.z.ai", "bind":"0.0.0.0", "port":8787,
   "token":"<工牌访问令牌>",
   "asr_url":"http://<FunASR主机>:8000", "asr_model":"SenseVoiceSmall", "asr_key":"",
   "tts_voice":"zh-CN-XiaoxiaoNeural", "ask_timeout":180}

依赖：websocket-client；语音另需 pip install edge-tts miniaudio（缺省时 /asr /tts 返回 501）。
协议细节见 PROTOCOL.md。
安全：所有出站上游 URL 仅允许 http/https；发送前解析 host 并拒绝 localhost/环回/私有/
保留地址；http 上游钉已校验 IP 直连（防 DNS rebinding）且全链路不跟随重定向。
asr_url 若指向内网 FunASR，需先经公网域名反代（网关侧不做私网放行）。
"""
import argparse
import asyncio
import io
import json
import os
import socket
import sys
import threading
import time
import uuid
import wave
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zcode_remote as zr

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "gateway.log")
LOG_MAX_BYTES = 8 * 1024 * 1024
EVENT_RING_MAX = 2000
AUDIO_MAX_BYTES = 8 * 1024 * 1024     # /ask /asr 上传上限
AUDIO_TTL_SEC = 300                   # TTS 结果保存期
AUDIO_STORE_MAX = 20
# 会话状态缓存必须有界：无上限时浏览过的每个会话 topic 都会永久驻留，
# 且 snapshot 帧会把整段会话历史全量缓存下来，长期运行内存只增不减。
CONV_STATE_MAX_TOPICS = 16            # conv_state 保留的会话 topic 上限（按 updatedAt LRU 淘汰）
TEXT_CACHE_MAX_CHARS = 8000           # 单条历史文本缓存上限，超出截断。只影响 history 回看；
                                      # /ask 的答案取自 events 环中的完整文本，不受此限。


def validate_upstream_url(url, source="config"):
    """出站上游 URL 校验（统一严格）：仅 http/https；host 必须可解析；
    解析结果一律拒绝 localhost/环回/私有/保留/链路本地/组播地址（SSRF 防护）。
    返回解析出的公网 IP 列表。source 仅用于日志定位。
    注：若 FunASR 等自建服务在内网/本机，请经公网域名反代后再配置（如 nginx 反代），
    网关侧不做私网放行。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("上游仅允许 http/https（收到 %r, source=%s）" % (u.scheme, source))
    host = u.hostname
    if not host:
        raise ValueError("上游 URL 缺少 host（source=%s）" % source)
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError("上游 host 解析失败：%s（source=%s）" % (e, source))
    import ipaddress
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_reserved
                or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
            raise ValueError("拒绝私有/环回/保留地址：%s（source=%s）" % (ip, source))
        ips.append(str(ip))
    if not ips:
        raise ValueError("上游 host 无可用地址（source=%s）" % source)
    return ips


def _tc3_authorization(secret_id, secret_key, service, host, action, version,
                       payload_json, region, timestamp=None):
    """腾讯云 TC3-HMAC-SHA256 签名（与后端 repair.service.ts 同一套 API）。"""
    import hashlib
    import hmac as _hmac
    ts = timestamp or int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(ts))
    ct = "application/json; charset=utf-8"
    hashed_payload = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    canonical = ("POST\n/\n\ncontent-type:%s\nhost:%s\nx-tc-action:%s\n\n"
                 "content-type;host;x-tc-action\n%s" % (ct, host, action.lower(), hashed_payload))
    scope = "%s/%s/tc3_request" % (date, service)
    string_to_sign = ("TC3-HMAC-SHA256\n%d\n%s\n%s" %
                      (ts, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()))

    def _sign(key, msg):
        return _hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _sign(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _sign(secret_date, service)
    secret_signing = _sign(secret_service, "tc3_request")
    signature = _hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    auth = ("TC3-HMAC-SHA256 Credential=%s/%s, SignedHeaders=content-type;host;x-tc-action, Signature=%s"
            % (secret_id, scope, signature))
    headers = {"Content-Type": ct, "Host": host,
               "X-TC-Action": action, "X-TC-Version": version,
               "X-TC-Timestamp": str(ts), "X-TC-Region": region,
               "Authorization": auth}
    return headers


def _pcm_to_wav(pcm, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _post_no_redirect(url, data, headers, timeout=60):
    """经完整边界校验的 POST：
    - 先 validate_upstream_url（协议/host/解析IP 全部公网校验）
    - http:// → 直连已校验的 IP（http.client 不做第二次 DNS 解析，杜绝 DNS rebinding；
      且 http.client 天然不跟随重定向）
    - https:// → 无重定向 opener，3xx 直接抛错（TLS 需按域名握手，无法钉 IP；
      校验与连接之间的重解析窗口极小，且目标为操作者配置的固定上游）
    - 非 2xx 一律抛错，不吞响应。"""
    u = urlparse(url)
    ips = validate_upstream_url(url, "upstream-post")
    if u.scheme == "http":
        import http.client
        conn = http.client.HTTPConnection(ips[0], u.port or 80, timeout=timeout)
        try:
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            h = dict(headers)
            h["Host"] = u.hostname
            conn.request("POST", path, body=data, headers=h)
            resp = conn.getresponse()
            payload = resp.read()
            status = resp.status
        finally:
            conn.close()
        if status // 100 != 2:
            raise RuntimeError("上游返回 %d: %r" % (status, payload[:200]))
        return payload

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with opener.open(req, timeout=timeout) as r:
        if r.status // 100 != 2:
            raise RuntimeError("上游返回 %d" % r.status)
        return r.read()


# ---------------- 日志（接管 zr.log；JSONL 全量落盘在服务器上关闭） ----------------

_logf = None
_loglock = threading.Lock()


def gw_log(msg):
    global _logf
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _loglock:
        try:
            if _logf is None:
                _logf = open(LOG_FILE, "a", encoding="utf-8")
            elif os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                _logf.close()
                try:
                    os.replace(LOG_FILE, LOG_FILE + ".old")
                except OSError:
                    pass
                _logf = open(LOG_FILE, "a", encoding="utf-8")
            _logf.write(line + "\n")
            _logf.flush()
        except OSError:
            pass
        if sys.stdout is not None:
            try:
                print(line, flush=True)
            except Exception:
                pass


zr.log = gw_log
zr.log_event = lambda direction, obj: None   # 服务器上不落全量 JSONL（防磁盘爆）


class ReconnectSignal(Exception):
    pass


# ---------------- 网关态终端 ----------------

class GatewayTerminal(zr.ZCodeTerminal):
    """线程化改造：发送加锁；rpc_call 改为事件等待（由泵线程分发响应）。"""

    def __init__(self, params, origin, gw):
        super().__init__(params, origin)
        self.gw = gw
        self.send_lock = threading.Lock()
        self.fatal = None            # 'KICKED' / 'degraded' / ...

    def _send(self, obj):
        with self.send_lock:
            super()._send(obj)

    # 覆盖为纯等待：泵线程负责收包分发
    def _pump(self, deadline, cond=None):
        while time.time() < deadline:
            if cond is not None and cond():
                return True
            time.sleep(0.05)
        return cond() if cond is not None else False

    def rpc_call(self, channel, method, args=None, timeout=zr.APP_TIMEOUT):
        rid = self.next_rpc_id
        self.next_rpc_id += 1
        ev = threading.Event()
        slot = {"result": None, "error": None, "done": False, "ev": ev}
        self.pending[rid] = slot
        buf = zr.benc([zr.REQ_PROMISE, rid, channel, method]) + zr.benc(args if args is not None else [])
        self.send_rpc_buf(buf)
        gw_log("[rpc←] %s.%s (#%d)" % (channel, method, rid))
        if not ev.wait(timeout):
            self.pending.pop(rid, None)
            raise TimeoutError("RPC 超时: %s.%s" % (channel, method))
        self.pending.pop(rid, None)
        if slot["error"] is not None:
            raise zr.RpcError("%s.%s → %s" % (channel, method,
                              json.dumps(slot["error"], ensure_ascii=False, default=repr)[:300]))
        return slot["result"]

    # 分发：在父类基础上给等待方发事件
    def _dispatch_rpc(self, buf):
        before = {rid: s["done"] for rid, s in self.pending.items()}
        super()._dispatch_rpc(buf)
        for rid, was in before.items():
            s = self.pending.get(rid)
            if s is not None and s["done"] and not was:
                s["ev"].set()

    # 静默帧处理：更新网关状态，不打大段日志
    def _on_conversation_frame(self, data):
        self.gw.on_conversation_frame(data)

    def _on_sessions_index_frame(self, data):
        self.gw.on_sessions_index_frame(data)

    def _on_payload(self, p):
        zt = p.get("zcode_type")
        if zt == "workspace-list-updated":
            self.gw.update_tasks((p.get("result") or {}))
            return
        if zt == "bridge-degraded":
            self.fatal = "degraded:%s" % (p.get("reasonCode") or p.get("reason") or "?")
            gw_log("✗ 桥降级 %s，准备重连" % self.fatal)
            raise ReconnectSignal(self.fatal)
        if zt == "workspace-bridge-error":
            self.fatal = "bridge-error"
            raise ReconnectSignal(self.fatal)
        super()._on_payload(p)


# ---------------- 网关主体 ----------------

class Gateway:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.term = None
        self.status = "init"          # init/connecting/online/reconnecting/error
        self.last_error = None
        self.connected_at = None
        self.workspaces = []
        self.tasks = {}               # workspace-list 最新结果
        self.sessions = []            # sessions-index 快照
        self.conv_state = {}          # topic -> {snapshot 摘要}
        self.events = []              # 环形事件
        self.event_seq = 0
        self.stop = False
        self.workspace_obj = None
        self.audio_store = {}          # audioId -> (wav_bytes, expires_ts)

    # -- 事件环 --
    def append_event(self, kind, **kw):
        with self.lock:
            self.event_seq += 1
            ev = {"seq": self.event_seq, "ts": now_ms(), "kind": kind}
            ev.update(kw)
            self.events.append(ev)
            if len(self.events) > EVENT_RING_MAX:
                del self.events[:len(self.events) - EVENT_RING_MAX]
            return ev

    def events_since(self, since, task_id=None):
        with self.lock:
            out = [e for e in self.events if e["seq"] > since]
            if task_id:
                out = [e for e in out if e.get("taskId") in (task_id, None)]
            return out, self.event_seq

    def evict_conv_state(self):
        """conv_state 按 updatedAt 做 LRU 淘汰（调用方需持锁）。
        活跃会话每帧都刷新 updatedAt，不会被误淘汰；被淘汰的 topic 下次访问会重新订阅取回。"""
        over = len(self.conv_state) - CONV_STATE_MAX_TOPICS
        if over <= 0:
            return
        ranked = sorted(self.conv_state.items(),
                        key=lambda kv: (kv[1].get("updatedAt") or kv[1].get("snapshotAt") or 0))
        for topic, _st in ranked[:over]:
            self.conv_state.pop(topic, None)
            gw_log("conv_state LRU 淘汰 %s（余 %d/%d）"
                   % (topic, len(self.conv_state), CONV_STATE_MAX_TOPICS))

    # -- 状态更新回调 --
    def update_tasks(self, result):
        with self.lock:
            self.tasks = result
        gw_log("任务列表刷新：%d 个，active=%s" % (len(result.get("tasks") or []), result.get("activeTaskId")))

    def on_sessions_index_frame(self, data):
        try:
            payload = data["frame"]["payload"]
            if payload.get("kind") == "snapshot":
                with self.lock:
                    self.sessions = (payload.get("snapshot") or {}).get("sessions") or []
                gw_log("会话索引快照：%d 个会话" % len(self.sessions))
                self.append_event("sessions_index", count=len(self.sessions))
        except Exception as e:
            gw_log("索引帧解析失败: %s" % e)

    def on_conversation_frame(self, data):
        try:
            topic = data.get("topic") or ""
            task_id = topic.split("/", 1)[1] if "/" in topic else None
            fr = data.get("frame") or {}
            to_seq = fr.get("toSeq")
            if isinstance(to_seq, int):
                with self.lock:
                    st = self.conv_state.setdefault(topic, {})
                    if to_seq > (st.get("lastToSeq") or -1):
                        st["lastToSeq"] = to_seq
            payload = fr.get("payload") or {}
            kind = payload.get("kind")
            if kind == "snapshot":
                snap = payload.get("snapshot") or {}
                ctl = snap.get("control") or {}
                snap_texts = []
                _walk_assistant_text(payload, snap_texts)
                with self.lock:
                    st0 = self.conv_state.setdefault(topic, {})
                    st0.update({
                        "sessionId": snap.get("sessionId"),
                        "phase": ctl.get("phase"),
                        "sessionEnded": ctl.get("sessionEnded"),
                        "title": (snap.get("meta") or {}).get("title"),
                        "model": (snap.get("config") or {}).get("model"),
                        "usedTokens": ((snap.get("usage") or {}).get("contextWindow") or {}).get("usedTokens"),
                        "updatedAt": now_ms(),
                        "snapshotAt": now_ms(),
                    })
                    _cache_texts(st0, snap_texts)
                    self.evict_conv_state()
                self.append_event("phase", taskId=task_id, phase=ctl.get("phase"),
                                  title=(snap.get("meta") or {}).get("title"))
            elif kind == "deltas":
                texts = []
                _walk_assistant_text(payload, texts)
                for t in texts:
                    self.append_event("assistant_text", taskId=task_id, **t)
                if texts:
                    with self.lock:
                        _cache_texts(self.conv_state.setdefault(topic, {}), texts)
                        self.evict_conv_state()
                ops = []
                _walk_ops(payload, ops)
                for o in ops:
                    if o.get("op") in ("control.updated", "state.updated"):
                        ctl = (o.get("next") or o.get("control") or {})
                        phase = ctl.get("phase") if isinstance(ctl, dict) else None
                        if phase:
                            with self.lock:
                                st = self.conv_state.setdefault(topic, {})
                                if st.get("phase") != phase:
                                    st["phase"] = phase
                                    st["updatedAt"] = now_ms()
                                    self.append_event("phase", taskId=task_id, phase=phase)
        except Exception as e:
            gw_log("会话帧解析失败: %s" % e)

    # -- 主循环 --
    def run_forever(self):
        backoff = 3
        while not self.stop:
            try:
                self.status = "connecting"
                self._connect_once()          # 内部启动泵线程
                backoff = 3
                self.pump_thread.join()       # 泵线程死亡 = 连接终结
                if self.stop:
                    break
                err = self.pump_error
                if err is None and self.term and self.term.fatal:
                    err = ReconnectSignal(self.term.fatal)
                raise err or ReconnectSignal("pump exited")
            except ReconnectSignal as e:
                self.last_error = str(e)
                gw_log("重连（%s）" % e)
                if "KICKED" in str(e):
                    backoff = max(backoff, 30)   # 被官方端接管，退避长一点
            except Exception as e:
                self.last_error = "%s: %s" % (type(e).__name__, e)
                gw_log("连接异常：%s" % self.last_error)
            with self.lock:
                self.status = "reconnecting"
                self.term = None
            self._close_quiet()
            if self.stop:
                break
            gw_log("%ds 后重连…" % backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _close_quiet(self):
        try:
            if self.term and self.term.ws:
                self.term.ws.close()
        except Exception:
            pass

    def _connect_once(self):
        link_path = self.cfg.get("link_file", "link.txt")
        if not os.path.isabs(link_path):
            link_path = os.path.join(HERE, link_path)
        with open(link_path, encoding="utf-8") as f:
            link = f.read().strip()
        params = zr.parse_link(link)
        term = GatewayTerminal(params, self.cfg.get("origin", zr.DEFAULT_ORIGIN), self)
        if not term.connect():
            raise RuntimeError("中继连接失败")
        self.workspaces = term.bootstrap()
        wl = term.list_workspace_tasks()
        self.update_tasks(wl)
        # ★ follow-don't-lead：桥只开在桌面"当前活动工作区"上，连接/重连都不会把桌面 GUI
        #   拽到别的项目目录（修复"会话乱窜"）。拿不到活动工作区时才回退配置项。
        active_key = (wl or {}).get("activeWorkspaceKey")
        w = None
        if active_key:
            for cand in self.workspaces:
                if zr.workspace_key_of(cand) == active_key:
                    w = cand
                    break
        if not w:
            w = zr.pick_workspace(self.workspaces, self.cfg.get("workspace"))
        if not w:
            raise RuntimeError("无可用工作区")
        self.workspace_obj = w
        key = zr.workspace_key_of(w)
        task = zr.pick_task(wl, key, None if self.cfg.get("task") == "auto" else self.cfg.get("task"))
        task_id = (task or {}).get("taskId")
        gw_log("桥接目标：active_key=%r → 选定 key=%r（follow-don't-lead 匹配=%s）"
               % (active_key, key, bool(active_key) and key == active_key))
        term.open_bridge(key, task_id)
        with self.lock:
            self.term = term
        # 泵线程先起（conversation_connect 的 rpc_call 靠泵线程分发响应）
        self.pump_error = None
        self.pump_thread = threading.Thread(target=self._pump_safe, daemon=True)
        self.pump_thread.start()
        term.conversation_connect(w, task_id, subscribe=True)
        # home 工作区的会话帧监听已在 conversation_connect 内建立，登记避免重复
        self._ws_listens = {(w.get("workspacePath"), (w.get("workspaceIdentity") or "").strip())}
        with self.lock:
            self.status = "online"
            self.connected_at = now_ms()
        gw_log("★ 网关在线：workspace=%s task=%s" % (key, task_id))
        self.append_event("gateway_online", workspace=key, taskId=task_id)

    def _pump_safe(self):
        try:
            self._pump_forever()
        except ReconnectSignal as e:
            self.pump_error = e
        except Exception as e:
            self.pump_error = ReconnectSignal("%s: %s" % (type(e).__name__, e))
        finally:
            gw_log("泵线程退出：%s" % (self.pump_error or "正常"))

    def _pump_forever(self):
        term = self.term
        while not self.stop:
            term.ws.settimeout(5.0)
            msg = term._recv()
            if msg is None:
                if term.fatal:
                    raise ReconnectSignal(term.fatal)
                continue
            t = msg.get("type")
            if t == "data":
                term._on_payload(msg.get("payload") or {})
            elif t == "error":
                code = msg.get("code")
                gw_log("✗ relay error: %s %s" % (code, msg.get("message")))
                raise ReconnectSignal("KICKED" if code == "KICKED" else "relay:%s" % code)

    # -- 业务 --
    def active_task_id(self):
        with self.lock:
            return (self.tasks or {}).get("activeTaskId")

    def _require_online(self):
        with self.lock:
            term, status = self.term, self.status
        if term is None or status != "online":
            raise RuntimeError("网关未连接（status=%s）" % status)
        return term

    def _task_ws(self, task_id):
        """任务自身的工作区 (workspacePath, workspaceIdentity)。跨工作区订阅/发消息靠它，
        不再依赖桥接工作区；找不到则回退当前桥接工作区。"""
        for t in (self.tasks or {}).get("tasks") or []:
            if t.get("taskId") == task_id and t.get("workspacePath"):
                wp = t.get("workspacePath")
                ident = ""
                for w in (self.workspaces or []):
                    if w.get("workspacePath") == wp:
                        ident = (w.get("workspaceIdentity") or "").strip()
                        break
                return wp, ident
        wo = self.workspace_obj or {}
        return wo.get("workspacePath"), (wo.get("workspaceIdentity") or "").strip()

    def _ensure_ws_listen(self, term, wp, ident):
        """确保对该工作区有 onDynamicConversationFrame 监听：在同一条连接上懒加载，
        绝不切桥/重连（切桥才会牵动桌面活动工作区）。"""
        if not wp:
            return
        if not hasattr(self, "_ws_listens"):
            self._ws_listens = set()
        key = (wp, ident or "")
        if key in self._ws_listens:
            return
        filt = {"workspacePath": wp}
        if ident:
            filt["workspaceIdentity"] = ident
        term.rpc_listen(zr.CH_AGENT, "onDynamicConversationFrame", filt, term._on_conversation_frame)
        self._ws_listens.add(key)
        gw_log("新增会话帧监听 ws=%s" % wp)

    def ensure_conversation_sub(self, task_id):
        """确保目标会话已订阅；返回 True 表示本次新订阅（调用方应等快照帧落地再取基线）。
        ★ 按任务自身工作区订阅（不再用桥接工作区），跨工作区会话也能收到回复帧。"""
        term = self._require_online()
        if any(t == "conversation/%s" % task_id for t in term.subscriptions.values()):
            return False
        try:
            wp, ident = self._task_ws(task_id)
            self._ensure_ws_listen(term, wp, ident)
            sub = {"workspacePath": wp,
                   "connectionId": term.hello.get("connectionId"),
                   "clientMode": term.hello.get("clientMode"),
                   "visibility": "foreground",
                   "sessionId": task_id}
            if ident:
                sub["workspaceIdentity"] = ident
            r = term.rpc_call(zr.CH_AGENT, "subscribeConversationV4", [sub])
            term._note_sub(r, "conversation/%s" % task_id)
            return True
        except Exception as e:
            gw_log("补订阅失败（不影响发消息）: %s" % e)
            return False

    def send_prompt(self, text, task_id=None):
        self._require_online()
        t = self._resolve_task(task_id)
        if not t or not t.get("taskId"):
            task_id = task_id or self.active_task_id()
            if not task_id:
                raise RuntimeError("无目标任务：请传 taskId")
        else:
            task_id = t["taskId"]
        # ★ 不再 switch_workspace：那会重连中继并把桌面活动工作区拽走（"乱窜"根因）。
        #   ensure_conversation_sub 已按任务自身工作区订阅，sendPrompt 走 taskId 路由。
        self.ensure_conversation_sub(task_id)
        term = self._require_online()
        r = term.send_prompt(task_id, text)
        self.append_event("prompt_sent", taskId=task_id, chars=len(text))
        return {"ok": True, "taskId": task_id, "result": r}

    # -- 工牌 UI 数据 --
    @staticmethod
    def _path_label(p):
        return (p or "").replace("\\", "/").rstrip("/").split("/")[-1] or "?"

    def ui_workspaces(self):
        tasks = (self.tasks or {}).get("tasks") or []
        groups = {}
        for t in tasks:
            p = t.get("workspacePath") or "?"
            g = groups.get(p)
            if g is None:
                g = groups[p] = {"path": p, "label": t.get("workspaceLabel") or self._path_label(p),
                                 "total": 0, "running": 0, "activeTaskId": None,
                                 "activeTitle": None, "lastActivity": 0}
            g["total"] += 1
            if t.get("displayStatus") == "running":
                g["running"] += 1
            ua = t.get("updatedAt") or t.get("createdAt") or 0
            if ua > g["lastActivity"]:
                g["lastActivity"] = ua
        active = (self.tasks or {}).get("activeTaskId")
        for t in tasks:
            if t.get("taskId") == active:
                g = groups.get(t.get("workspacePath"))
                if g:
                    g["activeTaskId"] = active
                    g["activeTitle"] = t.get("title")
        for w in self.workspaces:          # bootstrap 里 0 任务的工作区也列出
            p = w.get("workspacePath")
            if p and p not in groups:
                groups[p] = {"path": p, "label": w.get("label") or self._path_label(p),
                             "total": 0, "running": 0, "activeTaskId": None,
                             "activeTitle": None, "lastActivity": 0}
        out = sorted(groups.values(), key=lambda g: (-g["running"], -g["lastActivity"]))
        cur = (self.workspace_obj or {}).get("workspacePath")
        for g in out:
            g["current"] = (g["path"] == cur)
        return out

    def ui_sessions(self, ws_path=None):
        tasks = (self.tasks or {}).get("tasks") or []
        if not ws_path:
            ws_path = (self.workspace_obj or {}).get("workspacePath")
        rows = [t for t in tasks if t.get("workspacePath") == ws_path]
        rows.sort(key=lambda t: -(t.get("updatedAt") or t.get("createdAt") or 0))
        out = [{"taskId": t.get("taskId"), "title": t.get("title") or "(无标题)",
                "status": t.get("displayStatus"), "updatedAt": t.get("updatedAt")} for t in rows]
        # 当前桥接工作区可用 sessions-index 快照补充实时状态
        for s in self.sessions:
            for o in out:
                if o["taskId"] == s.get("sessionId"):
                    o["phase"] = s.get("phase")
                    o["preview"] = (s.get("lastAssistantPreview") or "")[:80]
        return out

    # -- 工作区切换（重开桥） --
    def switch_workspace(self, ws_path, timeout=45):
        """★ 已废弃为 no-op：切换工作区会重连中继并把桌面 GUI 的当前项目拽走（"乱窜"根因）。
        跨工作区改由 ensure_conversation_sub 按任务自身 workspacePath 订阅 + taskId 路由 sendPrompt 实现。
        保留签名兼容旧调用方；恒返回 True（视为"已在目标工作区"），绝不重连/切桥。"""
        gw_log("switch_workspace 已禁用(no-op)，目标=%s" % ws_path)
        return True

    def _resolve_task(self, task_id):
        tasks = (self.tasks or {}).get("tasks") or []
        if not task_id:
            task_id = (self.tasks or {}).get("activeTaskId")
        for t in tasks:
            if t.get("taskId") == task_id:
                return t
        return {"taskId": task_id} if task_id else None

    # -- ASR / TTS --
    def asr(self, pcm, rate):
        """语音转文字。优先腾讯云一句话识别（asr_tencent_id/key，公网端点），
        否则 OpenAI 兼容端点（asr_url，如 FunASR）。"""
        if self.cfg.get("asr_tencent_id") and self.cfg.get("asr_tencent_key"):
            return self._asr_tencent(pcm, rate)
        return self._asr_openai(pcm, rate)

    def _asr_tencent(self, pcm, rate):
        if rate != 16000:
            # 腾讯 ASR 引擎只支持 16k；设备录 24kHz 时用 miniaudio 重采样
            orig_rate, orig_len = rate, len(pcm)
            try:
                import miniaudio
                wav_in = _pcm_to_wav(pcm, rate)
                dec = miniaudio.decode(wav_in, output_format=miniaudio.SampleFormat.SIGNED16,
                                       nchannels=1, sample_rate=16000)
                pcm = bytes(dec.samples)
                rate = 16000
                gw_log("ASR 重采样 %d→16000Hz (%d→%d B)" % (orig_rate, orig_len, len(pcm)))
            except ImportError:
                raise RuntimeError("腾讯云 ASR 需要 rate=16000（收到 %d）且 miniaudio 未安装" % orig_rate)
        import base64 as _b64
        wav_b64 = _b64.b64encode(_pcm_to_wav(pcm, rate)).decode("ascii")
        payload = json.dumps({
            "EngSerViceType": "16k_zh", "SourceType": 1, "VoiceFormat": "wav",
            "Data": wav_b64, "FilterDirty": 0, "FilterModal": 0, "FilterPunc": 0,
            "ConvertNumMode": 1,
        }, separators=(",", ":"))
        host = "asr.tencentcloudapi.com"
        headers = _tc3_authorization(
            self.cfg["asr_tencent_id"], self.cfg["asr_tencent_key"],
            "asr", host, "SentenceRecognition", "2019-06-14", payload,
            self.cfg.get("asr_tencent_region") or "ap-shanghai")
        data = _post_no_redirect("https://%s/" % host, payload.encode("utf-8"), headers, timeout=30)
        resp = json.loads(data.decode("utf-8", "replace")).get("Response") or {}
        if resp.get("Error"):
            raise RuntimeError("腾讯云ASR错误: %s" % json.dumps(resp["Error"], ensure_ascii=False)[:200])
        text = (resp.get("Result") or "").strip()
        gw_log("ASR(腾讯): %d 字节音频 → %r" % (len(pcm), text[:60]))
        return text

    def _asr_openai(self, pcm, rate):
        base = (self.cfg.get("asr_url") or "").rstrip("/")
        if not base:
            raise RuntimeError("ASR 未配置（gateway-config.json 的 asr_tencent_id/key 或 asr_url）")
        boundary = "----zcodegw%d" % uuid.uuid4().int
        model = self.cfg.get("asr_model") or "SenseVoiceSmall"
        wav_bytes = _pcm_to_wav(pcm, rate)
        body = io.BytesIO()
        body.write(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                    "Content-Type: audio/wav\r\n\r\n" % boundary).encode())
        body.write(wav_bytes)
        body.write(("\r\n--%s\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n%s"
                    "\r\n--%s--\r\n" % (boundary, model, boundary)).encode())
        headers = {"Content-Type": "multipart/form-data; boundary=%s" % boundary}
        if self.cfg.get("asr_key"):
            headers["Authorization"] = "Bearer %s" % self.cfg["asr_key"]
        data = _post_no_redirect(base + "/v1/audio/transcriptions", body.getvalue(), headers, timeout=60)
        text = (json.loads(data.decode("utf-8", "replace")).get("text") or "").strip()
        gw_log("ASR: %d 字节音频 → %r" % (len(pcm), text[:60]))
        return text

    def tts(self, text):
        """edge-tts → mp3 → miniaudio 解码重采样 → 16k mono s16 WAV 字节。"""
        try:
            import edge_tts
            import miniaudio
        except ImportError as e:
            raise RuntimeError("TTS 依赖缺失（pip install edge-tts miniaudio）: %s" % e)
        voice = self.cfg.get("tts_voice") or "zh-CN-XiaoxiaoNeural"
        text = text[:800]   # 工牌播报长度上限

        async def _synth():
            chunks = []
            comm = edge_tts.Communicate(text, voice)
            async for c in comm.stream():
                if c["type"] == "audio":
                    chunks.append(c["data"])
            return b"".join(chunks)

        mp3 = asyncio.run(_synth())
        if not mp3:
            raise RuntimeError("TTS 返回空音频")
        dec = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16,
                               nchannels=1, sample_rate=16000)
        return _pcm_to_wav(bytes(dec.samples), 16000)

    def save_audio(self, wav_bytes):
        aid = "a-%s" % uuid.uuid4().hex[:16]
        now = time.time()
        with self.lock:
            expired = [k for k, (_, exp) in self.audio_store.items() if exp < now]
            for k in expired:
                del self.audio_store[k]
            while len(self.audio_store) >= AUDIO_STORE_MAX:
                oldest = min(self.audio_store, key=lambda k: self.audio_store[k][1])
                del self.audio_store[oldest]
            self.audio_store[aid] = (wav_bytes, now + AUDIO_TTL_SEC)
        return aid

    def get_audio(self, aid):
        with self.lock:
            item = self.audio_store.get(aid)
            if item and item[1] >= time.time():
                return item[0]
            self.audio_store.pop(aid, None)
        return None

    # -- 问答闭环 --
    def wait_for_answer(self, task_id, baseline_seq, known_rids, timeout, min_entry_seq=None):
        """等新回复。因果门槛：条目的 createdAtSeq 必须大于发问时刻的日志尾部序号
        （min_entry_seq），历史回放帧无论何时到达都会被排除。known_rids 为辅助门槛。"""
        deadline = time.time() + timeout
        partial = ""
        while time.time() < deadline:
            evs, _ = self.events_since(baseline_seq, task_id)
            for e in evs:
                if e["kind"] != "assistant_text" or e.get("responseId") in known_rids:
                    if e["kind"] == "phase" and e.get("phase") in ("completedFailed", "error", "aborted"):
                        raise RuntimeError("会话进入错误状态: %s" % e.get("phase"))
                    continue
                eseq = e.get("createdAtSeq")
                if min_entry_seq is not None and isinstance(eseq, int) and eseq <= min_entry_seq:
                    continue                      # 历史条目（回放），跳过
                if e.get("state") == "complete" and (e.get("text") or "").strip():
                    return e["text"].strip()
                partial = e.get("text") or partial
            time.sleep(0.5)
        return partial.strip() or None

    def ask(self, task_id, pcm=None, rate=16000, text=None):
        t = self._resolve_task(task_id)
        if not t or not t.get("taskId"):
            raise RuntimeError("无目标任务：请传 taskId")
        task_id = t["taskId"]
        # ★ 不再 switch_workspace（避免牵动桌面活动工作区）；ensure_conversation_sub 按任务自身 ws 订阅。
        # 先补订阅；等日志尾部序号稳定（回放帧落完），记录因果门槛 min_entry_seq
        try:
            self.ensure_conversation_sub(task_id)
        except Exception as e:
            gw_log("ask 前订阅失败: %s" % e)
        if text is None:
            if not pcm:
                raise RuntimeError("无音频数据")
            text = self.asr(pcm, rate)
            if not text:
                return {"ok": False, "error": "ASR 未识别出内容", "question": ""}
        topic = "conversation/%s" % task_id
        last_seq, stable_at = -1, time.time()
        while time.time() - stable_at < 10:
            cur = (self.conv_state.get(topic) or {}).get("lastToSeq", -1)
            if cur != last_seq:
                last_seq, stable_at = cur, time.time()
            elif cur >= 0 and time.time() - stable_at >= 2.0:
                break
            time.sleep(0.4)
        min_entry_seq = last_seq if last_seq >= 0 else None
        with self.lock:
            baseline = self.event_seq
            known_rids = {e.get("responseId") for e in self.events
                          if e["kind"] == "assistant_text" and e.get("responseId")}
        self.send_prompt(text, task_id)
        timeout = int(self.cfg.get("ask_timeout") or 180)
        answer = self.wait_for_answer(task_id, baseline, known_rids, timeout, min_entry_seq)
        if not answer:
            return {"ok": False, "error": "等待回复超时", "question": text, "answer": ""}
        audio_id, audio_bytes = None, 0
        try:
            wav = self.tts(answer)
            audio_id = self.save_audio(wav)
            audio_bytes = len(wav)
        except Exception as e:
            gw_log("TTS 失败（不影响文本回答）: %s" % e)
        return {"ok": True, "taskId": task_id, "question": text, "answer": answer,
                "audioId": audio_id, "audioBytes": audio_bytes}

    def history(self, task_id, limit=8, wait=6.0):
        """最近 limit 条历史输出（依赖订阅回放帧缓存；首次订阅需等快照落地）。"""
        t = self._resolve_task(task_id)
        if t and t.get("taskId"):
            task_id = t["taskId"]
        topic = "conversation/%s" % task_id
        try:
            self.ensure_conversation_sub(task_id)
        except Exception as e:
            gw_log("history 前订阅失败: %s" % e)
        start = time.time()
        snap_at, entries = None, []
        while time.time() - start < wait:
            with self.lock:
                st = self.conv_state.get(topic) or {}
                snap_at = st.get("snapshotAt")
                entries = list((st.get("texts") or {}).values())
            if snap_at and (entries or time.time() - start > 2.5):
                break
            time.sleep(0.3)
        entries.sort(key=lambda e: e.get("createdAtSeq") or 0)
        tail = entries[-limit:] if limit and limit > 0 else entries
        tail.reverse()          # 最新在前：工牌打开详情页先看到最近的输出
        parts = []
        for i, e in enumerate(tail, 1):
            txt = (e.get("text") or "").strip()
            if len(txt) > 700:
                txt = txt[:700] + "…"
            parts.append("[%d] %s" % (i, txt))
        return {"ok": True, "taskId": task_id, "count": len(entries),
                "text": "\n\n".join(parts)}

    def health(self):
        with self.lock:
            term = self.term
            return {
                "ok": self.status == "online",
                "status": self.status,
                "lastError": self.last_error,
                "connectedAt": self.connected_at,
                "uptimeSec": int((now_ms() - self.connected_at) / 1000) if self.connected_at and self.status == "online" else 0,
                "hello": (term.hello or {}).get("clientMode") if term else None,
                "subscriptions": list(term.subscriptions.values()) if term else [],
                "workspaces": len(self.workspaces),
                "tasks": len((self.tasks or {}).get("tasks") or []),
                "activeTaskId": (self.tasks or {}).get("activeTaskId"),
                "sessions": len(self.sessions),
                "eventSeq": self.event_seq,
            }


def now_ms():
    return int(time.time() * 1000)


def _walk_assistant_text(obj, out):
    if isinstance(obj, dict):
        if obj.get("kind") == "assistantText" and isinstance(obj.get("text"), str):
            out.append({"text": obj["text"], "state": obj.get("state"),
                        "responseId": obj.get("assistantResponseId"),
                        "createdAtSeq": obj.get("createdAtSeq")})
        for v in obj.values():
            _walk_assistant_text(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_assistant_text(v, out)


def _cache_texts(st, texts):
    """会话历史输出缓存（调用方需持锁）：按 responseId 去重保留最长文本，上限 60 条，
    单条超过 TEXT_CACHE_MAX_CHARS 截断（历史回看用不到更长正文，却会长期占内存）。"""
    cache = st.setdefault("texts", {})
    for t in texts:
        txt = (t.get("text") or "").strip()
        if not txt:
            continue
        rid = t.get("responseId") or "seq-%s" % t.get("createdAtSeq")
        prev = cache.get(rid)
        if prev is None or len(txt) >= len((prev.get("text") or "").strip()):
            if len(txt) > TEXT_CACHE_MAX_CHARS:
                t = dict(t)
                t["text"] = txt[:TEXT_CACHE_MAX_CHARS] + "…（历史缓存已截断）"
            cache[rid] = t
    if len(cache) > 60:
        oldest = sorted(cache, key=lambda r: (cache[r].get("createdAtSeq") or 0))[:len(cache) - 60]
        for rid in oldest:
            cache.pop(rid, None)


def _walk_ops(obj, out):
    if isinstance(obj, dict):
        if isinstance(obj.get("op"), str):
            out.append(obj)
        for v in obj.values():
            _walk_ops(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_ops(v, out)


# ---------------- HTTP API ----------------

class Handler(BaseHTTPRequestHandler):
    gw = None

    def log_message(self, fmt, *args):
        gw_log("[http] %s" % (fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, default=repr).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        token = (self.gw.cfg.get("token") or "").strip()
        if not token:
            return True
        return self.headers.get("X-Gateway-Token") == token

    def _binary(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self, limit=AUDIO_MAX_BYTES):
        """支持 Content-Length 与 Transfer-Encoding: chunked（工牌边录边传）。"""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        cl = self.headers.get("Content-Length")
        gw_log("_read_body: TE=%r CL=%r" % (te, cl))
        if "chunked" in te:
            body = bytearray()
            nchunks = 0
            while True:
                raw = self.rfile.readline(64)
                if nchunks == 0:
                    gw_log("chunked 首行原始字节: %r" % raw[:48])
                line = raw.strip()
                if not line:
                    gw_log("chunked: 第%d块遇空行, 已收%d B" % (nchunks, len(body)))
                    break
                try:
                    n = int(line.split(b";")[0], 16)
                except ValueError:
                    gw_log("chunked: 非法块大小行 %r (第%d块)" % (line[:48], nchunks))
                    raise
                if n == 0:
                    self.rfile.readline()
                    gw_log("chunked: 完成 %d 块共 %d B" % (nchunks, len(body)))
                    break
                if len(body) + n > limit:
                    raise ValueError("请求体超过上限 %d 字节" % limit)
                body += self.rfile.read(n)
                self.rfile.readline()
                nchunks += 1
            return bytes(body)
        n = int(cl or 0)
        if n > limit:
            raise ValueError("请求体超过上限 %d 字节" % limit)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        from urllib.parse import parse_qs
        u = urlparse(self.path)
        path, qs = u.path.rstrip("/"), parse_qs(u.query)
        gw = self.gw
        try:
            if path == "/health":
                self._json(200, gw.health())
                return
            if not self._authed():
                self._json(401, {"error": "invalid or missing X-Gateway-Token"})
                return
            if path == "/workspaces":
                self._json(200, {"workspaces": gw.workspaces})
            elif path == "/ui/workspaces":
                self._json(200, {"workspaces": gw.ui_workspaces()})
            elif path == "/ui/sessions":
                ws = (qs.get("workspace") or [None])[0]
                self._json(200, {"sessions": gw.ui_sessions(ws)})
            elif path == "/ui/history":
                task_id = (qs.get("task") or [None])[0]
                if not task_id:
                    self._json(400, {"error": "task 必填"})
                else:
                    limit = int((qs.get("limit") or ["8"])[0])
                    self._json(200, gw.history(task_id, limit))
            elif path == "/tasks":
                self._json(200, gw.tasks or {})
            elif path == "/sessions":
                self._json(200, {"sessions": gw.sessions})
            elif path.startswith("/sessions/"):
                task_id = path.split("/sessions/", 1)[1]
                topic = "conversation/%s" % task_id
                st = gw.conv_state.get(topic)
                if st is None:
                    for s in gw.sessions:
                        if s.get("sessionId") == task_id:
                            st = {"from": "sessions-index", **{k: s.get(k) for k in
                                 ("sessionId", "title", "phase", "lastActivityAt", "lastAssistantPreview")}}
                            break
                self._json(200 if st else 404, st or {"error": "unknown session"})
            elif path.startswith("/audio/"):
                data = gw.get_audio(path.split("/audio/", 1)[1])
                if data is None:
                    self._json(404, {"error": "audio expired or unknown"})
                else:
                    self._binary(200, data, "audio/wav")
            elif path == "/events":
                since = int((qs.get("since") or ["0"])[0])
                wait = min(int((qs.get("wait") or ["20"])[0]), 55)
                task_id = (qs.get("taskId") or [None])[0]
                deadline = time.time() + wait
                while True:
                    evs, seq = gw.events_since(since, task_id)
                    if evs or time.time() >= deadline:
                        self._json(200, {"events": evs[-200:], "seq": seq})
                        break
                    time.sleep(0.5)
            else:
                self._json(404, {"error": "not found", "paths": [
                    "/health", "/workspaces", "/tasks", "/sessions", "/sessions/<id>",
                    "/prompt", "/events", "/ui/workspaces", "/ui/sessions", "/ui/history",
                    "/ask", "/asr", "/tts", "/audio/<id>"]})
        except Exception as e:
            self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        try:
            if not self._authed():
                self._json(401, {"error": "invalid or missing X-Gateway-Token"})
                return
            ctype = (self.headers.get("Content-Type") or "").lower()
            if path == "/ask":
                from urllib.parse import parse_qs
                qs = parse_qs(u.query)
                task_id = (qs.get("taskId") or [None])[0]
                rate = int((qs.get("rate") or ["16000"])[0])
                if rate not in (8000, 16000, 24000, 48000):
                    self._json(400, {"error": "rate 仅支持 8000/16000/24000/48000"})
                    return
                if "application/json" in ctype:
                    body = json.loads(self._read_body(64 * 1024) or b"{}")
                    text = (body.get("text") or "").strip()
                    if not text:
                        self._json(400, {"error": "text 必填"})
                        return
                    r = self.gw.ask(body.get("taskId") or task_id, text=text)
                else:
                    pcm = self._read_body()
                    if len(pcm) < rate:      # 至少 ~0.03s
                        gw_log("ask 音频过短: %d B (rate=%d)" % (len(pcm), rate))
                        self._json(400, {"error": "音频过短（收到 %d 字节）" % len(pcm)})
                        return
                    r = self.gw.ask(task_id, pcm=pcm, rate=rate)
                self._json(200, r)
            elif path == "/asr":
                from urllib.parse import parse_qs
                qs = parse_qs(u.query)
                rate = int((qs.get("rate") or ["16000"])[0])
                pcm = self._read_body()
                if not pcm:
                    self._json(400, {"error": "无音频数据"})
                    return
                self._json(200, {"text": self.gw.asr(pcm, rate)})
            elif path == "/tts":
                body = json.loads(self._read_body(64 * 1024) or b"{}")
                text = (body.get("text") or "").strip()
                if not text:
                    self._json(400, {"error": "text 必填"})
                    return
                self._binary(200, self.gw.tts(text), "audio/wav")
            elif path == "/prompt":
                body = json.loads(self._read_body(64 * 1024) or b"{}")
                text = (body.get("text") or "").strip()
                if not text:
                    self._json(400, {"error": "text 必填"})
                    return
                r = self.gw.send_prompt(text, body.get("taskId"))
                self._json(200, r)
            else:
                self._json(404, {"error": "not found"})
        except zr.RpcError as e:
            gw_log("do_POST RpcError: %s" % e)
            self._json(502, {"error": str(e)})
        except ValueError as e:
            gw_log("do_POST ValueError: %s" % e)
            self._json(400, {"error": str(e)})
        except Exception as e:
            gw_log("do_POST Exception: %s: %s" % (type(e).__name__, e))
            self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})


# ---------------- 入口 ----------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="ZCode 工牌网关服务")
    ap.add_argument("--config", default=os.path.join(HERE, "gateway-config.json"))
    ap.add_argument("--once", action="store_true", help="自检模式：跑 --duration 秒后退出（0=在线）")
    ap.add_argument("--duration", type=int, default=30)
    args = ap.parse_args()

    cfg = {"link_file": "link.txt", "workspace": None, "task": "auto",
           "origin": zr.DEFAULT_ORIGIN, "bind": "127.0.0.1", "port": 8787,
           "token": "", "asr_url": "", "asr_model": "SenseVoiceSmall", "asr_key": "",
           "asr_tencent_id": "", "asr_tencent_key": "", "asr_tencent_region": "ap-shanghai",
           "asr_env_file": "", "tts_voice": "zh-CN-XiaoxiaoNeural", "ask_timeout": 180}
    if os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            cfg.update(json.load(f))
    else:
        gw_log("（无 %s，用默认配置）" % args.config)

    # 可选密钥文件（KEY=VALUE 行格式，gitignore/服务器本地保存，不进代码与主配置）
    env_path = cfg.get("asr_env_file") or ""
    if env_path and not os.path.isabs(env_path):
        env_path = os.path.join(HERE, env_path)   # 计划任务 CWD 不定，相对路径按脚本目录解析
    if env_path and os.path.exists(env_path):
        try:
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k == "TENCENT_CLOUD_SECRET_ID" and not cfg["asr_tencent_id"]:
                        cfg["asr_tencent_id"] = v
                    elif k == "TENCENT_CLOUD_SECRET_KEY" and not cfg["asr_tencent_key"]:
                        cfg["asr_tencent_key"] = v
            gw_log("已加载密钥文件: %s (tencent_id=%s)" % (env_path, "有" if cfg["asr_tencent_id"] else "无"))
        except OSError as e:
            gw_log("密钥文件读取失败: %s" % e)

    gw = Gateway(cfg)
    Handler.gw = gw
    worker = threading.Thread(target=gw.run_forever, daemon=True)
    worker.start()

    srv = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), Handler)
    srv.daemon_threads = True
    gw_log("HTTP API 监听 %s:%s" % (cfg["bind"], cfg["port"]))

    if args.once:
        end = time.time() + args.duration
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(args.duration)
        h = gw.health()
        gw_log("自检结果: %s" % json.dumps(h, ensure_ascii=False))
        srv.shutdown()
        gw.stop = True
        sys.exit(0 if h["ok"] else 2)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        gw.stop = True
        srv.shutdown()


if __name__ == "__main__":
    main()
