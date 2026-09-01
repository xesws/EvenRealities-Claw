# deploy/ — 生产部署面

装到一台 Linux + systemd 的服务器上。开发机上跑的是 `./demo/start.sh`，跟这里无关。

## 里面有什么

| 文件 | 干什么 |
|---|---|
| `lens-gateway.service` | 网关（HTTP + WebSocket + ASR + 排版）的 systemd **用户** unit |
| `lens-agent.service` | 自研 agent 的 systemd 用户 unit。**只有 `agent.provider = "lens"` 时才需要** |
| `Caddyfile.example` | TLS 前端模板（`{$LENS_DOMAIN}` 占位）。不是能直接用的配置 |

配套的三个脚本在 `../scripts/`：

| 脚本 | 干什么 |
|---|---|
| `install-service.sh` | 装网关 unit 并起来 |
| `install-agent-service.sh` | 装 agent unit。preflight 会验 venv、`.env` 里的 LLM key、以及 unit 里的变量名 |
| `install-tls.sh` | 装 Caddy、写 Caddyfile、把网关收回 `127.0.0.1`、打开 `trust_forwarded_for` |

后两个脚本支持 `--check`：只跑 preflight，不碰 systemd，也不改任何文件。

## 装机顺序

```bash
# 0. venv（两个服务共用同一个）
python3 -m venv gateway/.venv
gateway/.venv/bin/pip install -r gateway/requirements.txt

# 1. 网关
bash scripts/install-service.sh

# 2. agent —— 只有 provider=lens 才要。凭证从仓库根 .env 读
bash scripts/install-agent-service.sh

# 3. TLS —— 需要域名，见下
LENS_DOMAIN=lens.example.com bash scripts/install-tls.sh
```

## 三件容易被漏掉的事

**用户服务默认只在你登录期间活着。** 没有 `sudo loginctl enable-linger <你的用户名>`，
服务器重启后两个服务都不会自己回来 —— 而这正是这套 unit 存在的理由。
`install-agent-service.sh` 会尝试替你打开它。

**网关不会拉起 agent。** `config.json` 里的 `ws://127.0.0.1:18790` 只是个要去连的地址，
`lens_gateway/` 里没有任何 `subprocess`。所以两者是**两个独立的服务**，
unit 之间也刻意只有 `After=` 排序、没有 `Requires=` —— 任何一个单独重启都不该带走另一个。
`provider=lens` 却没装 agent 的表现是：网关一切正常，只有说话没反应。

**`.env` 里的 `MODEL_NAME` 不被 agent 读取。** agent 认的是 `LENS_LLM_MODEL`
（`gateway/lens_agent/llm/deepseek.py`）。不设就静默落到代码里的默认模型，没有任何报错。
装完之后 `curl http://127.0.0.1:18790/healthz` 会自报实际用的是哪个模型 —— 以那个为准。

## 没有域名的时候

**先别装 TLS。** 明文 `ws://` 只在你自己信得过的网络里用（比如一台只有你能访问的
内网机器、或者临时演示）—— 配对码和 refresh token 都在那条连接上明文走。

真要对外，`install-tls.sh` 在没有 `LENS_DOMAIN` 时会打印三条路（买域名 ✅ /
Caddy `tls internal` + 手机装根证书 ⚠️ / sslip.io 旁路 ⚠️）以及各自的代价。
结论是买个便宜域名，另外两条花的时间都比那点钱贵。
