# CAN-WIFI 上位机（串口配置工具）

对 [CAN-WIFI](../CAN-WIFI) 设备做 WiFi / 邮箱配置与状态查询的 Windows 桌面工具：
切换 STA/AP 模式、修改 WiFi 名称与密码、设置 IP 规则（自动 .250 / 固定 IP）、
设置邮件收件地址、导入邮箱 OAuth 授权、验证邮箱连接，实时查看设备状态与串口日志。

功能上等价于在串口终端里敲 `mode / ssid / pass / ip / info / mail / mailto / mailauth / mailcheck`
命令，不需要记命令、设备重启后自动重连。命令与响应格式以固件文档
[CAN-WIFI docs/serial-protocol.md](../CAN-WIFI/docs/serial-protocol.md) 为准。

当前版本 **v0.2**：适配 2026-10-07 起的邮件版固件协议（2047 字节命令行、邮箱命令组、
`status` 邮箱字段、长授权分片写入、`mailcheck` 异步结果）。对更旧的固件（无 `status`）
会自动退回解析 `info` 文本，邮箱区显示「旧固件未提供」。

## 运行方式

### 方式一：exe（推荐，零依赖）

两种发行物都在 `dist\`，v0.2 均已打包：

| 发行物 | 说明 |
|---|---|
| `dist\CANMonHost-od\`（+ `CANMonHost-od.zip`） | **推荐**。文件夹版，启动不解压，秒开；整个文件夹拷给对方即可 |
| `dist\CANMonHost.exe` | 单文件版，约 9.8MB；启动时要解压到 `%TEMP%`，在部分受限/沙箱环境会被拦（见下） |

已发布版本也可直接从 GitHub Release 下载（无需克隆仓库）：
<https://github.com/lccc233/CAN-WIFI-Host/releases>（当前 `v0.2`，含上述两个文件）。

**新电脑无需安装任何东西**（Python、pyserial 都已打进 exe）。Windows 仅限 64 位；
exe 未做代码签名，第一次运行若弹「无法验证发布者」，点「运行」即可。
如被杀毒软件误报，添加信任。

### 方式二：Python 脚本

需要 Python 3.8+（Windows 官网安装包默认自带 tkinter），再加唯一第三方依赖 pyserial：

```
pip install -r requirements.txt
python canmon_gui.py
```

## 使用步骤

1. 用 USB 线连接设备，启动本工具——ESP32 的 USB 串口（USB-Serial/JTAG）会自动
   排在列表第一位并选中；没有就点「刷新」。
2. 点「连接」。左侧状态面板约 2 秒内显示当前 WiFi 模式 / SSID / IP / CAN 状态。
3. 需要改配置时：
   - **切 STA/AP 模式**：选单选框 → 「应用」→ 设备保存并重启，工具自动重连；
   - **改 WiFi 名称 / 密码**：填输入框 → 「设置」→ 设备立即重连，无需重启；
   - **改 IP 规则**：选「自动 .250」（绑定当前网段的 xxx.xxx.xxx.250）或「手动」
     填固定 IP → 「设置 IP」。
   - **改收件邮箱**：填「收件邮箱」→ 「设置收件人」（保存到设备 NVS，立即生效）；
   - **导入邮箱授权**：把 `mailauth` 用的单行 JSON 粘进「授权 JSON」框 → 「导入授权」。
     这是首次配置才需要的一步，凭据由设备保存并自动续期；设备回显与工具日志都用 `*` 掩码，
     导入成功后输入框会自动清空；也可用固件仓库的 `tools/provision-mail.py --port COM8`；
   - **验证邮箱连接**：「验证连接」等于 `mailcheck`，只验证 HTTPS、邮箱身份和自动续期，
     不发邮件；先返回「正在验证…」，数十秒后以 `MAILCHECK OK/ERROR` 结束（绿灯/红灯显示在终端）。
4. 中间终端窗显示设备的全部串口输出（启动日志、命令应答）；蓝色 = 发出的命令，
   红色 = 错误应答（含 `MAILAUTH ERROR` / `MAILCHECK ERROR`），暗绿色 = 状态 JSON
   与 `MAILAUTH OK` / `MAILCHECK OK`（自动轮询的收发默认隐藏，点「刷新状态」可见）。
5. 「重启设备」按钮等于 `reboot` 命令（有确认对话框）。
6. 左侧状态面板的「邮箱」组显示收件邮箱与授权状态（`status.mail_to` /
   `mail_configured`）；「CAN缓冲」行显示 `can_total` 累计报文数——固件的 `can_ring`
   在零条快照查询下恒为 0，不能用来判断是否在收报文。

## 与固件的配合

- 依赖固件的 `status` 命令（单行 JSON）。邮件版固件（2026-10-07 起）另外返回
  `mail_from` / `mail_to` / `mail_configured`，工具据此填充邮箱状态组；
  对旧固件会自动退回解析 `info` 文本，功能不受影响（邮箱区显示「旧固件未提供」）。
- 邮箱命令需要邮件版固件；旧固件会回 `未知命令 "mailauth"`，工具会提示升级固件。
- 输入校验与固件一致：SSID 1~32 字节、密码 8~63 字节、IPv4 格式；
  收件邮箱 1~127 字节（单个 ASCII 地址，本地部分非空、域名含点且不以点结尾）；
  授权 JSON 必须 `email` 精确为 `espdata@agent.qq.com`、`client_id` <128 字节、
  `refresh_token` <1024 字节。
- 单条命令最长 **2047 字节**（旧固件行缓冲为 128 字节，本工具 v0.2 起按 2047 录入）；
  超过 64 字节的命令自动按 32 字节 / 30ms 分片写入并放到后台线程，约 1KB 的授权
  也能完整送达（这是设备 20ms 轮询一次 FIFO 的节奏决定的）。
- `mailcheck` 是异步命令：启动应答与 `MAILCHECK OK/ERROR` 结果分开到达，工具最多等 120 秒；
  超时只提示，不代表设备任务已停止。
- 运行时间用 `status.uptime_s`；`info` 的「运行」行秒字段未按 60 归一化，工具已按
  固件实际含义换算（天×86400 + 小时×3600 + 剩余秒数）。

## 故障排查

| 现象 | 处理 |
|------|------|
| 打开串口提示"拒绝访问" | 串口被占用——关闭 idf.py monitor 或其他串口工具后重试 |
| 连上但敲命令没反应 | 确认设备固件含串口配置台；工具已自动断言 DTR，一般无需手工处理 |
| 「设备重启中…」后一直连不上 | 15 秒自动重连窗口过后手动点「连接」；确认设备没断电 |
| 发送失败提示端点异常 | USB 枚举卡死，重新插拔 USB 线 |
| 状态一直是 `info` 文本解析 | 固件较旧（无 `status` 命令），建议升级固件 |
| 点「查看邮箱」回 `未知命令 "mail"` | 固件不是邮件版（2026-10-07 之前），需重烧邮件版固件 |
| 「导入授权」提示 JSON 无效 | 检查是否单行完整、字段名大小写（`email`/`client_id`/`refresh_token`）、发件邮箱是否为 `espdata@agent.qq.com` |
| 授权导入成功但状态仍「未配置」 | 等下一次状态轮询；仍不对则重新导入，或先看终端里的 `MAILAUTH ERROR` 原因 |
| 「验证连接」长时间没有结果 | `mailcheck` 需 STA 联网且已授时，可能要数十秒；超时提示后不要连续重复点，任务可能仍在设备上运行 |

## 重新打包 exe（改过脚本后）

推荐用**文件夹版（onedir）**——启动时不需要往 `%TEMP%` 解压，因此不会踩到
"Could not create temporary directory!" 这类启动失败（本机已实测，见 HANDOFF 关键坑 8）：

```
pip install pyinstaller
pyinstaller --noconfirm --onedir --windowed --name CANMonHost canmon_gui.py
```

产物在 `dist\CANMonHost\`，整个文件夹拷给对方即可运行（打包成 zip 更好传）。

**单文件版（onefile）**只有一个 exe、体积更小，但启动时要把 900 多个依赖文件解压到
`%TEMP%`，在部分机器或安全软件环境下会被拦，表现就是双击后弹 Error 框或毫无反应：

```
pyinstaller -F -w --name CANMonHost canmon_gui.py
```

产物在 `dist\CANMonHost.exe`。
