// main/provision.c —— SoftAP 网页配网（首次使用 / 改配置）
//
// 工牌开一个开放热点 ZCode-Badge-Setup，手机/电脑连上后浏览器打开
// http://192.168.4.1 填 WiFi + 网关地址/令牌，提交后写 NVS 并重启进 STA。
// 配网是独立的一次启动模式（app_main 开机判定），避免 AP/STA 在同一次启动里混初始化。
#include "provision.h"
#include "badge_cfg.h"
#include "ui_badge.h"

#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_http_server.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <stdio.h>
#include <string.h>

static const char *TAG = "provision";
#define AP_SSID "ZCode-Badge-Setup"

static const char *HTML_FORM =
    "<!doctype html><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>ZCode 工牌配网</title>"
    "<style>body{font-family:system-ui,sans-serif;background:#0f1419;color:#e6edf3;"
    "margin:0;padding:24px}h2{color:#2f81f7}label{display:block;margin:14px 0 4px;"
    "color:#8b98a5;font-size:14px}input{width:100%;box-sizing:border-box;padding:10px;"
    "border:1px solid #2a3542;border-radius:8px;background:#1a222c;color:#e6edf3;"
    "font-size:16px}button{margin-top:18px;width:100%;padding:12px;border:0;border-radius:8px;"
    "background:#2f81f7;color:#fff;font-size:16px;font-weight:600}"
    ".tip{color:#6e7c8c;font-size:13px;line-height:1.6}</style>"
    "<h2>ZCode 工牌配网</h2>"
    "<h3>① 先在本机配置并跑起网关（bridge）</h3>"
    "<p class=tip>下载本仓库 gateway/ 目录：<b>github.com/Zcx-BaixiaoBai/zcode-passport</b>。</p>"
    "<p class=tip><b>配置三步</b>（或运行 <code>python setup-gateway.py</code> 一键完成）："
    "a. 远控链接：ZCode 桌面端生成『网页远控』链接，存为 <code>gateway/link.txt</code>；"
    "b. 令牌：向导自动生成，或自填进 <code>gateway-config.json</code> 的 token；"
    "c. 地址/端口：默认 0.0.0.0:8787，本机局域网 IP 用 <code>ipconfig</code> 或向导打印查看。</p>"
    "<p class=tip>只想体验可跳过配置：<code>python demo-gateway.py</code>（免 ZCode、免令牌）。"
    "配置好后双击 <code>start-gateway.bat</code> 启动，启动日志打印监听端口。</p>"
    "<p class=tip><b>语音</b>：ASR 密钥放 <code>gateway/asr.env</code>（腾讯云）或配 <code>asr_url</code>；"
    "TTS 用 edge-tts 免密钥；依赖 miniaudio+edge-tts（启动脚本自动装）。录音 24k 自动重采样 16k。</p>"
    "<h3>② 填写表单（WiFi + 网关地址 + 令牌）</h3>"
    "<form method=post action=/save>"
    "<label>WiFi 名称（仅 2.4G）</label><input name=ssid required autocomplete=off>"
    "<label>WiFi 密码</label><input name=pass type=password autocomplete=off>"
    "<label>网关地址</label><input name=gw_url required placeholder='http://192.168.1.20:8787' autocomplete=off>"
    "<p class=tip>= 运行 gateway.py / demo-gateway.py 那台电脑的局域网 IP + :8787。"
    "电脑开 cmd 输入 ipconfig 查看（192.168.x.x / 10.x.x.x）；手机与工牌需和电脑同一 WiFi。</p>"
    "<label>网关令牌（可留空）</label><input name=token autocomplete=off>"
    "<p class=tip>真实网关填你在 gateway-config.json 自设的 token（两边一致）；demo-gateway 留空。</p>"
    "<label style='color:#e6edf3'><input type=checkbox name=mute value=1 style='width:auto'> 静音（不播报语音，仅显示文字）</label>"
    "<button>保存并重启工牌</button></form>";

