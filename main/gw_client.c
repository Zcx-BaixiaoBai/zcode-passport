// main/gw_client.c —— 网关 HTTP 客户端实现（esp_http_client + cJSON）
#include "gw_client.h"

#include "esp_http_client.h"
#include "esp_log.h"
#include "cJSON.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *TAG = "gw_client";

static char s_base[96]  = "";
static char s_token[64] = "";

#define URL_BUF 512
#define CHUNK   2048

void gw_client_init(const char *base_url, const char *token)
{
    // 容忍配网时漏填协议头：无 "://" 时自动补 http://（用户常只填 192.168.x.x:8788）
    if (base_url && strncmp(base_url, "http://", 7) != 0 && strncmp(base_url, "https://", 8) != 0) {
        snprintf(s_base, sizeof(s_base), "http://%s", base_url);
    } else {
        snprintf(s_base, sizeof(s_base), "%s", base_url ? base_url : "");
    }
    // 去掉尾部 '/'
    size_t n = strlen(s_base);
    while (n && s_base[n - 1] == '/') s_base[--n] = '\0';
    snprintf(s_token, sizeof(s_token), "%s", token ? token : "");
}

static void apply_common(esp_http_client_handle_t c)
{
    if (s_token[0]) {
        esp_http_client_set_header(c, "X-Gateway-Token", s_token);
    }
}

// URL 百分号编码（query 值用：workspace 路径含反斜杠/空格/中文）
static void url_encode(const char *src, char *dst, size_t dst_sz)
{
    static const char hex[] = "0123456789ABCDEF";
    size_t o = 0;
    for (const unsigned char *p = (const unsigned char *)src; *p && o + 4 < dst_sz; p++) {
        if ((*p >= 'A' && *p <= 'Z') || (*p >= 'a' && *p <= 'z') ||
            (*p >= '0' && *p <= '9') || *p == '-' || *p == '_' || *p == '.' || *p == '~') {
            dst[o++] = *p;
        } else {
            dst[o++] = '%';
            dst[o++] = hex[*p >> 4];
            dst[o++] = hex[*p & 0xF];
        }
    }
    dst[o] = '\0';
}

