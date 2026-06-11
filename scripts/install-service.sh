#!/usr/bin/env bash
# 安装并启动 lens-gateway systemd 用户服务
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p ~/.config/systemd/user
cp deploy/lens-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now lens-gateway.service
echo "已启动。状态："
systemctl --user status lens-gateway.service --no-pager -l | head -8
echo
echo "健康检查（warmup 需 ~1 分钟，asr_ready=true 后可用）："
echo "  curl -s http://127.0.0.1:8443/healthz"
echo "生成配对码："
echo "  gateway/.venv/bin/python -m lens_gateway.main pair-code"
