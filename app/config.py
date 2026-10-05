"""全局配置：集中管理端口、数据文件路径等常量，便于后期迭代调整。"""
import sys
from pathlib import Path

APP_NAME = "MC Boss 计时器"
APP_VERSION = "1.4.0"

# 服务监听配置：0.0.0.0 允许局域网访问
HOST = "0.0.0.0"
PORT = 8000

# 打包成 exe 后，数据文件放在 exe 同目录，方便用户查看和备份；
# 开发运行时放在项目根目录。
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    # PyInstaller 解包出来的静态资源目录
    STATIC_DIR = Path(sys._MEIPASS) / "static"  # noqa: SLF001
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    STATIC_DIR = BASE_DIR / "static"

DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "bosses.json"
