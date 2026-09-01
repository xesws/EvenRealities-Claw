#!/usr/bin/env bash
# 给网关套上 TLS：Caddy 反代 + Let's Encrypt 自动证书
#
# 用法：
#   LENS_DOMAIN=lens.example.com bash scripts/install-tls.sh
#   LENS_DOMAIN=lens.example.com bash scripts/install-tls.sh --check   只跑 preflight
#
# 网关侧**一行代码都不用改**：server.py 的 run_app 本来就没有 ssl_context，
# 监听地址由 config.json 决定；_client_key() 早就为反代写好了 X-Forwarded-For 分支；
# 控制面鉴权也早从 loopback 判据换成了 Bearer。所以这个脚本做的全是配置与装包。
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PY="${ROOT}/gateway/.venv/bin/python"
STATE_DIR="${LENS_STATE_DIR:-${HOME}/.lens-gateway}"
CONFIG_JSON="${STATE_DIR}/config.json"
TEMPLATE="${ROOT}/deploy/Caddyfile.example"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

fail() { echo; echo "✗ $*" >&2; exit 1; }

# ---------------- 必须先有域名 ----------------

if [ -z "${LENS_DOMAIN:-}" ]; then
  cat >&2 <<'TXT'
✗ 需要 LENS_DOMAIN。用法：

    LENS_DOMAIN=lens.example.com bash scripts/install-tls.sh

没有域名的话，下面三条路，代价按顺序递增：

  ✅ 1. 买一个便宜域名（.xyz / .top 之类首年几块钱），解析一条 A 记录到本机公网 IP。
        这是**唯一**能拿到公开可信证书的路：浏览器、iOS、Android 都不用做任何事，
        扫码就能上 https://。花的钱远少于后面两条路要花的时间。

  ⚠️ 2. Caddy 的 `tls internal`（自签），然后在手机上安装并信任 Caddy 的根证书。
        iOS 要先装描述文件，再去「设置 → 通用 → 关于本机 → 证书信任设置」里
        手动打开**完全信任** —— 这一步藏得很深，不知道的人会卡在「证书装了还是不认」。
        每台要用的手机都得来一遍。演示前一晚不要选这条。

  ⚠️ 3. <公网IP>.sslip.io / nip.io 这类通配 DNS 旁路。**不推荐当依赖**：
        这两个域不在 Public Suffix List 上，Let's Encrypt 按整个 sslip.io 计算
        速率限制，历史上被打爆过（github.com/cunnie/sslip.io/issues/108 —— LE 把上限
        从 50 提到 25 万后拒绝了再提到 50 万）。能签出来算你运气好，
        签不出来时你会在演示前十分钟对着一条看不懂的 rate limit 报错。

选定域名之后，先把 A 记录指向本机公网 IP，等 DNS 生效，再回来跑这个脚本 ——
它会先验一遍解析，因为 Let's Encrypt 在这一步失败得非常难看（且有失败次数限制）。
TXT
  exit 1
fi

# ---------------- preflight ----------------

echo "▸ preflight：${LENS_DOMAIN}"

[ -x "${PY}" ] || fail "网关 venv 不在：${PY}（本脚本用它改 JSON 配置）"
[ -f "${TEMPLATE}" ] || fail "找不到 Caddyfile 模板：${TEMPLATE}"

PUBLIC_IP="$(curl -s -m 8 https://api.ipify.org || true)"
# 要求它长得像个 IPv4，而不是「非空就行」：ipify 挂掉时返回的是一段 HTML，
# 那样后面的比对会打印出一坨标签，让人以为是自己的域名配错了。
printf '%s' "${PUBLIC_IP}" | grep -qxE '[0-9]+(\.[0-9]+){3}' || fail "拿不到本机公网 IP。
  curl https://api.ipify.org 返回的不是一个 IPv4（这台机器没出网？ipify 被墙？）。
  确认解析没问题的话可以 LENS_SKIP_DNS_CHECK=1 跳过这项检查。"
echo "  · 本机公网 IP  ${PUBLIC_IP}"

# dig 不一定装（Ubuntu 最小镜像就没有 dnsutils），退回到 python 的解析器。
# 两者的差别要说清：dig 直接问权威/递归 DNS，python 走的是本机 resolver，
# 会吃到 /etc/hosts 和本地缓存 —— 后者「解析对了」不代表**公网上**也解析对了。
if command -v dig >/dev/null; then
  RESOLVED="$(dig +short "${LENS_DOMAIN}" A 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+){3}$' || true)"
else
  echo "  ⚠ 没有 dig（apt install dnsutils），退回本机 resolver —— 它会吃本地缓存与 /etc/hosts。"
  RESOLVED="$("${PY}" -c "import socket,sys
try: print('\n'.join(sorted({ai[4][0] for ai in socket.getaddrinfo(sys.argv[1], None, socket.AF_INET)})))
except Exception: pass" "${LENS_DOMAIN}" || true)"
fi

# 一行版，只用来打印：多条 A 记录时原样换行会把后面的缩进全打乱。
RESOLVED_LINE="${RESOLVED//$'\n'/, }"
RESOLVED_LINE="${RESOLVED_LINE:-无}"

