#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zcode_remote.py — ZCode Web Remote Control (v4) 终端连接器 v4

在 v3（鉴权/配对/bootstrap/开桥/rpc-frame 隧道）之上实现了完整的
VSCode 风格 RPC 会话层（逆向自 web bundle 与桌面端 app.asar）：

  rpc-frame 二进制消息 = 编码(header数组) + 编码(body)
  RequestTypes : Promise=100  PromiseCancel=101  EventListen=102  EventDispose=103
  ResponseTypes: Initialize=200  PromiseSuccess=201  PromiseError=202
                 PromiseErrorObj=203  EventFire=204

  握手顺序（桥建立后）：
    1. 双方互发 Initialize ([200]+undefined)；桌面端先发来，必须回发，
       否则桌面端 ChannelClient 停在 Uninitialized，一切请求被排队忽略
    2. zcode-agent.helloConversationV4()            → hello{connectionId,clientMode,...}
    3. zcode-agent.initializeConversationV4(clientHello)
       clientHello={kind,protocolVersion:3,clientId,clientKind,appVersion,capabilities}
       （未先 hello 会抛 fault.connection.helloRequired）
    4. EventListen [102,id,'zcode-agent','onDynamicConversationFrame',{workspacePath,...}]
       （onDynamic* 事件：body 是过滤器参数本身，不是数组）
    5. zcode-agent.subscribeSessionsIndexV4({workspacePath,workspaceIdentity?,
       connectionId,clientMode,visibility})
       zcode-agent.subscribeConversationV4(同上 + sessionId=<taskId>)
       → {ack:{subscriptionId,mode,logEpoch}}；数据经 EventFire [204,id,data] 推回
    6. 发消息：zcode-task.sendPrompt({taskId,traceId,content,clientMode})

用法：
  python zcode_remote.py --link-file link.txt                       # 全流程 + 监听帧
  python zcode_remote.py --link-file link.txt --task sess_xxx       # 指定任务
  python zcode_remote.py --link-file link.txt --prompt "你好"       # 向选中任务发消息
  python zcode_remote.py --link-file link.txt --no-subscribe        # 只握手不订阅
  python zcode_remote.py --link-file link.txt --no-bridge           # 只列工作区/任务

