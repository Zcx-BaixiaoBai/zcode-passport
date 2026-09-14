// main/badge_cfg.h —— 工牌运行期配置（NVS 持久化）
//
// 发布固件不内嵌任何凭据：WiFi / 网关地址 / 网关令牌一律存 NVS，
// 首次经 SoftAP 网页配网（provision.c）写入。
// CONFIG_ZCODE_* 仅作开发期回退默认；发布构建中它们为空，故 bin 不含凭据。
#pragma once
#include <stdbool.h>

#define CFG_SSID_MAX  33
#define CFG_PASS_MAX  65
#define CFG_URL_MAX   97
#define CFG_TOKEN_MAX 65

typedef struct {
    char ssid[CFG_SSID_MAX];
    char pass[CFG_PASS_MAX];
    char gw_url[CFG_URL_MAX];
    char gw_token[CFG_TOKEN_MAX];
    bool mute;        // 静音：不播报 TTS（文本仍上屏）
    bool onboarded;   // 已完成首次配网
} badge_cfg_t;

// 读 NVS；空字段回退 CONFIG_ZCODE_* 编译默认（发布构建为空）。
void cfg_load(badge_cfg_t *c);
// 整体写回 NVS。
void cfg_save(const badge_cfg_t *c);

// 配网请求标志：设置里点“配网”→置位→重启进 SoftAP 配网模式；保存后清除。
void cfg_set_provision_request(bool on);
bool cfg_get_provision_request(void);
