#!/bin/bash

set -e  # 如果任何命令失败，则立即退出

# 设置虚拟环境路径
VENV_PATH="/app/venv"
APP_PATH="/app"

echo "检查虚拟环境是否存在..."

if [ ! -d "$VENV_PATH" ]; then
    echo "虚拟环境不存在，正在创建..."
    python3.12 -m venv "$VENV_PATH"
    echo "虚拟环境创建完成"
    
    echo "激活虚拟环境并安装依赖..."
else
    echo "虚拟环境已存在，跳过创建步骤"
fi
source "$VENV_PATH/bin/activate"
pip install --upgrade pip
pip install -r "$APP_PATH/requirements.txt"
export PLAYWRIGHT_BROWSERS_PATH="$APP_PATH/.cache/ms-playwright"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
# 安装 Chromium 浏览器及其系统依赖
playwright install --with-deps chromium
echo "依赖安装完成"

echo "启动 OpenAI 代理服务..."
cd "$APP_PATH"
# uvicorn run:app --host 0.0.0.0 --port 8000 --reload
uvicorn run:app --host 0.0.0.0 --port 8000