// GET 并把完整响应体读进 malloc 缓冲（调用方 free）。失败返回 NULL。
static char *http_get(const char *url, int *out_len)
{
    esp_http_client_config_t cfg = {
        .url = url,
        .timeout_ms = 20000,
        .buffer_size = 2048,
        .disable_auto_redirect = true,   // 网关不应重定向；拒绝跟随
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return NULL;
    apply_common(c);

    char *body = NULL;
    int cap = 0, len = 0;
    esp_err_t err = esp_http_client_open(c, 0);
    if (err == ESP_OK) {
        int clen = esp_http_client_fetch_headers(c);
        int status = esp_http_client_get_status_code(c);
        if (status == 200) {
            cap = (clen > 0 ? clen : 8192) + 1;
            body = malloc(cap);
            if (body) {
                for (;;) {
                    if (len + CHUNK + 1 > cap) {
                        cap *= 2;
                        char *nb = realloc(body, cap);
                        if (!nb) { free(body); body = NULL; break; }
                        body = nb;
                    }
                    int r = esp_http_client_read(c, body + len, CHUNK);
                    if (r <= 0) break;
                    len += r;
                }
                if (body) body[len] = '\0';
            }
        } else {
            ESP_LOGW(TAG, "GET %s → %d", url, status);
        }
    } else {
        ESP_LOGW(TAG, "GET %s open 失败: %s", url, esp_err_to_name(err));
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    if (out_len) *out_len = len;
    return body;
}

// UTF-8 安全拷贝：截断时回退到字符边界，不把多字节汉字切成乱码
static void copy_cstr(char *dst, size_t sz, const char *s)
{
    if (!s) s = "";
    size_t n = strlen(s);
    if (n >= sz) {
        n = sz - 1;
        while (n > 0 && ((unsigned char)s[n] & 0xC0) == 0x80) n--;
    }
    memcpy(dst, s, n);
    dst[n] = '\0';
}

static void copy_str(char *dst, size_t sz, const cJSON *obj, const char *key)
{
    const cJSON *it = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (cJSON_IsString(it) && it->valuestring) {
        copy_cstr(dst, sz, it->valuestring);
    } else {
        dst[0] = '\0';
    }
}

esp_err_t gw_fetch_workspaces(gw_ws_t *out, int max, int *count)
{
    char url[URL_BUF];
    snprintf(url, sizeof(url), "%s/ui/workspaces", s_base);
    char *body = http_get(url, NULL);
    if (!body) return ESP_FAIL;

    cJSON *root = cJSON_Parse(body);
    free(body);
    if (!root) return ESP_ERR_INVALID_RESPONSE;
    cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "workspaces");
    int n = 0;
    if (cJSON_IsArray(arr)) {
        cJSON *it = NULL;
        cJSON_ArrayForEach(it, arr) {
            if (n >= max) break;
            gw_ws_t *w = &out[n];
            memset(w, 0, sizeof(*w));
            copy_str(w->path, sizeof(w->path), it, "path");
            copy_str(w->label, sizeof(w->label), it, "label");
            const cJSON *t = cJSON_GetObjectItemCaseSensitive(it, "total");
            const cJSON *r = cJSON_GetObjectItemCaseSensitive(it, "running");
            const cJSON *cu = cJSON_GetObjectItemCaseSensitive(it, "current");
            w->total = cJSON_IsNumber(t) ? t->valueint : 0;
            w->running = cJSON_IsNumber(r) ? r->valueint : 0;
            w->current = cJSON_IsTrue(cu);
            n++;
        }
    }
    cJSON_Delete(root);
    *count = n;
    return n > 0 ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t gw_fetch_sessions(const char *ws_path, gw_sess_t *out, int max, int *count)
{
    char enc[GW_WS_PATH * 3 + 8];
    char url[URL_BUF + GW_WS_PATH * 3];
    url_encode(ws_path, enc, sizeof(enc));
    snprintf(url, sizeof(url), "%s/ui/sessions?workspace=%s", s_base, enc);
    char *body = http_get(url, NULL);
    if (!body) return ESP_FAIL;

    cJSON *root = cJSON_Parse(body);
    free(body);
    if (!root) return ESP_ERR_INVALID_RESPONSE;
    cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "sessions");
    int n = 0;
    if (cJSON_IsArray(arr)) {
        cJSON *it = NULL;
        cJSON_ArrayForEach(it, arr) {
            if (n >= max) break;
            gw_sess_t *s = &out[n];
            memset(s, 0, sizeof(*s));
            copy_str(s->task_id, sizeof(s->task_id), it, "taskId");
            copy_str(s->title, sizeof(s->title), it, "title");
            copy_str(s->status, sizeof(s->status), it, "status");
            copy_str(s->phase, sizeof(s->phase), it, "phase");
            copy_str(s->preview, sizeof(s->preview), it, "preview");
            if (s->task_id[0]) n++;
        }
    }
    cJSON_Delete(root);
    *count = n;
    return ESP_OK;
}

esp_err_t gw_ask_voice(const char *task_id, gw_audio_src_t src, void *ctx, gw_ask_t *res)
{
    memset(res, 0, sizeof(*res));
    char url[URL_BUF];
    snprintf(url, sizeof(url), "%s/ask?rate=24000&taskId=%s", s_base, task_id);

    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 190000,            // 覆盖网关 ask_timeout(180s)
        .buffer_size = 4096,
        .buffer_size_tx = CHUNK,
        .disable_auto_redirect = true,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return ESP_ERR_NO_MEM;
    apply_common(c);
    esp_http_client_set_header(c, "Content-Type", "application/octet-stream");

    // write_len<0 → Transfer-Encoding: chunked（边录边传，不占大内存）
    esp_err_t err = esp_http_client_open(c, -1);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ask open 失败: %s", esp_err_to_name(err));
        esp_http_client_cleanup(c);
        return err;
    }

    uint8_t buf[CHUNK];
    bool aborted = false;
    for (;;) {
        size_t got = 0;
        if (src(buf, sizeof(buf), &got, ctx) != ESP_OK) { aborted = true; break; }
        if (got == 0) break;                       // 录音正常结束
        // ★ ESP-IDF 的 esp_http_client_write 只发裸字节，不自动加 chunked 帧。
        //   Transfer-Encoding: chunked 要求应用层手动包装：<hex长度>\r\n<data>\r\n
        //   （之前发裸音频→网关按 chunked 解析时 int(音频字节,16) 报 ValueError→400→RST）
        char chdr[16];
        int hlen = snprintf(chdr, sizeof(chdr), "%X\r\n", (unsigned)got);
        if (esp_http_client_write(c, chdr, hlen) < hlen) { aborted = true; break; }
        if (esp_http_client_write(c, (const char *)buf, got) < (int)got) { aborted = true; break; }
        if (esp_http_client_write(c, "\r\n", 2) < 2) { aborted = true; break; }
    }

    if (aborted) {
        // 取消/麦克风失败：立即断开，不等服务器把残缺音频处理完。
        // 取消场景 do_ask 会优先显示"已取消"，这里的文案对应真实故障。
        ESP_LOGW(TAG, "ask 上传中止，立即断开");
        esp_http_client_close(c);
        esp_http_client_cleanup(c);
        snprintf(res->error, sizeof(res->error), "录音中断（麦克风或网络）");
        return ESP_ERR_INVALID_STATE;
    }

    // chunked 终止块（esp_http_client 不会自动发，必须手动）
    esp_http_client_write(c, "0\r\n\r\n", 5);
    ESP_LOGI(TAG, "ask 上传完毕，等待网关处理（ASR→提问→回复→TTS）…");
    esp_http_client_fetch_headers(c);
    int status = esp_http_client_get_status_code(c);
    if (status != 200) {
        // 读出服务器错误 JSON，把真实原因（如"音频过短"）透传到界面
        char ebody[512];
        int etotal = 0;
        while (etotal < (int)sizeof(ebody) - 1) {
            int r = esp_http_client_read(c, ebody + etotal, sizeof(ebody) - 1 - etotal);
            if (r <= 0) break;
            etotal += r;
        }
        ebody[etotal] = '\0';
        cJSON *er = cJSON_Parse(ebody);
        const cJSON *e = er ? cJSON_GetObjectItemCaseSensitive(er, "error") : NULL;
        if (cJSON_IsString(e) && e->valuestring[0]) {
            copy_cstr(res->error, sizeof(res->error), e->valuestring);
        } else if (status == 0) {
            snprintf(res->error, sizeof(res->error), "网关无响应（超时）");
        } else {
            snprintf(res->error, sizeof(res->error), "网关返回 %d", status);
        }
        if (er) cJSON_Delete(er);
        ESP_LOGW(TAG, "ask 失败 %d: %s", status, res->error);
        esp_http_client_close(c);
        esp_http_client_cleanup(c);
        return ESP_FAIL;
    }

    char body[2048];
    int total = 0;
    while (total < (int)sizeof(body) - 1) {
        int r = esp_http_client_read(c, body + total, sizeof(body) - 1 - total);
        if (r <= 0) break;
        total += r;
    }
    body[total] = '\0';
    esp_http_client_close(c);
    esp_http_client_cleanup(c);

    cJSON *root = cJSON_Parse(body);
    if (!root) return ESP_ERR_INVALID_RESPONSE;
    res->ok = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(root, "ok"));
    copy_str(res->question, sizeof(res->question), root, "question");
    copy_str(res->answer, sizeof(res->answer), root, "answer");
    copy_str(res->audio_id, sizeof(res->audio_id), root, "audioId");
    copy_str(res->error, sizeof(res->error), root, "error");
    cJSON_Delete(root);
    return res->ok ? ESP_OK : ESP_FAIL;
}

