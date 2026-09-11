// main/wifi_sta.c —— 最小 STA 实现：连接 + 断线退避重连。
#include "wifi_sta.h"

#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

static const char *TAG = "wifi_sta";
static EventGroupHandle_t s_eg;
static const int BIT_IP = BIT0;
static bool s_started;

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_eg, BIT_IP);
        ESP_LOGW(TAG, "断线，2s 后重连");
        vTaskDelay(pdMS_TO_TICKS(2000));
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "已连接, IP=" IPSTR, IP2STR(&ev->ip_info.ip));
        xEventGroupSetBits(s_eg, BIT_IP);
    }
}

void wifi_sta_start(const char *ssid, const char *pass)
{
    if (s_started) return;
    s_started = true;
    s_eg = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL, NULL));

    wifi_config_t cfg = { 0 };
    snprintf((char *)cfg.sta.ssid, sizeof(cfg.sta.ssid), "%s", ssid);
    snprintf((char *)cfg.sta.password, sizeof(cfg.sta.password), "%s", pass);
    cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    // ★ 禁用 WiFi 省电：CPU 不进轻度睡眠，I2S DMA 持续运行。
    //   默认 WIFI_PS_MIN_MODEM 会让 CPU 在 beacon 间隔睡眠 → I2S DMA 停转 →
    //   麦克风录几块（陈旧 DMA 数据）后就再也没有新数据 → 读阻塞/超时。
    //   飞书固件 feishu_network.c:105 同样禁用省电，录音正常。
    esp_wifi_set_ps(WIFI_PS_NONE);
    ESP_LOGI(TAG, "STA 启动, SSID=%s", ssid);
}

bool wifi_wait_connected(int timeout_sec)
{
    return xEventGroupWaitBits(s_eg, BIT_IP, pdFALSE, pdTRUE,
                               pdMS_TO_TICKS(timeout_sec * 1000)) & BIT_IP;
}

bool wifi_is_connected(void)
{
    return s_eg && (xEventGroupGetBits(s_eg) & BIT_IP);
}
