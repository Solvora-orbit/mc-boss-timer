"""游戏内悬浮窗：置顶、半透明、可拖动的小窗。

游戏中也能看倒计时、一键标记击杀 / 重置、快速添加 Boss。
数据来自目标主机的 /api/state（默认本机，客户端模式下可指向队友主机）。
"""
import ipaddress
import json
import queue
import socket
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
from urllib.parse import urlparse

from .config import DATA_DIR

FETCH_INTERVAL = 1.0          # 向服务器拉取全量状态的间隔（秒）
TICK_MS = 500                 # 界面刷新间隔（毫秒）

C = {
    "bg": "#15181e",
    "panel": "#1e232c",
    "border": "#2e3542",
    "text": "#e6e9ef",
    "dim": "#8b93a3",
    "blue": "#42a5f5",
    "orange": "#ff9800",
    "green": "#4caf50",
    "red": "#ef5350",
    "btn": "#2a3140",
}
STATUS_TEXT = {
    "idle": "未开始",
    "waiting": "倒计时中",
    "possible": "可能已复活",
    "respawned": "应已复活",
}
STATUS_COLOR = {"idle": "dim", "waiting": "blue", "possible": "orange", "respawned": "green"}


def validate_base_url(url: str) -> str | None:
    """校验用户输入的服务器地址，返回规范化后的 URL，非法则返回 None。

    本工具是局域网共享客户端，网络边界被显式限定为「仅限本机/私网地址」：
    主机名必须解析到环回、私网或链路本地 IP 才允许请求，
    公网地址与无法解析的地址一律拒绝。
    """
    if not isinstance(url, str) or not url or any(ch.isspace() for ch in url):
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    # 禁止内嵌凭据、查询串等附加成分，避免 URL 结构被滥用
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    try:
        port = parsed.port  # 超范围端口会在此抛 ValueError
    except ValueError:
        return None
    if port is not None and not (1 <= port <= 65535):
        return None
    if not _host_is_lan(parsed.hostname):
        return None
    return url.rstrip("/")


