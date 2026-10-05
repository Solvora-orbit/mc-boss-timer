"""数据持久化层：JSON 文件存储，带锁与原子写入，防止并发写坏文件。"""
import json
import threading
import uuid
from datetime import datetime, timezone

from .config import DATA_DIR, DATA_FILE

# RLock 允许读写函数互相嵌套调用
_lock = threading.RLock()

# Boss 数据字段及其类型，读写时做清洗，保证文件内容始终合法
_BOSS_FIELDS = {
    "name": str,
    "mode": str,                # "fixed" 固定间隔 | "range" 复活区间
    "respawn_minutes": (int, float),
    "respawn_min": (int, float),
    "respawn_max": (int, float),
    "location": str,
    "drops": str,
    "notes": str,
    "ocr_keywords": str,        # OCR 击杀识别关键词，逗号分隔，可选
    "last_kill_at": (int, float, type(None)),  # 击杀时间 unix 秒
}

# 击杀历史文件：滚动保留最近 MAX_HISTORY 条
MAX_HISTORY = 1000
_HISTORY_FILE = DATA_DIR / "history.json"


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def _clean_boss(raw: dict) -> dict:
    """按字段类型清洗一条 Boss 记录，剔除非法字段。"""
    boss = {}
    for key, types in _BOSS_FIELDS.items():
        value = raw.get(key)
        if isinstance(value, types):
            boss[key] = value
        elif type(None) in types:
            boss[key] = None
        elif str in types:
            boss[key] = ""
        else:
            boss[key] = 0
    boss["id"] = raw.get("id") or uuid.uuid4().hex[:8]
    boss["mode"] = boss["mode"] if boss["mode"] in ("fixed", "range") else "fixed"
    boss.setdefault("created_at", raw.get("created_at") or _now())
    boss.setdefault("updated_at", raw.get("updated_at") or _now())
    return boss


def _read_raw() -> list:
    """调用方需已持有 _lock。文件损坏时备份后按空数据处理。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.write_text("[]", encoding="utf-8")
        return []
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        DATA_FILE.replace(DATA_FILE.with_suffix(".json.bak"))
        DATA_FILE.write_text("[]", encoding="utf-8")
        return []
    return raw if isinstance(raw, list) else []


def _write_raw(items: list) -> None:
    """调用方需已持有 _lock。原子写入：先写临时文件再替换。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


def load_all() -> list[dict]:
    with _lock:
        return [_clean_boss(item) for item in _read_raw() if isinstance(item, dict)]


def get_boss(boss_id: str) -> dict | None:
    return next((b for b in load_all() if b["id"] == boss_id), None)


def save_boss(data: dict, boss_id: str | None = None) -> dict:
    """新增（boss_id 为 None）或更新一条 Boss，返回保存后的完整记录。"""
    with _lock:
        items = [i for i in _read_raw() if isinstance(i, dict)]
        if boss_id is None:
            boss = _clean_boss(data)
            boss["id"] = uuid.uuid4().hex[:8]
            boss["created_at"] = _now()
            boss["updated_at"] = _now()
            items.append(boss)
        else:
            boss = None
            for i, item in enumerate(items):
                if item.get("id") == boss_id:
                    merged = {**_clean_boss(item), **data, "id": boss_id}
                    merged["updated_at"] = _now()
                    items[i] = merged
                    boss = merged
                    break
            if boss is None:
                raise KeyError(boss_id)
        _write_raw(items)
        return boss


def delete_boss(boss_id: str) -> bool:
    with _lock:
        items = [i for i in _read_raw() if isinstance(i, dict)]
        remaining = [i for i in items if i.get("id") != boss_id]
        if len(remaining) == len(items):
            return False
        _write_raw(remaining)
        return True


# ---------- 击杀历史 ----------

def _read_history() -> list:
    try:
        raw = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []


def add_history(entry: dict) -> dict:
    """追加一条击杀历史，超限滚动裁剪。"""
    record = {
        "boss_id": str(entry.get("boss_id", "")),
        "boss_name": str(entry.get("boss_name", "")),
        "source": entry.get("source") if entry.get("source") in ("manual", "hotkey", "ocr") else "manual",
        "kill_at": float(entry.get("kill_at") or 0),
    }
    with _lock:
        items = _read_history()
        items.append(record)
        if len(items) > MAX_HISTORY:
            items = items[-MAX_HISTORY:]
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _HISTORY_FILE.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def load_history(limit: int = 200) -> list[dict]:
    return list(reversed(_read_history()))[:max(0, limit)]


def clear_history() -> None:
    with _lock:
        try:
            _HISTORY_FILE.unlink()
        except OSError:
            pass
