// main/audio_pipe.h —— 录音/播放管道（ES8311 via BSP，16k mono s16le）
#pragma once

#include "esp_err.h"
#include <stddef.h>
#include <stdint.h>

// 初始化 codec + I2S，统一 16kHz/16bit/单声道，音量 90%。
esp_err_t audio_init(void);

// ---- 录音（独立采集任务 + 流缓冲；消费侧带超时，永不永久阻塞）----
// 启动一次录音：重开 codec 刷新状态 → 播提示音 → 起采集任务。
esp_err_t audio_rec_start(void);
// 读已录 PCM：
//   ESP_OK 且 *got>0   = 数据
//   ESP_OK 且 *got==0  = 已 stop 且排空（正常结束）
//   ESP_ERR_TIMEOUT    = timeout_ms 内无任何数据（麦克风挂起，应中止）
//   其他错误           = 采集失败
esp_err_t audio_rec_read(uint8_t *buf, size_t want, size_t *got, int timeout_ms);
void audio_rec_stop(void);      // 正常结束（缓冲余量仍可读完）
void audio_rec_cancel(void);    // 取消（采集任务尽快退出，数据作废）

// ---- WAV 流式播放器（网关 TTS 返回标准 WAV，边下边播）----
// 用法：audio_play_begin() → 多次 audio_play_feed(收到的字节) → 无需显式结束。
void audio_play_begin(void);
esp_err_t audio_play_feed(const uint8_t *data, size_t len);
