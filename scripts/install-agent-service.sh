#!/usr/bin/env bash
# 安装并启动 lens-agent systemd 用户服务（只有 agent.provider = "lens" 才需要它）
#
# 用法：
#   bash scripts/install-agent-service.sh           装
#   bash scripts/install-agent-service.sh --check    只跑 preflight，不碰 systemd
#
# preflight 之所以要占掉这个脚本的一半篇幅：agent 缺 key 时是**起来又立刻挂**
# （lens_agent/llm/deepseek.py 的 read_api_key 在构造 provider 时就抛），
# 而 Restart=always 会让它每 3 秒重来一次。表现是网关一切正常、
# 只有说话没反应 —— 没人会想到去 journal 里找一个自己都不知道装了的服务。
# 所以宁可在这里退出得很难看，也不要装一个「看起来装上了」的东西。
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

PY="${ROOT}/gateway/.venv/bin/python"
ENV_FILE="${ROOT}/.env"
UNIT_SRC="${ROOT}/deploy/lens-agent.service"
UNIT_DIR="${HOME}/.config/systemd/user"
STATE_DIR="${LENS_STATE_DIR:-${HOME}/.lens-gateway}"
CONFIG_JSON="${STATE_DIR}/config.json"
AGENT_DIR="${HOME}/.lens-agent"
AGENT_PORT="${LENS_AGENT_PORT:-18790}"

fail() { echo; echo "✗ $*" >&2; exit 1; }

# ---------------- preflight ----------------

echo "▸ preflight"

[ -x "${PY}" ] || fail "网关 venv 里没有可执行的 python：${PY}
  unit 的 ExecStart 直接指向它，缺了会得到 status=203/EXEC。先建好：
    python3 -m venv gateway/.venv
    gateway/.venv/bin/pip install -r gateway/requirements.txt"
echo "  · venv       ${PY}"

[ -f "${UNIT_SRC}" ] || fail "找不到 unit 模板：${UNIT_SRC}"

# .env 是 unit 的 EnvironmentFile，**systemd 不会继承你当前 shell 的环境**。
# 所以这里只认文件里的内容 —— 「我 export 过了」在这条路上不算数。
env_defines() {  # $1=变量名 → 文件里有一条 systemd 解析得动的非空赋值
  [ -f "${ENV_FILE}" ] && grep -Eq "^[[:space:]]*$1=[^[:space:]#]" "${ENV_FILE}"
}

[ -f "${ENV_FILE}" ] || fail "没有 ${ENV_FILE}。
  unit 用 EnvironmentFile=-（容忍缺失）是为了让 systemd 不因为文件不在就拒绝启动，
  但 agent 没有 key 一样活不下来。建一个，至少写上：
    LENS_LLM_API_KEY=sk-...
  （也认 OPENAI_API_KEY；两者都在时前者优先，见 llm/deepseek.py 的 read_api_key）"

# systemd 的 EnvironmentFile 解析器不认 shell 的 `export`：那种行会被当成一个
# 名叫 "export FOO" 的非法变量名，**整行静默丢掉**。于是 key 明明写在文件里，
# 进程却拿不到 —— 这正是最难查的那种错，所以在这里就拦掉。
BAD_EXPORT="$(grep -nE '^[[:space:]]*export[[:space:]]+[A-Za-z_]' "${ENV_FILE}" || true)"
[ -z "${BAD_EXPORT}" ] || fail "${ENV_FILE} 里有 shell 风格的 export 行，systemd 读不了：
${BAD_EXPORT}
  去掉行首的 export，改成裸的 KEY=VALUE。"

if env_defines LENS_LLM_API_KEY; then
  echo "  · LLM key    LENS_LLM_API_KEY"
elif env_defines OPENAI_API_KEY; then
  echo "  · LLM key    OPENAI_API_KEY（LENS_LLM_API_KEY 优先级更高，可用它覆盖）"
else
  fail "${ENV_FILE} 里没有可用的 LLM key。
  加上这两个之一（前者优先）：
    LENS_LLM_API_KEY=sk-...
    OPENAI_API_KEY=sk-...
  这条链路指向的是 DeepSeek 端点；很多机器上的 OPENAI_API_KEY 是别的项目的，
  混用会得到一个很难查的 401 —— 所以更推荐显式写 LENS_LLM_API_KEY。"
fi

