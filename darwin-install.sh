#!/bin/bash
# macOS 安装脚本 (支持 Intel �?Apple Silicon)

# 确保 Homebrew 已安�?
if ! command -v brew &> /dev/null; then
    echo "未检测到 Homebrew，开始安�?.."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    
    # 检�?Homebrew 安装路径并设置环境变�?
    if [[ -f "/opt/homebrew/bin/brew" ]]; then
        # Apple Silicon Mac
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -f "/usr/local/bin/brew" ]]; then
        # Intel Mac
        eval "$(/usr/local/bin/brew shellenv)"
    fi
fi

# 更新 brew 并安�?git
brew update
brew install git

# 安装 uv (Python 环境管理工具)
curl -Ls https://astral.sh/uv/install.sh | sh

# 确保 uv �?PATH �?
export PATH="$HOME/.local/bin:$PATH"

# 克隆或进入项目目�?
if [ -f "./web.py" ]; then
    # 已经在目标目�?
    :
elif [ -f "./gcli2api/web.py" ]; then
    cd ./gcli2api
else
    git clone https://github.com/qw31415/gcli2api.git
    cd ./gcli2api
fi

# 拉取最新代�?
git pull

# 创建并同步虚拟环�?
uv sync

# 激活虚拟环�?
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    echo "�?未找到虚拟环境，请检�?uv 是否安装成功"
    exit 1
fi

# 启动项目
python3 web.py
