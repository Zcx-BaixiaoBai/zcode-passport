# ZCode Web Remote Control v4 — 协议逆向文档（完整）

> 来源：`https://zcode.z.ai/remote/v4` 前端 bundle（4.8MB）+ 桌面端 `app.asar`（307MB）静态分析
> + 真机全流程实测（2026-09-08，含 sendPrompt 闭环）。连接器实现：`zcode_remote.py`（v4）。
> 状态：**全部打通** — 鉴权→配对→bootstrap→开桥→RPC 握手→订阅→会话帧实时流入→发消息唤醒会话→回复流式收回。

## 1. 配对链接

```
https://zcode.z.ai/remote/v4?sid=<deviceSid>&hash=<passHash>&t=<ts>&mid=<deviceMid>&name=<deviceName>&app_version=<ver>
```

| 参数 | 含义 |
| --- | --- |
| sid | deviceSid（`d_` 前缀），终端设备身份 |
| hash | passHash，HMAC 密钥（URL 编码的 base64） |
| mid | deviceMid（UUID），连接时追加为 `?mid=` 查询参数 |
| t | 时间戳（疑似签发时间） |
| name / app_version | 设备名 / 客户端版本（进 auth_init.meta） |

**单终端槽位**：一个 sid 同时只允许一个终端连接；官方手机/Web 端上线会 `KICKED` 掉先到的连接器（实测证实）。硬件工牌必须使用独立配对链接。

## 2. 传输层（relay WS）

- **WS 端点**：`wss://zcode.z.ai/ws/remote-control/terminal?mid=<deviceMid>`（服务端对任意路径回 101，路由在消息层）
- **鉴权**（challenge-response）：
  1. C→S `{"type":"auth_init","role":"terminal","device_sid":sid,"meta":{"platform":"web","version":ver,"name":"..."},"client_ts":ms}`
  2. S→C `{"type":"auth_challenge","nonce":"wp_...","server_ts":s}`
  3. C→S `{"type":"auth_response","device_sid":sid,"proof":P,"client_ts":ms}`
     **P = base64url(HMAC-SHA256(key=utf8(passHash), msg=`"<nonce>|terminal|<sid>"`))**
  4. S→C `{"type":"auth_ack","terminal_sid":"t_...","pair_status":"matched|waiting"}`
- **心跳**：周期 `{"type":"pair_status_query"}` → `pair_status_ack`（15s）
- **错误码**：`KICKED`（槽位被接管）/ `DEVICE_OFFLINE` / `AUTH_FAILED` / `WRONG_PARAM` / `INTERNAL`
- **信封**：出站 `{"type":"data","payload":{...},"client_ts":ms}`；入站同构（多 server_ts）

## 3. 应用层消息（zcode_type 词汇表）

`bootstrap-request/response` · `workspace-list-request` · `workspace-list-updated`（服务端推送） · `workspace-bridge-open` → `workspace-bridge-ready` · `rpc-frame` / `rpc-frame-ack` · `bridge-degraded` · `workspace-bridge-error` · `app-error`

- `bootstrap-request {requestId}` → `{result:{workspaces:[{kind,label,workspacePath,workspaceIdentity?,workspacePurpose}], mobileViewState}}`
- `workspace-list-request {requestId}` → `{result:{activeTaskId, activeWorkspaceKey, tasks:[{taskId,title,displayStatus,provider,workspacePath,createdAt,updatedAt}]}}`
- **workspaceKey 规则**：`workspaceIdentity?.trim() || workspacePath`
- 开桥：`{"zcode_type":"workspace-bridge-open","requestId":"workspace-bridge-<uuid>","bridgeSessionId":"bridge-<uuid>","bridgeGeneration":1,"workspaceKey":"<key>","taskId":"<可选>"}` → `workspace-bridge-ready {bridge:{bridgeSessionId,bridgeGeneration,recoveryId?,initialTaskId,kind}}`
  - **recoveryId 为空时必须省略字段**（带 `recoveryId:""` 会被桌面端判 invalid payload 丢弃并降级桥）

## 4. rpc-frame 隧道（物理层）

信封：`{zcode_type:"rpc-frame", bridgeSessionId, bridgeGeneration, recoveryId?, seq, messageSeq, messageBytes, fragmentIndex, fragmentCount, checksum:{algorithm:"crc32",value:"%08x"}, dataBase64}`

