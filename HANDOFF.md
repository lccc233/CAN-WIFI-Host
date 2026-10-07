# CAN-WIFI-Host（PC 上位机）HANDOFF

> 接手者从本文件读起。本项目**计划独立为一个正式项目**，本文按自足文档编写——
> 不需要翻 CAN-WIFI 固件仓库也能维护本工具；只有「协议契约」一节依赖固件侧事实。

## 项目定位

- CAN-WIFI 设备（ESP32-S3 CAN 总线监控器）的 **PC 串口配置工具**：
  切 STA/AP、改 SSID/密码、设 IP 规则（自动 .250/固定）、状态面板 + 串口终端。
- **独立 git 仓库**，与固件仓库 `../CAN-WIFI` 同级、互不包含。
  固件仓库只在 README/HANDOFF 里引用本目录。
- 技术栈：Python 3.8+ / tkinter / pyserial，**单文件 GUI**（canmon_gui.py）；
  发行物为 PyInstaller 单文件 exe（新电脑零依赖）。
- 版本：v0.1（尚未在界面/标签中体现版本号，见路线图 Phase 1）。

## 当前状态（2026-09-25）

- git：`main` 分支，提交 `7fc06e8`（feat: initial release）。
  **工作区有未提交改动**：全深色 UI 打磨（clam 主题 + PAL 调色板 + 布局重构），
  下次提交应包含 HANDOFF.md（本文件）与该改动。
- 验证情况：
  - 真机 COM8（ESP32-S3 USB-Serial/JTAG）链路测试通过：status JSON 与 info 文本
    解析逐字段一致
  - GUI 自动化冒烟通过：连接、状态面板 14 项、`ip auto` 应答判定、`reboot` 后
    自动刷新（启动日志经同一句柄无缝流入）
  - exe 打包 + 启动验证通过（深色 UI 版）
- 待人工回归：`mode ap|sta` 切换（确认对话框路径）、ssid/pass 真机重连。

## 协议契约（与 CAN-WIFI 固件 `main/serial_cli.c` 的耦合面）

改本工具前先读这节；固件改协议时同步改这里。

- 命令集：`help` `info` `status` `mode ap|sta` `ssid <名称>` `pass <密码>`
  `ip [auto|x.x.x.x]` `reboot`；行结束 `\r\n`；**设备逐字符回显、无提示符**。
- `status` 应答为**单行 JSON**（`\r\n` 结尾），字段：
  `mode(sta|ap) link(bool) ssid rssi channel ap_clients ip netmask gw mac
  ipmode(auto|static) static_ip mdns uptime_s can_ring can_total twai(string|null)
  tec rec(number|null)`
- 应答判定 = 内容匹配固件 verbatim 字符串（含全角标点），与到达顺序无关，
  可与静默轮询并发。关键 token：
  - ssid 成功 `SSID 已保存为 "` / 失败 `设置失败`
  - pass 成功 `密码已保存` / 失败 `设置失败`
  - ip auto 成功 `已切换为自动 .250`；固定成功 `已设置固定 IP `；失败 `IP 地址无效`
  - mode 成功 `已切换为 ` / 提示 `当前已是 `
  - reboot 成功 `设备即将重启`；未知命令 `未知命令 "`
- 旧固件无 `status`（回`未知命令`）→ 上位机自动退回**正则解析 info 文本**
  （模块级 `_RE_*` 一组，标签与全角标点 verbatim）。
- 本地校验与固件一致：SSID 1~32 字节、密码 8~63 字符、IPv4 格式、
  单条命令 ≤120 字节（固件行缓冲 128）。

## 关键坑（改代码前必读，全部真机踩过）

1. **USJ 必须断言 DTR**：串口 open 后 `ser.dtr=True, ser.rts=False`，否则设备丢弃
   主机输入。
2. **必须设 `write_timeout=1.0`**：USJ OUT 端点异常卡死时，无超时的 write 永久阻塞。
3. **S3 `esp_restart()` 对 USB-Serial/JTAG 只是 USB 总线复位，端口不掉、句柄有效**
   ——重启后启动日志从同一句柄无缝流出（idf.py monitor 跨重启不死同理）。
   因此重启流程 = 应答匹配后 3.5s 主动刷新状态（`_post_reboot_refresh`）；
   `SerialLink.begin_reconnect`（0.8s 重试 × 15s）仅作真掉线兜底，确认端口存活后
   `cancel_reconnect()` 恢复轮询。
