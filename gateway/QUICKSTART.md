# ZCode 工牌网关 · 快速开始

工牌不直连 ZCode，而是连一个“网关”（`gateway.py`）。网关可以**免费跑在你自己开 ZCode 桌面端的那台电脑上**，无需服务器、无需云。

## 0. 下载 bridge（网关）

- Git：`git clone https://github.com/Zcx-BaixiaoBai/zcode-passport.git`，然后进入 `gateway/` 目录；
- 或浏览器打开仓库页 → Code → Download ZIP，解压后进入 `gateway/`。

`gateway/` 内含全部网关文件：`gateway.py`（真实网关）、`zcode_remote.py`（远控协议）、`setup-gateway.py`（**网页配置**）、`demo-gateway.py`（免 ZCode 体验）、`start-gateway.bat/.sh`（一键启动）、`requirements.txt`、本指南。

## 你需要什么

- 一台电脑：装着 ZCode 桌面端并登录（它是被遥控的对象），Python 3.10+。
- 工牌已刷入本仓库固件。
- 只想先体验、还没装 ZCode？直接跑 `demo-gateway.py`（见文末）。

## 1. 拿配对链接

在 ZCode 桌面端生成一条“网页远控 / 配对”链接，**给网关专用**（别和手机端共用同一条，会互踢），保存为网关目录下的 `link.txt`。

## 2. 配置

在 `gateway/` 目录运行 `python setup-gateway.py`：自动起一个**本地配置页并打开浏览器**（`http://127.0.0.1:8790`，只绑回环、密钥不外传）。页面上四项填完点「保存配置」：

1. **ZCode 远控配对链接**（写入 `link.txt`；在 ZCode 桌面端生成，给网关专用别和手机共用）
2. **访问令牌**（自动生成，可改可重生成；写入 `gateway-config.json`）
3. **监听地址 / 端口**（页面显示本机局域网 IP，工牌要填的就是它）
4. **语音 ASR/TTS**（腾讯云 SecretId/Key 写入 `asr.env`；或自建 FunASR；或暂不配置）

保存后页面直接显示工牌要填的「网关地址」和「令牌」。

> 无浏览器的服务器 / SSH：用终端模式 `python setup-gateway.py --cli`。

手动配置则：`token` 自定义（工牌配网填同一个），其余默认即可（`link_file` 指向 `link.txt`）。

## 语音配置（ASR / TTS）

- **依赖**：`miniaudio`（24k→16k 重采样）+ `edge-tts`（TTS）；`start-gateway` 会自动安装。缺 miniaudio 会报"ASR 需要 16000 收到 24000"。
- **ASR（语音→文字）**：腾讯云一句话识别，密钥放 `gateway/asr.env`：
  ```
  TENCENT_CLOUD_SECRET_ID=<你的ID>
  TENCENT_CLOUD_SECRET_KEY=<你的KEY>
  ```
  不用腾讯云可在 `gateway-config.json` 设 `asr_url`（OpenAI 兼容端点，如 FunASR）+ `asr_model`。
- **TTS（回复语音）**：edge-tts，免密钥；音色 `tts_voice`（默认 `zh-CN-XiaoxiaoNeural`）。
- **采样率**：录音固定 24kHz（ES8311 硬件时钟要求），网关自动重采样到 16kHz 送 ASR，**不要**改固件去采 16k（会破坏麦克风时钟）。

## 3. 启动

- Windows：双击 `start-gateway.bat`（自动建 venv + 装依赖 + 启动）。
- macOS / Linux：`./start-gateway.sh`。

看到 `HTTP API 监听 ...:<你设的端口>` 即成功（默认 8787）。

## 4. 让工牌连上它

1. 查本机局域网 IP：Windows `ipconfig`（看 `192.168.x.x` / `10.x.x.x`），macOS/Linux `ifconfig` 或 `ip a`。
2. 工牌：**长按 OK → 设置 → 配网设置** → 手机/电脑连热点 `ZCode-Badge-Setup` → 浏览器开 `http://192.168.4.1` → 填你的 WiFi（仅 2.4G）与网关地址 `http://<本机局域网IP>:<你设的端口>`、令牌 → 保存，工牌自动重启。
3. 防火墙：放行你设的端口（默认 8787）入站（Windows 首次会弹窗，选“允许专用网络”）。

## 5. 玩

首页浏览工作区 / 会话 → 进会话短按 OK 说话 → 再按 OK 发送 → 回答语音播报 + 屏显。长按 OK 进设置（配网 / 静音 / 测试网关 / 指导）。

## 只想先体验（免 ZCode / 免配对）

```
python demo-gateway.py
```

仅标准库即可运行。工牌网关地址填 `http://<本机IP>:8787`（端口被占可 `python demo-gateway.py --port 8788`）。它返回演示工作区/会话与演示语音，让你零门槛走通“浏览 → 说话 → 听回复”全流程；满意后再按上面接真实 ZCode。

## 免费云托管（进阶，可选）

想常在线 / 远程使用，可把本网关部署到免费云（如 Oracle Cloud Always Free 虚拟机）：上传本目录、装依赖、配好 `gateway-config.json` + `link.txt`、放行端口、用 systemd 或计划任务常驻。注意网关需能出网连 ZCode 中继，且工牌能访问到它（公网 IP / 域名）。
