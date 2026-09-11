// main/ui_badge.c —— 三级界面实现（LVGL 9，深色卡片风）
// 字体：lv_font_ai_passport_14（思源黑体 14px，bpp1 压缩，同设备/LVGL 9.5 已验证可渲染）
#include "ui_badge.h"
#include "bsp_display.h"
#include "lvgl.h"

#include <stdio.h>
#include <string.h>

LV_FONT_DECLARE(lv_font_ai_passport_14);
#define FONT_MAIN (&lv_font_ai_passport_14)

// 深色主题调色板
#define C_BG      0x0F1419u   // 页面底色
#define C_CARD    0x1A222Cu   // 卡片/顶栏
#define C_LINE    0x2A3542u   // 分隔线
#define C_ACCENT  0x2F81F7u   // 强调蓝（选中行/加载动画/滚动条）
#define C_TEXT    0xE6EDF3u   // 主文字
#define C_SUB     0x8B98A5u   // 次级文字
#define C_FAINT   0x6E7C8Cu   // 弱提示
#define C_OK      0x4ADE80u   // 电量充足
#define C_WARN    0xFBBF24u   // 状态/低电量
#define C_ERR     0xF85149u   // 错误
#define C_ON_ACC  0xFFFFFFu   // 选中行主文字
#define C_ACC_SUB 0xD6E4FFu   // 选中行次级文字

#define ROWS_MAX     32
#define HDR_H        44
#define BODY_Y       46
#define BODY_H       240
#define ROW_H        34
#define ROW_W        220
#define FOOT_Y       288
#define FOOT_H       32
#define VISIBLE_ROWS 5

static lv_obj_t *s_scr;
static lv_obj_t *s_hdr_title, *s_hdr_batt;
static lv_obj_t *s_body;                 // 当前内容容器（列表/详情），重建时销毁
static lv_obj_t *s_hint;
static lv_obj_t *s_busy;                 // 遮罩（NULL=无）
static lv_obj_t *s_busy_lbl;

// 列表态
static lv_obj_t *s_rows[ROWS_MAX];
static int s_row_n, s_sel;

// 详情态
static lv_obj_t *s_state_lbl, *s_ans_cont, *s_ans_lbl;

static void lock(void)   { bsp_lvgl_lock(2000); }
static void unlock(void) { bsp_lvgl_unlock(); }

// ---------- 基础构件 ----------

