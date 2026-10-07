# CAN-WIFI-Host（PC 上位机）HANDOFF

> 接手者从本文件读起。本项目**计划独立为一个正式项目**，本文按自足文档编写——
> 不需要翻 CAN-WIFI 固件仓库也能维护本工具；只有「协议契约」一节依赖固件侧事实。

## 项目定位

- CAN-WIFI 设备（ESP32-S3 CAN 总线监控器）的 **PC 串口配置工具**：
  切 STA/AP、改 SSID/密码、设 IP 规则（自动 .250/固定）、配置收件邮箱与导入邮箱授权、
  邮箱自检、状态面板 + 串口终端。
- **独立 git 仓库**，与固件仓库 `../CAN-WIFI` 同级、互不包含。
  固件仓库只在 README/HANDOFF 里引用本目录。
- 技术栈：Python 3.8+ / tkinter / pyserial，**单文件 GUI**（canmon_gui.py）；
  发行物为 PyInstaller 单文件 exe（新电脑零依赖）。
- 版本：**v0.2**（`__version__` 常量，显示在窗口标题；v0.1 为纯 WiFi 配置版）。

## 当前状态（2026-10-07，v0.2 协议适配）

- 触发：固件侧发布邮件版协议（`CAN-WIFI/docs/serial-protocol.md`，2026-10-07：
  `mail`/`mailto`/`mailauth`/`mailcheck`、`status` 邮箱字段、行缓冲 128→2048）。
  本工具按该文档升级协议层与界面，**不依赖固件仓库**（协议事实全部落在本节与
  「协议契约」里，改工具不用翻固件源码）。
- 改动：
  - 命令上限 120→**2047 字节**；超过 64 字节的命令按 32 字节/30ms 分片、放后台线程写
    （设备每 20ms 才轮询一次 FIFO，1KB 授权整行直写会丢字节）；
  - 新命令与应答令牌：`mail`、`mailto`、`mailauth`、`mailcheck`（异步双阶段）；
  - `status` 解析增加 `mail_from/mail_to/mail_configured`，状态面板新增「邮箱」组；
  - `mailauth` 凭据保护：设备回显与工具日志/状态栏一律 `*` 掩码，导入成功后清空输入框；
  - 收件邮箱与授权 JSON 先做**本地校验**（与固件同位规则），不合法不发命令；
  - `can_ring` 恒为 0 → 面板改显示 `can_total` 累计值；`info` 的「运行」行秒字段未归一化，
    解析改为「天×86400 + 小时×3600 + 剩余秒数」；
  - 窗口按屏幕高度自适应（邮箱区多出几行），标题带版本号。
- 验证（本机真机 COM8 + 隐藏窗口自动化）：
  - 离线自测：status/邮箱字段解析、info 运行时间换算、收件邮箱 20 组正反用例、
    授权 JSON 7 组拒绝用例、掩码、9 条应答令牌、分片写入（字节完整性/片大小/节奏）、
    隐藏窗口 UI 冒烟 —— 全部通过；
  - 实机联调（本工具自己的 `SerialLink`/`App` 代码路径）：status 邮箱字段入库、
    307 字节分片命令得到 JSON 应答、`mail` 三行进终端、假凭据被固件拒绝且终端无明文、
    非法收件邮箱被本地拦截 —— 全部通过；
  - 未做：`mailcheck` 真机触发（会轮换 refresh token，留给人工）、`mode/ssid/pass/ip`
    成功路径（会改设备配置）、exe 重新打包。
- 待人工回归：邮箱按钮的真机点击路径（导入真实授权 → 验证连接 → 收件箱确认）。

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

改本工具前先读这节；固件改协议时同步改这里。**协议权威文档：固件仓库
`docs/serial-protocol.md`（含每个命令的逐字符响应表）。**

- 命令集：`help` `?` `info` `status` `mode ap|sta` `ssid <名称>` `pass <密码>`
  `ip [auto|x.x.x.x]` `mail` `mailto <邮箱>` `mailauth <JSON>` `mailcheck` `reboot`；
  行结束 `\r\n`；**设备逐字符回显、无提示符**；命令与参数区分大小写。
- 单行上限 **2047 字节**（固件 `CLI_LINE_MAX=2048`），超长整行被拒绝并回
  `命令过长，已拒绝执行`；旧固件（≤2026-09-25）行缓冲只有 128 字节。
- 长命令必须分片写：设备每 20ms 轮询一次 FIFO、每次最多 64 字节 →
  本工具按 32 字节/30ms 写（与固件 `tools/provision-mail.py` 一致），放后台线程。
