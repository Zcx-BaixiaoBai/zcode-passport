// main/wifi_sta.h —— STA 模式连网（凭据来自 menuconfig，见 Kconfig.projbuild）
#pragma once

#include <stdbool.h>

// 启动 WiFi STA 并发起连接（非阻塞；断线自动重连）。
void wifi_sta_start(const char *ssid, const char *pass);

// 阻塞等待拿到 IP。超时返回 false。
bool wifi_wait_connected(int timeout_sec);

bool wifi_is_connected(void);