// 扁平卡片：无边框、无阴影、圆角 radius
static void flat(lv_obj_t *o, uint32_t bg, int radius)
{
    lv_obj_set_style_bg_color(o, lv_color_hex(bg), 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(o, 0, 0);
    lv_obj_set_style_shadow_width(o, 0, 0);
    lv_obj_set_style_radius(o, radius, 0);
    lv_obj_set_style_pad_all(o, 0, 0);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
}

static lv_obj_t *mk_label(lv_obj_t *parent, const char *text, uint32_t color)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_label_set_text(l, text);
    lv_obj_set_style_text_font(l, FONT_MAIN, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    return l;
}

static void style_scrollbar(lv_obj_t *o)
{
    lv_obj_set_scrollbar_mode(o, LV_SCROLLBAR_MODE_AUTO);
    lv_obj_set_style_width(o, 3, LV_PART_SCROLLBAR);
    lv_obj_set_style_bg_color(o, lv_color_hex(C_ACCENT), LV_PART_SCROLLBAR);
    lv_obj_set_style_bg_opa(o, LV_OPA_70, LV_PART_SCROLLBAR);
    lv_obj_set_style_radius(o, 2, LV_PART_SCROLLBAR);
}

void ui_init(void)
{
    lock();
    s_scr = lv_obj_create(NULL);
    lv_obj_remove_flag(s_scr, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(s_scr, lv_color_hex(C_BG), 0);
    lv_obj_set_style_bg_opa(s_scr, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(s_scr, 0, 0);
    lv_obj_set_style_pad_all(s_scr, 0, 0);

    // 顶栏：卡片底 + 强调色分隔线
    lv_obj_t *hdr = lv_obj_create(s_scr);
    lv_obj_set_size(hdr, 240, HDR_H);
    lv_obj_set_pos(hdr, 0, 0);
    flat(hdr, C_CARD, 0);
    lv_obj_set_style_border_width(hdr, 2, 0);
    lv_obj_set_style_border_color(hdr, lv_color_hex(C_ACCENT), 0);
    lv_obj_set_style_border_side(hdr, LV_BORDER_SIDE_BOTTOM, 0);
    s_hdr_title = mk_label(hdr, "ZCode 工牌", C_TEXT);
    lv_label_set_long_mode(s_hdr_title, LV_LABEL_LONG_DOT);
    lv_obj_set_width(s_hdr_title, 170);
    lv_obj_align(s_hdr_title, LV_ALIGN_LEFT_MID, 12, 0);
    s_hdr_batt = mk_label(hdr, "--", C_SUB);
    lv_obj_align(s_hdr_batt, LV_ALIGN_RIGHT_MID, -12, 0);

    // 底部按键提示
    lv_obj_t *foot = lv_obj_create(s_scr);
    lv_obj_set_size(foot, 240, FOOT_H);
    lv_obj_set_pos(foot, 0, FOOT_Y);
    flat(foot, C_BG, 0);
    lv_obj_set_style_border_width(foot, 1, 0);
    lv_obj_set_style_border_color(foot, lv_color_hex(C_LINE), 0);
    lv_obj_set_style_border_side(foot, LV_BORDER_SIDE_TOP, 0);
    s_hint = mk_label(foot, "", C_FAINT);
    lv_label_set_long_mode(s_hint, LV_LABEL_LONG_DOT);
    lv_obj_set_width(s_hint, 232);
    lv_obj_set_style_text_align(s_hint, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_center(s_hint);

    lv_screen_load(s_scr);
    unlock();
}

void ui_set_header(const char *title)
{
    lock();
    lv_label_set_text(s_hdr_title, title);
    unlock();
}

void ui_set_battery(int soc)
{
    char buf[16];
    uint32_t col;
    if (soc < 0) {
        snprintf(buf, sizeof(buf), "--");
        col = C_SUB;
    } else {
        snprintf(buf, sizeof(buf), "%d%%", soc);
        col = soc <= 20 ? C_ERR : (soc <= 40 ? C_WARN : C_OK);
    }
    lock();
    lv_label_set_text(s_hdr_batt, buf);
    lv_obj_set_style_text_color(s_hdr_batt, lv_color_hex(col), 0);
    unlock();
}

void ui_set_hint(const char *text)
{
    lock();
    lv_label_set_text(s_hint, text);
    unlock();
}

void ui_set_busy(const char *msg)
{
    lock();
    if (!msg) {
        if (s_busy) { lv_obj_delete(s_busy); s_busy = NULL; s_busy_lbl = NULL; }
    } else if (!s_busy) {
        s_busy = lv_obj_create(s_scr);
        lv_obj_set_size(s_busy, 240, 320);
        lv_obj_set_pos(s_busy, 0, 0);
        flat(s_busy, C_BG, 0);
        lv_obj_set_style_bg_opa(s_busy, LV_OPA_90, 0);
        lv_obj_set_flex_flow(s_busy, LV_FLEX_FLOW_COLUMN);
        lv_obj_set_flex_align(s_busy, LV_FLEX_ALIGN_CENTER,
                              LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_set_style_pad_row(s_busy, 14, 0);

        lv_obj_t *sp = lv_spinner_create(s_busy);
        lv_spinner_set_anim_params(sp, 1100, 220);
        lv_obj_set_size(sp, 34, 34);
        lv_obj_set_style_arc_width(sp, 3, LV_PART_MAIN);
        lv_obj_set_style_arc_color(sp, lv_color_hex(C_LINE), LV_PART_MAIN);
        lv_obj_set_style_arc_width(sp, 3, LV_PART_INDICATOR);
        lv_obj_set_style_arc_color(sp, lv_color_hex(C_ACCENT), LV_PART_INDICATOR);

        s_busy_lbl = mk_label(s_busy, msg, C_TEXT);
        lv_label_set_long_mode(s_busy_lbl, LV_LABEL_LONG_WRAP);
        lv_obj_set_width(s_busy_lbl, 200);
        lv_obj_set_style_text_align(s_busy_lbl, LV_TEXT_ALIGN_CENTER, 0);
    } else if (s_busy_lbl) {
        lv_label_set_text(s_busy_lbl, msg);
    }
    unlock();
}

static void body_clear(void)
{
    if (s_body) { lv_obj_delete(s_body); s_body = NULL; }
    s_row_n = 0; s_sel = 0;
    s_state_lbl = s_ans_cont = s_ans_lbl = NULL;
}

static lv_obj_t *body_create(bool scrollable)
{
    body_clear();
    s_body = lv_obj_create(s_scr);
    lv_obj_set_size(s_body, 240, BODY_H);
    lv_obj_set_pos(s_body, 0, BODY_Y);
    lv_obj_set_style_bg_opa(s_body, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(s_body, 0, 0);
    lv_obj_set_style_shadow_width(s_body, 0, 0);
    lv_obj_set_style_radius(s_body, 0, 0);
    lv_obj_set_style_pad_all(s_body, 8, 0);
    lv_obj_set_style_pad_row(s_body, 6, 0);
    lv_obj_set_flex_flow(s_body, LV_FLEX_FLOW_COLUMN);
    if (scrollable) {
        lv_obj_add_flag(s_body, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_scroll_dir(s_body, LV_DIR_VER);
        style_scrollbar(s_body);
    } else {
        lv_obj_remove_flag(s_body, LV_OBJ_FLAG_SCROLLABLE);
    }
    return s_body;
}

// ---------- 列表（工作区/会话共用） ----------

// 选中态=强调蓝填充+白字；普通态=卡片底+浅字。行内子对象顺序：[主文本, 右附注?]
static void style_row(lv_obj_t *row, bool sel)
{
    lv_obj_set_style_bg_color(row, lv_color_hex(sel ? C_ACCENT : C_CARD), 0);
    lv_obj_set_style_bg_opa(row, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(row, 0, 0);
    lv_obj_set_style_shadow_width(row, 0, 0);
    lv_obj_set_style_radius(row, 8, 0);
    lv_obj_t *lm = lv_obj_get_child(row, 0);
    lv_obj_t *lr = lv_obj_get_child(row, 1);
    if (lm) lv_obj_set_style_text_color(lm, lv_color_hex(sel ? C_ON_ACC : C_TEXT), 0);
    if (lr) lv_obj_set_style_text_color(lr, lv_color_hex(sel ? C_ACC_SUB : C_SUB), 0);
}

void ui_show_list(const ui_row_t *rows, int n, int sel)
{
    lock();
    body_create(n > VISIBLE_ROWS);
    if (n > ROWS_MAX) n = ROWS_MAX;
    if (n <= 0) {
        lv_obj_t *empty = mk_label(s_body, "（无）", C_FAINT);
        lv_obj_set_style_pad_top(empty, 36, 0);
        unlock();
        return;
    }
    for (int i = 0; i < n; i++) {
        lv_obj_t *row = lv_obj_create(s_body);
        lv_obj_set_size(row, ROW_W, ROW_H);
        lv_obj_remove_flag(row, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_style_pad_hor(row, 10, 0);
        lv_obj_set_style_pad_ver(row, 0, 0);

        lv_obj_t *lm = mk_label(row, rows[i].main ? rows[i].main : "", C_TEXT);
        lv_label_set_long_mode(lm, LV_LABEL_LONG_DOT);
        lv_obj_set_width(lm, 140);
        lv_obj_align(lm, LV_ALIGN_LEFT_MID, 0, 0);

        if (rows[i].right && rows[i].right[0]) {
            lv_obj_t *lr = mk_label(row, rows[i].right, C_SUB);
            lv_label_set_long_mode(lr, LV_LABEL_LONG_DOT);
            lv_obj_set_width(lr, 56);
            lv_obj_set_style_text_align(lr, LV_TEXT_ALIGN_RIGHT, 0);
            lv_obj_align(lr, LV_ALIGN_RIGHT_MID, 0, 0);
        }
        style_row(row, i == sel);
        s_rows[i] = row;
    }
    s_row_n = n;
    s_sel = sel;
    lv_obj_scroll_to_view(s_rows[sel], LV_ANIM_OFF);
    unlock();
}

void ui_list_move(int delta)
{
    lock();
    if (s_row_n > 0) {
        style_row(s_rows[s_sel], false);
        s_sel = (s_sel + delta + s_row_n) % s_row_n;
        style_row(s_rows[s_sel], true);
        lv_obj_scroll_to_view(s_rows[s_sel], LV_ANIM_ON);
    }
    unlock();
}

int ui_list_sel(void)   { return s_sel; }
int ui_list_count(void) { return s_row_n; }

// ---------- 详情页 ----------

void ui_show_detail(const char *title)
{
    lock();
    body_create(false);
    lv_obj_set_style_pad_row(s_body, 8, 0);

    lv_obj_t *t = mk_label(s_body, title, C_TEXT);
    lv_label_set_long_mode(t, LV_LABEL_LONG_DOT);
    lv_obj_set_width(t, 220);

    s_state_lbl = mk_label(s_body, "就绪", C_WARN);
    lv_label_set_long_mode(s_state_lbl, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_align(s_state_lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_width(s_state_lbl, 220);

    s_ans_cont = lv_obj_create(s_body);
    lv_obj_set_width(s_ans_cont, 220);
    lv_obj_set_flex_grow(s_ans_cont, 1);
    lv_obj_set_style_bg_color(s_ans_cont, lv_color_hex(C_CARD), 0);
    lv_obj_set_style_bg_opa(s_ans_cont, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(s_ans_cont, 0, 0);
    lv_obj_set_style_shadow_width(s_ans_cont, 0, 0);
    lv_obj_set_style_radius(s_ans_cont, 8, 0);
    lv_obj_set_style_pad_all(s_ans_cont, 8, 0);
    lv_obj_set_scroll_dir(s_ans_cont, LV_DIR_VER);
    style_scrollbar(s_ans_cont);

    s_ans_lbl = mk_label(s_ans_cont, "（回复显示在这里）", C_FAINT);
    lv_label_set_long_mode(s_ans_lbl, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(s_ans_lbl, 202);

    unlock();
}

void ui_set_state(const char *state_text)
{
    lock();
    if (s_state_lbl) {
        lv_label_set_text(s_state_lbl, state_text);
        lv_obj_set_style_text_color(s_state_lbl, lv_color_hex(C_WARN), 0);
    }
    unlock();
}

void ui_set_answer(const char *text)
{
    lock();
    if (s_ans_lbl) {
        lv_label_set_text(s_ans_lbl, text && text[0] ? text : "（无文本回复）");
        lv_obj_set_style_text_color(s_ans_lbl, lv_color_hex(C_TEXT), 0);
        lv_obj_scroll_to_y(s_ans_cont, 0, LV_ANIM_OFF);
    }
    unlock();
}

void ui_scroll_answer(int delta_pixels)
{
    lock();
    if (s_ans_cont) lv_obj_scroll_by(s_ans_cont, 0, delta_pixels, LV_ANIM_ON);
    unlock();
}

void ui_set_error(const char *msg)
{
    lock();
    if (s_state_lbl) {
        char buf[160];
        snprintf(buf, sizeof(buf), "出错：%s", msg ? msg : "未知");
        lv_label_set_text(s_state_lbl, buf);
        lv_obj_set_style_text_color(s_state_lbl, lv_color_hex(C_ERR), 0);
    }
    unlock();
}