# 点名这个坑：仓库根 .env 里躺着的 MODEL_NAME 是**别的东西在用**的，
# agent 只认 LENS_LLM_MODEL。不设就静默落到 deepseek.py 的 DEFAULT_MODEL，
# 表现是「我明明配了模型，它却在用另一个」，而且没有任何报错。
if ! env_defines LENS_LLM_MODEL; then
  echo "  ⚠ 没有 LENS_LLM_MODEL —— 模型会落到 lens_agent/llm/deepseek.py 的 DEFAULT_MODEL。"
  env_defines MODEL_NAME && \
    echo "    （.env 里的 MODEL_NAME **不被 agent 读取**，改它没有任何效果。）"
  echo "    要指定模型/端点，在 .env 里加 LENS_LLM_MODEL= 和 LENS_LLM_BASE_URL=。"
  echo "    装完之后 agent 的 /healthz 会自报实际用的是哪个，见文末的自检输出。"
fi

# .env 里是明文 API key，权限太松等于放在公共走廊上。
# GNU（Linux，实际部署环境）在前、BSD（macOS 开发机）兜底。反过来不行：
# GNU 的 `stat -f` 是「查文件系统」，遇到 %Lp 会打印一个 "?" 并**正常退出**，
# 于是永远走到 else 分支，报一句「权限是 ?」的假警告。
PERM="$(stat -c '%a' "${ENV_FILE}" 2>/dev/null || stat -f '%Lp' "${ENV_FILE}" 2>/dev/null || echo '')"
case "${PERM}" in
  ''|600|400) ;;
  *) echo "  ⚠ ${ENV_FILE} 权限是 ${PERM}，里面是明文 key。建议 chmod 600 .env" ;;
esac

# unit 里写的每个 LENS_* 变量名都要在 agent 代码里真有读取点。
# 写错一个字母不会有任何报错，只会让那个设置**看起来生效了**。
MISSING_VARS=""
for v in $(grep -oE 'LENS_[A-Z_]+' "${UNIT_SRC}" | sort -u); do
  grep -rq "\"${v}\"" "${ROOT}/gateway/lens_agent/" || MISSING_VARS="${MISSING_VARS} ${v}"
done
[ -z "${MISSING_VARS}" ] || fail "unit 里这些变量在 gateway/lens_agent/ 里搜不到读取点：${MISSING_VARS}
  多半是变量名写错了。"
echo "  · unit 环境变量名 全部能在 lens_agent/ 里找到读取点"

if [ "${CHECK_ONLY}" = 1 ]; then
  echo
  echo "✓ preflight 通过（--check：没有改动任何东西）"
  exit 0
fi

command -v systemctl >/dev/null || fail "这台机器上没有 systemctl。
  本 unit 是给 Linux + systemd 的服务器准备的；开发机上请用 ./demo/start.sh --lens。"

# ---------------- 安装 ----------------

# agent 的全部状态（audit.jsonl / reminders.json / lists.json）都落在这里。
# 700 不是洁癖：审计日志里有用户说过的每一句话。
mkdir -p "${AGENT_DIR}"
chmod 700 "${AGENT_DIR}"

mkdir -p "${UNIT_DIR}"
# 记下装之前跑没跑，因为 `enable --now` 对**已经在跑**的服务什么都不做 ——
# 改完 unit 重跑本脚本会得到「装好了」却仍在用旧配置的进程，这种无声的空操作
# 最容易让人以为改动没生效而去乱改别的地方。
WAS_ACTIVE="$(systemctl --user is-active lens-agent.service 2>/dev/null || true)"
cp "${UNIT_SRC}" "${UNIT_DIR}/"

# 作答语言跟着网关的 asr.language 走。unit 模板里写死的是 zh（与代码默认值一致），
# 不一致时用 drop-in 覆盖而不是改那份模板 —— 改模板会让下次 git pull 冲突，
# 而 drop-in 是 systemd 自己就为「本机差异」准备的地方。
LOCALE="$("${PY}" - "${CONFIG_JSON}" <<'PYEOF'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
try:
    print(json.loads(p.read_text()).get("asr", {}).get("language", "zh"))
except Exception:
    print("zh")
PYEOF
)"
DROPIN_DIR="${UNIT_DIR}/lens-agent.service.d"
if [ "${LOCALE}" = "zh" ]; then
  rm -f "${DROPIN_DIR}/locale.conf"
  rmdir "${DROPIN_DIR}" 2>/dev/null || true
