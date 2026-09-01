"""闸 4：审计日志。单行 JSON 追加写。

700 行代码 + 完整调用日志，一个人可以真正复核 —— 这是小项目才负担得起、
也才有意义的审计方式。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(os.environ.get("LENS_AGENT_AUDIT",
                                   "~/.lens-agent/audit.jsonl")).expanduser()


class Audit:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_PATH

    def record(self, session_key: str, skill: str, tool: str, args: str,
               result: str, ok: bool, elapsed_ms: int) -> None:
        row = {
            "ts": round(time.time(), 3),
            "session": session_key,
            "skill": skill,
            "tool": tool,
            "args": args[:500],
            "ok": ok,
            "elapsed_ms": elapsed_ms,
            "result": result[:500],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            # 审计写不进去不该让用户的这轮对话失败，但必须留下痕迹
            log.exception("审计日志写入失败：%s", self.path)

    def denied(self, session_key: str, skill: str, tool: str, reason: str) -> None:
        """被 policy 拒掉的调用**同样要记** —— 这才是真正值得看的那部分日志。"""
        self.record(session_key, skill, tool, "", f"DENIED: {reason}", False, 0)
