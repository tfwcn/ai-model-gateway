#!/bin/bash

set -e  # 如果任何命令失败，则立即退出

# 默认值
HOST="0.0.0.0"
PORT="${PORT:-8000}"  # 优先使用环境变量 PORT，默认 8000
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

# 检查 Chromium 浏览器及其依赖是否完整可用
check_chromium_installed() {
    # 1. 检查浏览器目录是否存在
    CHROMIUM_DIR_EXISTS=false
    for dir in $PLAYWRIGHT_BROWSERS_PATH/chromium-*; do
        if [ -d "$dir" ]; then
            CHROMIUM_DIR_EXISTS=true
            break
        fi
    done
    
    if [ "$CHROMIUM_DIR_EXISTS" = false ]; then
        echo "Chromium 浏览器目录不存在"
        return 1
    fi
    
    # 2. 检查关键运行库是否可用（以 libglib-2.0 为例）
    if ! ldconfig -p | grep -q "libglib-2.0"; then
        echo "关键运行库 libglib-2.0 未找到"
        return 1
    fi
    
    # 3. 尝试运行 chromium 检查是否能正常加载
    CHROMIUM_BINARY="$(find $PLAYWRIGHT_BROWSERS_PATH/chromium-*/chrome-linux* -name 'chrome' -o -name 'chrome-headless-shell' 2>/dev/null | head -1)"
    if [ -n "$CHROMIUM_BINARY" ] && [ -f "$CHROMIUM_BINARY" ]; then
        if ! ldd "$CHROMIUM_BINARY" 2>/dev/null | grep -q "not found"; then
            echo "Chromium 浏览器及依赖检查通过"
            return 0
        else
            echo "Chromium 浏览器存在但缺少依赖库"
            return 1
        fi
    fi
    
    echo "Chromium 浏览器可执行文件未找到"
    return 1
}

echo "检查 Chromium 浏览器状态..."
if check_chromium_installed; then
    echo "Chromium 浏览器已安装且可用，跳过安装步骤"
else
    echo "Chromium 浏览器不完整或不可用，正在安装..."
    playwright install --with-deps chromium
    playwright install --with-deps chromium-headless-shell
    echo "Chromium 浏览器安装完成"
fi
echo "依赖安装完成"

# 启动 Redis 服务器
echo "启动 Redis 服务器..."
REDIS_DATA_DIR="$APP_PATH/data/redis"
mkdir -p "$REDIS_DATA_DIR"
redis-server --dir "$REDIS_DATA_DIR" --dbfilename dump.rdb --daemonize yes
echo "Redis 服务器已启动，数据目录: $REDIS_DATA_DIR"

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