if [ "${LENS_SKIP_DNS_CHECK:-0}" = 1 ]; then
  echo "  ⚠ LENS_SKIP_DNS_CHECK=1，跳过解析校验（解析：${RESOLVED_LINE}）"
elif [ -z "${RESOLVED}" ]; then
  fail "${LENS_DOMAIN} 没有解析出任何 A 记录。
  先在域名商那里加一条 A 记录指向 ${PUBLIC_IP}，等生效（TTL 那么久）再重跑。"
elif ! printf '%s\n' "${RESOLVED}" | grep -qxF "${PUBLIC_IP}"; then
  fail "${LENS_DOMAIN} 解析到了 ${RESOLVED_LINE}，而本机公网 IP 是 ${PUBLIC_IP}。
  这样跑下去 Let's Encrypt 的 HTTP-01 校验必然失败 —— 它会去访问那个 IP 上的机器，
  而那台机器不是这台。而且**失败次数本身有配额**（同一域名每小时 5 次），
  连着试几轮就得等一小时。先把 A 记录改对。
  确实是你有意为之（比如中间还有一层代理），LENS_SKIP_DNS_CHECK=1 可以跳过。"
else
  echo "  · DNS         ${LENS_DOMAIN} → ${PUBLIC_IP} ✓"
fi

GW_PORT="$("${PY}" - "${CONFIG_JSON}" <<'PYEOF'
import json, pathlib, sys
try:
    print(json.loads(pathlib.Path(sys.argv[1]).read_text()).get("port", 8443))
except Exception:
    print(8443)
PYEOF
)"
echo "  · 上游         127.0.0.1:${GW_PORT}（网关明文口）"

if [ "${CHECK_ONLY}" = 1 ]; then
  echo
  echo "✓ preflight 通过（--check：没有改动任何东西）"
  exit 0
fi

command -v systemctl >/dev/null || fail "这台机器上没有 systemctl。本脚本是给 Linux 服务器写的。"

# ---------------- 装 caddy ----------------

if command -v caddy >/dev/null; then
  echo "▸ caddy 已装：$(caddy version | head -1)"
else
  echo "▸ 装 caddy（Cloudsmith 上的官方 apt 源）"
  sudo apt-get update
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y caddy
fi

# ---------------- 写 Caddyfile ----------------

echo "▸ 写 ${CADDYFILE}"
TMP_CADDY="$(mktemp)"
trap 'rm -f "${TMP_CADDY}"' EXIT
# 用 python 而不是 sed：模板里的占位符是 {$LENS_DOMAIN}，花括号和 $ 在 sed
# 表达式里都要转义，写错了是**替换成空字符串**而不是报错 —— 那会得到一个
# 语法合法、但监听 :443 上所有域名的 Caddyfile。
"${PY}" - "${TEMPLATE}" "${LENS_DOMAIN}" "${GW_PORT}" > "${TMP_CADDY}" <<'PYEOF'
import pathlib, sys, time
tpl, domain, port = sys.argv[1], sys.argv[2], sys.argv[3]
text = pathlib.Path(tpl).read_text()
# 断言而不是「找不到就原样输出」：占位符哪天被改名，静默生成一份没有域名的
# Caddyfile 会得到一个语法合法、却对 :443 上所有域名生效的反代。
assert "TEMPLATE-ONLY-END" in text, "模板里没有 TEMPLATE-ONLY-END 分界"
assert "{$LENS_DOMAIN}" in text, "模板里没有 {$LENS_DOMAIN} 占位符"
assert "127.0.0.1:8443" in text, "模板里没有 127.0.0.1:8443 上游"
# 分界以上是「这是个模板」的说明，落到 /etc/caddy/Caddyfile 上就变成假话了。
body = text.split("TEMPLATE-ONLY-END", 1)[1].split("\n", 1)[1]
sys.stdout.write(
    f"# 由 scripts/install-tls.sh 从 deploy/Caddyfile.example 生成于 "
    f"{time.strftime('%Y-%m-%d %H:%M:%S')}。\n"
    f"# 要改就改模板再重跑脚本；直接改这里，下次跑脚本会被覆盖（会先备份）。\n"
    + body.replace("{$LENS_DOMAIN}", domain)
          .replace("127.0.0.1:8443", f"127.0.0.1:{port}"))
PYEOF

# 先验后装：验的是临时文件，不合法就根本不会落到 /etc/caddy 上。
# 反过来（先装再验）在验不过时会留下一个坏配置，而 caddy 下次重启才会发现。
caddy validate --config "${TMP_CADDY}" --adapter caddyfile
echo "  · caddy validate 通过"

if [ -f "${CADDYFILE}" ]; then
  BAK="${CADDYFILE}.bak.$(date +%Y%m%d-%H%M%S)"
  sudo cp -p "${CADDYFILE}" "${BAK}"
  echo "  · 备份 → ${BAK}"
