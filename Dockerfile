FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 安装系统基础依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    ca-certificates \
    redis-server \
    && rm -rf /var/lib/apt/lists/*

# 注意：所有应用代码、虚拟环境和依赖将在运行时通过start.sh安装

# 注意：所有应用代码（包括run.py和start.sh）将在运行时通过卷挂载提供

# 临时使用root用户启动（调试用）
# 创建非root用户以提高安全性
# RUN adduser --disabled-password --gecos '' appuser && \
#     chown -R appuser:appuser /app
# USER appuser

# 暴露端口（根据FastAPI默认端口）
EXPOSE 8000

# 启动命令 - 使用start.sh脚本
CMD ["bash", "start.sh"]