- 每帧必须回 `{"zcode_type":"rpc-frame-ack", ...bridgeIdentity, "ackMessageSeq":<messageSeq>}`；超时未 ack → `bridge-degraded(remote.rpcFrame.replayGraceExceeded)` + 丢弃缓冲
- 大消息按 `fragmentIndex/fragmentCount` 分片，同 `messageSeq`，按序拼接
- **dataBase64 = 自定义二进制序列化**（tag+varint，同 VSCode IPC BufferWriter）：
  - tag：`0=Undefined 1=String 2=Buffer 3=VSBuffer 4=Array 5=Object(JSON字节) 6=Int`
  - String/Buffer：tag + varint(字节长) + 原始字节；Int：tag + varint；Array：tag + varint(个数) + 元素；Object：tag + varint(len) + JSON.stringify 字节
  - 实测校验：`04 01 06 c8 01 00` = `[Int 200]` + Undefined（Initialize 消息），crc32 与信封 checksum 一致

## 5. RPC 消息层（VSCode 风格，逆向自 bundle `Uu/Wu/Gu` 枚举 + `onBuffer`）

每条 rpc-frame 消息 = **编码(header 数组) + 编码(body)** 两段拼接。

| 类型 | 值 | header | body |
| --- | --- | --- | --- |
| Promise 请求 | 100 | `[100, reqId, channelName, methodName]` | 参数数组 `[arg0,...]` |
| PromiseCancel | 101 | `[101, reqId]` | undefined |
| EventListen | 102 | `[102, evId, channelName, eventName]` | **过滤器参数本身**（不是数组） |
| EventDispose | 103 | `[103, evId]` | undefined |
| Initialize | 200 | `[200]` | undefined |
| PromiseSuccess | 201 | `[201, reqId]` | 返回值 |
| PromiseError | 202 | `[202, reqId]` | 错误字符串 |
| PromiseErrorObj | 203 | `[203, reqId]` | 错误对象 |
| EventFire | 204 | `[204, evId]` | 事件数据 |

**Initialize 握手是硬前提**：桥就绪后桌面端立即发 `[200]`；终端必须回发 `[200]`，否则桌面端 ChannelClient 停在 Uninitialized，其所有出站请求排队、对终端发来的非法形状消息静默丢弃（v3 卡住数小时的根因）。

### 频道名（asar `h0` 枚举，kebab-case）

`file` `media-preview` `system` `terminal` `git` `git-checkpoint` `setting` `credential` `cua-permission` `broadcast` **`zcode-task`** `window-controller` **`zcode-agent`** `zcode-session` `file-watcher` `oauth` `model-provider` `usage-stats` `coding-plan-subscription` `client-scenes` `skills` `skill-sync` `mcp-sync` `plugin-sync` `plugins` `plugin-management` `subagents` `commands` `hooks` `memory` `output-style` `settings-sync` `bots` `feedback` `repo-wiki` `prompt-attachment-transfer` `off-peak-task`

### 会话 V4 握手与订阅（全部在 `zcode-agent` 频道，实测跑通）

```jsonc
// 1. hello（无参）
[100,1,"zcode-agent","helloConversationV4"] + []
// → [201,1] + {kind:"hello", protocolVersion:3, connectionId:"host-rpc-<uuid>",
//    clientMode:"web-remote-replayable"|"desktop-continuous", deliveryProfile:"replayable"|"continuous",
//    serverTime:ms, capabilities:{nativeDialogs,localTerminal,binaryFrames:false,compression:"none",workspaceHookReview}, auth:{}}

// 2. clientHello（未先 hello → fault.connection.helloRequired）
[100,2,"zcode-agent","initializeConversationV4"] + [{
  kind:"clientHello", protocolVersion:3, clientId:"<uuid4>",
  clientKind: hello.clientMode==="desktop-continuous" ? "desktop" : "web",
  appVersion:"unknown", capabilities:{workspaceHookReviewUi:true}}]

// 3. 事件监听（onDynamic* 前缀 = 事件；body 是过滤器对象本身）
[102,3,"zcode-agent","onDynamicConversationFrame"] + {workspacePath, workspaceIdentity?}
[102,4,"zcode-agent","onDynamicSessionsIndexFrame"] + {workspacePath, workspaceIdentity?}

// 4. 订阅（connectionId/clientMode 必须来自 hello）
[100,5,"zcode-agent","subscribeSessionsIndexV4"] + [{workspacePath, workspaceIdentity?,
  connectionId, clientMode, visibility:"foreground"}]
[100,6,"zcode-agent","subscribeConversationV4"] + [{同上, sessionId:"<taskId>"}]
// → [201,n] + {ack:{subscriptionId:"sub-…"|"six-…", mode:"snapshot"|"resume", logEpoch}}
```

