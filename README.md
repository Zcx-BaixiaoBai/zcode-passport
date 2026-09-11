# ZCode Passport（ZCode 工牌）

把 [FoloToy AI Passport](https://github.com/folotoy/ai-passport)（ESP32-C3 智能工牌）改造成 **ZCode 语音遥控工牌**：在工牌上浏览你桌面端 ZCode 的工作区 / 会话，按住说话提问，ZCode 的回答会回显到屏幕并语音播报。

> 本项目基于 FoloToy/ai-passport 固件二次开发，遵循其 [LICENSE](LICENSE)。其中 ZCode 远控协议为逆向所得，仅供学习与个人自用，请自行遵守 ZCode 的服务条款。

## 它能做什么

- **三级界面**：工作区列表 → 会话列表 → 会话详情（深色卡片 UI，思源黑体点阵字库）。
- **PTT 语音问答**：短按 OK 录音 → 再按结束并发送 → 网关 ASR 转文字 → `sendPrompt` 唤醒目标 ZCode 会话 → 等待回复 → TTS 播报 + 屏显文本。
- **历史回看**：进入会话即拉取最近若干条输出（最新在前）。

## 架构

```
工牌(ESP32-C3) ──WiFi/HTTP──▶ 网关 gateway/ ──WSS──▶ ZCode 中继 ──▶ 桌面端 ZCode 会话
                              (Python：ASR + sendPrompt + TTS)
```

- **固件**（仓库根目录，ESP-IDF 工程）：`main/` 下 `gw_client.c`（网关 HTTP 客户端）、`ui_badge.c`（界面）、`audio_pipe.c`（录音子系统）、`wifi_sta.c`、`main.c`（PTT 状态机）；`components/bsp/`（ES8311 音频 / I2S）。
- **网关**（`gateway/`，Python）：`gateway.py`（HTTP API + ASR/TTS + 会话订阅）、`zcode_remote.py`（ZCode Web Remote Control v4 连接器）、`PROTOCOL.md`（协议笔记）。

## 快速开始

### 1. 部署网关

详见 [`gateway/BADGE-DEPLOY.md`](gateway/BADGE-DEPLOY.md)。要点：

- 复制 `gateway/gateway-config.example.json` → `gateway-config.json`，填 `token`、`link_file`（在 ZCode 桌面端生成的配对链接）。
- 复制 `gateway/asr.env.example` → `asr.env`，填腾讯云一句话识别密钥（TTS 用 edge-tts，免密钥）。
- 运行 `python gateway.py`，访问 `/health` 返回 `status=online` 即接通。

### 2. 构建固件

- 配置：`idf.py menuconfig` → “ZCode Badge 配置”，填 WiFi（仅 2.4G）、网关地址 `CONFIG_ZCODE_GW_URL`、令牌 `CONFIG_ZCODE_GW_TOKEN`（与网关 `token` 一致）。
- 构建：`idf.py set-target esp32c3 && idf.py build`；或推 tag 触发 GitHub Actions 产出整包镜像 `FoloToy-AI-Passport-full.bin`。

### 3. 刷写

把 `FoloToy-AI-Passport-full.bin` 刷到 **0x0**（整包含分区表），重启后屏幕出现「连接 WiFi…」即成功。

## 网关与桌面的相处方式（重要）

ZCode 中继是**单终端槽位**，且 host 的“活动工作区”会被远控连接牵动。本网关因此采用 **follow-don't-lead**：

- 连接时只桥接到桌面**当前活动工作区**，连接 / 重连都不会把桌面 GUI 拽到别的项目；
- **从不主动切换工作区**；跨工作区会话靠“按任务自身 `workspacePath` 订阅 + `taskId` 路由 `sendPrompt`”在同一条连接上完成，不切桥。

## 安全

- `sdkconfig.defaults`、`gateway-config.json`、`asr.env`、`link.txt` 的真实值含敏感凭据，**已列入 `.gitignore`，请勿提交**；仓库内均为占位符 / 示例。
- 网关出站仅允许 http/https，解析后拒绝私网 / 环回地址（SSRF 防护），http 上游钉 IP 直连防 DNS rebinding，全链路禁重定向，端口需 token 鉴权。

## 致谢

- 硬件与基础固件：[FoloToy / ai-passport](https://github.com/folotoy/ai-passport)
- 字库：思源黑体（Source Han Sans）
