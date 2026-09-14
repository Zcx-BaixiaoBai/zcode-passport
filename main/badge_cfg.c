// main/badge_cfg.c —— NVS 配置读写。命名空间 "zbadge"。
#include "badge_cfg.h"

#include "sdkconfig.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_log.h"

#include <stdio.h>
#include <string.h>

static const char *TAG = "cfg";
#define NS "zbadge"

static void get_str(nvs_handle h, const char *key, char *out, size_t cap)
{
    size_t len = cap;
    out[0] = 0;
    if (nvs_get_str(h, key, out, &len) != ESP_OK) {
        out[0] = 0;
    }
}

void cfg_load(badge_cfg_t *c)
{
    memset(c, 0, sizeof(*c));
    nvs_handle h;
    if (nvs_open(NS, NVS_READONLY, &h) == ESP_OK) {
        get_str(h, "ssid", c->ssid, sizeof(c->ssid));
        get_str(h, "pass", c->pass, sizeof(c->pass));
        get_str(h, "gw_url", c->gw_url, sizeof(c->gw_url));
        get_str(h, "gw_token", c->gw_token, sizeof(c->gw_token));
        uint8_t m = 0, ob = 0;
        nvs_get_u8(h, "mute", &m);
        nvs_get_u8(h, "onboarded", &ob);
        c->mute = (m != 0);
        c->onboarded = (ob != 0);
        nvs_close(h);
    }
    // 开发期回退：NVS 为空则用编译默认。发布构建 CONFIG_ZCODE_* 为空，不引入凭据。
    if (!c->ssid[0])     snprintf(c->ssid, sizeof(c->ssid), "%s", CONFIG_ZCODE_WIFI_SSID);
    if (!c->pass[0])     snprintf(c->pass, sizeof(c->pass), "%s", CONFIG_ZCODE_WIFI_PASS);
    if (!c->gw_url[0])   snprintf(c->gw_url, sizeof(c->gw_url), "%s", CONFIG_ZCODE_GW_URL);
    if (!c->gw_token[0]) snprintf(c->gw_token, sizeof(c->gw_token), "%s", CONFIG_ZCODE_GW_TOKEN);
    ESP_LOGI(TAG, "cfg: ssid=%s gw=%s onboarded=%d mute=%d",
             c->ssid, c->gw_url, (int)c->onboarded, (int)c->mute);
}

void cfg_save(const badge_cfg_t *c)
{
    nvs_handle h;
    if (nvs_open(NS, NVS_READWRITE, &h) != ESP_OK) {
        ESP_LOGE(TAG, "nvs open 失败");
        return;
    }
    nvs_set_str(h, "ssid", c->ssid);
    nvs_set_str(h, "pass", c->pass);
    nvs_set_str(h, "gw_url", c->gw_url);
    nvs_set_str(h, "gw_token", c->gw_token);
    nvs_set_u8(h, "mute", c->mute ? 1 : 0);
    nvs_set_u8(h, "onboarded", c->onboarded ? 1 : 0);
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "cfg 已保存");
}

void cfg_set_provision_request(bool on)
{
    nvs_handle h;
    if (nvs_open(NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_u8(h, "prov", on ? 1 : 0);
    nvs_commit(h);
    nvs_close(h);
}

bool cfg_get_provision_request(void)
{
    nvs_handle h;
    uint8_t v = 0;
    if (nvs_open(NS, NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u8(h, "prov", &v);
        nvs_close(h);
    }
    return v != 0;
}
