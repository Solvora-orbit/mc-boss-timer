"""OCR 击杀识别线程：截屏 -> OCR -> 关键词匹配 -> 自动标记击杀。

主机/客户端模式都可用，通过 settings.server.base_url 指向目标 API。
双引擎可切换（windows 系统自带 OCR / rapidocr 离线推理），懒加载，
选中哪个才导入哪个。所有配置每轮重读，改动即时生效。
"""
import threading
import time

import mss

from . import netclient, settings, storage

MAX_LOG = 200

# 进程内回调：悬浮窗注册后可收到「确认模式命中」通知
confirm_handler = None


class WatcherState:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_scan = 0.0
        self.last_text = ""
        self.last_hit = None     # 最近一次命中的 boss 名
        self.running = False
        self.error = ""
        self.logs: list[dict] = []  # {ts, kind, text}

    def log(self, kind: str, text: str):
        with self.lock:
            self.logs.append({"ts": time.time(), "kind": kind, "text": text[:300]})
            self.logs = self.logs[-MAX_LOG:]

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "last_scan": self.last_scan,
                "last_text": self.last_text,
                "last_hit": self.last_hit,
                "error": self.error,
            }


state = WatcherState()
_engine_cache: dict[str, object] = {}
_engine_lock = threading.Lock()


def status() -> dict:
    return state.snapshot()


def recent_log(limit: int = 50) -> list[dict]:
    with state.lock:
        return list(state.logs[-limit:])


def get_engine(name: str):
    """懒加载 OCR 引擎，返回 callable(pil_img) -> str。"""
    with _engine_lock:
        if name in _engine_cache:
            return _engine_cache[name]
        if name == "windows":
            import winocr

            def run_windows(pil_img, lang="zh-CN"):
                result = winocr.recognize_pil_sync(pil_img, lang)
                return result.get("text", "") if isinstance(result, dict) else str(result)

            _engine_cache[name] = run_windows
        elif name == "rapidocr":
            from rapidocr_onnxruntime import RapidOCR

            ocr = RapidOCR()

            def run_rapid(pil_img, lang=None):
                import numpy as np

                result, _ = ocr(np.asarray(pil_img))
                return " ".join(r[1] for r in result) if result else ""

            _engine_cache[name] = run_rapid
        else:
            raise ValueError(f"未知 OCR 引擎: {name}")
        return _engine_cache[name]


def _normalize(text: str) -> str:
    """去空白、转小写，减少 OCR 空格/大小写差异导致的匹配失败。"""
    return "".join(text.split()).lower()


def grab_region(region: dict | None):
    """截取屏幕区域，返回 PIL Image。region 为 None 时截主屏。"""
    from PIL import Image

    with mss.mss() as sct:
        if region:
            monitor = {"left": int(region["x"]), "top": int(region["y"]),
                       "width": int(region["w"]), "height": int(region["h"])}
        else:
            monitor = sct.monitors[1]  # 主屏
        raw = sct.grab(monitor)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def scan_once() -> dict:
    """执行一次完整识别与匹配，返回调试信息（/api/watcher/test 用）。"""
    cfg = settings.load()
    ocr_cfg = cfg["ocr"]
    base = cfg["server"]["base_url"].rstrip("/")
    info: dict = {"text": "", "hits": [], "error": ""}

    if not ocr_cfg.get("enabled"):
        info["error"] = "OCR 未开启"
        return info
    try:
        img = grab_region(ocr_cfg.get("region"))
        engine_fn = get_engine(ocr_cfg["engine"])
        text = engine_fn(img, ocr_cfg.get("lang", "zh-CN"))
    except Exception as e:  # 引擎/截屏失败不能让线程死掉
        with state.lock:
            state.error = str(e)
        state.log("error", f"OCR 失败: {e}")
        info["error"] = str(e)
        return info

    norm_text = _normalize(text)
    with state.lock:
        state.last_scan = time.time()
        state.last_text = text
        state.error = ""
    info["text"] = text

    if not norm_text:
        return info

    # 匹配：Boss 配置的关键词（逗号分隔）任一命中即触发
    cooldown = float(ocr_cfg.get("cooldown", 60))
    now = time.time()
    for boss in storage.load_all():
        keywords = [k for k in _normalize(boss.get("ocr_keywords", "")).split(",") if k]
        if not keywords:
            continue
        if not any(k in norm_text for k in keywords):
            continue
        info["hits"].append(boss["name"])
        state.log("hit", f"命中「{boss['name']}」: {text}")
        with state.lock:
            state.last_hit = boss["name"]
        # 冷却检查：最近一次击杀（无论来源）之后一段时间内不重复触发
        last_kill = boss.get("last_kill_at") or 0
        if now - last_kill < cooldown:
            state.log("skip", f"「{boss['name']}」冷却中，跳过")
            continue
        if ocr_cfg.get("mode") == "confirm" and confirm_handler:
            state.log("confirm", f"待确认:「{boss['name']}」")
            try:
                confirm_handler(boss)
            except Exception as e:
                state.log("error", f"确认回调失败: {e}")
        else:
            try:
                netclient.post_json(f"{base}/api/bosses/{boss['id']}/kill", {"source": "ocr"})
                state.log("kill", f"已自动标记:「{boss['name']}」")
            except Exception as e:
                state.log("error", f"标记失败: {e}")
    return info


def _notify_listeners():
    for cb in list(_status_listeners):
        try:
            cb(status())
        except Exception:
            pass


_status_listeners: list = []  # callable(status: dict)


def on_status(cb) -> None:
    _status_listeners.append(cb)


def _loop():
    while True:
        cfg = settings.load()
        ocr_cfg = cfg["ocr"]
        if not ocr_cfg.get("enabled"):
            if state.running:
                with state.lock:
                    state.running = False
                _notify_listeners()
            time.sleep(1)
            continue
        if not state.running:
            with state.lock:
                state.running = True
            _notify_listeners()
        interval = max(1, float(ocr_cfg.get("interval", 3)))
        started = time.time()
        try:
            scan_once()
        except Exception as e:  # 兜底，保证线程不死
            state.log("error", f"scan 异常: {e}")
        # 间隔从扫描开始时间起算，保证节奏稳定
        time.sleep(max(0.2, interval - (time.time() - started)))


def start() -> threading.Thread:
    t = threading.Thread(target=_loop, name="ocr-watcher", daemon=True)
    t.start()
    return t
