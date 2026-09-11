// main/gw_client.h —— ZCode 工牌网关 HTTP 客户端
//
// 对应 gateway.py 的 API：
//   GET  /ui/workspaces                  工作区+进度
//   GET  /ui/sessions?workspace=<path>   会话列表
//   POST /ask?taskId=<id>&rate=16000     流式上传 PCM → {question, answer, audioId}
//   GET  /audio/<id>                     回复语音 WAV（流式取回播放）
#pragma once

#include "esp_err.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define GW_MAX_WS    12
#define GW_WS_PATH   160
#define GW_WS_LABEL  64
#define GW_SESS_ID   64
#define GW_SESS_TITLE 128

typedef struct {
    char path[GW_WS_PATH];
    char label[GW_WS_LABEL];
    int  total;
    int  running;
    bool current;
} gw_ws_t;

typedef struct {
    char task_id[GW_SESS_ID];
    char title[GW_SESS_TITLE];
    char status[24];      // running / completed / ...
    char phase[24];
    char preview[192];
} gw_sess_t;

typedef struct {
    bool ok;
    char question[256];
    char answer[1024];
    char audio_id[48];
    char error[128];
} gw_ask_t;

// PTT 音频源回调：填充 buf（want 字节，实填 *got）。
// 返回非 ESP_OK 中止本次提问（如用户松开/超时/取消）。
typedef esp_err_t (*gw_audio_src_t)(uint8_t *buf, size_t want, size_t *got, void *ctx);

// 音频下载分块回调（WAV 字节流，交给 audio_pipe 解析播放）。
typedef esp_err_t (*gw_chunk_cb_t)(const uint8_t *data, size_t len, void *ctx);

void gw_client_init(const char *base_url, const char *token);

esp_err_t gw_fetch_workspaces(gw_ws_t *out, int max, int *count);
esp_err_t gw_fetch_sessions(const char *ws_path, gw_sess_t *out, int max, int *count);

// 拉取会话最近的历史输出（网关从订阅回放帧缓存里取，文本已格式化）。
// 成功且 out 非空 = 有历史；out 为空 = 该会话还没有输出。
esp_err_t gw_fetch_history(const char *task_id, char *out, size_t out_sz);

// 完整语音问答：流式上传录音 → 网关 ASR→提问→等回复→TTS → 解析 JSON 结果。
// 阻塞直至拿到回复或超时（网关 ask_timeout=180s，客户端 190s）。
esp_err_t gw_ask_voice(const char *task_id, gw_audio_src_t src, void *ctx, gw_ask_t *res);

esp_err_t gw_fetch_audio(const char *audio_id, gw_chunk_cb_t chunk, void *ctx);
