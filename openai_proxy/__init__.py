import os
import logging
from logging.handlers import TimedRotatingFileHandler

# 加载环境变量（支持 .env 文件）
from dotenv import load_dotenv
load_dotenv()

# 配置日志 - 简化版本
# 向后兼容：同时检查 DEBUG_LOGS 和 DEBUG
log_level = logging.DEBUG if os.getenv('DEBUG', '').lower() in ('true', '1', 'yes') or os.getenv('DEBUG_LOGS', '').lower() in ('true', '1', 'yes') else logging.INFO
enable_console = os.getenv('ENABLE_CONSOLE_LOGS', 'true').lower() not in ('false', '0', 'no')

# 创建根日志记录器
root_logger = logging.getLogger()
root_logger.setLevel(log_level)  # 根日志器级别由 DEBUG 环境变量控制

# 清除现有的处理器
root_logger.handlers.clear()

# 设置格式化器
formatter = logging.Formatter(
    '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# 1. 文件处理器 - 始终启用，记录所有级别的日志（包括 DEBUG）
log_dir = os.getenv('LOG_DIR', os.getenv('PROXY_LOG_DIR', 'logs'))
os.makedirs(log_dir, exist_ok=True)
log_path = os.path.join(log_dir, 'proxy.log')

file_handler = TimedRotatingFileHandler(
    filename=log_path,
    when='midnight',      # 每天午夜轮转
    interval=1,           # 每1天轮转一次
    backupCount=7,        # 保留7天的日志
    encoding='utf-8',
    utc=True              # 使用 UTC 时间
)
file_handler.setLevel(logging.DEBUG)  # 文件记录所有级别
file_handler.setFormatter(formatter)
file_handler.suffix = "%Y-%m-%d"      # 轮转文件后缀格式
root_logger.addHandler(file_handler)

# 2. 控制台处理器 - 默认启用，级别与根日志器一致
if enable_console:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)  # 控制台级别与根日志器一致
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

# 抑制某些库的过多日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