- `status` 应答为**单行 JSON**（`\r\n` 结尾）。WiFi/CAN 字段：
  `mode(sta|ap) link(bool) ssid rssi channel ap_clients ip netmask gw mac
  ipmode(auto|static) static_ip mdns uptime_s can_ring can_total twai(string|null)
  tec rec(number|null)`；邮件版追加
  `mail_from(mail_fixed) mail_to mail_configured(bool)`。
  `can_ring` 恒为 0（零条快照查询），收报文数看 `can_total`（开机或 `POST /api/clear` 后归零）。
- 应答判定 = 内容匹配固件 verbatim 字符串（含全角标点），与到达顺序无关，
  可与静默轮询并发。关键 token：
  - ssid 成功 `SSID 已保存为 "` / 失败 `设置失败`
  - pass 成功 `密码已保存` / 失败 `设置失败`
  - ip auto 成功 `已切换为自动 .250`；固定成功 `已设置固定 IP `；失败 `IP 地址无效`
  - mode 成功 `已切换为 ` / 提示 `当前已是 `
  - reboot 成功 `设备即将重启`；未知命令 `未知命令 "`
  - mail 成功 `发件邮箱: `；mailto 成功 `收件邮箱已保存为 ` / 失败 `收件邮箱设置失败`
  - mailauth 成功 `MAILAUTH OK：` / 失败 `MAILAUTH ERROR：`
  - mailcheck 启动 `正在验证设备邮箱连接` / 无法启动 `邮箱验证未启动`；
    异步结果 `MAILCHECK OK：` / `MAILCHECK ERROR：`（与启动应答分离，最长等 120s）
- 旧固件无 `status`（回`未知命令`）→ 上位机自动退回**正则解析 info 文本**
  （模块级 `_RE_*` 一组，标签与全角标点 verbatim）；info 的「运行」行秒字段未按 60
  归一化，`uptime_from_text()` 按「天×86400+小时×3600+剩余秒数」换算，不要逐项相加。
- 本地校验与固件一致：SSID 1~32 字节、密码 8~63 字节、IPv4 格式；
  收件邮箱 1~127 字节且字符集为 ASCII 字母数字 + `.!#$%&'*+-/=?^_\`{|}~@`，
  单个 `@`、本地部分非空、域名含点且不以点结尾；授权 JSON 的 `email` 必须精确等于
  `espdata@agent.qq.com`、`client_id` <128 字节、`refresh_token` <1024 字节，
  字段名区分大小写。
- **凭据不许落明文**：`mailauth` 的参数在设备侧逐字节回显为 `*`；本工具用
  `mask_secret()` 掩码一切会显示/记录命令文本的地方（终端回显匹配、超时提示、
  发送失败提示），成功后清空输入框。新增涉及该命令的消息时务必走 `PendingCmd.display`。

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
8. **onefile 版在本机（DSH 会话内）实测起不来**（2026-10-07，非本工具代码问题）：
   onefile 启动时必须先在 `%TEMP%` 建 `_MEIxxxxxx` 再解压 900+ 文件；DSH 沙箱把这个
   目录树标成低完整性并带 `Everyone:(DENY)(DC)` 继承 ACE，实测解压出的 `_MEI*` 目录里
   **0 个文件**、进程弹 `Error` 框，之后连 `Remove-Item` 都被拒（要管理员 `takeown` 才删得掉）。
   `TMP`/`TEMP` 指到项目内目录同样失败（同一沙箱 ACL）；最小复现：只 `import tkinter` 的
   hello.py 用 `-F` 打包也一样报错 → 与 canmon_gui.py 无关。
   **注意这是"代理会话里跑不起来"，不等于用户双击起不来**：本机正常双击未证实；
   `dist\CANMonHost.exe`（onefile）内容已用 PyInstaller 归档读取核对过，是 v0.2 代码。
   结论：发行仍推荐 `--onedir`（启动不解压，已实测秒开）；
   若必须用单文件，可在普通用户会话里双击验证，或加 `--runtime-tmpdir <固定目录>` 绕开 `%TEMP%`。
   另外新 exe 未做代码签名，经 ShellExecute 打开会弹「无法验证发布者」提示，点「运行」即可。
9. **1KB 级命令不能整行直写**（2026-10-07 邮件版联调）：`mailauth` 授权约 1KB，
   设备 CLI 每 20ms 才取一次 FIFO（单次 ≤64 字节），整行 `ser.write()` 会丢字节，
   表现为 `MAILAUTH ERROR：ESP_ERR_INVALID_ARG`。本工具在 `SerialLink.send_line`
   里对 >64 字节的行走 32 字节/30ms 分片，并放到后台线程（否则界面卡约 1s），
   失败经 `("send_error", …)` 事件回报 UI。
10. **`mailcheck` 是两段式应答**：先回 `正在验证设备邮箱连接…`，数十秒后才输出
    `MAILCHECK OK/ERROR`。工具在第一段成功后挂一条 `kind="mailcheck"`、
    超时 120s 的观察命令等结果；超时只提示"可能仍在运行"，**不得**据此认为失败或
    立刻重发（设备端 `s_sending` 会拒绝并发，返回 `ESP_ERR_INVALID_STATE`）。
