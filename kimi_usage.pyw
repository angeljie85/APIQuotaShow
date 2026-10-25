# -*- coding: utf-8 -*-
"""
Kimi 额度浮窗（Kimi Usage Widget）

常驻桌面右下角的无边框小窗，实时显示 Kimi For Coding 的两个额度窗口：
  - 5 小时额度（滚动窗口）
  - 7 天额度（周额度）

数据来源：GET https://api.kimi.com/coding/v1/usages
API Key 的取法（按优先级）：
  1. 环境变量 KIMI_API_KEY
  2. 本机配置文件里用 Windows DPAPI 加密过的 Key
  3. 本机装了 CC Switch 时，只读它的数据库里 Kimi 供应商的 Key
Key 只存在本机，只用于请求上面那一个接口，不会上传到任何地方。

操作：
  左键拖动      移动位置（位置会记住）
  点「刷新」    立即拉取
  点右上「—」   收起进系统托盘（不退出）
  点托盘图标    重新打开 / 再点一次收起
  右键菜单      刷新 / 开机自启 / 退出
"""

import base64
import ctypes
import datetime
import json
import os
import queue
import sqlite3
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from tkinter import messagebox
import urllib.request

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_OK = True
except Exception:
    TRAY_OK = False

API = "https://api.kimi.com/coding/v1/usages"
REFRESH_MS = 60 * 1000

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "KimiUsageWidget")
CFG_PATH = os.path.join(APP_DIR, "config.json")
CC_DB = os.path.expanduser("~/.cc-switch/cc-switch.db")

# 配色（深色，夜间/白天都看得清）
BG = "#1C1C1E"
BORDER = "#3A3A3C"
TRACK = "#333336"
FG = "#EDEDED"
DIM = "#9A9AA0"
FAINT = "#6E6E73"
GREEN = "#30D158"
ORANGE = "#FF9F0A"
RED = "#FF453A"

RGB_GREEN = (48, 209, 88)
RGB_ORANGE = (255, 159, 10)
RGB_RED = (255, 69, 58)
RGB_GRAY = (142, 142, 147)

FONT = "Microsoft YaHei UI"

# 逻辑尺寸（会按系统 DPI 缩放）
BASE_W, BASE_H, BASE_PAD = 300, 182, 14
S = 1.0
W = BASE_W
H = BASE_H
PAD = BASE_PAD

_mutex = None


