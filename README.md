# CAN-WIFI 上位机（串口配置工具）

对 [CAN-WIFI](../CAN-WIFI) 设备做 WiFi 配置与状态查询的 Windows 桌面工具：
切换 STA/AP 模式、修改 WiFi 名称与密码、设置 IP 规则（自动 .250 / 固定 IP）、
实时查看设备状态与串口日志。

功能上等价于在串口终端里敲 `mode / ssid / pass / ip / info` 命令，
不需要记命令、设备重启后自动重连。

## 运行方式

### 方式一：exe（推荐，零依赖）

双击 `dist\CANMonHost.exe` 即可，**新电脑无需安装任何东西**（Python、pyserial
都已打进 exe）。Windows 仅限 64 位；如被杀毒软件拦截，添加信任即可（PyInstaller
单文件 exe 的常见误报）。

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
4. 中间终端窗显示设备的全部串口输出（启动日志、命令应答）；蓝色 = 发出的命令，
   红色 = 错误应答，暗绿色 = 状态 JSON（自动轮询的收发默认隐藏，点「刷新状态」可见）。
5. 「重启设备」按钮等于 `reboot` 命令（有确认对话框）。

## 与固件的配合

- 依赖固件的 `status` 命令（单行 JSON），**CAN-WIFI 固件 2026-09-24 之后烧录的版本
  均支持**；对旧固件工具会自动退回解析 `info` 文本，功能不受影响。
- 输入校验与固件一致：SSID 1~32 字节、密码 8~63 字符、IPv4 格式；
  单条命令最长 120 字节（固件行缓冲 128）。

## 故障排查

| 现象 | 处理 |
|------|------|
| 打开串口提示"拒绝访问" | 串口被占用——关闭 idf.py monitor 或其他串口工具后重试 |
| 连上但敲命令没反应 | 确认设备固件含串口配置台；工具已自动断言 DTR，一般无需手工处理 |
| 「设备重启中…」后一直连不上 | 15 秒自动重连窗口过后手动点「连接」；确认设备没断电 |
| 发送失败提示端点异常 | USB 枚举卡死，重新插拔 USB 线 |
| 状态一直是 `info` 文本解析 | 固件较旧（无 `status` 命令），建议升级固件 |

## 重新打包 exe（改过脚本后）

```
pip install pyinstaller
pyinstaller -F -w --name CANMonHost canmon_gui.py
```

产物在 `dist\CANMonHost.exe`。
