"""本机功能设置：统一读写 data/settings.json（OCR/提醒/热键/服务器地址）。

这些是「每台机器自己的」配置，不做局域网共享；各后台线程每轮重读，
改动即时生效。默认所有功能关闭。
"""
import json
import threading

from .config import DATA_DIR

_SETTINGS_FILE = DATA_DIR / "settings.json"
_lock = threading.RLock()

DEFAULTS = {
    "server": {
        "base_url": "http://127.0.0.1:8000",   # 悬浮窗/后台线程访问的 API 地址
    },
    "ocr": {
        "enabled": False,        # 默认关闭
        "engine": "windows",     # windows | rapidocr
        "interval": 3,           # 识别间隔（秒）
        "mode": "auto",          # auto 自动标记 | confirm 仅提醒待确认
        "cooldown": 60,          # 同一 Boss 触发冷却（秒）
        "region": None,          # 监测区域 {x,y,w,h}，None=全屏
        "lang": "zh-CN",
    },
    "notify": {
        "enabled": False,
        "sound": True,           # 提示音
        "toast": True,           # Windows 系统通知
        "banner": True,          # 悬浮窗顶部提醒条
        "range_mode": "both",    # both 区间两端都提醒 | earliest 仅最早复活
    },
    "hotkeys": {
        "enabled": False,
        "kill": "ctrl+alt+k",
        "reset": "ctrl+alt+r",
    },
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict:
    """读当前设置（与默认值深合并，文件损坏自动回默认）。"""
    with _lock:
        data = {}
        try:
            data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        return _deep_merge(DEFAULTS, data if isinstance(data, dict) else {})


def update(patch: dict) -> dict:
    """局部更新设置（深合并），返回更新后的完整设置。"""
    with _lock:
        current = {}
        try:
            current = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        merged = _deep_merge(current if isinstance(current, dict) else {}, patch)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _SETTINGS_FILE.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return _deep_merge(DEFAULTS, merged)
