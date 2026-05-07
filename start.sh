#!/bin/bash

set -e  # 如果任何命令失败，则立即退出

# 默认值
HOST="0.0.0.0"
PORT="8000"
RELOAD=false

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --reload)
            RELOAD=true
            shift
            ;;
        *)
            echo "未知参数: $1"
            echo "用法: $0 [--host HOST] [--port PORT] [--reload]"
            exit 1
            ;;
    esac
done

if [ -d "/app" ]; then
    cd /app
fi

# 设置虚拟环境路径
VENV_PATH="$(pwd)/.venv"
APP_PATH="$(pwd)"

echo "检查虚拟环境是否存在..."

if [ ! -d "$VENV_PATH" ]; then
    echo "虚拟环境不存在，正在创建..."
    python -m venv "$VENV_PATH"
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

# 检查并终止已存在的进程
echo "检查是否存在正在运行的实例..."
# 构建搜索模式，匹配所有可能的参数组合
SEARCH_PATTERN="uvicorn run:app.*--host $HOST.*--port $PORT"
PIDS=$(pgrep -f "$SEARCH_PATTERN" || true)
if [ ! -z "$PIDS" ]; then
    echo "发现正在运行的实例 (PID: $PIDS)，正在终止..."
    kill $PIDS 2>/dev/null || true
    # 等待进程完全终止
    sleep 2
    # 如果进程仍然存在，强制终止
    if pgrep -f "$SEARCH_PATTERN" > /dev/null; then
        echo "强制终止进程..."
        pkill -9 -f "$SEARCH_PATTERN" || true
        sleep 1
    fi
    echo "旧进程已终止"
else
    echo "没有发现正在运行的实例"
fi

echo "启动 AI Model Gateway..."
cd "$APP_PATH"
if [ "$RELOAD" = true ]; then
    uvicorn run:app --host $HOST --port $PORT --reload
else
    uvicorn run:app --host $HOST --port $PORT
fi