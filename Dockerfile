FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/app/.cache/ms-playwright

# 安装系统基础依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    ca-certificates \
    redis-server \
    procps \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

# 创建浏览器缓存目录并安装 Playwright 浏览器
RUN mkdir -p /app/.cache/ms-playwright && \
    playwright install --with-deps chromium && \
    playwright install --with-deps chromium-headless-shell && \
    # 清理 Playwright 下载缓存以减小镜像体积
    rm -rf /root/.cache/ms-playwright/download-archives && \
    rm -rf /root/.cache/pip && \
    rm -rf /var/lib/apt/lists/*

# 复制应用代码
COPY . .

# 临时使用root用户启动（调试用）
# 创建非root用户以提高安全性
# RUN adduser --disabled-password --gecos '' appuser && \
#     chown -R appuser:appuser /app
# USER appuser

# 暴露端口（根据FastAPI默认端口）
EXPOSE 8000

# 启动命令 - 使用start.sh脚本
CMD ["bash", "start.sh"]