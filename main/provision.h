// main/provision.h —— SoftAP 网页配网
#pragma once

// 进入配网模式：开热点 ZCode-Badge-Setup + 本地网页表单（192.168.4.1）。
// 阻塞直至用户在网页提交配置；提交后写 NVS、清配网标志并 esp_restart()。
// 仅在开机时由 app_main 调用（配网是独立的一次启动模式，避免 AP/STA 混初始化）。
void provision_run(void);
