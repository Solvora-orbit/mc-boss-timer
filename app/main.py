"""FastAPI 应用入口：API 路由 + 托管前端页面 + 启动时打印局域网地址。"""
import socket
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import settings, storage, timer, watcher
from .config import APP_NAME, APP_VERSION, HOST, PORT, STATIC_DIR


def get_lan_ips() -> list[str]:
    """获取本机所有局域网 IPv4 地址，供打印和页面展示。"""
    ips = set()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        ips.update(info[4][0] for info in infos if not info[4][0].startswith("127."))
    except OSError:
        pass
    # 通过 UDP 连接探测默认路由出口地址（不真正发包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80), )
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


def _open_browser_later() -> None:
    def open_it():
        time.sleep(1.5)
        try:
            webbrowser.open(f"http://127.0.0.1:{PORT}")
        except Exception:
            pass
    threading.Thread(target=open_it, daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ips = get_lan_ips()
    print("=" * 52)
    print(f"  {APP_NAME} v{APP_VERSION}")
    print(f"  本机访问:   http://127.0.0.1:{PORT}")
    for ip in ips:
        print(f"  局域网访问: http://{ip}:{PORT}  <- 发给队友")
    print("  按 Ctrl+C 停止服务")
    print("=" * 52)
    _open_browser_later()
    yield


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)


class BossIn(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    mode: str = Field(pattern="^(fixed|range)$")
    respawn_minutes: float | None = Field(default=None, gt=0, le=24 * 60 * 7)
    respawn_min: float | None = Field(default=None, gt=0, le=24 * 60 * 7)
    respawn_max: float | None = Field(default=None, gt=0, le=24 * 60 * 7)
    location: str = Field(default="", max_length=200)
    drops: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=1000)
    ocr_keywords: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def check_respawn(self):
        if self.mode == "fixed" and not self.respawn_minutes:
            raise ValueError("固定模式必须填写复活间隔（分钟）")
        if self.mode == "range":
            if not self.respawn_min or not self.respawn_max:
                raise ValueError("区间模式必须填写最早/最晚复活时间")
            if self.respawn_min > self.respawn_max:
                raise ValueError("最早复活时间不能晚于最晚复活时间")
        return self


def _boss_payload(boss: BossIn) -> dict:
    data = boss.model_dump()
    if data["mode"] == "fixed":
        # 固定模式清掉区间字段，反之亦然，保持文件干净
        data["respawn_min"] = data["respawn_max"] = None
    else:
        data["respawn_minutes"] = None
    return data


@app.get("/api/state")
def api_state():
    server_now = datetime.now(timezone.utc).timestamp()
    return {"server_time": server_now, "version": APP_VERSION,
            "lan_ips": get_lan_ips(), "port": PORT, "bosses": timer.build_state(server_now)}


@app.post("/api/bosses")
def api_create_boss(boss: BossIn):
    return storage.save_boss(_boss_payload(boss))


@app.put("/api/bosses/{boss_id}")
def api_update_boss(boss_id: str, boss: BossIn):
    try:
        return storage.save_boss(_boss_payload(boss), boss_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Boss 不存在")


@app.delete("/api/bosses/{boss_id}")
def api_delete_boss(boss_id: str):
    if not storage.delete_boss(boss_id):
        raise HTTPException(status_code=404, detail="Boss 不存在")
    return {"ok": True}


class KillIn(BaseModel):
    kill_at: float | None = None  # unix 秒，None 表示刚刚击杀
    source: str = "manual"        # manual 网页/悬浮窗 | hotkey 热键 | ocr 自动识别


@app.post("/api/bosses/{boss_id}/kill")
def api_kill_boss(boss_id: str, body: KillIn | None = None):
    """标记击杀。kill_at 为 unix 秒，不传则取当前时间（支持补录）。"""
    existing = storage.get_boss(boss_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Boss 不存在")
    now = datetime.now(timezone.utc).timestamp()
    source = body.source if body and body.source in ("manual", "hotkey", "ocr") else "manual"
    ts = body.kill_at if body and body.kill_at and body.kill_at <= now else now
    saved = storage.save_boss({"last_kill_at": ts}, boss_id)
    storage.add_history({"boss_id": boss_id, "boss_name": existing["name"],
                         "source": source, "kill_at": ts})
    return saved


@app.post("/api/bosses/{boss_id}/reset")
def api_reset_boss(boss_id: str):
    """清除击杀记录，回到未开始状态。"""
    if storage.get_boss(boss_id) is None:
        raise HTTPException(status_code=404, detail="Boss 不存在")
    return storage.save_boss({"last_kill_at": None}, boss_id)


@app.get("/api/export")
def api_export():
    """导出全部 Boss 配置（含关键词），用于换机/备份。"""
    return {"version": APP_VERSION, "exported_at": datetime.now(timezone.utc).timestamp(),
            "bosses": storage.load_all()}


@app.post("/api/import")
def api_import(payload: dict):
    """导入 Boss 配置：按 id 覆盖已存在的、新增没有的，无效记录跳过。"""
    items = payload.get("bosses") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="缺少 bosses 数组")
    added = updated = skipped = 0
    existing_ids = {b["id"] for b in storage.load_all()}
    for raw in items:
        if not isinstance(raw, dict) or not raw.get("name") or raw.get("mode") not in ("fixed", "range"):
            skipped += 1
            continue
        boss_id = raw.get("id")
        try:
            if boss_id and boss_id in existing_ids:
                storage.save_boss(raw, boss_id)
                updated += 1
            else:
                storage.save_boss(raw)   # 无 id 或 id 冲突不到时都作为新增
                added += 1
        except Exception:
            skipped += 1
    return {"added": added, "updated": updated, "skipped": skipped}


@app.get("/api/history")
def api_history(limit: int = 200):
    """击杀历史（倒序）+ 每 Boss 简单统计。"""
    entries = storage.load_history(limit)
    stats: dict[str, dict] = {}
    by_boss: dict[str, list[float]] = {}
    for e in reversed(entries):
        by_boss.setdefault(e["boss_id"], []).append(e["kill_at"])
    for boss_id, times in by_boss.items():
        intervals = [b - a for a, b in zip(times, times[1:])]
        stats[boss_id] = {
            "count": len(times),
            "last_at": times[-1],
            "avg_interval": round(sum(intervals) / len(intervals), 1) if intervals else None,
        }
    return {"entries": entries, "stats": stats}


@app.delete("/api/history")
def api_clear_history():
    storage.clear_history()
    return {"ok": True}


@app.get("/api/settings")
def api_get_settings():
    return settings.load()


@app.put("/api/settings")
def api_update_settings(patch: dict):
    return settings.update(patch)


@app.get("/api/watcher")
def api_watcher_status():
    """OCR 识别线程运行状态与最近日志。"""
    return {"status": watcher.status(), "log": watcher.recent_log(50)}


@app.post("/api/watcher/test")
def api_watcher_test():
    """立即执行一次 OCR 识别（调试用，返回识别文本与命中情况）。"""
    return watcher.scan_once()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