协议逆向详见 PROTOCOL.md。安全：仅 http/https(ws/wss)，host 解析校验，拒绝环回/私有/保留地址。
"""
import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import socket
import sys
import time
import uuid
import zlib
from urllib.parse import urlparse, parse_qs

DEFAULT_ORIGIN = "https://zcode.z.ai"
WS_PATH_CANDIDATES = [
    "/ws/remote-control/terminal",
    "/ws/remote-control/mobile",
    "/ws/remote-control/relay",
    "/ws/remote-control/device",
    "/ws/remote-control",
    "/ws/relay",
    "/ws/v4",
    "/ws",
]
LOG_PATH = "zcode_session_log.jsonl"
HEARTBEAT_INTERVAL = 15.0
APP_TIMEOUT = 25.0

# RPC 类型枚举（逆向自 bundle Uu/Wu）
REQ_PROMISE, REQ_PROMISE_CANCEL, REQ_EVENT_LISTEN, REQ_EVENT_DISPOSE = 100, 101, 102, 103
RSP_INITIALIZE, RSP_SUCCESS, RSP_ERROR, RSP_ERROR_OBJ, RSP_EVENT_FIRE = 200, 201, 202, 203, 204

CH_AGENT = "zcode-agent"
CH_TASK = "zcode-task"


def log(msg):
    print(msg, flush=True)


def die(msg):
    print("[错误] %s" % msg, file=sys.stderr)
    sys.exit(1)


def xid(prefix):
    return "%s-%s" % (prefix, uuid.uuid4())


def now_ms():
    return int(time.time() * 1000)


# ---------------- 二进制编解码（tag+varint，逆向自 bundle Bu/Vu） ----------------
# tag: 0=Undefined 1=String 2=Buffer 3=VSBuffer 4=Array 5=Object(内嵌JSON) 6=Int

def _varint(n, out):
    if n == 0:
        out.append(0)
        return
    while n:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)


def benc(v):
    out = bytearray()
    _benc(v, out)
    return bytes(out)


def _benc(v, out):
    if v is None:
        out.append(0)
    elif isinstance(v, bool):
        b = json.dumps(v).encode("utf-8")          # JS boolean 走 Object 分支
        out.append(5); _varint(len(b), out); out += b
    elif isinstance(v, str):
        b = v.encode("utf-8")
        out.append(1); _varint(len(b), out); out += b
    elif isinstance(v, (bytes, bytearray)):
        out.append(2); _varint(len(v), out); out += bytes(v)
    elif isinstance(v, list):
        out.append(4); _varint(len(v), out)
        for it in v:
            _benc(it, out)
    elif isinstance(v, int):
        out.append(6); _varint(v, out)
    else:                                           # dict/float → JSON 内嵌
        b = json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        out.append(5); _varint(len(b), out); out += b


def bdec(buf):
    """返回 (value, trailing_bytes)。"""
    pos = 0

    def rv():
        nonlocal pos
        shift = val = 0
        while True:
            b = buf[pos]; pos += 1
            val |= (b & 0x7F) << shift
            if not (b & 0x80):
                return val
            shift += 7

    def rd():
        nonlocal pos
        tag = buf[pos]; pos += 1
        if tag == 0:
            return None
        if tag == 1:
            n = rv(); s = buf[pos:pos + n].decode("utf-8", "replace"); pos += n; return s
        if tag in (2, 3):
            n = rv(); b = bytes(buf[pos:pos + n]); pos += n; return b
        if tag == 4:
            c = rv(); return [rd() for _ in range(c)]
        if tag == 5:
            n = rv(); s = buf[pos:pos + n].decode("utf-8", "replace"); pos += n
            try:
                return json.loads(s)
            except Exception:
                return s
        if tag == 6:
            return rv()
        raise ValueError("unknown tag %d @%d" % (tag, pos - 1))

    val = rd()
    return val, bytes(buf[pos:])


# ---------------- 安全：出站 URL 校验 ----------------

def validate_origin(origin):
    u = urlparse(origin)
    if u.scheme not in ("http", "https"):
        die("仅允许 http/https origin（收到 %r）" % u.scheme)
    host = u.hostname
    if not host:
        die("origin 缺少 host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        die("DNS 解析失败：%s" % e)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_reserved
                or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
            die("拒绝连接私有/保留地址：%s" % ip)
    return host


# ---------------- 链接解析与鉴权 ----------------

def parse_link(url):
    q = parse_qs(urlparse(url.strip()).query)
    sid = (q.get("sid") or [None])[0]
    pass_hash = (q.get("hash") or [None])[0]
    if not sid or not pass_hash:
        die("链接缺少 sid 或 hash 参数")
    return {"sid": sid, "hash": pass_hash,
            "mid": (q.get("mid") or [""])[0],
            "version": (q.get("app_version") or ["web"])[0],
            "name": (q.get("name") or [""])[0]}


def b64url(raw):
    return base64.b64encode(raw).decode("ascii").replace("+", "-").replace("/", "_").rstrip("=")


def calc_proof(pass_hash, nonce, role, device_sid):
    key = pass_hash.encode("utf-8")
    msg = "%s|%s|%s" % (nonce, role, device_sid)
    return b64url(hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest())


# ---------------- 会话日志 ----------------

_logf = None


def log_event(direction, obj):
    global _logf
    if _logf is None:
        _logf = open(LOG_PATH, "a", encoding="utf-8")
    try:
        line = json.dumps({"ts": time.time(), "dir": direction, "msg": obj}, ensure_ascii=False, default=repr)
    except Exception:
        line = json.dumps({"ts": time.time(), "dir": direction, "msg": repr(obj)})
    _logf.write(line + "\n")
    _logf.flush()


class RpcError(Exception):
    pass


# ---------------- 连接器 ----------------

class ZCodeTerminal:
    def __init__(self, params, origin):
        import websocket
        self.websocket = websocket
        self.params = params
        self.origin = origin
        validate_origin(origin)
        self.ws = None
        self.state = "idle"
        self.bridge = None
        self.out_seq = 0
        self.last_hb = 0.0
        # RPC 层状态
        self.next_rpc_id = 1
        self.pending = {}            # req_id -> {"result":..,"error":..,"done":bool}
        self.event_handlers = {}     # event_id -> callable(data)
        self.init_sent = False
        self.init_received = False
        self.hello = None
        self.fragments = {}          # messageSeq -> {count, parts{idx:bytes}}
        self.subscriptions = {}      # subscriptionId -> topic

    # -- 传输 --

    def connect(self):
        ws_base = self.origin.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        mid = self.params["mid"]
        # Windows Server 的 Python 可能缺系统 CA 链 → 优先用 certifi 的 CA 包（仍完整校验证书）
        kw = {}
        try:
            import certifi
            kw["sslopt"] = {"ca_certs": certifi.where()}
        except ImportError:
            pass
        for path in WS_PATH_CANDIDATES:
            url = ws_base + path + ("?mid=" + mid if mid else "")
            log("→ 尝试 %s" % url)
            try:
                self.ws = self.websocket.create_connection(
                    url, timeout=10,
                    header=["Origin: %s" % self.origin,
                            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) zcode-bridge/0.4"],
                    **kw)
            except Exception as e:
                log("  连接失败：%s" % e)
                continue
            if self._authenticate():
                return True
            try:
                self.ws.close()
            except Exception:
                pass
        return False

    def _send(self, obj):
        self.ws.send(json.dumps(obj))
        log_event("send", obj)

    def send_payload(self, payload):
        self._send({"type": "data", "payload": payload, "client_ts": now_ms()})

    def _authenticate(self):
        p = self.params
        self._send({"type": "auth_init", "role": "terminal", "device_sid": p["sid"],
                    "meta": {"platform": "web", "version": p["version"], "name": "zcode-bridge"},
                    "client_ts": now_ms()})
        self.state = "authenticating"
        deadline = time.time() + APP_TIMEOUT
        self.ws.settimeout(5.0)
        while time.time() < deadline:
            msg = self._recv()
            if msg is None:
                continue
            t = msg.get("type")
            if t == "auth_challenge":
                proof = calc_proof(p["hash"], msg.get("nonce", ""), "terminal", p["sid"])
                self._send({"type": "auth_response", "device_sid": p["sid"],
                            "proof": proof, "client_ts": now_ms()})
                log("  ✓ 挑战应答已发送")
            elif t in ("auth_ack", "pair_status_ack"):
                ps = msg.get("pair_status")
                log("  pair_status=%s terminal_sid=%s" % (ps, msg.get("terminal_sid", "-")))
                if ps == "matched":
                    self.state = "paired"
                    log("  ★ 已配对")
                    return True
                if ps == "waiting":
                    self.state = "waiting"
                    log("  … 等待桌面端确认配对（请在 ZCode 桌面端接受）")
            elif t == "error":
                log("  ✗ relay error: %s %s" % (msg.get("code"), msg.get("message")))
                if msg.get("code") in ("AUTH_FAILED", "WRONG_PARAM"):
                    die("鉴权失败/参数错误——链接可能已过期，请在 ZCode 桌面端重新生成配对链接")
                return False
        return False

    def _recv(self):
        try:
            raw = self.ws.recv()
        except self.websocket.WebSocketTimeoutException:
            self._maybe_heartbeat()
            return None
        except self.websocket.WebSocketConnectionClosedException:
            raise ConnectionError("连接被服务端关闭")
        if not raw:
            return None
        try:
            msg = json.loads(raw)
        except Exception:
            log("  [非JSON] %s" % str(raw)[:160])
            return None
        log_event("recv", msg)
        return msg

    def _maybe_heartbeat(self):
        if self.state in ("waiting", "paired") and time.time() - self.last_hb > HEARTBEAT_INTERVAL:
            self._send({"type": "pair_status_query", "client_ts": now_ms()})
            self.last_hb = time.time()

    # -- 应用层（bridge 前） --

    def request_payload(self, payload, match, timeout=APP_TIMEOUT):
        self.send_payload(payload)
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.5, deadline - time.time()))
            msg = self._recv()
            if msg is None:
                continue
            t = msg.get("type")
            if t == "data":
                p = msg.get("payload") or {}
                if match(p):
                    return p
                self._on_payload(p)
            elif t == "error":
                die("relay error: %s %s" % (msg.get("code"), msg.get("message")))
        raise TimeoutError("等待响应超时: %s" % payload.get("zcode_type"))

    def bootstrap(self):
        rid = xid("bootstrap")
        resp = self.request_payload(
            {"zcode_type": "bootstrap-request", "requestId": rid},
            lambda p: p.get("zcode_type") == "bootstrap-response" and p.get("requestId") == rid)
        workspaces = (resp.get("result") or {}).get("workspaces") or []
        log("★ bootstrap 成功：%d 个工作区" % len(workspaces))
        for i, w in enumerate(workspaces):
            log("  [%d] %s %s" % (i, w.get("label"), w.get("workspacePath")))
        return workspaces

    def list_workspace_tasks(self):
        rid = xid("wslist")
        try:
            resp = self.request_payload(
                {"zcode_type": "workspace-list-request", "requestId": rid},
                lambda p: p.get("requestId") == rid and p.get("zcode_type") != "workspace-list-request",
                timeout=15.0)
            result = resp.get("result") or {}
            tasks = result.get("tasks") or []
            log("★ 任务列表：%d 个（active=%s）" % (len(tasks), result.get("activeTaskId")))
            for t in tasks[:10]:
                log("  [%s] %s | %s | %s" % (t.get("displayStatus"), (t.get("title") or "")[:36],
                                             t.get("workspaceLabel"), t.get("taskId")))
            return result
        except TimeoutError:
            log("（workspace-list-request 无响应，跳过）")
            return {}

    def open_bridge(self, workspace_key, task_id=None):
        bsid = xid("bridge")
        req = {"zcode_type": "workspace-bridge-open", "requestId": xid("workspace-bridge"),
               "bridgeSessionId": bsid, "bridgeGeneration": 1, "workspaceKey": workspace_key}
        if task_id:
            req["taskId"] = task_id
        ready = self.request_payload(
            req, lambda p: p.get("zcode_type") == "workspace-bridge-ready" and p.get("bridgeSessionId") == bsid)
        b = ready.get("bridge") or {}
        self.bridge = {"bridgeSessionId": b.get("bridgeSessionId", bsid),
                       "bridgeGeneration": b.get("bridgeGeneration", 1)}
        rid = b.get("recoveryId")
        if rid:
            self.bridge["recoveryId"] = rid  # 官方模式：空 recoveryId 省略字段
        log("★ 桥已建立: %s" % json.dumps(self.bridge, ensure_ascii=False)[:200])
        return self.bridge

    # -- rpc-frame 隧道（物理层） --

    def send_rpc_buf(self, data):
        if not self.bridge:
            die("桥未建立")
        self.out_seq += 1
        frame = {"zcode_type": "rpc-frame",
                 "seq": self.out_seq, "messageSeq": self.out_seq,
                 "messageBytes": len(data),
                 "fragmentIndex": 0, "fragmentCount": 1,
                 "checksum": {"algorithm": "crc32", "value": "%08x" % (zlib.crc32(data) & 0xFFFFFFFF)},
                 "dataBase64": base64.b64encode(data).decode("ascii")}
        frame.update(self.bridge)
        self.send_payload(frame)

    def _ack_frame(self, p):
        seq = p.get("messageSeq", p.get("seq"))
        if seq is not None and self.bridge:
            ack = {"zcode_type": "rpc-frame-ack", "ackMessageSeq": seq}
            ack.update(self.bridge)
            self.send_payload(ack)

    def _on_payload(self, p):
        zt = p.get("zcode_type")
        if zt == "rpc-frame":
            raw = base64.b64decode(p.get("dataBase64", ""))
            # 分片重组（按 messageSeq）
            count = p.get("fragmentCount") or 1
            idx = p.get("fragmentIndex") or 0
            if count > 1:
                mseq = p.get("messageSeq")
                slot = self.fragments.setdefault(mseq, {"count": count, "parts": {}})
                slot["parts"][idx] = raw
                self._ack_frame(p)
                if len(slot["parts"]) < count:
                    return
                del self.fragments[mseq]
                raw = b"".join(slot["parts"][i] for i in range(count))
            else:
                self._ack_frame(p)
            try:
                self._dispatch_rpc(raw)
            except Exception as e:
                log("  [rpc→ 解码失败(%s)] %s" % (e, raw[:64].hex()))
        elif zt == "rpc-frame-ack":
            pass
        else:
            log("  [payload:%s] %s" % (zt, json.dumps(p, ensure_ascii=False)[:300]))

    # -- RPC 分发（消息层）：header数组 + body 两段编码 --

    def _dispatch_rpc(self, buf):
        header, rest = bdec(buf)
        body = None
        if rest:
            body, _ = bdec(rest)
        if not isinstance(header, list) or not header:
            log("  [rpc→ 非预期header] %r" % (header,))
            return
        t = header[0]
        if t == RSP_INITIALIZE:
            self.init_received = True
            log("  [rpc→] Initialize(200) 收到，回发我方 Initialize")
            self._send_initialize()
        elif t == RSP_SUCCESS:
            rid = header[1] if len(header) > 1 else None
            slot = self.pending.get(rid)
            if slot is not None:
                slot["result"], slot["done"] = body, True
        elif t in (RSP_ERROR, RSP_ERROR_OBJ):
            rid = header[1] if len(header) > 1 else None
            slot = self.pending.get(rid)
            if slot is not None:
                slot["error"], slot["done"] = body, True
        elif t == RSP_EVENT_FIRE:
            eid = header[1] if len(header) > 1 else None
            h = self.event_handlers.get(eid)
            if h is not None:
                h(body)
            else:
                log("  [rpc→ EventFire id=%s 无处理器] %s" % (eid, json.dumps(body, ensure_ascii=False, default=repr)[:200]))
        else:
            log("  [rpc→ 未知类型] header=%r body=%s" % (header, json.dumps(body, ensure_ascii=False, default=repr)[:200]))

    def _send_initialize(self):
        if self.init_sent:
            return
        self.init_sent = True
        self.send_rpc_buf(benc([RSP_INITIALIZE]) + benc(None))

    # -- RPC 客户端 --

    def _pump(self, deadline, cond=None):
        """处理消息直到 cond() 为真或超时。"""
        while time.time() < deadline:
            if cond is not None and cond():
                return True
            self.ws.settimeout(max(0.2, min(5.0, deadline - time.time())))
            msg = self._recv()
            if msg is None:
                continue
            if msg.get("type") == "data":
                self._on_payload(msg.get("payload") or {})
            elif msg.get("type") == "error":
                log("  ✗ relay error: %s %s" % (msg.get("code"), msg.get("message")))
        return cond() if cond is not None else False

    def rpc_call(self, channel, method, args=None, timeout=APP_TIMEOUT):
        rid = self.next_rpc_id
        self.next_rpc_id += 1
        slot = {"result": None, "error": None, "done": False}
        self.pending[rid] = slot
        buf = benc([REQ_PROMISE, rid, channel, method]) + benc(args if args is not None else [])
        self.send_rpc_buf(buf)
        log("  [rpc←] %s.%s (#%d)" % (channel, method, rid))
        deadline = time.time() + timeout
        ok = self._pump(deadline, cond=lambda: slot["done"])
        self.pending.pop(rid, None)
        if not ok:
            raise TimeoutError("RPC 超时: %s.%s" % (channel, method))
        if slot["error"] is not None:
            raise RpcError("%s.%s → %s" % (channel, method,
                           json.dumps(slot["error"], ensure_ascii=False, default=repr)[:400]))
        return slot["result"]

    def rpc_listen(self, channel, event_name, arg, handler):
        eid = self.next_rpc_id
        self.next_rpc_id += 1
        self.event_handlers[eid] = handler
        # EventListen 的 body 是过滤器参数本身（不是数组）
        buf = benc([REQ_EVENT_LISTEN, eid, channel, event_name]) + benc(arg)
        self.send_rpc_buf(buf)
        log("  [rpc←] EventListen %s.%s (#%d)" % (channel, event_name, eid))
        return eid

    # -- 会话层握手 --

    def conversation_connect(self, w, task_id=None, subscribe=True):
        """桥建立后：Initialize → hello → clientHello → 事件监听 → 订阅。"""
        # 1) 等桌面端 Initialize（此前观察：桥就绪后立刻到达），并回发我方
        deadline = time.time() + 8.0
        self._pump(deadline, cond=lambda: self.init_received)
        self._send_initialize()
        if not self.init_received:
            log("  （8s 内未收到桌面 Initialize，仍继续）")

        ws_filter = {"workspacePath": w.get("workspacePath")}
        ident = (w.get("workspaceIdentity") or "").strip()
        if ident:
            ws_filter["workspaceIdentity"] = ident

        # 2) hello
        hello = self.rpc_call(CH_AGENT, "helloConversationV4", [])
        if not isinstance(hello, dict):
            raise RpcError("hello 响应异常: %r" % (hello,))
        self.hello = hello
        log("★ hello: connectionId=%s clientMode=%s deliveryProfile=%s" %
            (hello.get("connectionId"), hello.get("clientMode"), hello.get("deliveryProfile")))

        # 3) clientHello
        client_hello = {
            "kind": "clientHello",
            "protocolVersion": 3,
            "clientId": str(uuid.uuid4()),
            "clientKind": "desktop" if hello.get("clientMode") == "desktop-continuous" else "web",
            "appVersion": "unknown",
            "capabilities": {"workspaceHookReviewUi": True},
        }
        self.rpc_call(CH_AGENT, "initializeConversationV4", [client_hello])
        log("★ clientHello 已被接受（clientId=%s）" % client_hello["clientId"])

        if not subscribe:
            return hello

        # 4) 事件监听（数据回流通道）
        self.rpc_listen(CH_AGENT, "onDynamicConversationFrame", ws_filter, self._on_conversation_frame)
        self.rpc_listen(CH_AGENT, "onDynamicSessionsIndexFrame", ws_filter, self._on_sessions_index_frame)

        # 5) 订阅
        sub = dict(ws_filter)
        sub.update({"connectionId": hello.get("connectionId"),
                    "clientMode": hello.get("clientMode"),
                    "visibility": "foreground"})
        r1 = self.rpc_call(CH_AGENT, "subscribeSessionsIndexV4", [sub])
        self._note_sub(r1, "sessions-index")
        if task_id:
            sub2 = dict(sub, sessionId=task_id)
            r2 = self.rpc_call(CH_AGENT, "subscribeConversationV4", [sub2])
            self._note_sub(r2, "conversation/%s" % task_id)
        return hello

    def _note_sub(self, resp, topic):
        ack = (resp or {}).get("ack") if isinstance(resp, dict) else None
        if isinstance(ack, dict):
            sid = ack.get("subscriptionId")
            self.subscriptions[sid] = topic
            log("★ 订阅成功 [%s] subscriptionId=%s mode=%s logEpoch=%s" %
                (topic, sid, ack.get("mode"), ack.get("logEpoch")))
        else:
            log("★ 订阅响应 [%s]: %s" % (topic, json.dumps(resp, ensure_ascii=False, default=repr)[:300]))

    # -- 帧事件处理 --

    def _on_conversation_frame(self, data):
        log_event("frame.conversation", data)
        self._print_frames("会话帧", data)

    def _on_sessions_index_frame(self, data):
        log_event("frame.sessions-index", data)
        self._print_frames("索引帧", data)

    def _print_frames(self, label, data):
        if not isinstance(data, dict):
            log("  [%s] %s" % (label, json.dumps(data, ensure_ascii=False, default=repr)[:400]))
            return
        sid = data.get("subscriptionId")
        topic = self.subscriptions.get(sid, data.get("topic", "?"))
        frames = data.get("frames")
        if isinstance(frames, list):
            kinds = []
            for f in frames:
                if isinstance(f, dict):
                    ft = f.get("type") or (f.get("payload") or {}).get("type") if isinstance(f.get("payload"), dict) else f.get("type")
                    kinds.append(str(ft))
            log("  [%s:%s] %d 帧 %s" % (label, topic, len(frames),
                json.dumps(kinds[:12], ensure_ascii=False) if kinds else ""))
            for f in frames[:3]:
                s = json.dumps(f, ensure_ascii=False, default=repr)
                log("      %s" % (s[:420] + ("…" if len(s) > 420 else "")))
        else:
            s = json.dumps(data, ensure_ascii=False, default=repr)
            log("  [%s:%s] %s" % (label, topic, s[:420] + ("…" if len(s) > 420 else "")))

    # -- 发送消息 --

    def send_prompt(self, task_id, content):
        req = {"taskId": task_id,
               "traceId": uuid.uuid4().hex,
               "content": content,
               "clientMode": (self.hello or {}).get("clientMode") or "desktop-continuous"}
        r = self.rpc_call(CH_TASK, "sendPrompt", [req], timeout=30.0)
        log("★ sendPrompt 响应: %s" % json.dumps(r, ensure_ascii=False, default=repr)[:400])
        return r

    # -- 监听循环 --

    def stream(self, duration):
        log("★ 监听 %ds（Ctrl+C 提前退出）…" % duration)
        end = time.time() + duration
        while time.time() < end:
            self.ws.settimeout(5.0)
            msg = self._recv()
            if msg is None:
                continue
            t = msg.get("type")
            if t == "data":
                self._on_payload(msg.get("payload") or {})
            elif t == "error":
                code = msg.get("code")
                if code == "KICKED":
                    log("  ✗ KICKED：同一配对链接被另一个终端（官方手机/Web 端）接管。")
                    log("    中继是单终端槽位：一个 sid 同时只允许一个终端连接。")
                    log("    → 关闭官方端后重跑即可；硬件工牌需为其生成独立配对链接。")
                else:
                    log("  ✗ relay error: %s %s" % (code, msg.get("message")))
                break

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# ---------------- 选择逻辑 ----------------

def pick_workspace(workspaces, hint):
    if not workspaces:
        return None
    if hint:
        for w in workspaces:
            blob = json.dumps(w, ensure_ascii=False).lower()
            if hint.lower() in blob:
                return w
        log("（--workspace %r 未匹配，回退第一个）" % hint)
    return workspaces[0]


def workspace_key_of(w):
    """官方规则：workspaceIdentity?.trim() || workspacePath"""
    if not isinstance(w, dict):
        return None
    ident = w.get("workspaceIdentity")
    if isinstance(ident, str) and ident.strip():
        return ident.strip()
    path = w.get("workspacePath")
    if isinstance(path, str) and path.strip():
        return path
    return None


def pick_task(wslist, workspace_key, hint):
    tasks = (wslist or {}).get("tasks") or []
    if hint:
        for t in tasks:
            if hint in (t.get("taskId") or "") or hint.lower() in (t.get("title") or "").lower():
                return t
    for t in tasks:
        if t.get("workspacePath") == workspace_key and t.get("displayStatus") == "running":
            return t
    for t in tasks:
        if t.get("workspacePath") == workspace_key:
            return t
    return None


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="ZCode Web Remote Control 终端连接器 v4")
    ap.add_argument("link", nargs="?", help="配对链接 URL")
    ap.add_argument("--link-file", help="从文件读取配对链接")
    ap.add_argument("--origin", default=DEFAULT_ORIGIN)
    ap.add_argument("--workspace", help="工作区匹配子串（默认第一个）")
    ap.add_argument("--task", help="任务匹配子串（taskId 或标题；默认该工作区 running 任务）")
    ap.add_argument("--prompt", help="向选中任务发送一条消息")
    ap.add_argument("--no-bridge", action="store_true", help="只 bootstrap/列表，不开桥")
    ap.add_argument("--no-subscribe", action="store_true", help="握手但不订阅")
    ap.add_argument("--duration", type=int, default=90, help="监听秒数（默认 90）")
    args = ap.parse_args()

    link = args.link
    if args.link_file:
        with open(args.link_file, encoding="utf-8") as f:
            link = f.read().strip()
    if not link:
        die("请提供配对链接（位置参数或 --link-file）")

    params = parse_link(link)
    log("sid=%s… mid=%s… version=%s" % (params["sid"][:12], (params["mid"] or "-")[:12], params["version"]))
    log("会话日志 → %s" % LOG_PATH)

    term = ZCodeTerminal(params, args.origin)
    try:
        if not term.connect():
            die("所有候选中继路径均失败")
        workspaces = term.bootstrap()
        wslist = term.list_workspace_tasks()
        if args.no_bridge:
            return
        w = pick_workspace(workspaces, args.workspace)
        if not w:
            die("没有可用工作区")
        key = workspace_key_of(w)
        if not key:
            die("无法提取 workspaceKey：%s" % json.dumps(w, ensure_ascii=False)[:200])
        task = pick_task(wslist, key, args.task)
        task_id = (task or {}).get("taskId")
        if task:
            log("→ 桥接任务: %s (%s)" % (task.get("title"), task_id))
        term.open_bridge(key, task_id)
        term.conversation_connect(w, task_id, subscribe=not args.no_subscribe)
        if args.prompt:
            if not task_id:
                die("--prompt 需要选中任务（--task 指定或该工作区有任务）")
            term.send_prompt(task_id, args.prompt)
        term.stream(args.duration)
    except (KeyboardInterrupt, ConnectionError) as e:
        log("\n退出：%s" % e)
    except (RpcError, TimeoutError) as e:
        log("\n[失败] %s" % e)
    finally:
        term.close()


if __name__ == "__main__":
    main()
