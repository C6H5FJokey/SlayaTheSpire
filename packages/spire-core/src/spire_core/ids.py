"""短 id 与哈希工具。

短 id 是候选 id 的组成部分（如 `play:h2->m0`），所以它们的**稳定性**是
可复现性的前提：顺序一律取自游戏数组下标，绝不排序。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# 区域前缀。与 docs/05-state-schema.md 的"实例短 id"表一一对应。
HAND = "h"
MONSTER = "m"
POTION = "p"
RELIC = "r"

# 区域名（牌堆）
ZONE_HAND = "hand"
ZONE_DRAW = "draw"
ZONE_DISCARD = "discard"
ZONE_EXHAUST = "exhaust"
ZONE_DECK = "deck"

ZONES = (ZONE_HAND, ZONE_DRAW, ZONE_DISCARD, ZONE_EXHAUST, ZONE_DECK)


def hand_id(index: int) -> str:
    """手牌第 index 张的短 id。"""
    return f"{HAND}{index}"


def monster_id(index: int) -> str:
    """敌人第 index 个的短 id。"""
    return f"{MONSTER}{index}"


def potion_id(index: int) -> str:
    """药水槽第 index 个的短 id。"""
    return f"{POTION}{index}"


def relic_id(index: int) -> str:
    """遗物第 index 个的短 id。"""
    return f"{RELIC}{index}"


def node_id(x: int, y: int) -> str:
    """地图节点 id。地图坐标天然唯一，不需要实例顺序。"""
    return f"n{x}_{y}"


def canonical_json(obj: Any) -> str:
    """确定性 JSON：键排序、不转义非 ASCII、无多余空白。

    core 里所有参与哈希或落盘比较的 JSON 都必须经过它。
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def short_hash(obj: Any, length: int = 16) -> str:
    """对象的确定性短哈希，用 canonical_json 做规范化。"""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:length]