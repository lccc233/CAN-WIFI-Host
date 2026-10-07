#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CAN-WIFI 上位机 —— 串口配置工具

通过串口（ESP32 USB-Serial/JTAG 或 UART 桥）对 CAN-WIFI 设备做 WiFi 配置与状态查询。
命令协议见 CAN-WIFI 固件 main/serial_cli.c：
  help / info / status / mode ap|sta / ssid <名称> / pass <密码> / ip [auto|x.x.x.x] / reboot
  行结束符 \\r\\n；设备逐字符回显、无提示符；status 应答为单行 JSON
  （旧固件无 status 命令时自动退回解析 info 文本）。

串口层的几个关键点（与固件联调得出的坑）：
  - USB-Serial/JTAG 必须断言 DTR 设备才接收主机输入（open 后置 dtr=True）；
  - write_timeout 必须设置：USJ OUT 端点异常卡死时，无超时的 write 会永久阻塞；
  - 设备重启（mode/reboot）会掉口，用后台线程按 1s 间隔自动重开。
"""

import json
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror("缺少依赖", "未安装 pyserial。\n\n请先执行：\n    pip install pyserial")
    sys.exit(1)

# ================= 常量 =================

USJ_VID, USJ_PID = 0x303A, 0x1001        # ESP32-S3 USB-Serial/JTAG
CONNECT_BAUD = 115200
LINE_END = "\r\n"
UI_DRAIN_MS = 50                          # 串口事件排空周期
TIMEOUT_S = 2.5                           # 命令应答超时
POLL_MS = 2000                            # status 自动轮询周期
CMD_MAX_BYTES = 120                       # 固件行缓冲 128 字节，留余量
RECONNECT_WINDOW_S = 15                   # 设备重启后的自动重连窗口

# ================= 深色主题（工业风） =================

FONT_UI = ("Microsoft YaHei UI", 9)
FONT_SMALL = ("Microsoft YaHei UI", 8)
FONT_TITLE = ("Microsoft YaHei UI", 10, "bold")
FONT_TERM = ("Consolas", 10)

PAL = {
    "bg":       "#16191E",   # 窗口底
    "bg1":      "#1F242C",   # 卡片底
    "bg2":      "#2A313C",   # 输入框/按钮底
    "bg3":      "#353E4B",   # 悬停
    "border":   "#39424F",
    "fg":       "#E6E9EE",
    "fg_dim":   "#9AA4B2",
    "accent":   "#3B82F6",   # 主色（连接/应用按钮、模式高亮）
    "accent_h": "#5A9BFF",
    "ok":       "#34D399",   # 已连接 / TWAI RUNNING
    "warn":     "#FBBF24",   # RECOVERING / AP 未启动
    "err":      "#F87171",   # 错误 / BUS_OFF
    "danger":   "#7F1D1D",   # 重启设备按钮
    "danger_h": "#991B1B",
    "term_bg":  "#121418",   # 终端与状态栏底
}


def now_ts():
    return time.strftime("%H:%M:%S")


def format_uptime(s):
    """与固件 print_uptime 保持一致的中文时长。"""
    if s is None:
        return "—"
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}天{h}小时{m}分{s}秒"
    if h:
        return f"{h}小时{m}分{s}秒"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def blank_state():
    return {
        "mode": None,          # 'sta' / 'ap'
        "link": None,          # True/False
        "ssid": "",
        "rssi": 0,
        "channel": 0,
        "ap_clients": 0,
        "ip": "", "netmask": "", "gw": "", "mac": "",
        "ipmode": None,        # 'auto' / 'static'
        "static_ip": "",
        "mdns": "",
        "uptime_s": None,
        "uptime_text": "",
        "can_ring": 0,
        "can_total": None,
        "twai": None,          # 'STOPPED'/'RUNNING'/... 或 None=未初始化
        "tec": None, "rec": None,
    }


def state_from_json(obj):
    st = blank_state()
    st["mode"] = obj.get("mode") if obj.get("mode") in ("sta", "ap") else None
    st["link"] = bool(obj.get("link"))
    st["ssid"] = str(obj.get("ssid") or "")
    st["rssi"] = int(obj.get("rssi") or 0)
    st["channel"] = int(obj.get("channel") or 0)
    st["ap_clients"] = int(obj.get("ap_clients") or 0)
    st["ip"] = str(obj.get("ip") or "")
    st["netmask"] = str(obj.get("netmask") or "")
    st["gw"] = str(obj.get("gw") or "")
    st["mac"] = str(obj.get("mac") or "")
    st["ipmode"] = obj.get("ipmode") if obj.get("ipmode") in ("auto", "static") else None
    st["static_ip"] = str(obj.get("static_ip") or "")
    st["mdns"] = str(obj.get("mdns") or "")
    st["uptime_s"] = int(obj.get("uptime_s") or 0)
    st["can_ring"] = int(obj.get("can_ring") or 0)
    st["can_total"] = int(obj.get("can_total") or 0)
    st["twai"] = obj.get("twai")
    st["tec"] = obj.get("tec")
    st["rec"] = obj.get("rec")
    return st


# info 文本回退解析（正则按固件 serial_cli.c 的 verbatim 标签，标签冒号为 ASCII，标点为全角）
_RE_MODE = re.compile(r"\s*模式[:：]\s*(.+)")
_RE_SSID = re.compile(r"\s*SSID[:：]\s*(.*)")
_RE_LINK_OK = re.compile(r"\s*状态[:：]\s*已连接（信号\s*(-?\d+)\s*dBm）")
_RE_LINK_DOWN = re.compile(r"\s*状态[:：]\s*未连接")
_RE_AP_OK = re.compile(r"\s*状态[:：]\s*运行中，已连接设备\s*(\d+)")
_RE_AP_DOWN = re.compile(r"\s*状态[:：]\s*未启动")
_RE_CH = re.compile(r"\s*信道[:：]\s*(\d+)")
_RE_IP = re.compile(r"\s*IP[:：]\s*(\S+)")
_RE_MASK = re.compile(r"\s*掩码[:：]\s*(\S+)")
_RE_GW = re.compile(r"\s*网关[:：]\s*(\S+)")
_RE_MAC = re.compile(r"\s*MAC[:：]\s*([0-9A-Fa-f:]+)")
_RE_IPRULE_AUTO = re.compile(r"\s*IP规则[:：]\s*自动绑定")
_RE_IPRULE_STATIC = re.compile(r"\s*IP规则[:：]\s*固定\s+(\S+)")
_RE_MDNS = re.compile(r"\s*mDNS[:：]\s*http://(.+?)\.local")
_RE_UPTIME = re.compile(r"\s*运行[:：]\s*(.+)")
_RE_CAN_RING = re.compile(r"\s*CAN缓冲[:：]\s*(\d+)")
_RE_TWAI = re.compile(r"\s*CAN[:：]\s*(\S+)（TEC=(\d+)\s+REC=(\d+)）")
_RE_TWAI_NA = re.compile(r"\s*CAN[:：]\s*未初始化")


def parse_info_lines(lines):
    st = blank_state()
    for line in lines:
        if (m := _RE_MODE.match(line)) is not None:
            st["mode"] = "ap" if m.group(1).startswith("AP") else "sta"
        elif (m := _RE_SSID.match(line)) is not None:
            st["ssid"] = m.group(1).strip()
        elif (m := _RE_LINK_OK.match(line)) is not None:
            st["link"] = True
            st["rssi"] = int(m.group(1))
        elif _RE_LINK_DOWN.match(line) is not None:
            st["link"] = False
        elif (m := _RE_AP_OK.match(line)) is not None:
            st["link"] = True
            st["ap_clients"] = int(m.group(1))
        elif _RE_AP_DOWN.match(line) is not None:
            st["link"] = False
        elif (m := _RE_CH.match(line)) is not None:
            st["channel"] = int(m.group(1))
        elif (m := _RE_IP.match(line)) is not None:
            st["ip"] = m.group(1)
        elif (m := _RE_MASK.match(line)) is not None:
            st["netmask"] = m.group(1)
        elif (m := _RE_GW.match(line)) is not None:
            st["gw"] = m.group(1)
        elif (m := _RE_MAC.match(line)) is not None:
            st["mac"] = m.group(1).upper()
        elif _RE_IPRULE_AUTO.match(line) is not None:
            st["ipmode"] = "auto"
        elif (m := _RE_IPRULE_STATIC.match(line)) is not None:
            st["ipmode"] = "static"
            st["static_ip"] = m.group(1)
        elif (m := _RE_MDNS.match(line)) is not None:
            st["mdns"] = m.group(1)
        elif (m := _RE_UPTIME.match(line)) is not None:
            st["uptime_text"] = m.group(1).strip()
        elif (m := _RE_CAN_RING.match(line)) is not None:
            st["can_ring"] = int(m.group(1))
        elif (m := _RE_TWAI.match(line)) is not None:
            st["twai"] = m.group(1)
            st["tec"] = int(m.group(2))
            st["rec"] = int(m.group(3))
        elif _RE_TWAI_NA.match(line) is not None:
            st["twai"] = None
    return st


def valid_ipv4(text):
    parts = text.strip().split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or not 0 <= int(p) <= 255:
            return False
    return True


# ================= 串口传输层 =================

class SerialLink:
    """串口传输：读线程 + 事件队列 + 重启自动重连。所有方法线程安全。"""

    def __init__(self, events):
        self.events = events            # queue.Queue，元素为 (kind, payload)
        self._ser = None
        self._port = None
        self._lock = threading.RLock()
        self._gen = 0
        self._stop_evt = None
        self.reconnect_active = False

    @property
    def is_open(self):
        with self._lock:
            return self._ser is not None

    @property
    def port(self):
        with self._lock:
            return self._port

    @property
    def generation(self):
        with self._lock:
            return self._gen

    @staticmethod
    def open_serial(port):
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = CONNECT_BAUD
        ser.bytesize = serial.EIGHTBITS
        ser.parity = serial.PARITY_NONE
        ser.stopbits = serial.STOPBITS_ONE
        ser.timeout = 0.1               # 读线程轮询周期
        ser.write_timeout = 1.0         # 防 USJ OUT 端点卡死时写挂死
        ser.open()
        try:
            ser.dtr = True              # USJ 断言 DTR 才接收主机输入
            ser.rts = False
        except Exception:
            pass
        return ser

    def open(self, port):
        with self._lock:
            self._stop_locked()
            ser = self.open_serial(port)
            self._ser = ser
            self._port = port
            self._gen += 1
            stop = threading.Event()
            self._stop_evt = stop
            gen = self._gen
        threading.Thread(target=self._rx_loop, args=(gen, stop), daemon=True).start()

    def _stop_locked(self):
        """持锁调用：停旧读线程并关旧句柄。"""
        self._gen += 1
        if self._stop_evt is not None:
            self._stop_evt.set()
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._stop_locked()

    def _rx_loop(self, gen, stop):
        while not stop.is_set():
            with self._lock:
                ser = self._ser
            if ser is None:
                return
            try:
                data = ser.read(512)
            except Exception:
                # 端口失效：设备重启中或已拔出（事件带 gen，供 UI 丢弃遗留事件）
                if self._gen == gen and not stop.is_set():
                    self.events.put(("lost", gen))
                return
            if data:
                self.events.put(("data", data))

    def send_line(self, text):
        with self._lock:
            ser = self._ser
            if ser is None:
                raise serial.SerialException("串口未连接")
            ser.write((text + LINE_END).encode("utf-8"))

    def begin_reconnect(self, window=RECONNECT_WINDOW_S):
        """设备即将重启：后台线程按 ~1s 间隔尝试重开同一串口，直到窗口超时。"""
        self.reconnect_active = True
        cancel = threading.Event()
        self._reconnect_cancel = cancel

        def loop():
            deadline = time.monotonic() + window
            while time.monotonic() < deadline and not cancel.is_set():
                time.sleep(0.8)
                if cancel.is_set():
                    break
                if self.is_open:
                    continue        # 掉口比预期慢（句柄由 lost 事件关闭），继续等
                try:
                    self.open(self.port)
                    self.reconnect_active = False
                    self.events.put(("reconnected", None))
                    return
                except Exception:
                    continue        # 设备尚未重新枚举，稍后再试
            self.reconnect_active = False
            if not cancel.is_set() and not self.is_open:
                self.events.put(("reconnect_failed", None))

        threading.Thread(target=loop, daemon=True).start()

    def cancel_reconnect(self):
        """手动断开时取消自动重连，避免后台线程又把口打开。"""
        self.reconnect_active = False
        evt = getattr(self, "_reconnect_cancel", None)
        if evt is not None:
            evt.set()


# ================= 命令应答匹配 =================

class PendingCmd:
    """
    一条已发出、等待应答的命令。内容匹配（与到达顺序无关），三种模式：
      - 令牌模式：应答行包含 ok/fail 关键字（固件 verbatim 字符串，含全角标点）
      - json_mode：应答行为 '{' 开头的单行 JSON；'未知命令' 表示旧固件
      - terminator：info 文本，收齐到 '=' 分隔结束行后整体解析
    """

    def __init__(self, cmd, kind="user", ok_tokens=(), fail_tokens=(),
                 on_done=None, timeout=TIMEOUT_S, json_mode=False,
                 terminator=False, silent=False):
        self.cmd = cmd.strip()
        self.kind = kind
        self.ok_tokens = ok_tokens
        self.fail_tokens = fail_tokens
        self.on_done = on_done
        self.deadline = time.monotonic() + timeout
        self.json_mode = json_mode
        self.terminator = terminator
        self.silent = silent
        self.lines = []

    def feed(self, line):
        """返回 (done, ok, payload, token)。"""
        if self.json_mode:
            if line.startswith("{"):
                try:
                    return True, True, json.loads(line), "json"
                except ValueError:
                    return True, False, line, None
            if "未知命令" in line:
                return True, False, "legacy", None
            return False, None, None, None
        if self.terminator:
            self.lines.append(line)
            s = line.strip()
            if s and set(s) == {"="} and 20 <= len(s) <= 40:
                return True, True, self.lines, "done"
            if "未知命令" in line:
                return True, False, line, None
            return False, None, None, None
        for t in self.fail_tokens:
            if t in line:
                return True, False, line, t
        for t in self.ok_tokens:
            if t in line:
                return True, True, line, t
        return False, None, None, None


# ================= 主界面 =================

class App:
    def __init__(self, root):
        self.root = root
        root.title("CAN-WIFI 上位机 — 串口配置工具")
        root.geometry("1000x680")
        root.minsize(880, 600)
        self._setup_style()
        self._center_window()

        self.events = queue.Queue()
        self.link = SerialLink(self.events)
        self.line_buf = b""
        self.pendings = []              # PendingCmd 列表
        self.recent_sends = []          # (原始命令, 显示文本, 截止时刻, silent)
        self.json_supported = True      # 旧固件无 status 命令时转 False
        self._port_map = {}
        self.mode_touched_at = 0.0      # 用户手选模式/IP 后 5s 内状态轮询不回写控件
        self.ip_touched_at = 0.0

        self._build_ui()
        self._rescan()
        root.after(UI_DRAIN_MS, self._drain_events)
        root.after(300, self._check_timeouts)
        root.after(POLL_MS, self._poll_loop)

    # ---------- 界面搭建 ----------

    def _setup_style(self):
        """clam 主题 + 深色调色板，所有控件统一走样式。"""
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.configure(bg=PAL["bg"])
        # Combobox 下拉列表（Tk 经典列表框，只能 option_add）
        self.root.option_add("*TCombobox*Listbox.background", PAL["bg2"])
        self.root.option_add("*TCombobox*Listbox.foreground", PAL["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", PAL["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")

        style.configure(".", background=PAL["bg"], foreground=PAL["fg"],
                        bordercolor=PAL["border"], lightcolor=PAL["bg1"],
                        darkcolor=PAL["bg1"], troughcolor=PAL["bg"],
                        focuscolor=PAL["accent"], selectbackground=PAL["accent"],
                        selectforeground="#FFFFFF", font=FONT_UI, arrowsize=12)
        style.configure("TFrame", background=PAL["bg"])
        style.configure("TLabel", background=PAL["bg"], foreground=PAL["fg"])
        style.configure("Dim.TLabel", background=PAL["bg"],
                        foreground=PAL["fg_dim"], font=FONT_SMALL)
        style.configure("Title.TLabel", background=PAL["bg"],
                        foreground=PAL["fg"], font=FONT_TITLE)
        style.configure("TSeparator", background=PAL["border"])

        # 卡片
        style.configure("Card.TFrame", background=PAL["bg1"])
        style.configure("Card.TLabel", background=PAL["bg1"], foreground=PAL["fg"])
        style.configure("Key.TLabel", background=PAL["bg1"],
                        foreground=PAL["fg_dim"], font=FONT_SMALL)
        style.configure("Cap.TLabel", background=PAL["bg1"],
                        foreground=PAL["accent"], font=FONT_SMALL)
        style.configure("Card.TLabelframe", background=PAL["bg1"], relief="solid",
                        borderwidth=1, bordercolor=PAL["border"],
                        lightcolor=PAL["border"], darkcolor=PAL["border"])
        style.configure("Card.TLabelframe.Label", background=PAL["bg"],
                        foreground=PAL["fg_dim"], font=FONT_SMALL)
        style.configure("Card.TSeparator", background=PAL["border"])

        # 按钮：普通 / 主色 / 危险
        style.configure("TButton", background=PAL["bg2"], foreground=PAL["fg"],
                        bordercolor=PAL["border"], relief="flat", padding=(10, 4))
        style.map("TButton",
                  background=[("disabled", PAL["bg"]), ("pressed", PAL["bg3"]),
                              ("active", PAL["bg3"])],
                  foreground=[("disabled", PAL["fg_dim"])],
                  bordercolor=[("disabled", PAL["bg"])])
        style.configure("Accent.TButton", background=PAL["accent"],
                        foreground="#FFFFFF", bordercolor=PAL["accent"])
        style.map("Accent.TButton",
                  background=[("disabled", "#22375C"), ("pressed", PAL["accent_h"]),
                              ("active", PAL["accent_h"])],
                  foreground=[("disabled", "#6E88B0")])
        style.configure("Danger.TButton", background=PAL["danger"],
                        foreground="#FFD9D9", bordercolor=PAL["danger"])
        style.map("Danger.TButton",
                  background=[("disabled", PAL["bg"]), ("pressed", PAL["danger_h"]),
                              ("active", PAL["danger_h"])],
                  foreground=[("disabled", PAL["fg_dim"])],
                  bordercolor=[("disabled", PAL["bg"])])

        # 输入
        style.configure("TEntry", fieldbackground=PAL["bg2"], foreground=PAL["fg"],
                        insertcolor=PAL["fg"], lightcolor=PAL["bg2"],
                        darkcolor=PAL["bg2"], padding=4)
        style.map("TEntry", bordercolor=[("focus", PAL["accent"])],
                  lightcolor=[("focus", PAL["accent"])],
                  darkcolor=[("focus", PAL["accent"])])
        style.configure("TCombobox", fieldbackground=PAL["bg2"], background=PAL["bg2"],
                        foreground=PAL["fg"], arrowcolor=PAL["fg"], padding=3)
        style.map("TCombobox",
                  fieldbackground=[("readonly", PAL["bg2"])],
                  foreground=[("readonly", PAL["fg"])],
                  arrowcolor=[("disabled", PAL["fg_dim"]), ("active", PAL["accent"])],
                  bordercolor=[("focus", PAL["accent"])])

        # 单选/复选（指示点选中变主色）
        style.configure("TRadiobutton", background=PAL["bg1"], foreground=PAL["fg"],
                        focuscolor=PAL["accent"], indicatorcolor=PAL["bg2"], padding=2)
        style.map("TRadiobutton",
                  background=[("active", PAL["bg1"])],
                  indicatorcolor=[("selected", PAL["accent"]), ("pressed", PAL["bg3"])])
        style.configure("TCheckbutton", background=PAL["bg1"], foreground=PAL["fg_dim"],
                        focuscolor=PAL["accent"], indicatorcolor=PAL["bg2"], padding=0)
        style.map("TCheckbutton",
                  background=[("active", PAL["bg1"])],
                  indicatorcolor=[("selected", PAL["accent"]), ("pressed", PAL["bg3"])])

        # 滚动条
        style.configure("Vertical.TScrollbar", background=PAL["bg2"],
                        troughcolor=PAL["term_bg"], bordercolor=PAL["bg1"],
                        lightcolor=PAL["bg2"], darkcolor=PAL["bg2"],
                        arrowcolor=PAL["fg_dim"], relief="flat")
        style.map("Vertical.TScrollbar",
                  background=[("active", PAL["bg3"]), ("pressed", PAL["accent"])])

        # 底部状态栏
        style.configure("Status.TFrame", background=PAL["term_bg"])
        style.configure("Status.TLabel", background=PAL["term_bg"],
                        foreground=PAL["fg_dim"])

    def _center_window(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x = max((self.root.winfo_screenwidth() - w) // 2, 0)
        y = max((self.root.winfo_screenheight() - h) // 3, 0)
        self.root.geometry(f"+{x}+{y}")

    def _build_ui(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # ----- 顶栏：标题 + 串口连接 -----
        top = ttk.Frame(root, padding=(12, 10, 12, 8))
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="CAN-WIFI 上位机", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="串口配置工具", style="Dim.TLabel").pack(
            side="left", padx=(8, 0), pady=(3, 0))
        self.dot = tk.Canvas(top, width=12, height=12, highlightthickness=0,
                             bg=PAL["bg"])
        self.dot_id = self.dot.create_oval(1, 1, 11, 11, fill="#4B5563", outline="")
        self.dot.pack(side="right", pady=(4, 0))
        self.btn_connect = ttk.Button(top, text="连接", width=9, style="Accent.TButton",
                                      command=self._toggle_connect)
        self.btn_connect.pack(side="right", padx=(0, 10))
        self.btn_rescan = ttk.Button(top, text="刷新", width=6, command=self._rescan)
        self.btn_rescan.pack(side="right")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var,
                                       width=44, state="readonly")
        self.port_combo.pack(side="right", padx=(0, 8))
        ttk.Label(top, text="串口", style="Dim.TLabel").pack(side="right", pady=(3, 0))
        ttk.Separator(root, orient="horizontal").grid(row=1, column=0, sticky="ew")

        body = ttk.Frame(root, padding=(12, 10))
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # ----- 左：状态卡片（WiFi / 网络 / 设备 三组） -----
        left = ttk.Labelframe(body, text=" 设备状态 ", style="Card.TLabelframe", padding=10)
        left.grid(row=0, column=0, sticky="nw", padx=(0, 10))
        self.status_labels = {}

        def st_row(r, key, name):
            ttk.Label(left, text=name, style="Key.TLabel").grid(
                row=r, column=0, sticky="w", pady=1)
            val = ttk.Label(left, text="—", style="Card.TLabel", width=24, wraplength=190)
            val.grid(row=r, column=1, sticky="w", pady=1, padx=(10, 0))
            self.status_labels[key] = val

        def st_cap(r, text):
            ttk.Label(left, text=text, style="Cap.TLabel").grid(
                row=r, column=0, columnspan=2, sticky="w", pady=(8, 2))

        def st_sep(r):
            ttk.Separator(left, orient="horizontal",
                          style="Card.TSeparator").grid(
                row=r, column=0, columnspan=2, sticky="ew", pady=8)

        r = 0
        st_cap(r, "WIFI"); r += 1
        for key, name in (("mode", "模式"), ("link", "连接"), ("ssid", "SSID"),
                          ("rssi", "信号"), ("channel", "信道"), ("iprule", "IP规则")):
            st_row(r, key, name); r += 1
        st_sep(r); r += 1
        st_cap(r, "网络"); r += 1
        for key, name in (("ip", "IP"), ("netmask", "掩码"), ("gw", "网关"),
                          ("mac", "MAC"), ("mdns", "mDNS")):
            st_row(r, key, name); r += 1
        st_sep(r); r += 1
        st_cap(r, "设备"); r += 1
        for key, name in (("uptime", "运行时间"), ("can", "CAN缓冲"), ("twai", "TWAI")):
            st_row(r, key, name); r += 1

        self.autorefresh_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="自动刷新（2s）", variable=self.autorefresh_var,
                        command=self._manual_refresh).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(left, text="刷新状态", command=self._manual_refresh).grid(
            row=r + 1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        # ----- 右：终端 + 配置 -----
        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        term = ttk.Labelframe(right, text=" 终端 ", style="Card.TLabelframe", padding=8)
        term.grid(row=0, column=0, sticky="nsew")
        term.columnconfigure(0, weight=1)
        term.rowconfigure(1, weight=1)
        self.btn_clear = ttk.Button(term, text="清屏", width=6, command=self._clear_term)
        self.btn_clear.grid(row=0, column=1, sticky="e", pady=(0, 6))
        self.term = tk.Text(term, height=10, state="disabled", wrap="none",
                            bg=PAL["term_bg"], fg="#C9CFD8", insertbackground="#C9CFD8",
                            selectbackground=PAL["accent"], selectforeground="#FFFFFF",
                            font=FONT_TERM, relief="flat", borderwidth=0,
                            highlightthickness=1, highlightbackground=PAL["border"],
                            padx=8, pady=6)
        self.term.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(term, orient="vertical", command=self.term.yview)
        sb.grid(row=1, column=1, sticky="ns", padx=(6, 0))
        self.term.configure(yscrollcommand=sb.set)
        for tag, fg in (("rx", "#C9CFD8"), ("sent", "#6CB2FF"),
                        ("status", "#5FA97C"), ("err", "#F87171"),
                        ("ts", "#5C6672"), ("note", "#D8B46A")):
            self.term.tag_configure(tag, foreground=fg)

        cfg = ttk.Labelframe(right, text=" 配置 ", style="Card.TLabelframe", padding=10)
        cfg.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        cfg.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(cfg, text="WIFI 模式", style="Cap.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w"); r += 1
        self.mode_var = tk.StringVar(value="sta")
        modef = ttk.Frame(cfg, style="Card.TFrame")
        modef.grid(row=r, column=0, columnspan=3, sticky="ew")
        ttk.Radiobutton(modef, text="STA（连接路由器）", value="sta",
                        variable=self.mode_var,
                        command=self._touch_mode).pack(side="left")
        ttk.Radiobutton(modef, text="AP（本机热点）", value="ap",
                        variable=self.mode_var,
                        command=self._touch_mode).pack(side="left", padx=(16, 0))
        ttk.Button(modef, text="应用", width=8, style="Accent.TButton",
                   command=self._apply_mode).pack(side="right")
        r += 1

        ttk.Label(cfg, text="STA 连接", style="Cap.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0)); r += 1
        ttk.Label(cfg, text="WiFi名称", style="Key.TLabel").grid(row=r, column=0, sticky="w")
        self.ssid_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.ssid_var, width=28).grid(
            row=r, column=1, sticky="we", padx=8)
        ttk.Button(cfg, text="设置 SSID", command=self._set_ssid).grid(
            row=r, column=2, sticky="e")
        r += 1
        ttk.Label(cfg, text="密码", style="Key.TLabel").grid(row=r, column=0, sticky="w", pady=(6, 0))
        self.pass_var = tk.StringVar()
        ttk.Entry(cfg, textvariable=self.pass_var, width=28).grid(
            row=r, column=1, sticky="we", padx=8, pady=(6, 0))
        ttk.Button(cfg, text="设置密码", command=self._set_pass).grid(
            row=r, column=2, sticky="e", pady=(6, 0))
        r += 1

        ttk.Label(cfg, text="IP 设置", style="Cap.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0)); r += 1
        ipf = ttk.Frame(cfg, style="Card.TFrame")
        ipf.grid(row=r, column=0, columnspan=3, sticky="ew")
        self.ip_var = tk.StringVar(value="auto")
        ttk.Radiobutton(ipf, text="自动 .250", value="auto", variable=self.ip_var,
                        command=self._touch_ip).pack(side="left")
        ttk.Radiobutton(ipf, text="手动", value="static", variable=self.ip_var,
                        command=self._touch_ip).pack(side="left", padx=(16, 6))
        self.ip_text = tk.StringVar()
        ttk.Entry(ipf, textvariable=self.ip_text, width=15).pack(side="left")
        ttk.Button(ipf, text="设置 IP", command=self._set_ip).pack(side="right")
        r += 1

        ttk.Separator(cfg, orient="horizontal", style="Card.TSeparator").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=(10, 8)); r += 1
        ttk.Button(cfg, text="info", width=9, command=lambda: self._send(
            "info", ok_tokens=["======== 设备状态 ========"], kind="user",
            on_done=lambda *a: None)).grid(row=r, column=0, sticky="w")
        ttk.Button(cfg, text="help", width=9, command=lambda: self._send(
            "help", ok_tokens=["命令列表:"], kind="user",
            on_done=lambda *a: None)).grid(row=r, column=1, sticky="w", padx=8)
        ttk.Button(cfg, text="重启设备", width=9, style="Danger.TButton",
                   command=self._reboot).grid(row=r, column=2, sticky="e")

        # ----- 底部状态栏 -----
        sbf = ttk.Frame(root, style="Status.TFrame", padding=(12, 5))
        sbf.grid(row=3, column=0, sticky="ews")
        self.sb_port = ttk.Label(sbf, text="未连接", style="Status.TLabel")
        self.sb_port.pack(side="left")
        self.sb_msg = ttk.Label(sbf, text="就绪", style="Status.TLabel")
        self.sb_msg.pack(side="right")

        self._set_connected_ui(False)

    # ---------- 终端显示 ----------

    def _term_append(self, text, tag="rx"):
        at_bottom = self.term.yview()[1] >= 0.999
        self.term.configure(state="normal")
        self.term.insert("end", f"[{now_ts()}] ", "ts")
        if not text.endswith("\n"):
            text += "\n"
        self.term.insert("end", text, tag)
        self.term.configure(state="disabled")
        if at_bottom:
            self.term.see("end")

    def _note(self, text):
        self._term_append(text, "note")

    def _clear_term(self):
        self.term.configure(state="normal")
        self.term.delete("1.0", "end")
        self.term.configure(state="disabled")

    def _statusbar(self, msg, err=False):
        self.sb_msg.configure(text=msg,
                              foreground=PAL["err"] if err else PAL["fg_dim"])

    # ---------- 串口连接 ----------

    def _rescan(self):
        ports = list(list_ports.comports())
        ports.sort(key=lambda p: (0 if (p.vid == USJ_VID and p.pid == USJ_PID) else 1,
                                  p.device))
        labels, mapping = [], {}
        for p in ports:
            desc = (p.description or "").strip()
            if p.vid == USJ_VID and p.pid == USJ_PID:
                desc = (desc + " " if desc else "") + "[ESP32 USB 串口]"
            label = p.device + (f" — {desc}" if desc and desc != p.device else "")
            labels.append(label)
            mapping[label] = p.device
        self._port_map = mapping
        self.port_combo["values"] = labels
        if labels and self.port_var.get() not in labels:
            self.port_var.set(labels[0])   # ESP32 USB 串口排第一
        self._statusbar(f"找到 {len(labels)} 个串口" if labels
                        else "未找到串口，请插好设备后点「刷新」")

    def _toggle_connect(self):
        if self.link.is_open:
            self.link.cancel_reconnect()
            self.link.close()
            self._set_connected_ui(False)
            self._statusbar("已断开")
            return
        label = self.port_var.get()
        port = self._port_map.get(label, label.split(" — ")[0])
        if not port:
            messagebox.showinfo("提示", "请先选择串口；若列表为空请点「刷新」。")
            return
        try:
            self.link.open(port)
        except Exception as e:
            msg = f"无法打开 {port}：\n{e}"
            if any(s in str(e) for s in ("拒绝访问", "Access is denied", "PermissionError")):
                msg += "\n\n串口可能被其他程序占用（如 idf.py monitor），请关闭后重试。"
            messagebox.showerror("连接失败", msg)
            self._statusbar("连接失败", err=True)
            return
        self.line_buf = b""
        self.pendings.clear()
        self.recent_sends.clear()
        self._set_connected_ui(True)
        self._note(f"已连接 {port}（{CONNECT_BAUD} 8N1，DTR 已断言）")
        self._statusbar(f"已连接 {port}")
        self._refresh_status()

    def _set_connected_ui(self, on):
        self.dot.itemconfigure(self.dot_id, fill=PAL["ok"] if on else "#4B5563")
        self.btn_connect.configure(text="断开" if on else "连接")
        state = "normal" if on else "disabled"
        for btn in self._config_buttons():
            btn.configure(state=state)
        self.port_combo.configure(state="readonly" if not on else "disabled")
        self.sb_port.configure(
            text=f"{self.link.port}（已连接）" if on and self.link.port else "未连接")

    def _config_buttons(self):
        """随连接状态启停的按钮（排除顶栏连接/刷新、终端清屏——断开时也要能用）。"""
        btns = []
        for w in self.root.winfo_children():
            self._collect_buttons(w, btns)
        return [b for b in btns
                if b not in (self.btn_connect, self.btn_rescan, self.btn_clear)]

    def _collect_buttons(self, w, out):
        if isinstance(w, ttk.Button):
            out.append(w)
        for c in w.winfo_children():
            self._collect_buttons(c, out)

    # ---------- 命令发送与应答 ----------

    def _send(self, cmd, ok_tokens=(), fail_tokens=(), on_done=None,
              kind="user", json_mode=False, terminator=False,
              silent=False, timeout=TIMEOUT_S):
        if not self.link.is_open:
            return False
        if len(cmd.encode("utf-8")) > CMD_MAX_BYTES:
            self._statusbar("命令过长，设备行缓冲为 128 字节", err=True)
            return False
        if any(p.cmd == cmd.strip() for p in self.pendings):
            return False                # 同一命令已在等待应答
        try:
            self.link.send_line(cmd)
        except Exception as e:
            self._statusbar(f"发送失败：{e}", err=True)
            self._note(f"⚠ 发送失败：{e}（USJ 端点异常时请重新插拔 USB）")
            return False
        self.recent_sends.append((cmd.strip(), cmd.strip(), time.monotonic() + 5, silent))
        if len(self.recent_sends) > 16:
            self.recent_sends.pop(0)
        self.pendings.append(PendingCmd(
            cmd, kind=kind, ok_tokens=ok_tokens, fail_tokens=fail_tokens,
            on_done=on_done, timeout=timeout, json_mode=json_mode,
            terminator=terminator, silent=silent))
        return True

    # 各配置动作

    def _touch_mode(self):
        self.mode_touched_at = time.monotonic()

    def _touch_ip(self):
        self.ip_touched_at = time.monotonic()

    def _apply_mode(self):
        target = self.mode_var.get()
        name = "STA" if target == "sta" else "AP"
        if not messagebox.askyesno(
                "切换模式", f"切换为 {name} 模式？\n配置将保存，设备会自动重启。"):
            self._sync_radios_from_state()
            return
        sent = self._send(
            "mode " + target,
            ok_tokens=["已切换为 ", "当前已是 "],
            fail_tokens=["用法: mode"],
            on_done=lambda ok, pl, tk_: self._after_mode(ok, pl, tk_))
        if sent:
            self._statusbar(f"正在切换为 {name} 模式…")

    def _after_mode(self, ok, payload, token):
        if ok and token == "已切换为 ":
            self._note("── 设备即将重启以生效新模式 ──")
            self._statusbar("设备重启中…")
            self._arm_post_reboot()
        elif ok:
            self._statusbar("设备已处于该模式")
            self._refresh_status()

    def _set_ssid(self):
        val = self.ssid_var.get()
        if val != val.strip() or not val:
            messagebox.showwarning("SSID 无效", "SSID 不能为空，且首尾不能有空格。")
            return
        nb = len(val.encode("utf-8"))
        if not 1 <= nb <= 32:
            messagebox.showwarning("SSID 无效", f"SSID 需 1~32 字节（当前 {nb} 字节）。")
            return
        self._send(
            f"ssid {val}",
            ok_tokens=['SSID 已保存为 "'],
            fail_tokens=["设置失败"],
            on_done=lambda ok, pl, tk_: self._after_cfg(ok, pl, "SSID"))

    def _set_pass(self):
        val = self.pass_var.get()
        nb = len(val.encode("utf-8"))
        if not 8 <= nb <= 63:
            messagebox.showwarning("密码无效", f"密码需 8~63 个字符（当前 {nb}）。")
            return
        self._send(
            f"pass {val}",
            ok_tokens=["密码已保存"],
            fail_tokens=["设置失败"],
            on_done=lambda ok, pl, tk_: self._after_cfg(ok, pl, "密码"))

    def _set_ip(self):
        if self.ip_var.get() == "auto":
            self._send(
                "ip auto",
                ok_tokens=["已切换为自动 .250"],
                fail_tokens=["保存失败", "IP 地址无效"],
                on_done=lambda ok, pl, tk_: self._after_cfg(ok, pl, "IP"))
        else:
            ip = self.ip_text.get().strip()
            if not valid_ipv4(ip):
                messagebox.showwarning("IP 无效", "请输入合法的 IPv4 地址，如 192.168.1.250")
                return
            self._send(
                f"ip {ip}",
                ok_tokens=["已设置固定 IP "],
                fail_tokens=["IP 地址无效"],
                on_done=lambda ok, pl, tk_: self._after_cfg(ok, pl, "IP"))

    def _after_cfg(self, ok, payload, name):
        if ok:
            self._statusbar(f"{name} 已保存，设备正在重连生效")
        else:
            self._statusbar(f"{name} 设置失败：{payload}", err=True)

    def _reboot(self):
        if not messagebox.askyesno("重启设备", "确定重启设备？"):
            return
        sent = self._send(
            "reboot", ok_tokens=["设备即将重启"],
            on_done=lambda ok, pl, tk_: self._after_reboot(ok))
        if sent:
            self._statusbar("设备重启中…")

    def _after_reboot(self, ok):
        if ok:
            self._note("── 设备即将重启 ──")
            self._statusbar("设备重启中…")
            self._arm_post_reboot()

    def _arm_post_reboot(self):
        """
        重启后的两条路径：
        - 主路径：USJ 重启只做 USB 总线复位，端口通常不掉（idf.py monitor 跨重启
          不死同理）→ 3.5s 后直接刷新状态并取消兜底重连；
        - 兜底：端口若真掉线（拔插/某些 UART 桥），lost→begin_reconnect 链路接管。
        """
        self.link.begin_reconnect()
        self.root.after(3500, self._post_reboot_refresh)

    def _post_reboot_refresh(self):
        if not self.link.is_open:
            return                    # 已掉线且仍在重连，交给 reconnected 路径
        if self.link.reconnect_active:
            self.link.cancel_reconnect()
            self._statusbar("设备已重启")
        self._refresh_status()

    # ---------- 状态查询 ----------

    def _manual_refresh(self):
        if self.link.is_open:
            self._refresh_status(manual=True)

    def _refresh_status(self, manual=False):
        if not self.link.is_open or self.link.reconnect_active:
            return
        if any(p.kind == "status" for p in self.pendings):
            return                    # 已有一次状态查询在途
        if self.json_supported:
            self._send("status", kind="status", json_mode=True, silent=True,
                       timeout=2.0,
                       on_done=lambda ok, pl, tk_: self._after_status(ok, pl, manual))
        else:
            self._send("info", kind="status", terminator=True, silent=True,
                       timeout=3.0,
                       on_done=lambda ok, pl, tk_: self._after_info(ok, pl, manual))

    def _after_status(self, ok, payload, manual=False):
        if ok:
            self._apply_state(state_from_json(payload))
            if manual:
                self._statusbar(f"状态已更新 {now_ts()}")
        elif payload == "legacy":
            self.json_supported = False
            self._note("（设备固件无 status 命令，已切换为解析 info 文本）")
            self._refresh_status()
        else:
            self._statusbar("status 应答异常", err=True)

    def _after_info(self, ok, lines, manual=False):
        if ok:
            self._apply_state(parse_info_lines(lines))
            if manual:
                self._statusbar(f"状态已更新 {now_ts()}")
        else:
            self._statusbar("info 无应答", err=True)

    def _apply_state(self, st):
        d = self.status_labels
        d["mode"].configure(text={"sta": "STA（连接路由器）",
                                  "ap": "AP（本机热点）"}.get(st["mode"], "—"))
        if st["mode"] == "ap":
            link = f"运行中（客户端 {st['ap_clients']}）" if st["link"] else "未启动"
        elif st["mode"] == "sta":
            link = "已连接" if st["link"] else "未连接"
        else:
            link = "—"
        d["link"].configure(text=link)
        d["ssid"].configure(text=st["ssid"] or "—")
        d["rssi"].configure(
            text=f"{st['rssi']} dBm" if (st["mode"] == "sta" and st["link"]) else "—")
        d["channel"].configure(text=str(st["channel"]) if st["channel"] else "—")
        d["ip"].configure(text=st["ip"] or "—")
        d["netmask"].configure(text=st["netmask"] or "—")
        d["gw"].configure(text=st["gw"] or "—")
        d["mac"].configure(text=st["mac"] or "—")
        if st["ipmode"] == "auto":
            iprule = "自动绑定同网段 .250"
        elif st["ipmode"] == "static":
            iprule = f"固定 {st['static_ip']}"
        else:
            iprule = "—"
        d["iprule"].configure(text=iprule)
        d["mdns"].configure(text=f"http://{st['mdns']}.local" if st["mdns"] else "—")
        d["uptime"].configure(
            text=st["uptime_text"] or format_uptime(st["uptime_s"]))
        if st["can_total"] is not None:
            d["can"].configure(text=f"{st['can_ring']} 条待显示（累计 {st['can_total']}）")
        else:
            d["can"].configure(text=f"{st['can_ring']} 条待显示" if st["can_ring"] else "—")
        if st["twai"]:
            twai = f"{st['twai']}（TEC={st['tec']} REC={st['rec']}）"
        else:
            twai = "未初始化" if st["mode"] is not None else "—"
        d["twai"].configure(text=twai)
        self._sync_radios_from_state(st)
        self._color_state(st)

    def _color_state(self, st):
        """关键状态值着色：模式=主色，连接=绿/红/黄，TWAI=绿/黄/红。"""
        labels = self.status_labels
        labels["mode"].configure(
            foreground=PAL["accent"] if st["mode"] else PAL["fg_dim"])
        link_fg = PAL["fg_dim"]
        if st["mode"] == "ap":
            link_fg = PAL["ok"] if st["link"] else PAL["warn"]
        elif st["mode"] == "sta":
            link_fg = PAL["ok"] if st["link"] else PAL["err"]
        labels["link"].configure(foreground=link_fg)
        twai_fg = PAL["fg_dim"]
        if st["twai"]:
            twai_fg = {"RUNNING": PAL["ok"],
                       "RECOVERING": PAL["warn"]}.get(st["twai"], PAL["err"])
        labels["twai"].configure(foreground=twai_fg)

    def _sync_radios_from_state(self, st=None):
        if st is None:
            return
        now = time.monotonic()
        if st["mode"] and now - self.mode_touched_at > 5:
            self.mode_var.set(st["mode"])
        if st["ipmode"] and now - self.ip_touched_at > 5:
            self.ip_var.set(st["ipmode"])
            if st["ipmode"] == "static" and st["static_ip"] and not self.ip_text.get():
                self.ip_text.set(st["static_ip"])

    def _poll_loop(self):
        if (self.autorefresh_var.get() and self.link.is_open
                and not self.link.reconnect_active):
            self._refresh_status()
        self.root.after(POLL_MS, self._poll_loop)

    # ---------- 事件排空与超时 ----------

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "data":
                    self._feed_bytes(payload)
                elif kind == "lost":
                    self._on_link_lost(payload)
                elif kind == "reconnected":
                    self._on_reconnected()
                elif kind == "reconnect_failed":
                    self._on_reconnect_failed()
        except queue.Empty:
            pass
        self.root.after(UI_DRAIN_MS, self._drain_events)

    def _feed_bytes(self, data):
        self.line_buf += data
        while b"\n" in self.line_buf:
            raw, self.line_buf = self.line_buf.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            if line:
                self._handle_line(line)

    def _handle_line(self, line):
        # 1) 先喂给在途命令（内容匹配）
        results = []
        for p in list(self.pendings):
            done, ok, payload, token = p.feed(line)
            if done:
                self.pendings.remove(p)
                results.append((p, ok, payload, token))
        # 2) 决定终端显示（静默轮询的收发都不进终端）
        consumed_by_silent = any(p.silent for p, *_ in results)
        tag, shown = "rx", line
        now = time.monotonic()
        echo_hit = None
        for item in list(self.recent_sends):
            raw, disp, deadline, silent = item
            if line == raw and now <= deadline:
                echo_hit = item
                break
        self.recent_sends = [i for i in self.recent_sends if i[2] > now]
        if echo_hit is not None:
            raw, disp, _, silent = echo_hit
            tag, shown = (None, None) if (silent or consumed_by_silent) \
                else ("sent", disp)
        elif consumed_by_silent:
            tag, shown = None, None
        elif line.startswith("{"):
            tag, shown = "status", line
        elif "未知命令" in line or "失败" in line:
            tag, shown = "err", line
        if shown is not None:
            self._term_append(shown, tag)
        # 3) 回调（可能再次 _send，如旧固件回退 info）
        for p, ok, payload, token in results:
            if p.on_done:
                try:
                    p.on_done(ok, payload, token)
                except Exception as e:
                    self._statusbar(f"内部错误：{e}", err=True)

    def _check_timeouts(self):
        now = time.monotonic()
        expired = [p for p in self.pendings if now > p.deadline]
        for p in expired:
            self.pendings.remove(p)
            if p.kind == "user" or not p.silent:
                self._statusbar(f"命令无应答：{p.cmd}", err=True)
            if p.on_done:
                try:
                    p.on_done(False, None, None)
                except Exception:
                    pass
        self.root.after(300, self._check_timeouts)

    def _on_link_lost(self, gen=None):
        if gen is not None and gen != self.link.generation:
            return                    # 旧连接的遗留事件
        # 关掉失效句柄：重连线程靠 is_open 判断能否尝试重开
        self.link.close()
        self._set_connected_ui(False)
        self.pendings.clear()
        if self.link.reconnect_active:
            self._note("设备正在重启，等待重新连接…")
            self._statusbar("设备重启中，等待重新连接…")
        else:
            self._note("⚠ 串口连接丢失（设备重启或已拔出）")
            self._statusbar("连接丢失", err=True)

    def _on_reconnected(self):
        self.line_buf = b""
        self._set_connected_ui(True)
        self._note("── 设备已重新连接 ──")
        self._statusbar("设备已重启并重连")
        self._refresh_status()

    def _on_reconnect_failed(self):
        if self.link.is_open:
            return                    # 用户已手动重连，忽略过期失败
        self._note("⚠ 自动重连失败，请手动点「连接」")
        self._statusbar("重连失败", err=True)
        messagebox.showwarning(
            "重连失败",
            "设备重启后未能自动重连串口。\n请确认设备已上电，然后手动点击「连接」。")


# ================= 入口 =================

def main():
    if sys.platform == "win32":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
