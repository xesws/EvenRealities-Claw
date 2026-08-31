"""加载跨语言共享的 HUD 契约 `protocol/hud-contract.json`。

网关（Python）与插件（TypeScript）读**同一个文件**：画布几何、三容器版式、
语义字形表都只有一份定义，两端不可能漂移。规格出处见 `docs/HARDWARE-SPEC.md`。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

#: 契约文件路径。仓库布局：<repo>/protocol/hud-contract.json
CONTRACT_PATH = Path(__file__).resolve().parents[3] / "protocol" / "hud-contract.json"


@lru_cache(maxsize=1)
def contract() -> dict:
    """读取并缓存 HUD 契约。"""
    if not CONTRACT_PATH.exists():
        raise FileNotFoundError(
            f"缺少 HUD 契约文件 {CONTRACT_PATH}——网关与插件的版式/字形都由它定义"
        )
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
