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
echo
# 网关**不会自己拉起 agent**（lens_gateway/ 全目录没有一处 subprocess），
# config.json 里那个 ws://127.0.0.1:18790 只是个要去连的地址。
# provider=lens 而没装 agent 的表现是：网关一切正常，只有说话没反应。
echo "若 ~/.lens-gateway/config.json 里 agent.provider = \"lens\"，还要装 agent 服务："
echo "  bash scripts/install-agent-service.sh"
echo "另：用户服务默认只在登录期间活着。要让它开机自启，需要 sudo loginctl enable-linger $(id -un)"
