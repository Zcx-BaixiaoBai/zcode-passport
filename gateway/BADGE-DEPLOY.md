# ZCode 工牌 — 部署与刷写指南

> 配套：`gateway/`（网关桥）+ 本仓库固件（基于 [FoloToy/ai-passport](https://github.com/folotoy/ai-passport)）。
> ⚠️ 下文所有 `<...>` / `$HOST` / `$TOKEN` 占位符请替换成你自己的值；真实凭据切勿提交到仓库。

## 架构

```
AI Passport 工牌(ESP32-C3) ──WiFi/HTTP──▶ 你的网关主机($HOST:8787) ──WSS──▶ ZCode 中继 ──▶ 桌面端 ZCode 会话
   三级界面 + PTT 语音                       gateway.py(常驻/计划任务)
                                            ASR(一句话识别) + TTS
```

按键语义：

| 层级 | 上 / 下 | OK 短按 | OK 长按 |
| --- | --- | --- | --- |
| L1 工作区列表（含 n/m 运行进度） | 切换工作区 | 进入 L2 | 刷新/重试 |
| L2 会话列表 | 切换会话 | 进入 L3 | 返回 L1 |
| L3 会话详情 | 滚动回复文本 | 开始录音 / 再按=结束并发送 | 返回 L2（录音中=取消） |

语音闭环：录音 PCM 边录边传（HTTP chunked，**注意 ESP-IDF `esp_http_client_write` 只发裸字节，需手动加 `<hex>\r\n<data>\r\n` 分块帧**）→ 网关 ASR → `sendPrompt` 唤醒会话 → 因果序号等新回复 → TTS 合成 → 工牌边下边播 + 屏显文本。录音上限 15 秒。

## 网关部署

- 把 `gateway/` 放到一台工牌能访问到的主机（公网或局域网），装 Python 3.10+。
- 复制 `gateway-config.example.json` → `gateway-config.json`，填：
  - `link_file`：指向你的 ZCode 配对链接文件（在 ZCode 桌面端生成，**勿提交**）
  - `token`：自定义网关访问令牌（要与工牌固件 `CONFIG_ZCODE_GW_TOKEN` 一致）
  - `origin`：ZCode 中继地址（默认 `https://zcode.z.ai`）
  - `bind` / `port`：监听地址 / 端口
- ASR/TTS 密钥放 `asr.env`（参考 `asr.env.example`，**勿提交**）。
- 放行防火墙对应端口；除 `/health` 外所有 API 都需请求头 `X-Gateway-Token`。

常用 API（`TOKEN`=你配置的 token，`HOST`=你的网关主机）：

```bash
curl -H "X-Gateway-Token: $TOKEN" http://$HOST:8787/ui/workspaces
curl -H "X-Gateway-Token: $TOKEN" "http://$HOST:8787/ui/sessions?workspace=<urlencode后的路径>"
curl -H "X-Gateway-Token: $TOKEN" "http://$HOST:8787/ui/history?task=sess_...&limit=8"
curl -X POST -H "X-Gateway-Token: $TOKEN" -H "Content-Type: application/json" \
     -d '{"text":"...","taskId":"sess_..."}' http://$HOST:8787/ask
curl -X POST -H "X-Gateway-Token: $TOKEN" --data-binary @rec.pcm \
     "http://$HOST:8787/ask?rate=24000&taskId=sess_..."
```

> **单终端槽位**：网关与官方手机/Web 远控共用同一条配对链接时会互踢（`KICKED`，30 秒自动重连）。
> 建议在 ZCode 桌面端为网关**单独生成一条配对链接**写入 `link.txt`。
>
> **follow-don't-lead**：网关只跟随桌面当前活动工作区、绝不主动切换工作区，避免把桌面 GUI 拽到别的项目目录。
> 跨工作区会话靠"按任务自身 workspacePath 订阅 + taskId 路由 sendPrompt"在同一条连接上完成，不切桥。

## 固件构建（GitHub Actions，本机无需 ESP-IDF）

1. 配置：改 `sdkconfig.defaults` 尾部 `CONFIG_ZCODE_WIFI_SSID/PASS/GW_URL/GW_TOKEN`，或 `idf.py menuconfig` → “ZCode Badge 配置”。WiFi 仅支持 2.4GHz。
2. 推送 main → “Firmware checks” 自动编译校验。
3. 打 tag（`git tag v0.1.0 && git push origin v0.1.0`）或手动 dispatch “Build firmware” → 产物 `FoloToy-AI-Passport-full.bin`（整包合并镜像）。
4. 本地构建（装了 ESP-IDF v5.5.x）：`idf.py set-target esp32c3 && idf.py build`，产物在 `build/`。

## 刷写到工牌

1. 下载 CI 产物 `FoloToy-AI-Passport-full.bin`。
2. 用刷写工具（FoloToy 官方网页刷机，或 esptool / badgeflash）把 full.bin 刷到 **0x0**（整包含分区表）。
3. 刷完自动重启，屏幕出现「连接 WiFi…」即成功。

## 首次使用检查

1. 停在「连接 WiFi…」超 30s → SSID/密码不对（2.4G only）。
2. 「网关不可达」→ 网关没跑或端口被挡：`curl http://$HOST:8787/health`。
3. 说话没反应 → 桌面端 ZCode 必须开着且配对有效（网关 `/health` 里 `status=online`）。
4. 回复慢：会话冷启动要 5~15 秒（唤醒 runtime），属正常。

## 安全边界

- 网关出站仅 http/https；上游 URL 解析后拒绝私网/环回（SSRF 防护）；http 上游钉 IP 直连防 DNS rebinding；全链路禁重定向。
- 网关端口有 token 鉴权；公网部署建议把端口源限制到可信 IP，或加 HTTPS（域名 + 反代）。
- `asr.env` / `link.txt` / `gateway-config.json` / `sdkconfig.defaults`（真实值）含敏感信息，已列入 `.gitignore`，只应存在于你的服务器与本地。
