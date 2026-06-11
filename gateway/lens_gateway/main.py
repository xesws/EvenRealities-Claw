"""CLI 入口：serve / pair-code / devices / revoke。"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from .config import Config


def _admin(cfg: Config, method: str, path: str, body: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{cfg.port}{path}"
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def main() -> None:
    parser = argparse.ArgumentParser(prog="lens-gateway", description="OpenClaw Lens Gateway")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="启动网关服务")
    sub.add_parser("pair-code", help="生成一次性配对码（需服务运行中）")
    sub.add_parser("devices", help="列出已配对设备")
    p_revoke = sub.add_parser("revoke", help="吊销设备")
    p_revoke.add_argument("device_id")
    args = parser.parse_args()

    cfg = Config.load()
    if args.cmd == "serve":
        from .server import run
        run(cfg)
    elif args.cmd == "pair-code":
        try:
            out = _admin(cfg, "POST", "/admin/pair-code")
        except Exception as exc:
            print(f"无法连接运行中的网关（{exc}）。请先启动：lens-gateway serve", file=sys.stderr)
            sys.exit(1)
        print(f"配对码：{out['code']}（{out['ttl'] // 60} 分钟内有效，一次性）")
    elif args.cmd == "devices":
        for d in _admin(cfg, "GET", "/admin/devices"):
            mark = "（已吊销）" if d["revoked"] else ""
            print(f"{d['device_id']}  {d['name']}{mark}")
    elif args.cmd == "revoke":
        out = _admin(cfg, "POST", "/admin/revoke", {"deviceId": args.device_id})
        print("已吊销" if out["ok"] else "未找到该设备")


if __name__ == "__main__":
    main()
