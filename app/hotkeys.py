"""全局热键：游戏内按键直接标记击杀/重置，无需切换窗口。

默认 Ctrl+Alt+K 击杀「等待倒计时中最先复活的 Boss」，Ctrl+Alt+R 重置它；
键位与开关在 settings.hotkeys 配置，默认关闭。
"""
import threading
import time

from . import netclient, settings

_registered_keys: tuple = ()
_lock = threading.Lock()


def _pick_target(data: dict) -> dict | None:
    """选热键作用目标：倒计时等待中、最先复活的 Boss。"""
    waiting = [b for b in data["bosses"] if b["status"] == "waiting"]
    if not waiting:
        return None
    waiting.sort(key=lambda b: b["next_at"])
    return waiting[0]


def _kill_action():
    cfg = settings.load()
    base = cfg["server"]["base_url"].rstrip("/")
    try:
        data = netclient.get_json(f"{base}/api/state", timeout=4)
        target = _pick_target(data)
        if not target:
            return
        netclient.post_json(f"{base}/api/bosses/{target['id']}/kill", {"source": "hotkey"})
    except Exception:
        pass


def _reset_action():
    cfg = settings.load()
    base = cfg["server"]["base_url"].rstrip("/")
    try:
        data = netclient.get_json(f"{base}/api/state", timeout=4)
        target = _pick_target(data)
        if not target:
            return
        netclient.post_json(f"{base}/api/bosses/{target['id']}/reset", {})
    except Exception:
        pass


def _sync_hotkeys() -> None:
    """按当前配置注册/注销热键（配置变化时全量重建）。"""
    global _registered_keys
    import keyboard

    cfg = settings.load()["hotkeys"]
    if not cfg.get("enabled"):
        if _registered_keys:
            for key in _registered_keys:
                try:
                    keyboard.remove_hotkey(key)
                except (KeyError, ValueError):
                    pass
            _registered_keys = ()
        return

    wanted = (cfg.get("kill", "ctrl+alt+k"), cfg.get("reset", "ctrl+alt+r"))
    if wanted == _registered_keys:
        return
    if _registered_keys:
        for key in _registered_keys:
            try:
                keyboard.remove_hotkey(key)
            except (KeyError, ValueError):
                pass
    _registered_keys = (
        keyboard.add_hotkey(wanted[0], _kill_action),
        keyboard.add_hotkey(wanted[1], _reset_action),
    )


def _loop():
    while True:
        with _lock:
            try:
                _sync_hotkeys()
            except Exception:
                pass
        time.sleep(2)


def start() -> threading.Thread:
    t = threading.Thread(target=_loop, name="hotkeys", daemon=True)
    t.start()
    return t
