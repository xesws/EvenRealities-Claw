#!/usr/bin/env bash
# 本地演示：一条命令拉起完整链路，Ctrl+C 全部停掉。
#
#   ./demo/start.sh --lens   自研 agent（推荐）：拉起 lens_agent，直连 DeepSeek
#   ./demo/start.sh --real    连本机真的 OpenClaw 网关（需它已在 18789 上跑着）
#   ./demo/start.sh           替身模式：agent 换成 demo/fake_openclaw.py（离线调链路时用）
#
# 三种模式下，麦克风、faster-whisper 转写、HUD 状态机、折行分页、渲染节流
# 全部是同一套真实代码路径，差别只在 chat.send 的对端是谁。
#
# ⚠ 替身模式**不是可以拿去演示的东西**：fake_openclaw.py 在握手里自报 fixture:true，
#   网关会据此在状态条徽记上打「?」（W6 溯源）。屏幕自己会告状，这是有意的。
#   演示请用 --lens，并当场 curl /healthz 自证对端是谁。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/gateway/.venv/bin/python"
PORT=8443
AGENT_PORT=18789

AGENT_PORT_LENS=18790

MODE=demo
case "${1:-}" in
  --real) MODE=real ;;
  --lens) MODE=lens ;;
  "") ;;
  *) echo "未知参数：$1（可用：--lens / --real / 不带参数）"; exit 1 ;;
esac

[ -x "${PY}" ] || { echo "缺少虚拟环境，先执行："; echo "  python3 -m venv gateway/.venv && gateway/.venv/bin/pip install -r gateway/requirements.txt"; exit 1; }

if [ "${MODE}" = real ]; then
  # 真实模式用生产状态目录，配置留空 = 用代码默认值
  # （ws://127.0.0.1:18789 + ~/.openclaw/openclaw.json 的 gateway.auth.token）
  export LENS_STATE_DIR="${HOME}/.lens-gateway"
  mkdir -p "${LENS_STATE_DIR}"; chmod 700 "${LENS_STATE_DIR}"

  lsof -nP -iTCP:${AGENT_PORT} -sTCP:LISTEN >/dev/null 2>&1 || {
    echo "✗ 127.0.0.1:${AGENT_PORT} 上没有 OpenClaw 网关在监听。"
    echo "  先把 OpenClaw 跑起来（网关模块需开着），再执行本命令。"
    exit 1
  }
  [ -f "${HOME}/.openclaw/openclaw.json" ] || {
    echo "✗ 找不到 ~/.openclaw/openclaw.json（适配器从这里读 gateway.auth.token）。"
    echo "  若你的 token 在别处，在 ${LENS_STATE_DIR}/config.json 里覆盖 openclaw.config_path。"
    exit 1
  }
  "${PY}" -c "
import json,sys,pathlib
try:
    json.loads(pathlib.Path.home().joinpath('.openclaw/openclaw.json').read_text())['gateway']['auth']['token']
except Exception as e:
    sys.exit(f'✗ ~/.openclaw/openclaw.json 里读不到 gateway.auth.token：{e}')
" || exit 1

  # 只在没有 config.json 时写一份，绝不覆盖你已有的配置
  [ -f "${LENS_STATE_DIR}/config.json" ] || printf '{\n  "host": "127.0.0.1",\n  "port": %s\n}\n' "${PORT}" > "${LENS_STATE_DIR}/config.json"
  echo "▸ 真实模式：连 127.0.0.1:${AGENT_PORT} 的 OpenClaw，状态目录 ${LENS_STATE_DIR}"
