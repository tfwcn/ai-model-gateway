import os
import logging

# 加载环境变量（支持 .env 文件）
from dotenv import load_dotenv
load_dotenv()

# 配置日志 - 通过环境变量 DEBUG_LOGS 控制日志级别
log_level = logging.DEBUG if os.getenv('DEBUG_LOGS', '').lower() in ('true', '1', 'yes') else logging.INFO
logging.basicConfig(
    level=log_level,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)