def setup_dpi():
    """开启 DPI 感知，返回缩放系数（1.5 表示 150%）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        return ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
    except Exception:
        return 1.0


def single_instance():
    """已有实例在跑就不再开第二个。"""
    global _mutex
    try:
        k = ctypes.windll.kernel32
        _mutex = k.CreateMutexW(None, False, "Local\\KimiUsageWidget_v1")
        return k.GetLastError() != 183
    except Exception:
        return True


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def work_area():
    """屏幕可用区域（不含任务栏），物理像素。"""
    r = RECT()
    try:
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
        if r.right > r.left and r.bottom > r.top:
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, 1920, 1080


# ---------------------------------------------------------------- 配置

def load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_cfg(cfg):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---- Key 加密：只在当前 Windows 用户下可解，拷走文件也解不开

class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def dpapi_protect(text):
    if not text:
        return ""
    try:
        blob_in, _keep = _blob(text.encode("utf-8"))
        blob_out = _BLOB()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
        if not ok:
            return ""
        try:
            raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            return base64.b64encode(raw).decode("ascii")
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return ""


def dpapi_unprotect(b64):
    if not b64:
        return ""
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return ""
    try:
        blob_in, _keep = _blob(raw)
        blob_out = _BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
        if not ok:
            return ""
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8", "replace")
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return ""


def store_key(cfg, key):
    """把 Key 加密后写进配置，绝不明文落盘。"""
    cfg.pop("apiKey", None)
    enc = dpapi_protect(key)
    if enc:
        cfg["apiKeyEnc"] = enc
        save_cfg(cfg)
        return True
    return False


def key_from_cc_switch():
    """从 CC Switch 数据库里读 Kimi 供应商的 API Key（只读，不改动它）。"""
    try:
        con = sqlite3.connect("file:" + CC_DB.replace("\\", "/") + "?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select name, settings_config from providers where app_type='codex'"
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return None
    for name, cfg in rows:
        if "kimi" not in (name or "").lower():
            continue
        try:
            auth = json.loads(cfg).get("auth") or {}
        except Exception:
            continue
        key = (auth.get("OPENAI_API_KEY") or "").strip()
        if key.startswith("sk-"):
            return key
    return None


def resolve_key(cfg):
    """取 Key 的优先级：环境变量 → 本机加密配置 → CC Switch 数据库。"""
    env = (os.environ.get("KIMI_API_KEY") or "").strip()
    if env:
        return env, "环境变量"

    enc = cfg.get("apiKeyEnc")
    if enc:
        key = dpapi_unprotect(enc)
        if key:
            return key, "本地配置"

    # 兼容早期的明文配置：读到就地加密，然后把明文删掉
    plain = (cfg.get("apiKey") or "").strip()
    if plain:
        cfg.pop("apiKey", None)
        enc = dpapi_protect(plain)
        if enc:
            cfg["apiKeyEnc"] = enc
        else:
            cfg["apiKey"] = plain
        save_cfg(cfg)
        return plain, "本地配置"

    key = key_from_cc_switch()
    if key:
        return key, "CC Switch"
    return None, None


# ---------------------------------------------------------------- 取数

def parse_ts(s):
    if not s:
        return None
    try:
        d = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.astimezone()


def parse_usage(raw):
    d = json.loads(raw)
    us = d.get("usages") or {}

    u5 = u7 = None
    r5 = r7 = None

    l5 = us.get("limit_5h") or {}
    l7 = us.get("limit_7d") or {}
    if "used_ratio" in l5:
        u5 = float(l5["used_ratio"])
        r5 = parse_ts(l5.get("reset_time"))
    if "used_ratio" in l7:
        u7 = float(l7["used_ratio"])
        r7 = parse_ts(l7.get("reset_time"))

    # 接口老版本只有 limits / usage，兜底解析
    if u5 is None:
        for it in d.get("limits") or []:
            win = it.get("window") or {}
            if win.get("duration") == 300:
                det = it.get("detail") or {}
                try:
                    lim = float(det.get("limit") or 0)
                    used = float(det.get("used") or 0)
                except (TypeError, ValueError):
                    continue
                if lim > 0:
                    u5 = used / lim
                    r5 = parse_ts(det.get("resetTime"))
                break
    if u7 is None:
        ug = d.get("usage") or {}
        try:
            lim = float(ug.get("limit") or 0)
            used = float(ug.get("used") or 0)
        except (TypeError, ValueError):
            lim = used = 0
        if lim > 0:
            u7 = used / lim
            r7 = parse_ts(ug.get("resetTime"))

    return {"u5": u5, "u7": u7, "r5": r5, "r7": r7}


def fetch(key):
    req = urllib.request.Request(API, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "User-Agent": "KimiUsageWidget/1.0",
    })
    with urllib.request.urlopen(req, timeout=12) as resp:
        return parse_usage(resp.read().decode("utf-8"))


def fmt_reset(dt):
    if not dt:
        return ""
    now = datetime.datetime.now(dt.tzinfo)
    days = (dt.date() - now.date()).days
    if days == 0:
        return "今天 " + dt.strftime("%H:%M")
    if days == 1:
        return "明天 " + dt.strftime("%H:%M")
    return dt.strftime("%m-%d %H:%M")


def fmt_ago(t):
    try:
        sec = (datetime.datetime.now() - t).total_seconds()
    except Exception:
        return ""
    if sec < 90:
        return "刚刚更新"
    if sec < 3600:
        return "%d 分钟前更新" % int(sec // 60)
    return "%d 小时前更新" % int(sec // 3600)


def color_of(ratio):
    if ratio is None:
        return DIM
    if ratio >= 0.85:
        return RED
    if ratio >= 0.60:
        return ORANGE
    return GREEN


def rgb_of(ratio):
    if ratio is None:
        return RGB_GRAY
    if ratio >= 0.85:
        return RGB_RED
    if ratio >= 0.60:
        return RGB_ORANGE
    return RGB_GREEN


def tray_image(u5, u7):
    """托盘图标：上下两条进度条，分别表示 5 小时 / 7 天的已用比例。"""
    size = 64
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([1, 1, size - 2, size - 2], radius=15, fill=(28, 28, 30, 235))

    def bar(y, ratio, rgb):
        d.rounded_rectangle([9, y, size - 9, y + 15], radius=7, fill=(255, 255, 255, 48))
        if ratio and ratio > 0:
            full = size - 18
            wpx = max(full * min(ratio, 1.0), 9)
            d.rounded_rectangle([9, y, 9 + wpx, y + 15], radius=7, fill=rgb)

    bar(13, u5, rgb_of(u5))
    bar(35, u7, rgb_of(u7))
    return im


def tray_tip(u5, u7):
    a = "5h 剩 %d%%" % max(int(round((1 - u5) * 100)), 0) if u5 is not None else "5h —"
    b = "7d 剩 %d%%" % max(int(round((1 - u7) * 100)), 0) if u7 is not None else "7d —"
    return "Kimi 额度 · %s · %s" % (a, b)


# ---------------------------------------------------------------- 界面

class App:
    def __init__(self, cfg, key, key_src):
        self.cfg = cfg
        self.key = key
        self.key_src = key_src
        self.data = {"u5": None, "u7": None, "r5": None, "r7": None}
        self.err = None
        self.busy = False
        self.last_ok = None
        self._drag = None
        self._moved = False
        self.hidden = False
        self.job = None
        self.tray = None
        self.hinted = False
        self.q = queue.Queue()

        root = tk.Tk()
        self.root = root
        root.title("Kimi 额度")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.97)
        root.configure(bg=BG)
        try:
            root.tk.call("tk", "scaling", S * 96.0 / 72.0)
        except Exception:
            pass

        self.canvas = tk.Canvas(root, width=W, height=H, bg=BG, bd=0,
                                highlightthickness=1, highlightbackground=BORDER)
        self.canvas.pack()

        x, y = cfg.get("x"), cfg.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            left, top, right, bottom = work_area()
            x = right - W - int(24 * S)
            y = bottom - H - int(24 * S)
        root.geometry("+%d+%d" % (x, y))

        c = self.canvas
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Button-3>", self.on_menu)
        c.tag_bind("close", "<Button-1>", lambda e: self.collapse())
        c.tag_bind("refresh", "<Button-1>", lambda e: self.refresh())

        self.build_tray()

        self.draw()
        self.refresh()
        self.job = root.after(REFRESH_MS, self.tick)
        root.after(150, self.poll)

    # ------------------------------------------------------------ 托盘

    def build_tray(self):
        if not TRAY_OK:
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示 / 收起窗口", self.tray_toggle, default=True),
            pystray.MenuItem("立即刷新", self.tray_refresh),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自动启动", self.tray_auto,
                             checked=lambda item: bool(self.autostart_cmd())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self.tray_quit),
        )
        try:
            self.tray = pystray.Icon("kimi_usage", tray_image(None, None), "Kimi 额度", menu)
            self.tray.run_detached()
        except Exception:
            self.tray = None

    def tray_toggle(self, icon=None, item=None):
        self.q.put("toggle")

    def tray_refresh(self, icon=None, item=None):
        self.q.put("refresh")

    def tray_auto(self, icon=None, item=None):
        self.q.put("auto")

    def tray_quit(self, icon=None, item=None):
        self.q.put("quit")

    def update_tray(self):
        if not self.tray:
            return
        u5, u7 = self.data["u5"], self.data["u7"]
        try:
            self.tray.icon = tray_image(u5, u7)
            self.tray.title = tray_tip(u5, u7)
            self.tray.update_menu()
        except Exception:
            pass

    # ------------------------------------------------------------ 主线程队列

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if isinstance(msg, tuple):
                    if msg[0] == "data":
                        self.apply_data(msg[1], msg[2])
                    continue
                if msg == "toggle":
                    self.toggle()
                elif msg == "refresh":
                    self.refresh()
                elif msg == "auto":
                    self.toggle_autostart()
                elif msg == "quit":
                    self.quit()
                    return
                elif msg == "hide":
                    self.collapse()
        except queue.Empty:
            pass
        except Exception:
            pass
        self.root.after(150, self.poll)

    # ------------------------------------------------------------ 显示 / 收起

    def visible(self):
        return not self.hidden

    def _hwnd(self):
        if getattr(self, "_h", None):
            return self._h
        h = 0
        try:
            h = int(self.root.wm_frame(), 16)
        except Exception:
            h = 0
        if not h:
            try:
                h = self.root.winfo_id()
            except Exception:
                h = 0
        self._h = h
        return h

    def _click_through(self, hwnd, on):
        """把窗口设成鼠标穿透，避免透明后仍挡住下面的东西。"""
        if not hwnd:
            return
        try:
            u = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if on:
                ex |= (WS_EX_TRANSPARENT | WS_EX_LAYERED)
            else:
                ex &= ~WS_EX_TRANSPARENT
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
        except Exception:
            pass

    def collapse(self):
        if self.hidden:
            return
        self.hidden = True
        self.cfg["x"] = self.root.winfo_x()
        self.cfg["y"] = self.root.winfo_y()
        save_cfg(self.cfg)
        self._click_through(self._hwnd(), True)
        self.root.attributes("-alpha", 0.0)
        if self.tray and not self.hinted:
            self.hinted = True
            try:
                self.tray.notify("已收起到托盘，点一下托盘图标就能打开。", "Kimi 额度")
            except Exception:
                pass

    def show(self):
        if self.hidden:
            self.hidden = False
            x, y = self.cfg.get("x"), self.cfg.get("y")
            if isinstance(x, int) and isinstance(y, int):
                self.root.geometry("+%d+%d" % (x, y))
        self._click_through(self._hwnd(), False)
        self.root.attributes("-alpha", 0.97)
        self.root.attributes("-topmost", True)
        self.root.lift()

    def toggle(self):
        if self.visible():
            self.collapse()
        else:
            self.show()

    # ------------------------------------------------------------ 绘制

    def draw(self):
        c = self.canvas
        c.delete("all")
        c.create_text(PAD, int(9 * S), text="Kimi 额度", anchor="nw", fill=FG,
                      font=(FONT, 11, "bold"))
        c.create_text(W - PAD, int(7 * S), text="—", anchor="ne", fill=DIM,
                      font=(FONT, 14), tags=("close",))
        c.create_text(W - PAD - int(24 * S), int(11 * S), text="刷新", anchor="ne",
                      fill=FAINT, font=(FONT, 8), tags=("refresh",))

        self.draw_row(int(40 * S), "5 小时额度", self.data["u5"], self.data["r5"])
        self.draw_row(int(104 * S), "7 天额度", self.data["u7"], self.data["r7"])

        if self.err:
            text, color = self.err, RED
        elif self.busy:
            text, color = "更新中…", FAINT
        elif self.last_ok:
            text, color = fmt_ago(self.last_ok), FAINT
        else:
            text, color = "", FAINT
        if text:
            c.create_text(PAD, H - int(9 * S), text=text, anchor="sw",
                          fill=color, font=(FONT, 8))

    def draw_row(self, y, label, ratio, reset_dt):
        c = self.canvas
        color = color_of(ratio)
        c.create_text(PAD, y, text=label, anchor="nw", fill=DIM, font=(FONT, 9))

        text = "剩 —"
        if ratio is not None:
            pct = int(round((1.0 - ratio) * 100))
            text = "剩 %d%%" % max(pct, 0)
        c.create_text(W - PAD, y - int(3 * S), text=text, anchor="ne", fill=color,
                      font=(FONT, 12, "bold"))

        bar_y = y + int(26 * S)
        bar_h = max(int(6 * S), 4)
        c.create_rectangle(PAD, bar_y, W - PAD, bar_y + bar_h, fill=TRACK, outline="")
        if ratio and ratio > 0:
            full = W - PAD * 2
            wpx = max(full * min(ratio, 1.0), bar_h)
            c.create_rectangle(PAD, bar_y, PAD + wpx, bar_y + bar_h, fill=color, outline="")

        sub = "—"
        if ratio is not None:
            sub = "已用 %.1f%%" % (ratio * 100)
            when = fmt_reset(reset_dt)
            if when:
                sub += "  ·  " + when + " 重置"
        c.create_text(PAD, bar_y + int(15 * S), text=sub, anchor="nw", fill=FAINT,
                      font=(FONT, 8))

    # ------------------------------------------------------------ 交互

    def on_press(self, e):
        self._drag = (e.x, e.y)
        self._moved = False

    def on_drag(self, e):
        if not self._drag:
            return
        dx, dy = self._drag
        self.root.geometry("+%d+%d" % (e.x_root - dx, e.y_root - dy))
        self._moved = True

    def on_release(self, e):
        if self._drag and self._moved:
            self.cfg["x"] = self.root.winfo_x()
            self.cfg["y"] = self.root.winfo_y()
            save_cfg(self.cfg)
        self._drag = None

    def on_menu(self, e):
        m = tk.Menu(self.root, tearoff=0, bg="#2C2C2E", fg=FG,
                    activebackground="#0A84FF", activeforeground="#FFFFFF",
                    bd=0, font=(FONT, 9))
        m.add_command(label="立即刷新", command=self.refresh)
        m.add_separator()
        auto = tk.BooleanVar(value=bool(self.autostart_cmd()))
        m.add_checkbutton(label="开机自动启动", variable=auto,
                          command=lambda: self.autostart_on() if auto.get() else self.autostart_off())
        m.add_command(label="回到右下角", command=self.reset_pos)
        m.add_separator()
        if self.tray:
            m.add_command(label="收起进托盘", command=self.collapse)
            m.add_command(label="退出", command=self.quit)
        else:
            m.add_command(label="退出", command=self.quit)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def reset_pos(self):
        left, top, right, bottom = work_area()
        x = right - W - int(24 * S)
        y = bottom - H - int(24 * S)
        self.root.geometry("+%d+%d" % (x, y))
        self.cfg["x"], self.cfg["y"] = x, y
        save_cfg(self.cfg)

    def quit(self):
        if self.job:
            try:
                self.root.after_cancel(self.job)
            except Exception:
                pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------ 开机自启

    def _run_key(self):
        import winreg
        return winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"

    def autostart_cmd(self):
        try:
            import winreg
            hkey, path = self._run_key()
            with winreg.OpenKey(hkey, path) as k:
                val, _ = winreg.QueryValueEx(k, "KimiUsageWidget")
                return val
        except Exception:
            return None

    def autostart_on(self):
        try:
            import winreg
            exe = sys.executable or ""
            pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.isfile(pyw):
                exe = pyw
            script = os.path.abspath(__file__)
            cmd = '"%s" "%s"' % (exe, script)
            hkey, path = self._run_key()
            with winreg.OpenKey(hkey, path, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "KimiUsageWidget", 0, winreg.REG_SZ, cmd)
        except Exception as e:
            messagebox.showerror("Kimi 额度", "设置失败：%s" % e)

    def autostart_off(self):
        try:
            import winreg
            hkey, path = self._run_key()
            with winreg.OpenKey(hkey, path, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "KimiUsageWidget")
        except Exception:
            pass

    def toggle_autostart(self):
        if self.autostart_cmd():
            self.autostart_off()
        else:
            self.autostart_on()
        self.update_tray()

    # ------------------------------------------------------------ 刷新

    def tick(self):
        self.refresh()
        self.job = self.root.after(REFRESH_MS, self.tick)

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        self.draw()
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            data = fetch(self.key)
            self.q.put(("data", data, None))
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == 401:
                msg = "API Key 无效"
            elif code == 403:
                msg = "无访问权限"
            elif code:
                msg = "接口错误 HTTP %s" % code
            else:
                msg = "网络连接失败"
            self.q.put(("data", None, msg))

    def apply_data(self, data, err):
        self.busy = False
        if data:
            self.data = data
            self.err = None
            self.last_ok = datetime.datetime.now()
        elif err:
            self.err = "获取失败：" + err
        self.draw()
        self.update_tray()


# ---------------------------------------------------------------- 入口

def main():
    global S, W, H, PAD

    S = setup_dpi()
    W = int(round(BASE_W * S))
    H = int(round(BASE_H * S))
    PAD = int(round(BASE_PAD * S))

    if not single_instance():
        return

    cfg = load_cfg()
    key, src = resolve_key(cfg)

    if not key:
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        key = simpledialog.askstring(
            "Kimi 额度",
            "没找到 Kimi API Key。\n\n"
            "请粘贴 Kimi For Coding 的 API Key（sk- 开头）。\n"
            "它会用 Windows 凭据加密后只存在本机，不会上传。",
            parent=root,
        )
        root.destroy()
        key = (key or "").strip()
        if not key:
            return
        if not store_key(cfg, key):
            messagebox.showerror("Kimi 额度", "Key 加密保存失败，本次可用但重启后需要重新输入。")
        src = "手动输入"

    App(cfg, key, src).root.mainloop()


if __name__ == "__main__":
    main()
