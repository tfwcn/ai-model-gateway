FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv

# 创建虚拟环境并激活
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# 安装系统基础依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 安装Playwright浏览器及其系统依赖（自动安装所有必需的库）
RUN playwright install --with-deps chromium && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    playwright cache clear && \
    rm -rf /root/.cache/playwright

# 复制应用代码
COPY . .

# 创建非root用户以提高安全性
RUN adduser --disabled-password --gecos '' appuser && \
    chown -R appuser:appuser /app
USER appuser

# 暴露端口（根据FastAPI默认端口）
EXPOSE 8000

# 启动命令
CMD ["python", "run.py"]