### 帧回流（EventFire [204, evId, data]，实测）

```jsonc
{wireVersion:3, kind:"complete", deliveryKind:"initial"|"online",
 logicalFrameId, logicalFrameOrdinal, topic, subscriptionId,
 frame:{topic, subscriptionId, sentAt, fromSeq, toSeq, payload:{...}}}
```

- **initial 帧** = 全量快照（本会话实测 259KB）：`payload.snapshot = {protocolVersion:1, sessionId, logEpoch, seq, revision, control:{phase,sessionEnded,canStop,activeWorks,…}, availability, inputRouting, meta:{title}, config:{provider,model,thought,mode}, usage:{contextWindow:{usedTokens,maxTokens,cache,breakdown},cumulative}, queue, pendingInteractions, subagents, plan, …}`
- **online 帧** ≈1/s：`payload.kind="deltas"`，op 流（如 `{op:"state.updated"}`、assistantText 增量 `{kind:"assistantText", text, state:"complete"}`）
- sessions-index 快照：`{workspaceId, logEpoch, sessions:[{sessionId,title,phase,lastActivityAt,lastAssistantPreview,lastTerminalQuery,createdAt}]}`
- topic 格式：`conversation/<sessionId>`、`sessions-index/<workspaceKey>`、`workspace-config`（17 字符校验）
- 取消订阅：`unsubscribeConversationV4({topic, subscriptionId, connectionId})`；重同步：`resyncConversationV4({topic, connectionId})`
- 流控：`setConnectionFlowStateV4({connectionId, state:"saturated"|"drained"|"closed"})`（仅 trusted-host-relay 模式允许）

### 发消息（`zcode-task` 频道，实测闭环）

```jsonc
[100,7,"zcode-task","sendPrompt"] + [{taskId:"sess_…", traceId:"<uuid4hex>",
  content:"<文本>", clientMode:"<hello.clientMode>"}]
// → [201,7] + undefined（null 即成功）
// 效果：completed 会话被唤醒 → workspace-list-updated 推 displayStatus:"running"
//       → agent 跑完 → assistantText 经 onDynamicConversationFrame deltas 流回
// 可选字段（asar facade）：attachments[], toolDenylist[], botDeliveryTarget, queryId, messageId, automationId
```

`zcode-agent.sendPrompt` 为底层版本：`{workspacePath, workspaceIdentity?, sessionId, content, inputId?, queryId?, ...}`。

## 6. 实测记录（2026-09-08）

1. **订阅本会话**（sess_4513b1b3，running）：initial 259KB 快照 + 每秒 deltas，35s 零丢失；自己发言的 `lastAssistantPreview` 实时出现在索引帧里。
2. **sendPrompt 闭环**：向 completed 会话 sess_50dd8e40 发"只回复 OK"→ 会话唤醒（displayStatus→running）→ 数秒后 deltas 帧收到 `{kind:"assistantText", text:"OK", state:"complete"}`。
3. **KICKED**：官方客户端（配对原始设备）上线接管槽位 → 本连接器被踢。单 sid 单终端。
4. 桥拆除噪音：脚本退出后桌面端 replayGraceExceeded 降级属正常收尾，非协议错误。

## 7. 排错工具

- **桌面端日志 = 协议 oracle**：`~/.zcode/v2/logs/<date>.log`，`[web-remote-control]` 段会给出每帧被拒原因（`invalid external relay payload dropped`）、桥降级原因（reasonCode）。
- 连接器全程 JSONL：`zcode_session_log.jsonl`（send/recv/frame.conversation/frame.sessions-index）。
- 桌面端源码：`C:\Program Files\ZCode\resources\app.asar`（307MB，直接 `re.finditer` 二进制搜索即可，无需解包）。

## 8. 下一步（工牌集成方向）

1. **独立配对**：为工牌网关生成专属 sid 的配对链接，避免与手机端互踢。
2. **守护进程化**：KICKED/DEVICE_OFFLINE/断线自动重连（backoff），桥降级自动重开。
3. **语音入口**：badge PTT → 网关（ASR，复用 FunASR）→ `zcode-task.sendPrompt`；回复经 deltas 帧 → TTS → badge 扬声器。
4. 增量帧节流：deltas 1/s 对 ESP32 显示/播报过密，网关侧做聚合。
