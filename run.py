"""程序入口（开发运行 & PyInstaller 打包）。

用法：
  python run.py                        主机模式：启动服务 + 打开网页 + 悬浮窗
  python run.py http://192.168.1.5:8000   客户端模式：仅悬浮窗，连接指定主机

主机模式关闭悬浮窗会退出整个程序（服务随之停止）。
"""
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from app.config import APP_NAME, APP_VERSION, HOST, PORT
from app.main import app, get_lan_ips
from app.overlay import Overlay


def start_server() -> threading.Thread:
    """在后台线程启动 Web 服务（开发与打包共用）。"""
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread


def wait_server(timeout: float = 15.0) -> bool:
    """等待服务就绪。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=1):
                return True
        except Exception:
            time.sleep(0.2)
    return False


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1].startswith("http"):
        # 客户端模式：队友运行，只开悬浮窗，连到主机
        overlay = Overlay(sys_argv[1], is_host=False)
        overlay.run()
        return

    # 主机模式
    print("=" * 52)
    print(f"  {APP_NAME} v{APP_VERSION}")
    print(f"  本机访问:   http://127.0.0.1:{PORT}")
    for ip in get_lan_ips():
        print(f"  局域网访问: http://{ip}:{PORT}  <- 发给队友")
    print("  关闭悬浮窗即退出程序；网页在浏览器中独立打开")
    print("=" * 52)

    start_server()
    if not wait_server():
        print("[错误] 服务启动失败，请检查端口是否被占用")
        return
    threading.Thread(
        target=lambda: (time.sleep(1.2), _safe_open_browser()), daemon=True
    ).start()
    Overlay(f"http://127.0.0.1:{PORT}", is_host=True).run()


def _safe_open_browser() -> None:
    try:
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
