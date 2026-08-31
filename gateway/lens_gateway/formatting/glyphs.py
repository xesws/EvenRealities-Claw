"""HUD 语义字形表 —— 只允许 G2 字库里真实存在的字形。

**背景**：G2 的字库覆盖是有限的，字库外的字符固件**不会画出你要的那个图标**
（官方文档记为静默跳过，官方度量库记为 4px 占位框）。仓库早期用的 13 个 HUD 字形里
有 10 个不在字库：`⛓ ◉ ◔ ⚙ ▸ ✓ ✕ ⚠ ⏸ ⏹`（另有 `⚡` 也不在），
真机上会退化成「工　聆听 0:07」这样的空洞状态条 —— 也就是
`docs/DESIGN.md` 的「0.5 秒瞥视契约」整体失效。

**做法**：所有 HUD 图标改为按**语义名**索引，字形表在导入时用
`metrics.in_font()` 逐个自校验，任何不在字库的字形都会立刻抛错，
不可能被悄悄下发到眼镜。三套预置档位：

- ``symbol``（默认）：图形符号，最省宽度，全部经度量表验证
- ``cjk``：纯中文，字宽统一 20px，最稳
- ``ascii``：纯半角，给非中文语料或调试用

真机复验若发现某个字形观感不佳，改一行配置 restart 即可，无需动代码。
"""
from __future__ import annotations

from dataclasses import dataclass

from .contract import contract
from .metrics import metrics

# 语义名 → 各档位字形，来自跨语言共享契约 `protocol/hud-contract.json`。
# 契约里的 `_replaces` 字段记录了每个字形替换掉了哪个**不在 G2 字库**的原字形。
_PROFILES: dict[str, dict[str, str]] = {
    name: dict(table) for name, table in contract()["glyphProfiles"].items()
}

DEFAULT_PROFILE = "symbol"

# 这些语义名的字形在正文里是**正常内容**（列表符、省略号、页码箭头），永远不剥
_CONTENT_KEYS: frozenset[str] = frozenset(("bullet", "ellipsis", "page_prev", "page_next"))
# 会出现在状态条上的语义名
_STATUS_KEYS: frozenset[str] = frozenset(k for k in _PROFILES["symbol"] if k not in _CONTENT_KEYS)


def _is_strippable(glyph: str) -> bool:
    """这个字形出现在正文行首时，是否可以安全地剥掉。

    只剥**纯符号**。汉字和字母一律不动 —— `cjk` 档的字形是「转/完/思/断」这类
    极常用汉字，一旦纳入剥离集合，正文里的「转账给…」会被吃成「账给…」、
    「完成」会被吃成「成」。这是 golden 语料抓到的真实回归。
    """
    if len(glyph) != 1:
        return False
    cp = ord(glyph)
    if glyph.isalnum():
        return False
    # CJK 及汉字兼容区
    return not ((0x2E80 <= cp <= 0x9FFF) or (0xF900 <= cp <= 0xFAFF) or (0xAC00 <= cp <= 0xD7AF))


def _strippable_of(table: dict[str, str]) -> frozenset[str]:
    """可从正文行首安全剥离的状态字形。

    两道过滤：
    1. 只留纯符号（汉字/字母不剥，见 `_is_strippable`）
    2. **减去**用作正文内容的字形 —— `·` 既是 idle 状态字形又是 markdown 列表符，
       不排除的话「· 第一条」会被剥成「第一条」，列表的项目符号全丢。
    """
    content = {table[k] for k in _CONTENT_KEYS if k in table}
    return frozenset(
        g for k, g in table.items()
        if k in _STATUS_KEYS and _is_strippable(g) and g not in content
    )


# 兜底集合（未指定 GlyphSet 时使用）
STATUS_GLYPHS: frozenset[str] = _strippable_of(_PROFILES["symbol"])


class GlyphError(ValueError):
    """字形表里出现了 G2 字库外的字形。"""


@dataclass(frozen=True)
class GlyphSet:
    """一套经过校验的 HUD 字形。用 `glyph_set()` 构造，不要直接实例化。"""

    profile: str
    table: dict[str, str]

    def __getitem__(self, name: str) -> str:
        try:
            return self.table[name]
        except KeyError:
            raise KeyError(f"未知语义字形名 {name!r}，可用：{sorted(self.table)}") from None

    def get(self, name: str, default: str = "") -> str:
        return self.table.get(name, default)

    def strippable(self) -> frozenset[str]:
        """本档位中「出现在正文行首时可以安全剥掉」的状态字形。"""
        return _strippable_of(self.table)


def _validate(table: dict[str, str], where: str) -> None:
    """确保表里每个字形都能被固件真正画出来。"""
    m = metrics()
    bad: list[str] = []
    for name, glyph in table.items():
        for ch in glyph:
            if not m.in_font(ord(ch)):
                bad.append(f"{name}={glyph!r} 含 U+{ord(ch):04X} ({ch})")
    if bad:
        raise GlyphError(
            f"{where} 含 G2 字库外的字形，真机上画不出来：" + "；".join(bad)
            + "。可用替代见 docs/GLYPH-TABLE.md。"
        )


def glyph_set(profile: str = DEFAULT_PROFILE, overrides: dict[str, str] | None = None) -> GlyphSet:
    """取一套字形。`overrides` 允许按语义名覆盖单个字形（同样会被校验）。

    :raises GlyphError: 档位或覆盖项里出现了字库外的字形
    """
    if profile not in _PROFILES:
        raise GlyphError(f"未知字形档位 {profile!r}，可用：{sorted(_PROFILES)}")
    table = dict(_PROFILES[profile])
    if overrides:
        unknown = set(overrides) - set(table)
        if unknown:
            raise GlyphError(f"未知语义字形名：{sorted(unknown)}；可用：{sorted(table)}")
        table.update(overrides)
        _validate({k: v for k, v in table.items() if k in overrides}, "glyph_overrides")
    return GlyphSet(profile=profile, table=table)


def available_profiles() -> list[str]:
    return sorted(_PROFILES)


# 导入即校验全部预置档位 —— 有问题就在启动时炸，而不是等到眼镜上少了个图标。
for _name, _table in _PROFILES.items():
    _validate(_table, f"预置字形档位 {_name!r}")
