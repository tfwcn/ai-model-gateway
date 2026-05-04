#!/bin/bash

set -e  # 如果任何命令失败，则立即退出

# 设置虚拟环境路径
VENV_PATH="$(pwd)/.venv"
APP_PATH="$(pwd)"

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
# 安装 Chromium 浏览器及其系统依赖（仅在未安装时执行）
CHROMIUM_INSTALLED=false
for dir in $PLAYWRIGHT_BROWSERS_PATH/chromium-*; do
    if [ -d "$dir" ]; then
        CHROMIUM_INSTALLED=true
        break
    fi
done

if [ "$CHROMIUM_INSTALLED" = false ]; then
    echo "Chromium 浏览器未安装，正在安装..."
    playwright install --with-deps chromium
    echo "Chromium 浏览器安装完成"
else
    echo "Chromium 浏览器已安装，跳过安装步骤"
fi
echo "依赖安装完成"

echo "启动 AI Model Gateway..."
cd "$APP_PATH"
# uvicorn run:app --host 0.0.0.0 --port 8000 --reload
uvicorn run:app --host 0.0.0.0 --port 8000