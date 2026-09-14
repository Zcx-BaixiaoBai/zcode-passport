idf_component_register(
    SRCS "main.c"
         "ui_badge.c"
         "gw_client.c"
         "audio_pipe.c"
         "wifi_sta.c"
         "badge_cfg.c"
         "provision.c"
         "lv_font_ai_passport_14.c"
         "ui_pixel.c"
         "ui_pixel_math.c"
    INCLUDE_DIRS "."
    REQUIRES bsp esp_event esp_hw_support esp_netif esp_timer esp_wifi nvs_flash
             esp_http_client esp_http_server json
)
