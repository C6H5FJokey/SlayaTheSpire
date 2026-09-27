"""英文描述生成。

所有进入 `state` 与候选 criteria 的文本都在这里产生，保证：

- **英文**（走英文 checkpoint，见 docs/01-overview.md 的假设）
- **确定性**（纯函数，无时间/随机）
- **保留数值**（截断时宁可丢修饰语也不丢数字）
"""

from __future__ import annotations

import re

from .model import (
    CARD_TYPE_ATTACK,
    CARD_TYPE_CURSE,
    CARD_TYPE_POWER,
    CARD_TYPE_SKILL,
    ROOM_BOSS,
    ROOM_ELITE,
    ROOM_EVENT,
    ROOM_MONSTER,
    ROOM_REST,
    ROOM_SHOP,
    ROOM_TREASURE,
    ROOM_UNKNOWN,
    TARGET_ALL_ENEMY,
    TARGET_ENEMY,
    TARGET_NONE,
    TARGET_SELF,
    FairObservation,
    Monster,
    RawCard,
    RawMapNode,
    RawRelic,
    RawShopItem,
)

CARD_TEXT_LIMIT = 160
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

ROOM_LABEL = {
    ROOM_MONSTER: "Monster",
    ROOM_ELITE: "Elite",
    ROOM_EVENT: "Event",
    ROOM_REST: "Rest",
    ROOM_SHOP: "Shop",
    ROOM_TREASURE: "Treasure",
    ROOM_BOSS: "Boss",
    ROOM_UNKNOWN: "Unknown",
}