11. **`mailauth` 不能用回显匹配判断已发出**：设备把参数回显成 `*`，
    `line == raw` 永远不成立（这是合规行为，不是 bug）；同时保证本地任何日志/状态栏
    都走 `mask_secret()`，凭据不进终端、不进超时提示。
12. **`can_ring` 永远是 0**：固件用 `can_get_snapshot(NULL, 0, …)` 查询，复制条数必为 0；
    判断是否在收报文要看 `can_total`（累计计数）。`info` 的 `CAN缓冲:` 行同因恒为 0。
13. **`info` 的「运行」行秒字段未按 60 归一化**（固件 `print_uptime` 少一次 `%60`）：
    比如 `uptime_s=868` 显示 `14分867秒`。解析要用 `天×86400 + 小时×3600 + 剩余秒数`；
    正常运行时间优先用 `status.uptime_s`。

## 代码地图（canmon_gui.py，单文件 ~1500 行）

| 区块 | 内容 |
|------|------|
| 常量 / `PAL` / `FONT_*` | 协议参数（含 2047 上限、分片节奏、邮箱上限）、深色调色板、字体（改外观只动这里 + `_setup_style`） |
| `blank_state` / `state_from_json` / `parse_info_lines` / `uptime_from_text` | 状态模型：JSON 与 info 文本两条解析路径，产出同一 dict（含邮箱字段与运行时间归一化） |
| `valid_ipv4` / `valid_mail_address` / `validate_mailauth_json` / `mask_secret` | 与固件同位的本地校验 + 凭据掩码 |
| `SerialLink` | 串口传输：读线程 + 事件队列(data/lost/reconnected/reconnect_failed/send_error) + 重启兜底重连 + 长命令分片写 |
| `PendingCmd` | 在途命令：令牌 / json_mode / terminator 三种匹配模式；`display` 为掩码后的命令名 |
| `App._setup_style` / `_build_ui` | clam 深色主题；顶栏 + 状态卡片(WIFI/网络/设备/邮箱) + 终端 + 配置卡片(WiFi/IP/邮箱) + 状态栏 |
| `App._color_state` / `_classify_line` | 模式蓝 / 连接绿红黄 / TWAI 绿黄红 / 授权绿黄灰；终端行 JSON 与 OK 绿、错误红 |
| `App._send` / `_handle_line` / `_check_timeouts` | 发送（长命令后台分片）、内容匹配、超时；回显行识别（recent_sends） |
| `App._do_mail` / `_set_mailto` / `_import_mailauth` / `_mailcheck` | 邮箱四个动作与各自的应答回调（含 mailcheck 两段式观察命令） |

## 构建与发布

```
# 运行（开发）
pip install -r requirements.txt
python canmon_gui.py

# 打包发行（推荐 onedir，启动不解压到 %TEMP%，见关键坑 8）
pip install pyinstaller
pyinstaller --noconfirm --onedir --windowed --name CANMonHost canmon_gui.py
# 产物 dist/CANMonHost/  （整个文件夹分发；dist/CANMonHost-od.zip 为压缩包）

# 单文件版（体积小，但部分机器上会被拦，见关键坑 8）
pyinstaller -F -w --name CANMonHost canmon_gui.py   # 产物 dist/CANMonHost.exe
```

- `build/`、`__pycache__/` 已 gitignore；`dist/CANMonHost.exe` **入库**（发行物随仓库走）。
- 打包后 exe 须启动验证（`tasklist` 见双进程属正常，单文件解压结构）。

## 独立化路线图（计划）

**Phase 1 —— 立项（下一步就做）**
- [ ] 提交深色 UI + HANDOFF（本文件）
- [x] 窗口标题显示版本号（单处常量 `__version__`，v0.2 起）；「关于」对话框仍未做
- [x] 新增 `CHANGELOG.md`（Keep a Changelog 格式，从 v0.1 起记）
- [ ] 打 `v0.1` / `v0.2` 标签

**Phase 1.5 —— 协议适配（2026-10-07 已完成，见上）**
- [x] 按固件 `docs/serial-protocol.md` 升级：单行上限 120 → 2047 字节、邮箱命令组、
      `status` 邮箱字段、分片写、`mailcheck` 异步结果、凭据掩码
- [x] 真机（COM8）与隐藏窗口自动化验证
- [ ] 人工回归：导入真实授权 → `mailcheck` → 收件箱确认（授权导入会轮换 refresh token，
      不要在别的客户端同时续期）

**Phase 2 —— 正式发行（短期）**
- [ ] 协议版本化：固件 `status` JSON 增加 `"proto":1` 字段，上位机按 proto 兼容
      （改协议不再靠猜）——需要固件侧配合一次小改；本工具已能忽略未知字段
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