elif [ "${MODE}" = lens ]; then
  export LENS_STATE_DIR="${HOME}/.lens-gateway-lens"
  mkdir -p "${LENS_STATE_DIR}"; chmod 700 "${LENS_STATE_DIR}"

  # key 只从环境读，绝不落盘、绝不进仓库
  if [ -z "${LENS_LLM_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "✗ 需要 LENS_LLM_API_KEY（或 OPENAI_API_KEY）才能连 DeepSeek。"
    echo "  export LENS_LLM_API_KEY=sk-...   然后重跑本命令。"
    exit 1
  fi
  lsof -nP -iTCP:${AGENT_PORT_LENS} -sTCP:LISTEN >/dev/null 2>&1 && {
    echo "✗ 端口 ${AGENT_PORT_LENS} 已被占用，先停掉旧的 lens agent。"; exit 1
  }
  cat > "${LENS_STATE_DIR}/config.json" <<JSON
{
  "host": "127.0.0.1",
  "port": ${PORT},
  "agent": {
    "provider": "lens",
    "url": "ws://127.0.0.1:${AGENT_PORT_LENS}"
  }
}
JSON
  echo "▸ 自研 agent 模式：lens_agent → DeepSeek，状态目录 ${LENS_STATE_DIR}"
else
  # 演示状态目录独立于生产的 ~/.lens-gateway/，互不干扰
  export LENS_STATE_DIR="${HOME}/.lens-gateway-demo"
  mkdir -p "${LENS_STATE_DIR}"; chmod 700 "${LENS_STATE_DIR}"
  echo '{ "gateway": { "auth": { "token": "demo-local-token-not-a-secret" } } }' > "${LENS_STATE_DIR}/demo-openclaw.json"
  cat > "${LENS_STATE_DIR}/config.json" <<JSON
{
  "host": "127.0.0.1",
  "port": ${PORT},
  "openclaw": {
    "url": "ws://127.0.0.1:${AGENT_PORT}",
    "config_path": "${LENS_STATE_DIR}/demo-openclaw.json"
  }
}
JSON
  lsof -nP -iTCP:${AGENT_PORT} -sTCP:LISTEN >/dev/null 2>&1 && {
    echo "✗ 端口 ${AGENT_PORT} 已被占用（是不是真的 OpenClaw 在跑？那就用 ./demo/start.sh --real）"; exit 1
  }
  echo "▸ 替身模式：agent = demo/fake_openclaw.py，状态目录 ${LENS_STATE_DIR}"
fi

# 插件产物（网关托管 /plugin/，与页面同源，配对屏地址会自动推导）
# EVENHUB_HARNESS=1 是必须的：默认构建**不含** harness/probe（它们带官方 pretext 的
# 完整字形度量表 ~130KB，装到用户眼镜上是纯负担，见 plugin/vite.config.ts）。
# 少了它，下面打印的 /plugin/harness/harness.html 是 404，而这正是本脚本让人打开的地址。
if [ ! -f "${ROOT}/plugin/dist/harness/harness.html" ]; then
  echo "▸ 构建插件（含浏览器夹具）…"
  (cd "${ROOT}/plugin" && EVENHUB_HARNESS=1 npm run build >/dev/null)
fi

PIDS=()
cleanup() { echo; echo "▸ 停止…"; for p in "${PIDS[@]:-}"; do kill "${p}" 2>/dev/null || true; done; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1 && { echo "✗ 端口 ${PORT} 已被占用，先停掉旧的网关。"; exit 1; }

if [ "${MODE}" = demo ]; then
  echo "▸ 启动 agent 替身 ws://127.0.0.1:${AGENT_PORT} …"
  "${PY}" "${ROOT}/demo/fake_openclaw.py" --port "${AGENT_PORT}" & PIDS+=($!)
elif [ "${MODE}" = lens ]; then
  echo "▸ 启动自研 agent ws://127.0.0.1:${AGENT_PORT_LENS} …"
  (cd "${ROOT}/gateway" && PYTHONPATH=. LENS_AGENT_PORT="${AGENT_PORT_LENS}" \
     "${PY}" -m lens_agent.server) & PIDS+=($!)
  for _ in $(seq 1 20); do
    curl -s -m 1 "http://127.0.0.1:${AGENT_PORT_LENS}/healthz" >/dev/null 2>&1 && break
    sleep 0.5
  done
  AGENT_HEALTH=$(curl -s -m 2 "http://127.0.0.1:${AGENT_PORT_LENS}/healthz" 2>/dev/null || echo '{}')
  echo "  agent 自报：${AGENT_HEALTH}"
fi

echo "▸ 启动 Lens Gateway（首次会加载 whisper 模型，约 1 分钟）…"
(cd "${ROOT}/gateway" && PYTHONPATH=. "${PY}" -m lens_gateway.main serve) & PIDS+=($!)

echo -n "▸ 等待 ASR 就绪"
for _ in $(seq 1 60); do
  if curl -s -m 2 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null | grep -q '"asr_ready": true'; then
    echo " ✓"; break
  fi
  echo -n "."; sleep 2
done

HEALTH=$(curl -s -m 2 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null || echo '{}')
echo "${HEALTH}" | grep -q '"connected": true' || echo "⚠ healthz 显示 agent 侧未连上：${HEALTH}"

CODE=$(cd "${ROOT}/gateway" && PYTHONPATH=. "${PY}" -m lens_gateway.main pair-code | grep -o '[0-9]\{6\}')

cat <<EOF

──────────────────────────────────────────────────────────
  浏览器打开： http://127.0.0.1:${PORT}/plugin/harness/harness.html
  配对码：     ${CODE}
  首次进入需允许麦克风权限，否则眼镜屏会显示「无音频」。

  自证对端是谁（W6 agent 溯源）：
      curl -s http://127.0.0.1:${PORT}/healthz | python3 -m json.tool
  看 agent.backend / agent.model / agent.production。
  production=false 时，眼镜状态条的徽记会带一个「?」—— 屏幕自己会告状。
──────────────────────────────────────────────────────────
  Ctrl+C 停止全部服务。
EOF

wait