else
  mkdir -p "${DROPIN_DIR}"
  printf '# 由 scripts/install-agent-service.sh 按网关 asr.language 生成\n[Service]\nEnvironment=LENS_AGENT_LOCALE=%s\n' \
    "${LOCALE}" > "${DROPIN_DIR}/locale.conf"
  echo "  · 作答语言按网关 asr.language 设为 ${LOCALE}（drop-in：${DROPIN_DIR}/locale.conf）"
fi

systemctl --user daemon-reload
systemctl --user enable --now lens-agent.service
if [ "${WAS_ACTIVE}" = "active" ]; then
  echo "  · 之前就在跑，重启一次让新 unit 生效"
  systemctl --user restart lens-agent.service
fi

# 用户服务默认**只在你登录期间活着**。没开 linger 的话，服务器重启后
# 网关和 agent 都不会自己回来 —— 而这正是这套 unit 想解决的问题本身。
ME="$(id -un)"
if [ "$(loginctl show-user "${ME}" --property=Linger --value 2>/dev/null || echo no)" != "yes" ]; then
  echo "▸ 打开 linger（否则注销/重启后用户服务不会自动起来）"
  loginctl enable-linger "${ME}" 2>/dev/null || \
    echo "  ⚠ 没权限，请手动跑：sudo loginctl enable-linger ${ME}"
fi

# ---------------- 自检 ----------------

echo
echo "▸ 自检"
sleep 1
ACTIVE="$(systemctl --user is-active lens-agent.service || true)"
echo "  · systemctl is-active → ${ACTIVE}"
if [ "${ACTIVE}" != "active" ]; then
  systemctl --user status lens-agent.service --no-pager -l | head -20
  fail "服务没起来。上面是 status；完整日志：journalctl --user -u lens-agent -n 50"
fi

# agent 自己的 /healthz 会报出**实际生效的模型**，这是上面那条 LENS_LLM_MODEL
# 提醒唯一能被证伪的地方 —— 配错了在这里就能看见，不用等到说了话才发现。
AGENT_HEALTH=""
for _ in $(seq 1 20); do
  AGENT_HEALTH="$(curl -s -m 1 "http://127.0.0.1:${AGENT_PORT}/healthz" || true)"
  [ -n "${AGENT_HEALTH}" ] && break
  sleep 0.5
done
if [ -n "${AGENT_HEALTH}" ]; then
  echo "  · agent 自报 → ${AGENT_HEALTH}"
else
  echo "  ⚠ 连不上 http://127.0.0.1:${AGENT_PORT}/healthz。"
  echo "    若你改过 LENS_AGENT_PORT，用 LENS_AGENT_PORT=<端口> 重跑本脚本再看。"
fi

GW_PORT="$("${PY}" - "${CONFIG_JSON}" <<'PYEOF'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
try:
    print(json.loads(p.read_text()).get("port", 8443))
except Exception:
    print(8443)
PYEOF
)"
GW_HEALTH="$(curl -s -m 2 "http://127.0.0.1:${GW_PORT}/healthz" || true)"
if [ -z "${GW_HEALTH}" ]; then
  echo "  · 网关没在 127.0.0.1:${GW_PORT} 上应答 —— 它可能还没装/没起，这不影响 agent 本身。"
  echo "    装网关：bash scripts/install-service.sh"
else
  echo "  · 网关 /healthz → ${GW_HEALTH}"
  echo "    agent.connected 此刻是 false 属正常：网关的连接是懒建的，"
  echo "    刚重启过 agent 就更是如此，第一次说话时会自己连上。"
  echo "    想让它立刻显示 true：systemctl --user restart lens-gateway.service"
  echo "    （代价是所有在线眼镜的 WebSocket 会被断开重连一次。）"
fi

# ---------------- 收尾提示 ----------------

PROVIDER="$("${PY}" - "${CONFIG_JSON}" <<'PYEOF'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
try:
    print(json.loads(p.read_text()).get("agent", {}).get("provider", "openclaw"))
except Exception:
    print("openclaw")
PYEOF
)"
echo
if [ "${PROVIDER}" = "lens" ]; then
  echo "✓ 装好了，且网关的 agent.provider 已经是 lens —— 说的话会走到这个 agent。"
else
  echo "✓ 装好了，但网关的 agent.provider 现在是 \"${PROVIDER}\"，**不会用到这个 agent**。"
  echo "  要用它，在 ${CONFIG_JSON} 里写："
  echo '    "agent": { "provider": "lens", "url": "ws://127.0.0.1:18790" }'
  echo "  然后 systemctl --user restart lens-gateway.service"
fi
echo "日志：journalctl --user -u lens-agent -f"
