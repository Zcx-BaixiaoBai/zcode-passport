# ZCode Passport（ZCode 工牌）

把 [FoloToy AI Passport](https://github.com/folotoy/ai-passport)（ESP32-C3 智能工牌）改造成 **ZCode 语音遥控工牌**：在工牌上浏览你桌面端 ZCode 的工作区 / 会话，按住说话提问，ZCode 的回答会回显到屏幕并语音播报。

> 本项目基于 FoloToy/ai-passport 固件二次开发，遵循其 [LICENSE](LICENSE)。其中 ZCode 远控协议为逆向所得，仅供学习与个人自用，请自行遵守 ZCode 的服务条款。

## 它能做什么

- **三级界面**：工作区列表 → 会话列表 → 会话详情（深色卡片 UI，思源黑体点阵字库）。
- **PTT 语音问答**：短按 OK 录音 → 再按结束并发送 → 网关 ASR 转文字 → `sendPrompt` 唤醒目标 ZCode 会话 → 等待回复 → TTS 播报 + 屏显文本。
- **历史回看**：进入会话即拉取最近若干条输出（最新在前）。
- **零凭据固件 + 网页配网**：固件里不含任何 WiFi / 网关凭据，首次开机进引导，连工牌自己的热点在网页里填即可（写入 NVS）。
- **设置菜单**：主页长按 OK 进入 —— 重新配网 / 静音 / 测试网关 / 配网指导 / 关于。

## 架构

```
工牌(ESP32-C3) ──WiFi/HTTP──▶ 网关 gateway/ ──WSS──▶ ZCode 中继 ──▶ 桌面端 ZCode 会话
                              (Python：ASR + sendPrompt + TTS)
```

- **固件**（仓库根目录，ESP-IDF 工程）：`main/` 下 `gw_client.c`（网关 HTTP 客户端）、`ui_badge.c`（界面）、`audio_pipe.c`（录音子系统）、`wifi_sta.c`、`provision.c`（SoftAP 网页配网）、`badge_cfg.c`（NVS 配置）、`main.c`（PTT 状态机）；`components/bsp/`（ES8311 音频 / I2S）。
- **网关**（`gateway/`，Python）：`gateway.py`（HTTP API + ASR/TTS + 会话订阅）、`zcode_remote.py`（ZCode Web Remote Control v4 连接器）、`setup-gateway.py`（网页配置页）、`demo-gateway.py`（免 ZCode 体验）、`PROTOCOL.md`（协议笔记）。

## 快速开始

> 完整分步说明见 **[`gateway/QUICKSTART.md`](gateway/QUICKSTART.md)**，建议先读它。

### 1. 部署网关

网关**免费跑在你开着 ZCode 桌面端的那台电脑上**即可，无需服务器、无需云。

```bash
cd gateway
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # Windows；macOS/Linux 用 .venv/bin/pip
.venv\Scripts\python setup-gateway.py           # 打开网页配置页，四项填完点“保存配置”
.venv\Scripts\python gateway.py                 # 启动（或直接双击 start-gateway.bat / ./start-gateway.sh）
```

- **依赖**（`requirements.txt`）：`websocket-client`（连 ZCode 中继 —— **缺它网关能启动、端口也在监听，但永远配不上对，工作区恒为 0**）、`certifi`（CA 证书包）、`miniaudio`（ASR 24k→16k 重采样、TTS MP3→WAV 解码）、`edge-tts`（语音合成，免密钥）。`start-gateway.bat/.sh` 会自动安装并做依赖自检。
- **配置**：`setup-gateway.py` 起一个**只绑回环**的网页配置页（`http://127.0.0.1:8790`），填 ① ZCode 远控配对链接 ② 访问令牌（自动生成）③ 监听地址/端口 ④ 语音（腾讯云 ASR 密钥，或自建 FunASR，+ TTS 音色），保存后写 `link.txt` / `gateway-config.json` / `asr.env`。无图形界面的服务器用 `python setup-gateway.py --cli`。
- **验证**：日志出现 `★ 网关在线`、`/health` 返回 `status=online` 即接通。
- **只想先体验**（还没装 ZCode / 不想配对）：`python demo-gateway.py` —— 仅标准库即可运行，返回演示工作区与演示语音，零门槛走通“浏览 → 说话 → 听回复”。
- **Windows 注意**：防火墙入站规则是按**程序路径**匹配的，venv 里的 `python.exe` 与系统 python 是不同路径，没有规则时本机 curl 通、工牌从局域网却连不进来。`start-gateway.bat` 会自动添加规则 `ZCode Badge Gateway`（需管理员权限）。

### 2. 固件（不需要在 menuconfig 里填任何凭据）

固件已**去凭据化**：`CONFIG_ZCODE_WIFI_SSID / PASS / GW_URL / GW_TOKEN` 默认全为空，所有配置在设备上配网时写入 NVS。

- 取镜像：Releases 或 GitHub Actions 产物 `FoloToy-AI-Passport-full.bin`；也可自行 `idf.py set-target esp32c3 && idf.py build`。
- 刷写：整包刷到 **0x0**（含分区表）。**刷写会清空 NVS，刷完需重新配网。**

### 3. 配网（首次开机自动进引导）

1. 手机 / 电脑连工牌热点 **`ZCode-Badge-Setup`**（开放网络）；
2. 浏览器打开 **`http://192.168.4.1`**；
3. 填 WiFi（**仅 2.4G**）、网关地址 `http://<网关那台机器的局域网IP>:<端口>`、令牌（与配置页显示的一致）；
4. 保存后工牌自动重启，出现工作区列表即成功。

之后可随时 **主页长按 OK → 设置** 重新配网、静音或测试网关连通性。

## 网关与桌面的相处方式（重要）

ZCode 中继是**单终端槽位**，且 host 的“活动工作区”会被远控连接牵动。本网关因此采用 **follow-don't-lead**：

- 连接时只桥接到桌面**当前活动工作区**，连接 / 重连都不会把桌面 GUI 拽到别的项目；
- **从不主动切换工作区**；跨工作区会话靠“按任务自身 `workspacePath` 订阅 + `taskId` 路由 `sendPrompt`”在同一条连接上完成，不切桥。

因为是单槽位，**同一条配对链接不要同时给两个网关用**（会互踢）。给网关单独生成一条链接，别和手机端共用。

## 安全

- **固件不含任何凭据**：WiFi / 网关地址 / 令牌都在配网时写入设备 NVS；仓库内 `sdkconfig.defaults` 的 `CONFIG_ZCODE_*` 全为空字符串。
- `gateway-config.json`、`asr.env`、`link.txt`、`gateway.log` 含敏感值，**已列入 `gateway/.gitignore`，请勿提交**；仓库内只提供 `.example` 示例。
- 配置页只绑 `127.0.0.1` 且带一次性 nonce，密钥不会离开本机、也不会被其它页面提交。
- 网关出站仅允许 http/https，解析后拒绝私网 / 环回地址（SSRF 防护），http 上游钉 IP 直连防 DNS rebinding，全链路禁重定向，端口需 token 鉴权。

## 致谢

- 硬件与基础固件：[FoloToy / ai-passport](https://github.com/folotoy/ai-passport)
- 字库：思源黑体（Source Han Sans）