static int hexval(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static void url_decode(const char *in, char *out, size_t cap)
{
    size_t o = 0;
    for (size_t i = 0; in[i] && o + 1 < cap; i++) {
        char c = in[i];
        if (c == '+') {
            out[o++] = ' ';
        } else if (c == '%' && in[i + 1] && in[i + 2]) {
            int hi = hexval(in[i + 1]), lo = hexval(in[i + 2]);
            if (hi >= 0 && lo >= 0) { out[o++] = (char)((hi << 4) | lo); i += 2; }
            else out[o++] = c;
        } else {
            out[o++] = c;
        }
    }
    out[o] = 0;
}

// 从 url-encoded body 取 key 的值；找不到置空并返回 false。
static bool form_field(const char *body, const char *key, char *out, size_t cap)
{
    size_t klen = strlen(key);
    const char *p = body;
    out[0] = 0;
    while (p && *p) {
        const char *eq = strchr(p, '=');
        const char *amp = strchr(p, '&');
        if (!eq) break;
        if ((size_t)(eq - p) == klen && strncmp(p, key, klen) == 0) {
            const char *vs = eq + 1;
            const char *ve = amp ? amp : vs + strlen(vs);
            size_t vlen = (size_t)(ve - vs);
            char raw[256];
            if (vlen >= sizeof(raw)) vlen = sizeof(raw) - 1;
            memcpy(raw, vs, vlen);
            raw[vlen] = 0;
            url_decode(raw, out, cap);
            return true;
        }
        if (!amp) break;
        p = amp + 1;
    }
    return false;
}

static esp_err_t root_get(httpd_req_t *req)
{
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send(req, HTML_FORM, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

static esp_err_t save_post(httpd_req_t *req)
{
    char body[768];
    int total = 0;
    while (total < (int)sizeof(body) - 1) {
        int n = httpd_req_recv(req, body + total, (int)sizeof(body) - 1 - total);
        if (n <= 0) break;
        total += n;
        if (n < 128) break;      // 表单很小，收满即停
    }
    body[total] = 0;

    char ssid[CFG_SSID_MAX], pass[CFG_PASS_MAX], url[CFG_URL_MAX], tok[CFG_TOKEN_MAX], mutev[8];
    form_field(body, "ssid", ssid, sizeof(ssid));
    form_field(body, "pass", pass, sizeof(pass));
    form_field(body, "gw_url", url, sizeof(url));
    form_field(body, "token", tok, sizeof(tok));
    form_field(body, "mute", mutev, sizeof(mutev));

    if (!ssid[0] || !url[0]) {
        httpd_resp_set_type(req, "text/html; charset=utf-8");
        httpd_resp_sendstr(req, "<meta charset=utf-8>WiFi 名称与网关地址必填。<a href=/>返回</a>");
        return ESP_OK;
    }

    badge_cfg_t c;
    cfg_load(&c);
    snprintf(c.ssid, sizeof(c.ssid), "%s", ssid);
    snprintf(c.pass, sizeof(c.pass), "%s", pass);
    snprintf(c.gw_url, sizeof(c.gw_url), "%s", url);
    snprintf(c.gw_token, sizeof(c.gw_token), "%s", tok);
    c.mute = (mutev[0] == '1' || mutev[0] == 'o' || mutev[0] == 'O');
    c.onboarded = true;
    cfg_save(&c);
    cfg_set_provision_request(false);

    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_sendstr(req, "<meta charset=utf-8><h3>已保存，工牌重启中…</h3>请回到工牌查看。");
    ESP_LOGI(TAG, "配网保存成功 ssid=%s gw=%s，重启", ssid, url);
    vTaskDelay(pdMS_TO_TICKS(900));
    esp_restart();
    return ESP_OK;   // 不可达
}

static void start_ap(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));

    wifi_config_t cfg = { 0 };
    snprintf((char *)cfg.ap.ssid, sizeof(cfg.ap.ssid), "%s", AP_SSID);
    cfg.ap.ssid_len = (uint8_t)strlen(AP_SSID);
    cfg.ap.channel = 1;
    cfg.ap.max_connection = 4;
    cfg.ap.authmode = WIFI_AUTH_OPEN;   // 配网专用短窗口热点，仅写本机配置
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "AP 已启动: %s", AP_SSID);
}

void provision_run(void)
{
    ui_set_header("配网模式");
    ui_show_detail("配网模式");
    ui_set_state("等待手机/电脑连接");
    ui_set_answer("1) 连接 WiFi 热点：\n" AP_SSID "\n\n2) 浏览器打开：\nhttp://192.168.4.1\n\n"
                  "3) 填写你的 WiFi 与网关地址，保存。\n\n4) 工牌自动重启并连网。");
    ui_set_hint("保存后自动重启");

    start_ap();

    httpd_config_t hc = HTTPD_DEFAULT_CONFIG();
    hc.max_open_sockets = 4;
    httpd_handle_t server = NULL;
    if (httpd_start(&server, &hc) != ESP_OK) {
        ESP_LOGE(TAG, "httpd 启动失败");
        ui_set_error("配网服务启动失败");
        return;
    }
    httpd_uri_t u_root = { .uri = "/", .method = HTTP_GET, .handler = root_get };
    httpd_uri_t u_save = { .uri = "/save", .method = HTTP_POST, .handler = save_post };
    httpd_register_uri_handler(server, &u_root);
    httpd_register_uri_handler(server, &u_save);
    ESP_LOGI(TAG, "配网网页就绪 http://192.168.4.1");

    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));   // 保存后 save_post 内部重启
    }
}
