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
    source "$VENV_PATH/bin/activate"
    pip install --upgrade pip
    pip install -r "$APP_PATH/requirements.txt"
    echo "依赖安装完成"
else
    echo "虚拟环境已存在，跳过创建步骤"
    source "$VENV_PATH/bin/activate"
    pip install --upgrade pip
    pip install -r "$APP_PATH/requirements.txt"
fi

echo "启动 OpenAI 代理服务..."
cd "$APP_PATH"
python "run.py"