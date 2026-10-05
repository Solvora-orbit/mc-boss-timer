"""复活到点提醒线程：轮询 /api/state，到点时声音 + 系统通知 + 悬浮窗提醒条。

每个 Boss 每轮（一次击杀到下次击杀）每种提醒只发一次，击杀时间变化后重置。
全部行为由 settings.notify 控制，默认关闭。
"""
import threading
import time

from . import netclient, settings

# 进程内回调：悬浮窗注册后可收到提醒条消息和系统通知（需主线程执行）
banner_handler = None   # callable(text: str)
toast_handler = None    # callable(text: str)

# boss_id -> 已提醒过的阶段集合；kill_at 变化即重置
_notified: dict[str, tuple[float, set]] = {}


def _play_sound():
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass


def _send_toast(title: str, text: str):
    """Windows 系统通知。winrt 的静态方法只能在进程主线程调用，
    因此实际弹窗由悬浮窗通过 toast_handler 在主线程执行；
    悬浮窗未运行（纯服务模式）时静默降级（还有声音和提醒条兜底）。"""
    if toast_handler:
        try:
            toast_handler(f"{title}：{text}")
        except Exception:
            pass


def _notify(title: str, text: str):
    cfg = settings.load()["notify"]
    if cfg.get("sound"):
        _play_sound()
    if cfg.get("toast"):
        _send_toast(title, text)
    if cfg.get("banner") and banner_handler:
        try:
            banner_handler(f"🔔 {title}：{text}")
        except Exception:
            pass


def _check_once() -> None:
    cfg = settings.load()
    if not cfg["notify"].get("enabled"):
        return
    base = cfg["server"]["base_url"].rstrip("/")
    try:
        data = netclient.get_json(f"{base}/api/state", timeout=4)
    except Exception:
        return
    now = data["server_time"]
    range_mode = cfg["notify"].get("range_mode", "both")

    for b in data["bosses"]:
        kill_at = b.get("last_kill_at")
        if not kill_at or b.get("next_at") is None:
            continue
        # 击杀时间变了 -> 新的一轮，重置提醒状态
        prev = _notified.get(b["id"])
        if prev is None or prev[0] != kill_at:
            _notified[b["id"]] = (kill_at, set())
            prev = _notified[b["id"]]
        done = prev[1]
        name = b["name"]

        if b["mode"] == "range":
            # 进入复活区间（最早到点）
            if now >= b["next_at"] and "start" not in done:
                done.add("start")
                _notify(name, "可能已复活（进入复活区间）")
            # 超过最晚时间
            if (range_mode == "both" and b.get("next_at_max")
                    and now >= b["next_at_max"] and "end" not in done):
                done.add("end")
                _notify(name, "应已复活（超过最晚复活时间）")
        else:
            if now >= b["next_at"] and "done" not in done:
                done.add("done")
                _notify(name, "已到复活时间！")


def _loop():
    while True:
        try:
            _check_once()
        except Exception:
            pass
        time.sleep(1)


def start() -> threading.Thread:
    t = threading.Thread(target=_loop, name="notify", daemon=True)
    t.start()
    return t