fi
sudo mkdir -p "$(dirname "${CADDYFILE}")"
# install 而不是 cp：mktemp 建出来的是 0600，cp 会把这个权限一起带过去，
# 而 caddy 是以 caddy 用户跑的 —— 它读不了自己的配置文件。
sudo install -o root -g root -m 644 "${TMP_CADDY}" "${CADDYFILE}"

# ---------------- 改网关配置 ----------------

# 关键的一半：不把 host 收回 127.0.0.1，原来的 0.0.0.0:8443 明文口还开着，
# 任何人都能绕过刚装好的 TLS 直连它 —— 那这趟就白装了。
echo "▸ 改 ${CONFIG_JSON}"
"${PY}" - "${CONFIG_JSON}" <<'PYEOF'
import difflib, json, pathlib, sys, time

path = pathlib.Path(sys.argv[1])
if not path.parent.exists():
    # 状态目录里还会放 jwt.secret 与 control.secret，700 是这套代码一贯的约定
    # （demo/start.sh 也是这么建的）。默认 umask 会给出 755。
    path.parent.mkdir(parents=True)
    path.parent.chmod(0o700)
existed = path.exists()
before = path.read_text() if existed else "{}\n"
cfg = json.loads(before) if existed else {}
cfg["host"] = "127.0.0.1"
cfg["trust_forwarded_for"] = True
after = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"

if after == before:
    print("  · 已经是这个样子了，没动它")
    raise SystemExit(0)
if existed:
    bak = path.with_name(path.name + ".bak." + time.strftime("%Y%m%d-%H%M%S"))
    bak.write_text(before)
    bak.chmod(path.stat().st_mode & 0o777)
    print(f"  · 备份 → {bak}")
path.write_text(after)
if not existed:
    path.chmod(0o600)
# 先用一行说清真正改了什么。下面那份 diff 是 json.dumps 重排过的整份文件，
# 缩进变化会连带一堆没动过语义的行 —— 只看 diff 的人会以为 agent 段也被改了。
print('  · 实改两处：host → "127.0.0.1"、trust_forwarded_for → true'
      '（其余是 JSON 重新缩进，语义没变）')
sys.stdout.writelines(difflib.unified_diff(
    before.splitlines(True), after.splitlines(True),
    fromfile=f"{path} (旧)", tofile=f"{path} (新)"))
PYEOF

# 改完必须能被网关自己的加载器读回来。JSON 合法不等于配置合法：
# Config.load() 里有一串 __post_init__ 校验，不合法的值在这里就该炸，
# 而不是等 systemctl restart 之后网关起不来。
PYTHONPATH="${ROOT}/gateway" LENS_STATE_DIR="${STATE_DIR}" "${PY}" - <<'PYEOF'
from lens_gateway.config import Config
c = Config.load()
assert c.host == "127.0.0.1", c.host
assert c.trust_forwarded_for is True
print(f"  · 网关加载校验通过：host={c.host} port={c.port} "
      f"trust_forwarded_for={c.trust_forwarded_for}")
PYEOF

# ---------------- 重启并自检 ----------------

echo "▸ 重启"
sudo systemctl reload-or-restart caddy
# 不让这一步中断脚本：配置已经改完了，网关没装成用户服务（比如还在前台跑）
# 也不该把后面的自检和提示一起吞掉 —— 那会让人以为整件事失败了。
if ! systemctl --user restart lens-gateway.service; then
  echo "  ⚠ 重启 lens-gateway.service 失败（是不是没装成用户服务、还在前台跑？）。"
  echo "    config.json 已经改好了，但网关只在启动时读它 —— 手动重启一次才会生效。"
fi

echo
echo "▸ 自检"
HEALTH=""
for _ in $(seq 1 30); do
  HEALTH="$(curl -s -m 3 "https://${LENS_DOMAIN}/healthz" || true)"
  [ -n "${HEALTH}" ] && break
  sleep 2
done
if [ -n "${HEALTH}" ]; then
  echo "  · https://${LENS_DOMAIN}/healthz → ${HEALTH}"
  echo "    asr_ready 现在是 false 属正常：whisper 预热要 ~1 分钟。"
else
  echo "  ⚠ 一分钟内没等到 https://${LENS_DOMAIN}/healthz 的应答。首次签证书需要几十秒，"
  echo "    但更常见的原因是 **80/443 没开**：Let's Encrypt 要从公网访问进来，"
  echo "    云厂商的安全组和 ufw 都要放行。看日志：sudo journalctl -u caddy -n 50"
fi

echo
echo "✓ 完成。还有两件事该做："
echo "  1. 扫码/配对页的地址换成 https://${LENS_DOMAIN}/plugin/"
echo "     插件会按 location.protocol 自动把网关地址推导成 wss://（plugin/src/ui.ts 的"
echo "     defaultGatewayUrl），所以只要页面本身是 https 打开的，WebSocket 就自动是加密的。"
echo "  2. 把安全组/ufw 里 ${GW_PORT} 的公网入站关掉 —— 网关现在只听 127.0.0.1，"
echo "     那条规则已经没用了，留着只是给下一个人制造「这个口到底通不通」的困惑。"