def truncate_keep_numbers(text: str, limit: int = CARD_TEXT_LIMIT) -> str:
    """截断文本但保留全部数字。

    先按句子边界裁；若仍超长则硬裁，并把被裁掉的所有数字以
    ` +<num> <num>` 的形式补回尾部 —— 宁可丢修饰语也不丢数字，
    因为数值是决策的关键（见 docs/05-state-schema.md#3-文本裁剪）。
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text

    kept: list[str] = []
    used = 0
    for sentence in re.split(r"(?<=\.)\s+", text):
        if used + len(sentence) + 1 > limit:
            break
        kept.append(sentence)
        used += len(sentence) + 1

    kept_text = " ".join(kept).strip()
    dropped = text[len(kept_text) :] if kept_text else text
    dropped_numbers = NUMBER_RE.findall(dropped)
    if dropped_numbers:
        suffix = " +" + " ".join(dropped_numbers)
    else:
        suffix = "…"
    if not kept_text:
        # 单句就超长：硬裁
        kept_text = text[: max(0, limit - len(suffix))]
    return (kept_text + suffix)[: limit + len(suffix) + 4]


def card_name(card: RawCard) -> str:
    """带升级标记的牌名：`Bash+`。"""
    return card.name + ("+" * card.upgrades)


def card_cost(card: RawCard) -> str:
    if card.is_x_cost:
        return "X"
    cost = card.effective_cost
    if cost < 0:
        return "-"
    return str(cost)


def card_brief(card: RawCard) -> str:
    """一句话描述一张牌，用于 state 与 criteria。"""
    return f"{card_name(card)} ({card_cost(card)}E): {truncate_keep_numbers(card.text)}"


def monster_brief(m: Monster) -> str:
    parts = [f"{m.name} (m{m.index})", f"{m.hp}/{m.max_hp} HP"]
    if m.block:
        parts.append(f"{m.block} block")
    if m.powers:
        pw = ", ".join(f"{p.amount} {p.id}" for p in m.powers)
        parts.append(pw)
    if m.intent is not None and m.intent.text:
        parts.append(f"intent: {m.intent.text}")
    return ", ".join(parts)


def target_suffix(card: RawCard) -> str:
    if card.target_type == TARGET_ENEMY:
        return "on <target>"
    if card.target_type == TARGET_ALL_ENEMY:
        return "on all enemies"
    if card.target_type == TARGET_SELF:
        return "on self"
    if card.target_type == TARGET_NONE:
        return ""
    return ""


def deck_summary(fair: FairObservation) -> str:
    """卡组强度摘要：类型分布 + 升级数 + 总张数。

    只给计数，不给每张牌的文本 —— 省预算，且不损失公平性
    （牌组内容玩家可见，但牌面文本对当前决策影响有限）。
    """
    by_id = {s.id: s.count for s in fair.zones.deck}
    # 折叠后的 stacks 里没有 type，这里只能给"牌种数"与总张数；
    # 类型分布由 mod 在 raw.deck 里逐张提供，公平视图已折叠，故用 stacks 近似。
    total = sum(s.count for s in fair.zones.deck)
    upgraded = sum(s.count for s in fair.zones.deck if s.upgrades > 0)
    distinct = len(by_id)
    return f"{total} cards ({distinct} distinct, {upgraded} upgraded)"


def describe_map_candidate(
    fair: FairObservation, node: RawMapNode, depth: int = 2
) -> str:
    """地图候选描述。

    必须包含：节点类型 + 下游 depth 层可达结构摘要 + 精英/Boss 距离 + 当前资源。
    地图在游戏内本就全可见，所以下游统计不违反公平性。
    """
    if fair.map is None:
        return ROOM_LABEL.get(node.type, node.type)

    by_id = {n.id: n for n in fair.map.nodes}

    # BFS 下游可达
    seen: set[str] = {node.id}
    frontier = [node.id]
    counts: dict[str, int] = {}
    for _ in range(max(1, depth)):
        nxt: list[str] = []
        for nid in frontier:
            cur = by_id.get(nid)
            if cur is None:
                continue
            for child in cur.children:
                if child in seen:
                    continue
                seen.add(child)
                nxt.append(child)
                child_node = by_id.get(child)
                if child_node is not None:
                    counts[child_node.type] = counts.get(child_node.type, 0) + 1
        frontier = nxt
        if not frontier:
            break

    order = (
        ROOM_ELITE,
        ROOM_MONSTER,
        ROOM_EVENT,
        ROOM_REST,
        ROOM_SHOP,
        ROOM_TREASURE,
        ROOM_UNKNOWN,
    )
    parts = [f"next floor: {ROOM_LABEL.get(node.type, node.type)}"]
    summary = ", ".join(
        f"{counts[t]} {ROOM_LABEL[t]}" for t in order if counts.get(t)
    )
    if summary:
        parts.append(f"then within {depth} floors: {summary}")

    # 精英/Boss 距离
    if fair.map.boss:
        boss_node = by_id.get(fair.map.boss)
        if boss_node is not None:
            parts.append(f"boss is {abs(boss_node.y - node.y)} floors away")

    # 当前资源
    p = fair.player
    # 空槽不是药水（见 RawPotion.is_empty）：以前这里把空槽也数进去，
    # 地图候选上写着"你带着 3 瓶药水"，而玩家一瓶都没有。
    potions = len(fair.potions())
    parts.append(
        f"you have {p.hp}/{p.max_hp} HP, {p.gold} gold, {potions} potions, "
        f"{deck_summary(fair)}"
    )
    return "; ".join(parts)


def describe_map_node_short(node: RawMapNode) -> str:
    return f"{node.id} {ROOM_LABEL.get(node.type, node.type)}"


def relic_brief(relic: RawRelic) -> str:
    """遗物候选描述：名字 + 效果。人类在 Boss 遗物/宝箱界面读的就是这两样。"""
    name = relic.name or relic.id
    if relic.text:
        return f"Take relic {name}: {relic.text}"
    return f"Take relic {name}."


def shop_brief(item: RawShopItem) -> str:
    """商店商品描述：名字 + 价格 + 效果（原来只有 kind + id + 价格）。"""
    name = item.name or item.id
    head = f"Buy {name} for {item.price} gold."
    if item.text:
        return f"{head} {truncate_keep_numbers(item.text)}"
    return head


__all__ = [
    "CARD_TEXT_LIMIT",
    "ROOM_LABEL",
    "card_brief",
    "card_cost",
    "card_name",
    "deck_summary",
    "describe_map_candidate",
    "describe_map_node_short",
    "monster_brief",
    "relic_brief",
    "shop_brief",
    "target_suffix",
    "truncate_keep_numbers",
]
