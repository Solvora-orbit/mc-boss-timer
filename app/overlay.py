"""游戏内悬浮窗：置顶、半透明、可拖动的小窗。

游戏中也能看倒计时、一键标记击杀 / 重置、快速添加 Boss，
以及管理 OCR 识别、复活提醒、全局热键等本机功能。
数据来自目标主机的 /api/state（默认本机，可指向队友主机/虚拟组网地址）。
"""
import json
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

from . import netclient, notify, settings, watcher
from .config import DATA_DIR

FETCH_INTERVAL = 1.0          # 向服务器拉取全量状态的间隔（秒）
TICK_MS = 500                 # 界面刷新间隔（毫秒）
BANNER_MS = 8000              # 提醒条显示时长（毫秒）

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
STATUS_COLOR = {"idle": "dim", "waiting": "blue", "possible": "orange", "respawned": "green"}


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return "--:--:--"
    neg = seconds < 0
    s = int(abs(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{'+' if neg else ''}{h:02d}:{m:02d}:{s:02d}"


def _blink_color(t: float, base: str) -> str:
    """最后 60 秒的红色闪烁：按整秒交替深浅。"""
    return "#ef5350" if int(t) % 2 == 0 else "#8c2f2d"


class Overlay:
    def __init__(self, base_url: str, is_host: bool):
        # 高 DPI 屏幕下让 tkinter 使用真实像素，保证框选区域坐标与截屏一致
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        self.is_host = is_host
        saved = settings.load()["server"]["base_url"]
        self.base_url = netclient.validate_base_url(saved) or \
            netclient.validate_base_url(base_url) or "http://127.0.0.1:8000"
        self.bosses: list[dict] = []
        self.server_offset = 0.0
        self.collapsed = False
        self._events = queue.Queue()   # 抓取线程 -> UI 线程
        self._wake = threading.Event() # 操作后立即触发一次抓取
        self._row_widgets: dict[str, dict] = {}
        self._banner_job = None
        self._watcher_running = None

        self.root = tk.Tk()
        self.root.title("MC Boss 计时器 - 悬浮窗")
        self.root.configure(bg=C["border"])
        self.root.overrideredirect(True)      # 无边框
        self.root.attributes("-topmost", True)
        # 恢复上次的位置/透明度（配置持久化，重开软件不用重摆）
        ov = settings.load()["overlay"]
        self.root.attributes("-alpha", float(ov.get("alpha", 0.92)))
        self.root.geometry(f"+{int(ov.get('x', 80))}+{int(ov.get('y', 80))}")
        self.collapsed = bool(ov.get("collapsed", False))

        self.font = tkfont.Font(family="Microsoft YaHei", size=10)
        self.mono = tkfont.Font(family="Consolas", size=11, weight="bold")
        self.small = tkfont.Font(family="Microsoft YaHei", size=9)

        self._build_ui()
        if self.collapsed:      # 恢复折叠状态
            self.rows_frame.pack_forget()
            self.status_bar.pack_forget()
        self._rebuild_rows()   # 初始空列表也要显示提示文案
        self._bind_drag()

        # 注册进程内回调：OCR 确认、提醒条、系统通知、识别线程状态
        watcher.confirm_handler = self._on_ocr_confirm
        notify.banner_handler = self.show_banner
        notify.toast_handler = self.show_toast
        watcher.on_status(self._on_watcher_status)

        threading.Thread(target=self._fetch_loop, daemon=True).start()
        self.root.after(TICK_MS, self._tick)

    # ---------- UI ----------

    def _build_ui(self):
        # 提醒条（复活到点 / OCR 命中等），默认隐藏
        self.banner = tk.Label(self.root, text="", bg="#5d4037", fg="#ffe0b2",
                               font=self.font, padx=8, pady=4, anchor="w")

        # 标题栏：拖动手柄 + 操作按钮
        bar = tk.Frame(self.root, bg=C["panel"], padx=6, pady=4)
        bar.pack(fill="x")
        tk.Label(bar, text="⛏ Boss计时器", bg=C["panel"], fg=C["text"],
                 font=self.font).pack(side="left")
        self.ocr_toggle = tk.Label(bar, text="👁", bg=C["panel"], fg=C["dim"],
                                   font=self.font, padx=5, cursor="hand2")
        self.ocr_toggle.pack(side="right")
        self.ocr_toggle.bind("<Button-1>", lambda e: self._toggle_ocr())
        self.scan_btn = tk.Label(bar, text="🔍", bg=C["panel"], fg=C["dim"],
                                 font=self.font, padx=5, cursor="hand2")
        self.scan_btn.pack(side="right")
        self.scan_btn.bind("<Button-1>", lambda e: self._manual_scan())
        for text, color, cmd in (
            ("⚙", C["dim"], self._open_settings),
            ("＋", C["green"], self._open_quick_add),
            ("—", C["dim"], self._toggle_collapse),
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
            # 最后 60 秒红色闪烁（已过最早时间的区间 Boss 显示 +XX:XX:XX 不闪）
            if 0 < left <= 60 and status == "waiting":
                color = _blink_color(t, color)
            w["cd"].config(text=text, fg=color)

    def _tick(self):
        # 1. 收取抓取线程的结果与后台事件（banner/确认框/OCR状态）
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "state":
                    self.server_offset = payload["server_time"] - time.time()
                    old_ids = [b["id"] for b in self.bosses]
                    new_ids = [b["id"] for b in payload["bosses"]]
                    self.bosses = payload["bosses"]
                    if old_ids != new_ids:
                        self._rebuild_rows()
                    self._update_rows()
                    self._update_status_bar(online=True)
                elif kind == "error":
                    self._update_status_bar(online=False)
                elif kind == "banner":
                    self._show_banner_ui(payload)
                elif kind == "toast":
                    self._show_toast_ui(payload)
                elif kind == "confirm":
                    self._show_kill_confirm_ui(payload)
                elif kind == "watcher":
                    running = bool(payload.get("running"))
                    if running != self._watcher_running:
                        self._watcher_running = running
                        self.ocr_toggle.config(fg=C["green"] if running else C["dim"])
                        self._update_status_bar(online=True)
        except queue.Empty:
            pass
        # 2. 本地时钟推进倒计时（两次抓取之间也走秒）
        self._update_rows()
        self.root.after(TICK_MS, self._tick)

    def _update_status_bar(self, online: bool):
        if online:
            text = f"✔ {self.base_url}"
            color = C["green"]
        else:
            text = "✘ 连接断开，重试中…"
            color = C["red"]
        if self._watcher_running:
            text += "　👁 OCR运行中"
            color = C["green"]
        self.status_bar.config(text=text, fg=color)

    # ---------- 回调（来自后台线程，只投递事件，UI 线程消费） ----------

    def show_banner(self, text: str):
        self._events.put(("banner", text))

    def show_toast(self, text: str):
        self._events.put(("toast", text))

    def _show_toast_ui(self, text: str):
        """Windows 系统通知（必须在主线程调用）。

        注意：winrt 静态投影限制，须经 ToastNotificationManager.get_default()
        的 create_toast_notifier_with_id 创建；系统未对该 AUMID 开启横幅时
        通知会静默进入通知中心，声音与悬浮窗提醒条不受影响。
        """
        try:
            from xml.sax.saxutils import escape

            from winrt.windows.data.xml import dom as xml_dom
            from winrt.windows.ui.notifications import ToastNotification
            from winrt.windows.ui.notifications import ToastNotificationManager

            title, _, body = text.partition("：")
            doc = xml_dom.XmlDocument()
            doc.load_xml(
                "<toast><visual><binding template='ToastGeneric'>"
                f"<text>{escape(title)}</text><text>{escape(body or title)}</text>"
                "</binding></visual></toast>"
            )
            notifier = ToastNotificationManager.get_default().create_toast_notifier_with_id(
                "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe")
            notifier.show(ToastNotification(doc))
        except Exception:
            pass

    def _show_banner_ui(self, text: str):
        """提醒条从顶部滑入，停留 BANNER_MS 后消失。"""
        self.banner.config(text=text)
        self.banner.pack_forget()
        # 先放到屏幕外，再分步滑入（简单帧动画）
        self.banner.place(relx=0, y=-30, relwidth=1)
        step = 6

        def slide(y):
            if y >= 0:
                self.banner.place_forget()
                self.banner.pack(fill="x", before=self.root.winfo_children()[1])
                return
            self.banner.place(relx=0, y=y, relwidth=1)
            self.root.after(16, lambda: slide(y + step))

        slide(-30)
        if self._banner_job:
            self.root.after_cancel(self._banner_job)
        self._banner_job = self.root.after(BANNER_MS, lambda: self.banner.pack_forget())

    def _on_watcher_status(self, st: dict):
        self._events.put(("watcher", st))

    def _on_ocr_confirm(self, boss: dict):
        """OCR 确认模式命中：投递事件，UI 线程弹确认框。"""
        self._events.put(("confirm", boss))

    def _show_kill_confirm_ui(self, boss: dict):
        win = tk.Toplevel(self.root)
        win.title("OCR 识别到击杀")
        win.configure(bg=C["panel"], padx=14, pady=10)
        win.attributes("-topmost", True)
        tk.Label(win, text=f"识别到「{boss['name']}」的击杀关键词",
                 bg=C["panel"], fg=C["text"], font=self.font).pack()
        tk.Label(win, text="要标记为已击杀吗？", bg=C["panel"], fg=C["dim"],
                 font=self.small).pack(pady=(2, 8))
        btns = tk.Frame(win, bg=C["panel"])
        btns.pack()
        tk.Button(btns, text="标记击杀", bg="#2e7d32", fg="white", relief="flat",
                  font=self.font, command=lambda: (self._quick_action(boss["id"], "kill", source="ocr"),
                                                   win.destroy())).pack(side="left", padx=4)
        tk.Button(btns, text="忽略", bg=C["btn"], fg=C["text"], relief="flat",
                  font=self.font, command=win.destroy).pack(side="left", padx=4)

    # ---------- 网络 ----------

    def _fetch_loop(self):
        while True:
            try:
                data = netclient.get_json(f"{self.base_url}/api/state", timeout=2)
                self._events.put(("state", data))
            except Exception:
                self._events.put(("error", None))
            self._wake.wait(FETCH_INTERVAL)
            self._wake.clear()

    def _quick_action(self, boss_id: str, action: str, source: str = "manual"):
        def run():
            try:
                body = {"source": source} if action == "kill" else {}
                netclient.post_json(f"{self.base_url}/api/bosses/{boss_id}/{action}", body)
                self._wake.set()
            except Exception:
                self._events.put(("error", None))
        threading.Thread(target=run, daemon=True).start()

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
            def run():
                try:
                    netclient.post_json(f"{self.base_url}/api/bosses",
                                        {"name": n, "mode": "fixed", "respawn_minutes": m})
                    self._wake.set()
                except Exception:
                    pass
            threading.Thread(target=run, daemon=True).start()
            win.destroy()

        tk.Button(win, text="添加", command=submit, bg="#2e7d32", fg="white",
                  activebackground="#4caf50", activeforeground="white",
                  relief="flat", font=self.font, width=8).grid(row=2, column=0,
                                                               columnspan=2, pady=(8, 0))
        name.focus_set()

    # ---------- 设置窗 ----------

    def _open_settings(self):
        cfg = settings.load()
        win = self._centered_toplevel("设置")
        win.geometry("+200+140")
        nb = tk.Frame(win, bg=C["panel"])
        nb.pack(fill="both", expand=True)
        row = 0

        def section(title):
            nonlocal row
            lbl = tk.Label(nb, text=title, bg=C["panel"], fg=C["blue"], font=self.font)
            lbl.grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))
            row += 1

        def add_check(text, var):
            nonlocal row
            cb = tk.Checkbutton(nb, text=text, variable=var, bg=C["panel"], fg=C["text"],
                                selectcolor=C["bg"], activebackground=C["panel"],
                                font=self.small, anchor="w")
            cb.grid(row=row, column=0, columnspan=2, sticky="w")
            row += 1

        def add_row(label, widget):
            nonlocal row
            tk.Label(nb, text=label, bg=C["panel"], fg=C["text"],
                     font=self.small).grid(row=row, column=0, sticky="w", pady=2)
            widget.grid(row=row, column=1, sticky="e", pady=2)
            row += 1

        # —— 服务器 ——
        section("服务器")
        srv = tk.Entry(nb, bg=C["bg"], fg=C["text"], insertbackground=C["text"],
                       relief="flat", font=self.small, width=26)
        srv.insert(0, self.base_url)
        add_row("API 地址", srv)

        # —— OCR 识别 ——
        section("OCR 自动识别击杀")
        ocr = cfg["ocr"]
        v_ocr_on = tk.BooleanVar(value=ocr["enabled"])
        v_engine = tk.StringVar(value=ocr["engine"])
        v_interval = tk.StringVar(value=str(ocr["interval"]))
        v_mode = tk.StringVar(value=ocr["mode"])
        v_cooldown = tk.StringVar(value=str(ocr["cooldown"]))
        add_check("开启（游戏内自动识别击杀）", v_ocr_on)
        engine_combo = tk.OptionMenu(nb, v_engine, "windows", "rapidocr")
        engine_combo.config(bg=C["btn"], fg=C["text"], relief="flat",
                            activebackground="#3a4354", font=self.small)
        engine_combo.nametowidget(engine_combo.menuname).config(font=self.small)
        add_row("引擎", engine_combo)
        interval_spin = tk.Spinbox(nb, from_=1, to=60, textvariable=v_interval, width=5,
                                   bg=C["bg"], fg=C["text"], relief="flat",
                                   insertbackground=C["text"], buttonbackground=C["btn"])
        add_row("识别间隔(秒)", interval_spin)
        mode_frame = tk.Frame(nb, bg=C["panel"])
        tk.Radiobutton(mode_frame, text="自动标记", variable=v_mode, value="auto",
                       bg=C["panel"], fg=C["text"], selectcolor=C["bg"],
                       activebackground=C["panel"], font=self.small).pack(side="left")
        tk.Radiobutton(mode_frame, text="仅提醒待确认", variable=v_mode, value="confirm",
                       bg=C["panel"], fg=C["text"], selectcolor=C["bg"],
                       activebackground=C["panel"], font=self.small).pack(side="left", padx=6)
        add_row("触发方式", mode_frame)
        cooldown_spin = tk.Spinbox(nb, from_=5, to=3600, textvariable=v_cooldown, width=5,
                                   bg=C["bg"], fg=C["text"], relief="flat",
                                   insertbackground=C["text"], buttonbackground=C["btn"])
        add_row("冷却(秒)", cooldown_spin)
        region_text = "全屏" if not ocr.get("region") else \
            f"x={ocr['region']['x']}, y={ocr['region']['y']}, {ocr['region']['w']}×{ocr['region']['h']}"
        v_region_label = tk.Label(nb, text=region_text, bg=C["panel"], fg=C["dim"],
                                  font=self.small)
        add_row("监测区域", v_region_label)
        region_btns = tk.Frame(nb, bg=C["panel"])
        tk.Button(region_btns, text="框选区域", bg=C["btn"], fg=C["text"], relief="flat",
                  font=self.small, command=lambda: self._open_region_selector(
                      lambda r: (v_region_label.config(
                          text="全屏" if not r else f"x={r['x']}, y={r['y']}, {r['w']}×{r['h']}"),
                          win.lift()))).pack(side="left")
        tk.Button(region_btns, text="清除", bg=C["btn"], fg=C["text"], relief="flat",
                  font=self.small, command=lambda: (settings.update({"ocr": {"region": None}}),
                                                    v_region_label.config(text="全屏"))).pack(side="left", padx=4)
        nb.grid_columnconfigure(1, weight=1)
        row += 0
        tk.Label(nb, text="关键词在每个 Boss 的编辑里配置（逗号分隔）\n建议用短词，如：末影龙,击杀",
                 bg=C["panel"], fg=C["dim"], font=self.small, justify="left").grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1

        # —— 复活提醒 ——
        section("复活到点提醒")
        nf = cfg["notify"]
        v_notify_on = tk.BooleanVar(value=nf["enabled"])
        v_sound = tk.BooleanVar(value=nf["sound"])
        v_toast = tk.BooleanVar(value=nf["toast"])
        v_banner = tk.BooleanVar(value=nf["banner"])
        v_range_mode = tk.StringVar(value=nf["range_mode"])
        add_check("开启", v_notify_on)
        add_check("提示音", v_sound)
        add_check("Windows 系统通知", v_toast)
        add_check("悬浮窗提醒条", v_banner)
        rm_frame = tk.Frame(nb, bg=C["panel"])
        tk.Radiobutton(rm_frame, text="区间两端都提醒", variable=v_range_mode, value="both",
                       bg=C["panel"], fg=C["text"], selectcolor=C["bg"],
                       activebackground=C["panel"], font=self.small).pack(side="left")
        tk.Radiobutton(rm_frame, text="仅最早复活", variable=v_range_mode, value="earliest",
                       bg=C["panel"], fg=C["text"], selectcolor=C["bg"],
                       activebackground=C["panel"], font=self.small).pack(side="left", padx=6)
        add_row("区间 Boss", rm_frame)

        # —— 全局热键 ——
        section("全局热键")
        hk = cfg["hotkeys"]
        v_hk_on = tk.BooleanVar(value=hk["enabled"])
        v_kill_key = tk.StringVar(value=hk["kill"])
        v_reset_key = tk.StringVar(value=hk["reset"])
        add_check("开启（游戏内直接按键，无需切窗）", v_hk_on)
        kill_entry = tk.Entry(nb, textvariable=v_kill_key, width=14, bg=C["bg"], fg=C["text"],
                              insertbackground=C["text"], relief="flat", font=self.small)
        add_row("击杀热键", kill_entry)
        reset_entry = tk.Entry(nb, textvariable=v_reset_key, width=14, bg=C["bg"], fg=C["text"],
                               insertbackground=C["text"], relief="flat", font=self.small)
        add_row("重置热键", reset_entry)
        tk.Label(nb, text="作用目标：倒计时中最先复活的 Boss", bg=C["panel"], fg=C["dim"],
                 font=self.small).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        # —— 悬浮窗外观 ——
        section("悬浮窗外观")
        ov = cfg["overlay"]
        v_alpha = tk.DoubleVar(value=float(ov.get("alpha", 0.92)))
        alpha_scale = tk.Scale(nb, from_=0.6, to=1.0, resolution=0.02, orient="horizontal",
                               variable=v_alpha, bg=C["panel"], fg=C["text"],
                               highlightthickness=0, troughcolor=C["bg"], length=180)
        alpha_scale.set(float(ov.get("alpha", 0.92)))
        add_row("不透明度", alpha_scale)
        # 拖动滑条实时预览透明度
        alpha_scale.config(command=lambda v: self.root.attributes("-alpha", float(v)))

        def save():
            srv_url = netclient.validate_base_url(srv.get().strip())
            if srv_url:
                self.base_url = srv_url
            settings.update({
                "server": {"base_url": self.base_url},
                "overlay": {"alpha": round(float(v_alpha.get()), 2)},
                "ocr": {
                    "enabled": v_ocr_on.get(),
                    "engine": v_engine.get(),
                    "interval": max(1, int(float(v_interval.get() or 3))),
                    "mode": v_mode.get(),
                    "cooldown": max(5, int(float(v_cooldown.get() or 60))),
                },
                "notify": {
                    "enabled": v_notify_on.get(),
                    "sound": v_sound.get(),
                    "toast": v_toast.get(),
                    "banner": v_banner.get(),
                    "range_mode": v_range_mode.get(),
                },
                "hotkeys": {
                    "enabled": v_hk_on.get(),
                    "kill": v_kill_key.get().strip() or "ctrl+alt+k",
                    "reset": v_reset_key.get().strip() or "ctrl+alt+r",
                },
            })
            self._wake.set()
            win.destroy()

        tk.Button(win, text="保存设置", command=save, bg="#2e7d32", fg="white",
                  activebackground="#4caf50", activeforeground="white",
                  relief="flat", font=self.font, width=12).pack(pady=(12, 0))

    def _toggle_ocr(self):
        cfg = settings.load()["ocr"]
        settings.update({"ocr": {"enabled": not cfg["enabled"]}})
        self.show_banner("OCR 识别已开启" if not cfg["enabled"] else "OCR 识别已关闭")

    def _manual_scan(self):
        """🔍 立即识别一次：常驻模式也可用，手动模式的唯一触发入口。"""
        cfg = settings.load()["ocr"]
        if not cfg.get("enabled"):
            self.show_banner("请先点 👁 开启 OCR 识别")
            return
        self.show_banner("正在识别屏幕…")

        def run():
            res = watcher.scan_once()
            if res.get("error"):
                self.show_banner(f"识别失败：{res['error'][:60]}")
            elif res.get("hits"):
                self.show_banner(f"命中：{'、'.join(res['hits'])}")
            else:
                self.show_banner(f"识别完成，未命中（{(res.get('text') or '无文字')[:40]}）")
        threading.Thread(target=run, daemon=True).start()

    # ---------- 区域框选 ----------

    def _open_region_selector(self, on_done=None):
        """全屏覆盖层拖拽框选监测区域，选中后写入设置。"""
        try:
            shot = watcher.grab_region(None)  # 主屏全屏
        except Exception as e:
            self.show_banner(f"截屏失败: {e}")
            return
        from PIL import ImageTk

        top = tk.Toplevel(self.root)
        top.attributes("-fullscreen", True)
        top.attributes("-topmost", True)
        top.configure(bg="black", cursor="crosshair")

        dim = _blend_black(shot, 0.45)
        photo = ImageTk.PhotoImage(dim, master=top)
        canvas = tk.Canvas(top, highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        canvas.create_image(0, 0, image=photo, anchor="nw")

        # 缩放比对：DPI aware 下画布像素 == 截屏像素；保险起见按比例换算
        state_box = {"start": None, "rect": None}

        hint = canvas.create_text(
            shot.width // 2, 40, text="拖拽框选游戏内提示文字出现的区域（如左下角聊天栏）",
            fill="#ffe0b2", font=("Microsoft YaHei", 16, "bold"))

        def to_screen(x, y):
            return int(x), int(y)

        def on_press(e):
            state_box["start"] = to_screen(e.x, e.y)
            if state_box["rect"]:
                canvas.delete(state_box["rect"])
            state_box["rect"] = canvas.create_rectangle(
                e.x, e.y, e.x, e.y, outline="#4caf50", width=2)

        def on_drag(e):
            if not state_box["start"]:
                return
            canvas.coords(state_box["rect"], state_box["start"][0], state_box["start"][1],
                          e.x, e.y)

        def on_release(e):
            if not state_box["start"]:
                return
            x0, y0 = state_box["start"]
            x1, y1 = to_screen(e.x, e.y)
            state_box["region"] = {
                "x": min(x0, x1), "y": min(y0, y1),
                "w": abs(x1 - x0), "h": abs(y1 - y0),
            }
            canvas.itemconfig(hint, text=f"已选 {state_box['region']['w']}×{state_box['region']['h']}"
                                         "　点击下方按钮确认", fill="#4caf50")

        state_box["region"] = None
        canvas.bind("<Button-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)

        bar = tk.Frame(top, bg="#1e232c", padx=10, pady=6)
        # place 在底部中央
        bar.place(relx=0.5, rely=0.97, anchor="s")

        def confirm():
            if state_box["region"] and state_box["region"]["w"] > 10 and state_box["region"]["h"] > 10:
                settings.update({"ocr": {"region": state_box["region"]}})
                if on_done:
                    on_done(state_box["region"])
            top.destroy()

        tk.Button(bar, text="✔ 确认区域", command=confirm, bg="#2e7d32", fg="white",
                  relief="flat", font=self.font, width=12).pack(side="left", padx=4)
        tk.Button(bar, text="使用全屏", command=lambda: (settings.update({"ocr": {"region": None}}),
                                                         on_done(None) if on_done else None,
                                                         top.destroy()),
                  bg=C["btn"], fg=C["text"], relief="flat", font=self.font, width=10).pack(side="left", padx=4)
        tk.Button(bar, text="✕ 取消", command=top.destroy, bg="#7f2220", fg="white",
                  relief="flat", font=self.font, width=8).pack(side="left", padx=4)

    # ---------- 窗口行为 ----------

    def _toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.rows_frame.pack_forget()
            self.status_bar.pack_forget()
        else:
            self.rows_frame.pack(fill="x")
            self.status_bar.pack(fill="x")
        settings.update({"overlay": {"collapsed": self.collapsed}})   # 记住折叠状态

    def _save_position(self):
        """拖动结束后记录窗口位置，下次启动恢复。"""
        try:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            settings.update({"overlay": {"x": x, "y": y}})
        except Exception:
            pass

    def _bind_drag(self):
        def start(e):
            self._dx, self._dy = e.x, e.y

        def move(e):
            self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

        def release(e):
            self._save_position()

        # 标题栏与行容器的空白处可拖动；内部按钮/输入框有自己的事件
        bar = self.root.winfo_children()[1]  # [0] 是隐藏的 banner
        for widget in (bar, self.rows_frame):
            widget.bind("<Button-1>", start)
            widget.bind("<B1-Motion>", move)
            widget.bind("<ButtonRelease-1>", release)

    def _close(self):
        self._save_position()   # 退出前记住位置
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


def _blend_black(img, factor: float):
    """图片压暗，用于全屏框选时的背景。"""
    from PIL import Image

    black = Image.new("RGB", img.size, "black")
    return Image.blend(img.convert("RGB"), black, factor)