4. **lost 事件带连接代数 gen**：`_on_link_lost` 收到后先 `link.close()` 关失效句柄
   （否则重连线程的 is_open 守卫永远跳过重开——初版真 bug），gen 不匹配的丢弃。
5. **VOFA+ 等串口工具占 COM8** 时，烧录/连接均报 `PermissionError(13)`；
   上位机对此有中文占用提示（匹配"拒绝访问/Access is denied/PermissionError"）。
6. **静默轮询**（2s status）的收发不进终端、不覆盖状态栏；只有手动刷新写
   「状态已更新」。别把轮询改成可见，会刷屏。
7. 打包/验证注意：GUI exe 继承 stdout 管道句柄，脚本里后台启动会导致管道不 EOF——
   启动验证用 `tasklist` + `taskkill //IM CANMonHost.exe //F` 收尾。

## 代码地图（canmon_gui.py，单文件 ~1100 行）

| 区块 | 内容 |
|------|------|
| 常量 / `PAL` / `FONT_*` | 协议参数、深色调色板、字体（改外观只动这里 + `_setup_style`） |
| `blank_state` / `state_from_json` / `parse_info_lines` | 状态模型：JSON 与 info 文本两条解析路径，产出同一 dict |
| `SerialLink` | 串口传输：读线程 + 事件队列(data/lost/reconnected/reconnect_failed) + 重启兜底重连 |
| `PendingCmd` | 在途命令：令牌 / json_mode / terminator 三种匹配模式 |
| `App._setup_style` / `_build_ui` | clam 深色主题；顶栏 + 状态卡片(WIFI/网络/设备) + 终端 + 配置卡片 + 状态栏 |
| `App._color_state` | 模式蓝 / 连接绿红黄 / TWAI 绿黄红 |
| `App._send` / `_handle_line` / `_check_timeouts` | 发送、内容匹配、超时；回显行识别（recent_sends） |

## 构建与发布

```
# 运行（开发）
pip install -r requirements.txt
python canmon_gui.py

# 打包发行
pip install pyinstaller
pyinstaller -F -w --name CANMonHost canmon_gui.py   # 产物 dist/CANMonHost.exe
```

- `build/`、`__pycache__/` 已 gitignore；`dist/CANMonHost.exe` **入库**（发行物随仓库走）。
- 打包后 exe 须启动验证（`tasklist` 见双进程属正常，单文件解压结构）。

## 独立化路线图（计划）

**Phase 1 —— 立项（下一步就做）**
- [ ] 提交深色 UI + HANDOFF（本文件）
- [ ] 打 `v0.1` 标签；窗口标题与「关于」处显示版本号（单处常量 `__version__`）
- [ ] 新增 `CHANGELOG.md`（Keep a Changelog 格式，从 v0.1 起记）

**Phase 2 —— 正式发行（短期）**
- [ ] 协议版本化：固件 `status` JSON 增加 `"proto":1` 字段，上位机按 proto 兼容
      （改协议不再靠猜）——需要固件侧配合一次小改
- [ ] 建远程仓库（GitHub），tag → Release 自动挂 exe（手工上传亦可起步）
- [ ] exe 图标与版本资源（PyInstaller `--icon` + version 文件）

**Phase 3 —— 功能扩展（按需，做前先论证）**
- [ ] CAN 监控页签：走设备 HTTP API（/api/messages 等）在工具内看报文——
      与固件网页功能重叠，是否值得做待定
- [ ] 多设备/配置档案：常用串口与参数记忆
- [ ] 固件升级辅助（集成 esptool 拖拽烧录）

**Phase 4 —— 工程化（规模触发再动）**
- [ ] 单文件拆包（serial_link.py / protocol.py / ui.py）——行数超 ~1500 或多人协作时
- [ ] build.bat 一键打包 + CI 自动出 exe
- [ ] 文件日志（排障留痕）
