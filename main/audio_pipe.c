// main/audio_pipe.c —— 录音/播放实现
//
// 播放侧是按字节喂入的 RIFF 解析状态机：逐 chunk 扫描，fmt 校验，
// data chunk 里的 PCM 攒满 1KB 写一次 codec（阻塞写=天然按采样率节流）。
// 网关固定输出 16kHz/16bit/mono WAV（edge-tts mp3 → miniaudio 重采样）。
#include "audio_pipe.h"

#include "bsp_audio.h"
#include "esp_log.h"
#include "esp_system.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/stream_buffer.h"

#include <math.h>
#include <string.h>

static const char *TAG = "audio_pipe";

esp_err_t audio_init(void)
{
    esp_err_t err = bsp_audio_init();
    if (err != ESP_OK) return err;
    err = bsp_audio_set_format(24000, 16, 1);   // 24kHz：BCLK=768kHz，满足 ES8311 ADC 时钟需求（16kHz 时 BCLK=512kHz 不足→录几块就停）
    if (err != ESP_OK) return err;
    bsp_audio_set_volume(90);
    return ESP_OK;
}

// ---- 录音子系统 ----
// 设计参照飞书终端固件 feishu_asr.c 的真机可用架构：阻塞的 bsp_audio_read
// 隔离在独立采集任务里，worker 通过流缓冲消费且带超时——codec 万一挂起，
// 卡死的只是采集任务，worker 2.5s 超时报错退出，界面永不僵死。
// 另：每次录音前 reopen codec（修长时间空闲后首次读失败）+ 播提示音
// （用户反馈 + TX 通路预热，官方 demo 同样是先放音再录音）。

#define REC_SB_BYTES   (32 * 1024)   // 流缓冲 ≈1s 音频余量
#define REC_CHUNK      2048          // 采集块 64ms
#define REC_WARMUP     2             // 丢弃前 2 块（提示音尾 + DMA 陈旧数据）

static StreamBufferHandle_t s_rec_sb;
static volatile bool s_cap_run;      // 采集任务运行许可
static volatile bool s_cap_stop;     // 正常停止（排空后结束）
static volatile bool s_cap_cancel;   // 取消
static volatile bool s_cap_alive;    // 采集任务存活
static volatile esp_err_t s_cap_err;
static uint8_t  s_cap_chunk[REC_CHUNK];
static int16_t  s_beep[1920];        // 120ms 880Hz 提示音
static bool     s_beep_ready;

static void beep_prepare(void)
{
    if (s_beep_ready) return;
    for (int i = 0; i < 1920; i++)
        s_beep[i] = (int16_t)(3000.0f * sinf(6.2831853f * 880.0f * (float)i / 16000.0f));
    s_beep_ready = true;
}

static void cap_task(void *arg)
{
    (void)arg;
    s_cap_alive = true;
    int warm = REC_WARMUP;
    while (s_cap_run && !s_cap_cancel && !s_cap_stop) {
        esp_err_t err = bsp_audio_read(s_cap_chunk, sizeof(s_cap_chunk));
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "capture: mic 读失败 %s", esp_err_to_name(err));
            s_cap_err = err;
            break;
        }
        if (warm > 0) { warm--; continue; }
        if (xStreamBufferSend(s_rec_sb, s_cap_chunk, sizeof(s_cap_chunk),
                              pdMS_TO_TICKS(800)) != sizeof(s_cap_chunk))
            ESP_LOGW(TAG, "capture: 流缓冲满，丢块");   // 上传太慢，丢弃保活
    }
    s_cap_alive = false;
    vTaskDelete(NULL);
}

