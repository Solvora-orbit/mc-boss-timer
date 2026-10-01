"""复活时间计算：把击杀时间 + 复活配置换算成下次复活时刻与状态。"""
from .storage import load_all

# 状态含义：
# idle       未记录过击杀，计时未开始
# waiting    距最早复活时间还有一段时间
# possible   已进入复活区间，随时可能出现（仅 range 模式）
# respawned  已超过复活时间，应该已复活
STATUS_LABELS = {
    "idle": "未开始",
    "waiting": "倒计时中",
    "possible": "可能已复活",
    "respawned": "应已复活",
}


def compute_respawn(boss: dict, server_now: float) -> dict:
    """返回单个 Boss 的复活计算结果，字段前端直接可用。"""
    kill_at = boss.get("last_kill_at")
    result = {
        "status": "idle",
        "next_at": None,        # 固定模式：确切复活时刻；区间模式：最早复活时刻
        "next_at_max": None,    # 区间模式：最晚复活时刻
        "seconds_left": None,   # 距最早复活时刻的秒数，可为负
        "seconds_left_max": None,
    }
    if not kill_at:
        return result

    if boss["mode"] == "range":
        min_left = kill_at + boss["respawn_min"] * 60 - server_now
        max_left = kill_at + boss["respawn_max"] * 60 - server_now
        result["next_at"] = kill_at + boss["respawn_min"] * 60
        result["next_at_max"] = kill_at + boss["respawn_max"] * 60
        result["seconds_left"] = min_left
        result["seconds_left_max"] = max_left
        if max_left <= 0:
            result["status"] = "respawned"
        elif min_left <= 0:
            result["status"] = "possible"
        else:
            result["status"] = "waiting"
    else:
        left = kill_at + boss["respawn_minutes"] * 60 - server_now
        result["next_at"] = kill_at + boss["respawn_minutes"] * 60
        result["seconds_left"] = left
        result["status"] = "respawned" if left <= 0 else "waiting"
    return result


def build_state(server_now: float) -> list[dict]:
    """给每个 Boss 附加复活计算字段，供 API 返回。"""
    bosses = load_all()
    for boss in bosses:
        boss.update(compute_respawn(boss, server_now))
        boss["status_label"] = STATUS_LABELS[boss["status"]]
    # 按最近复活时刻排序；未开始击杀的排在最后，按名称排
    def sort_key(b):
        has_timer = b["next_at"] is not None
        respawned = b["status"] == "respawned"
        return (
            0 if has_timer and not respawned else 1 if has_timer else 2,
            b["next_at"] or 0,
            b["name"],
        )
    bosses.sort(key=sort_key)
    return bosses