esp_err_t gw_fetch_history(const char *task_id, char *out, size_t out_sz)
{
    out[0] = '\0';
    char enc[GW_SESS_ID * 3 + 8];
    char url[URL_BUF + GW_SESS_ID * 3];
    url_encode(task_id, enc, sizeof(enc));
    snprintf(url, sizeof(url), "%s/ui/history?task=%s&limit=8", s_base, enc);
    char *body = http_get(url, NULL);
    if (!body) return ESP_FAIL;
    cJSON *root = cJSON_Parse(body);
    free(body);
    if (!root) return ESP_ERR_INVALID_RESPONSE;
    esp_err_t ret = ESP_FAIL;
    if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(root, "ok"))) {
        const cJSON *t = cJSON_GetObjectItemCaseSensitive(root, "text");
        if (cJSON_IsString(t) && t->valuestring) {
            copy_cstr(out, out_sz, t->valuestring);
            ret = ESP_OK;
        }
    }
    cJSON_Delete(root);
    return ret;
}

esp_err_t gw_fetch_audio(const char *audio_id, gw_chunk_cb_t chunk, void *ctx)
{
    char url[URL_BUF];
    snprintf(url, sizeof(url), "%s/audio/%s", s_base, audio_id);

    esp_http_client_config_t cfg = {
        .url = url, .timeout_ms = 30000, .buffer_size = 4096,
        .disable_auto_redirect = true,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return ESP_ERR_NO_MEM;
    apply_common(c);

    esp_err_t ret = esp_http_client_open(c, 0);
    if (ret != ESP_OK) { esp_http_client_cleanup(c); return ret; }
    esp_http_client_fetch_headers(c);
    if (esp_http_client_get_status_code(c) != 200) {
        esp_http_client_close(c);
        esp_http_client_cleanup(c);
        return ESP_ERR_NOT_FOUND;
    }

    static uint8_t buf[4096];
    for (;;) {
        int r = esp_http_client_read(c, (char *)buf, sizeof(buf));
        if (r <= 0) break;
        if (chunk(buf, r, ctx) != ESP_OK) { ret = ESP_ERR_INVALID_STATE; break; }
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    return ret;
}