esp_err_t audio_rec_start(void)
{
    if (s_cap_alive) {
        // 上次采集任务未退（可能挂在 codec 读里）：取消 + reopen 解卡
        s_cap_cancel = true;
        s_cap_run = false;
        bsp_audio_reopen();
        for (int i = 0; i < 20 && s_cap_alive; i++) vTaskDelay(pdMS_TO_TICKS(50));
        if (s_cap_alive) {
            ESP_LOGE(TAG, "采集任务拒不退出，麦克风不可用");
            return ESP_ERR_INVALID_STATE;
        }
    }
    if (s_rec_sb) { vStreamBufferDelete(s_rec_sb); s_rec_sb = NULL; }
    s_cap_err = ESP_OK;
    s_cap_stop = false;
    s_cap_cancel = false;

    // 仅 set_format：同格式(24kHz)短路复用 boot 时的打开状态，不 close+open。
    // 强制 reopen 会 toggle PA 功放→电流浪涌→WiFi 常开下掉电复位（飞书固件同样只 set_format 不 reopen）。
    esp_err_t err = bsp_audio_set_format(24000, 16, 1);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "codec set_format 失败: %s", esp_err_to_name(err));
        return err;
    }
    beep_prepare();
    // 暂时禁用提示音：beep 写 TX 可能扰动 I2S 共享时钟，导致 RX 录音几块后停数据
    // bsp_audio_write(s_beep, sizeof(s_beep));
    ESP_LOGI(TAG, "录音启动（空闲堆 %u B）", (unsigned)esp_get_free_heap_size());

    s_rec_sb = xStreamBufferCreate(REC_SB_BYTES, 1);
    if (!s_rec_sb) return ESP_ERR_NO_MEM;
    s_cap_run = true;
    if (xTaskCreate(cap_task, "rec_cap", 4096, NULL, 6, NULL) != pdPASS) {
        vStreamBufferDelete(s_rec_sb);
        s_rec_sb = NULL;
        s_cap_run = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t audio_rec_read(uint8_t *buf, size_t want, size_t *got, int timeout_ms)
{
    *got = 0;
    if (!s_rec_sb) return ESP_ERR_INVALID_STATE;
    size_t n = xStreamBufferReceive(s_rec_sb, buf, want, pdMS_TO_TICKS(timeout_ms));
    if (n > 0) { *got = n; return ESP_OK; }
    if (s_cap_err != ESP_OK) return s_cap_err;              // 采集侧报错
    if (!s_cap_alive && xStreamBufferIsEmpty(s_rec_sb))     // 已停止且排空
        return ESP_OK;                                      // *got=0 = 正常结束
    return ESP_ERR_TIMEOUT;                                 // 超时无数据=麦克风挂起
}

void audio_rec_stop(void)   { s_cap_stop = true; }

void audio_rec_cancel(void)
{
    s_cap_cancel = true;
    s_cap_run = false;
}

// ---- WAV 流式状态机 ----
typedef enum {
    W_RIFF,        // 累积 12 字节 "RIFF"???? "WAVE"
    W_CHUNK_ID,    // 累积 4 字节 chunk id
    W_CHUNK_SIZE,  // 累积 4 字节大小
    W_SKIP,        // 跳过非 data chunk（fmt 顺手采集前 8 字节）
    W_DATA,        // 播放 data
} wav_state_t;

static wav_state_t s_state;
static uint8_t  s_riff[12];
static uint8_t  s_id[4];
static uint8_t  s_sz[4];
static uint8_t  s_fmt[8];
static int      s_acc_n;          // 当前累积计数（RIFF/ID/SIZE 共用）
static int      s_fmt_i;          // fmt 采集计数
static bool     s_is_fmt;         // 当前跳过的是 fmt chunk
static uint32_t s_chunk_left;     // 当前 chunk 剩余字节（含奇数 pad）
static uint32_t s_fmt_rate, s_fmt_ch;
static uint8_t  s_data_buf[1024];
static size_t   s_data_n;

void audio_play_begin(void)
{
    s_state = W_RIFF;
    s_acc_n = 0;
    s_fmt_i = 0;
    s_is_fmt = false;
    s_chunk_left = 0;
    s_data_n = 0;
    s_fmt_rate = s_fmt_ch = 0;
}

static uint32_t le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void flush_data(void)
{
    if (s_data_n) {
        bsp_audio_write(s_data_buf, s_data_n);
        s_data_n = 0;
    }
}

esp_err_t audio_play_feed(const uint8_t *data, size_t len)
{
    for (size_t i = 0; i < len; i++) {
        uint8_t b = data[i];
        switch (s_state) {

        case W_RIFF:
            s_riff[s_acc_n++] = b;
            if (s_acc_n == 12) {
                if (memcmp(s_riff, "RIFF", 4) || memcmp(s_riff + 8, "WAVE", 4)) {
                    ESP_LOGE(TAG, "非 RIFF/WAVE 流");
                    return ESP_ERR_INVALID_ARG;
                }
                s_acc_n = 0;
                s_state = W_CHUNK_ID;
            }
            break;

        case W_CHUNK_ID:
            s_id[s_acc_n++] = b;
            if (s_acc_n == 4) {
                s_acc_n = 0;
                s_state = W_CHUNK_SIZE;
            }
            break;

        case W_CHUNK_SIZE:
            s_sz[s_acc_n++] = b;
            if (s_acc_n == 4) {
                s_acc_n = 0;
                uint32_t sz = le32(s_sz);
                if (memcmp(s_id, "data", 4) == 0) {
                    s_chunk_left = sz;
                    s_state = W_DATA;
                    ESP_LOGI(TAG, "data %u B, fmt=%uHz/%uch", (unsigned)sz,
                             (unsigned)s_fmt_rate, (unsigned)s_fmt_ch);
                    if (s_fmt_rate && s_fmt_rate != 16000) {
                        ESP_LOGW(TAG, "采样率 %u != 16000，重设格式", (unsigned)s_fmt_rate);
                        bsp_audio_set_format(s_fmt_rate, 16,
                                             (uint8_t)(s_fmt_ch ? s_fmt_ch : 1));
                    }
                } else {
                    s_is_fmt = (memcmp(s_id, "fmt ", 4) == 0);
                    s_fmt_i = 0;
                    s_chunk_left = sz + (sz & 1);   // RIFF 奇数字节 pad
                    s_state = W_SKIP;
                }
            }
            break;

        case W_SKIP:
            if (s_is_fmt && s_fmt_i < (int)sizeof(s_fmt)) {
                s_fmt[s_fmt_i++] = b;
                if (s_fmt_i == 8) {
                    // PCM fmt: [0..1]=audioFormat [2..3]=channels [4..7]=sampleRate
                    s_fmt_ch   = (uint32_t)(s_fmt[2] | (s_fmt[3] << 8));
                    s_fmt_rate = le32(s_fmt + 4);
                }
            }
            if (s_chunk_left) s_chunk_left--;
            if (s_chunk_left == 0) {
                s_is_fmt = false;
                s_state = W_CHUNK_ID;
            }
            break;

        case W_DATA:
            s_data_buf[s_data_n++] = b;
            if (s_data_n == sizeof(s_data_buf)) flush_data();
            if (s_chunk_left) s_chunk_left--;
            if (s_chunk_left == 0) {
                flush_data();
                s_state = W_CHUNK_ID;
            }
            break;
        }
    }
    return ESP_OK;
}