def _host_is_lan(host: str) -> bool:
    """主机名（或 IP 字面量）必须解析到环回/私网/链路本地地址。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        except (OSError, ValueError):
            return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


class _LANRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向目标必须同样通过局域网边界校验，否则中止。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if validate_base_url(newurl) is None:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_LANRedirectHandler())


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return "--:--:--"
    neg = seconds < 0
    s = int(abs(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{'+' if neg else ''}{h:02d}:{m:02d}:{s:02d}"


class Overlay:
    def __init__(self, base_url: str, is_host: bool):
        self.base_url = validate_base_url(base_url) or "http://127.0.0.1:8000"
        self.is_host = is_host
        self.bosses: list[dict] = []
        self.server_offset = 0.0
        self.online = False
        self.collapsed = False
        self._events = queue.Queue()   # 抓取线程 -> UI 线程
        self._wake = threading.Event() # 操作后立即触发一次抓取
        self._row_widgets: dict[str, dict] = {}
        self._settings_file = DATA_DIR / "overlay.json"
        self._load_settings()

        self.root = tk.Tk()
        self.root.title("MC Boss 计时器 - 悬浮窗")
        self.root.configure(bg=C["border"])
        self.root.overrideredirect(True)      # 无边框
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.geometry("+80+80")

        self.font = tkfont.Font(family="Microsoft YaHei", size=10)
        self.mono = tkfont.Font(family="Consolas", size=11, weight="bold")
        self.small = tkfont.Font(family="Microsoft YaHei", size=9)

        self._build_ui()
        self._rebuild_rows()   # 初始空列表也要显示提示文案
        self._bind_drag()
        threading.Thread(target=self._fetch_loop, daemon=True).start()
        self.root.after(TICK_MS, self._tick)

    # ---------- UI ----------

    def _build_ui(self):
        # 标题栏：拖动手柄 + 操作按钮
        bar = tk.Frame(self.root, bg=C["panel"], padx=6, pady=4)
        bar.pack(fill="x")
        tk.Label(bar, text="⛏ Boss计时器", bg=C["panel"], fg=C["text"],
                 font=self.font).pack(side="left")
        for text, color, cmd in (
            ("＋", C["green"], self._open_quick_add),
            ("—", C["dim"], self._toggle_collapse),
            ("⚙", C["dim"], self._open_settings),
            ("✕", C["red"], self._close),
        ):
            lbl = tk.Label(bar, text=text, bg=C["panel"], fg=color,
                           font=self.font, padx=5, cursor="hand2")
            lbl.pack(side="right")
            lbl.bind("<Button-1>", lambda e, f=cmd: f())

        # Boss 行容器
        self.rows_frame = tk.Frame(self.root, bg=C["bg"], padx=6, pady=4)
        self.rows_frame.pack(fill="x")

        # 底部状态栏
        self.status_bar = tk.Label(self.root, text="连接中…", anchor="w",
                                   bg=C["panel"], fg=C["dim"], font=self.small, padx=6, pady=2)
        self.status_bar.pack(fill="x")

    def _rebuild_rows(self):
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self._row_widgets.clear()
        if not self.bosses:
            tk.Label(self.rows_frame, text="暂无 Boss，去网页添加\n或点右上角 ＋ 快速添加",
                     bg=C["bg"], fg=C["dim"], font=self.small,
                     justify="center").grid(row=0, column=0, columnspan=3, pady=6)
            return
        for i, b in enumerate(self.bosses):
            name_lbl = tk.Label(self.rows_frame, text=b["name"][:8], bg=C["bg"],
                                fg=C["text"], font=self.font, width=8, anchor="w")
            name_lbl.grid(row=i, column=0, sticky="w")
            cd_lbl = tk.Label(self.rows_frame, text="--:--:--", bg=C["bg"],
                              fg=C["dim"], font=self.mono, width=9, anchor="e")
            cd_lbl.grid(row=i, column=1, sticky="e", padx=(2, 4))
            btns = tk.Frame(self.rows_frame, bg=C["bg"])
            btns.grid(row=i, column=2)
            for text, color, act in (("杀", C["green"], "kill"), ("↺", C["dim"], "reset")):
                lbl = tk.Label(btns, text=text, bg=C["btn"], fg=color, font=self.small,
                               padx=5, pady=1, cursor="hand2")
                lbl.pack(side="left", padx=1)
                lbl.bind("<Button-1>", lambda e, bid=b["id"], a=act: self._quick_action(bid, a))
            self._row_widgets[b["id"]] = {"cd": cd_lbl}

    def _update_rows(self):
        t = time.time() + self.server_offset
        for b in self.bosses:
            w = self._row_widgets.get(b["id"])
            if not w:
                continue
            status = b["status"]
            if status == "idle":
                w["cd"].config(text="未开始", fg=C["dim"])
                continue
            left = b["next_at"] - t
            if b["mode"] == "range":
                text = "应已复活" if status == "respawned" else _fmt(left)
                color = C["green"] if status == "respawned" else C["orange"] if status == "possible" else C["blue"]
            else:
                text = "已复活" if status == "respawned" else _fmt(left)
                color = C["green"] if status == "respawned" else C["blue"]
            w["cd"].config(text=text, fg=color)

    def _tick(self):
        # 1. 收取抓取线程的结果
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "state":
                    self.online = True
                    self.server_offset = payload["server_time"] - time.time()
                    old_ids = [b["id"] for b in self.bosses]
                    new_ids = [b["id"] for b in payload["bosses"]]
                    self.bosses = payload["bosses"]
                    if old_ids != new_ids:
                        self._rebuild_rows()
                    self._update_rows()
                    self.status_bar.config(text=f"✔ {self.base_url}", fg=C["green"])
                elif kind == "error":
                    self.online = False
                    self.status_bar.config(text="✘ 连接断开，重试中…", fg=C["red"])
        except queue.Empty:
            pass
        # 2. 本地时钟推进倒计时（两次抓取之间也走秒）
        self._update_rows()
        self.root.after(TICK_MS, self._tick)

    # ---------- 网络 ----------

    def _fetch_loop(self):
        while True:
            try:
                with _opener.open(f"{self.base_url}/api/state", timeout=2) as r:
                    data = json.loads(r.read())
                self._events.put(("state", data))
            except Exception:
                self._events.put(("error", None))
            self._wake.wait(FETCH_INTERVAL)
            self._wake.clear()

    def _request(self, path: str, body: dict | None = None, method: str = "POST"):
        def run():
            try:
                data = json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(
                    f"{self.base_url}{path}", data=data, method=method,
                    headers={"Content-Type": "application/json"})
                _opener.open(req, timeout=3)
                self._wake.set()
            except Exception:
                self._events.put(("error", None))
        threading.Thread(target=run, daemon=True).start()

    def _quick_action(self, boss_id: str, action: str):
        self._request(f"/api/bosses/{boss_id}/{action}")

    # ---------- 弹窗 ----------

    def _centered_toplevel(self, title: str) -> tk.Toplevel:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=C["panel"], padx=12, pady=10)
        win.attributes("-topmost", True)
        win.transient(self.root)
        return win

    def _open_quick_add(self):
        win = self._centered_toplevel("快速添加 Boss（固定间隔）")
        tk.Label(win, text="Boss 名称", bg=C["panel"], fg=C["text"],
                 font=self.font).grid(row=0, column=0, sticky="w", pady=2)
        name = tk.Entry(win, bg=C["bg"], fg=C["text"], insertbackground=C["text"],
                        relief="flat", font=self.font, width=18)
        name.grid(row=0, column=1, pady=2)
        tk.Label(win, text="复活间隔(分)", bg=C["panel"], fg=C["text"],
                 font=self.font).grid(row=1, column=0, sticky="w", pady=2)
        minutes = tk.Entry(win, bg=C["bg"], fg=C["text"], insertbackground=C["text"],
                           relief="flat", font=self.font, width=18)
        minutes.insert(0, "60")
        minutes.grid(row=1, column=1, pady=2)

        def submit():
            n = name.get().strip()
            try:
                m = float(minutes.get())
            except ValueError:
                m = 0
            if not n or m <= 0:
                return
            self._request("/api/bosses", {"name": n, "mode": "fixed", "respawn_minutes": m})
            win.destroy()

        tk.Button(win, text="添加", command=submit, bg="#2e7d32", fg="white",
                  activebackground="#4caf50", activeforeground="white",
                  relief="flat", font=self.font, width=8).grid(row=2, column=0,
                                                               columnspan=2, pady=(8, 0))
        name.focus_set()

    def _open_settings(self):
        win = self._centered_toplevel("服务器地址")
        tk.Label(win, text="主机地址", bg=C["panel"], fg=C["text"],
                 font=self.font).grid(row=0, column=0, sticky="w", pady=2)
        entry = tk.Entry(win, bg=C["bg"], fg=C["text"], insertbackground=C["text"],
                         relief="flat", font=self.font, width=24)
        entry.insert(0, self.base_url)
        entry.grid(row=0, column=1, pady=2)
        tk.Label(win, text="仅支持局域网/本机地址\n如 http://192.168.1.5:8000",
                 bg=C["panel"], fg=C["dim"], font=self.small,
                 justify="left").grid(row=1, column=0, columnspan=2, sticky="w")

        def save():
            url = validate_base_url(entry.get().strip())
            if url:
                self.base_url = url
                self._save_settings()
                self._wake.set()
            win.destroy()

        tk.Button(win, text="保存", command=save, bg="#2e7d32", fg="white",
                  activebackground="#4caf50", activeforeground="white",
                  relief="flat", font=self.font, width=8).grid(row=2, column=0,
                                                               columnspan=2, pady=(8, 0))
        entry.focus_set()

    # ---------- 窗口行为 ----------

    def _toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.rows_frame.pack_forget()
            self.status_bar.pack_forget()
        else:
            self.rows_frame.pack(fill="x")
            self.status_bar.pack(fill="x")

    def _bind_drag(self):
        def start(e):
            self._dx, self._dy = e.x, e.y

        def move(e):
            self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

        # 标题栏与行容器的空白处可拖动；内部按钮/输入框有自己的事件
        bar = self.root.winfo_children()[0]
        for widget in (bar, self.rows_frame):
            widget.bind("<Button-1>", start)
            widget.bind("<B1-Motion>", move)

    def _load_settings(self):
        try:
            saved = json.loads(self._settings_file.read_text(encoding="utf-8"))
            url = validate_base_url(saved.get("base_url", ""))
            if url:
                self.base_url = url
        except (OSError, json.JSONDecodeError):
            pass

    def _save_settings(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self._settings_file.write_text(
                json.dumps({"base_url": self.base_url}, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass

    def _close(self):
        if self.is_host:
            from tkinter import messagebox
            if not messagebox.askyesno(
                    "确认退出",
                    "关闭悬浮窗将同时停止主机服务，队友将无法访问。\n确定退出吗？",
                    parent=self.root):
                return
        self.root.destroy()

    def run(self):
        self.root.mainloop()
