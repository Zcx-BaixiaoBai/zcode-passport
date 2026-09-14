// main/ui_badge.h —— 工牌三级界面
//
// 层级：工作区列表 → 会话列表 → 会话详情（PTT 语音问答）
// 所有函数内部自带 LVGL 锁，可在任意任务调用。
#pragma once

#include <stdbool.h>

typedef struct {
    const char *main;     // 左侧主文本
    const char *right;    // 右侧附注（进度/状态），可为 NULL
} ui_row_t;

void ui_init(void);

void ui_set_header(const char *title);
void ui_set_battery(int soc);              // -1 = 不可用
void ui_set_mute(bool on);                 // 顶栏静音角标
void ui_set_hint(const char *text);        // 底部按键提示

void ui_set_busy(const char *msg);         // 全屏遮罩消息；NULL 关闭

void ui_show_list(const ui_row_t *rows, int n, int sel);
void ui_list_move(int delta);              // 循环移动光标；返回新选中下标
int  ui_list_sel(void);
int  ui_list_count(void);

void ui_show_detail(const char *title);
void ui_set_state(const char *state_text); // 中部状态行（聆听中/思考中/播报中…）
void ui_set_answer(const char *text);      // 回复正文（可滚动）
void ui_scroll_answer(int delta_pixels);
void ui_set_error(const char *msg);        // 状态行变红显示错误
