"""观测模型。

两层结构：

- `Raw*`  —— mod 上报的**未过滤**观测，字段与游戏对象一一对应。含隐藏信息。
- `Fair*` —— `fairness.filter_` 产出的**公平视图**，字段更少，是唯一允许进入
  `serialize.state` 的东西。

两层的字段集合是刻意"手抄"的（不是自动复制），这样新增字段默认不会泄漏 ——
见 docs/04-fairness.md 的"过滤是删字段，不是置空"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ids import (
    ZONE_DECK,
    ZONE_DISCARD,
    ZONE_EXHAUST,
    ZONE_HAND,
)

# --------------------------------------------------------------------------- #
# 枚举取值（用字符串常量而不是 Enum：JSON 往返更直接，且游戏侧就是字符串）
# --------------------------------------------------------------------------- #

CARD_TYPE_ATTACK = "ATTACK"
CARD_TYPE_SKILL = "SKILL"
CARD_TYPE_POWER = "POWER"
CARD_TYPE_STATUS = "STATUS"
CARD_TYPE_CURSE = "CURSE"

TARGET_NONE = "NONE"
TARGET_SELF = "SELF"
TARGET_ENEMY = "ENEMY"
TARGET_ALL_ENEMY = "ALL_ENEMY"

# 空药水槽在游戏里也是 `PotionSlot` 对象（id 就是 "Potion Slot"），
# 而它的 `canUse()` 返回 true。详见 docs/04-fairness.md#药水。
EMPTY_POTION_ID = "Potion Slot"

ROOM_MONSTER = "MONSTER"
ROOM_ELITE = "ELITE"
ROOM_EVENT = "EVENT"
ROOM_REST = "REST"
ROOM_SHOP = "SHOP"
ROOM_TREASURE = "TREASURE"
ROOM_BOSS = "BOSS"
ROOM_UNKNOWN = "UNKNOWN"

SCREEN_NONE = "NONE"
SCREEN_MAP = "MAP"
SCREEN_CARD_REWARD = "CARD_REWARD"
SCREEN_COMBAT_REWARD = "COMBAT_REWARD"
SCREEN_GRID = "GRID"
SCREEN_EVENT = "EVENT"
SCREEN_SHOP_ROOM = "SHOP_ROOM"
SCREEN_REST = "REST"
SCREEN_BOSS_RELIC = "BOSS_RELIC"
SCREEN_NEOW = "NEOW"
SCREEN_GAME_OVER = "GAME_OVER"

# 卡组强度摘要用得到的分组
ATTACK_LIKE = (CARD_TYPE_ATTACK,)
SKILL_LIKE = (CARD_TYPE_SKILL,)


def _d(d: dict[str, Any], key: str, default: Any = None) -> Any:
    """从 dict 里取值，缺失时给默认值。显式函数便于将来加类型校验。"""
    v = d.get(key, default)
    return default if v is None else v


def _tuple_of(items: Any, fn) -> tuple:
    if not items:
        return ()
    return tuple(fn(x) for x in items)


# --------------------------------------------------------------------------- #
# Raw：未过滤观测
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RawPower:
    id: str
    amount: int
    # 人类悬停就能看到能力的名字与说明，这是公平信息（模组从 eng 资源取英文）。
    name: str = ""
    text: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawPower":
        return RawPower(
            id=str(d["id"]),
            amount=int(_d(d, "amount", 0)),
            name=str(_d(d, "name", "")),
            text=str(_d(d, "text", "")),
        )


@dataclass(frozen=True)
class RawRelic:
    id: str
    counter: int = -1
    name: str = ""
    text: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawRelic":
        return RawRelic(
            id=str(d["id"]),
            counter=int(_d(d, "counter", -1)),
            name=str(_d(d, "name", "")),
            text=str(_d(d, "text", "")),
        )


@dataclass(frozen=True)
class RawPotion:
    id: str
    can_use: bool = True
    requires_target: bool = False
    name: str = ""
    text: str = ""
    # 空槽在模组里也是一个 `PotionSlot` 对象。`empty` 由模组显式给出；
    # 老版本观测没有这个字段时退回按 id 判定（见 `is_empty`）。
    empty: bool = False

    @property
    def is_empty(self) -> bool:
        return self.empty or self.id == EMPTY_POTION_ID

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawPotion":
        pid = str(d["id"])
        return RawPotion(
            id=pid,
            can_use=bool(_d(d, "can_use", True)),
            requires_target=bool(_d(d, "requires_target", False)),
            name=str(_d(d, "name", "")),
            text=str(_d(d, "text", "")),
            empty=bool(_d(d, "empty", pid == EMPTY_POTION_ID)),
        )


@dataclass(frozen=True)
class RawCard:
    """一张牌的实例。

    `index` 是它**所在区域**的 0-based 下标；手牌区域用它构造 `h<i>`。
    """

    index: int
    id: str
    name: str
    type: str
    cost: int
    cost_for_turn: int
    upgrades: int
    rarity: str
    exhausts: bool
    ethereal: bool
    is_playable: bool
    target_type: str
    uuid: str
    text: str
    damage: int = 0
    block: int = 0
    magic_number: int = 0

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawCard":
        cost = int(_d(d, "cost", 0))
        return RawCard(
            index=int(_d(d, "index", 0)),
            id=str(d["id"]),
            name=str(_d(d, "name", d["id"])),
            type=str(_d(d, "type", CARD_TYPE_SKILL)),
            cost=cost,
            cost_for_turn=int(_d(d, "cost_for_turn", cost)),
            upgrades=int(_d(d, "upgrades", 0)),
            rarity=str(_d(d, "rarity", "SPECIAL")),
            exhausts=bool(_d(d, "exhausts", False)),
            ethereal=bool(_d(d, "ethereal", False)),
            is_playable=bool(_d(d, "is_playable", False)),
            target_type=str(_d(d, "target_type", TARGET_NONE)),
            uuid=str(_d(d, "uuid", "")),
            text=str(_d(d, "text", "")),
            damage=int(_d(d, "damage", 0)),
            block=int(_d(d, "block", 0)),
            magic_number=int(_d(d, "magic_number", 0)),
        )

    @property
    def effective_cost(self) -> int:
        """本回合实际费用。费用被改过时 `cost_for_turn` 才是准的。"""
        return self.cost_for_turn

    @property
    def is_x_cost(self) -> bool:
        return self.cost < 0 and self.cost != -2  # -2 是"不可打出/诅咒"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "cost": self.cost,
            "cost_for_turn": self.cost_for_turn,
            "upgrades": self.upgrades,
            "rarity": self.rarity,
            "exhausts": self.exhausts,
            "ethereal": self.ethereal,
            "is_playable": self.is_playable,
            "target_type": self.target_type,
            "uuid": self.uuid,
            "text": self.text,
            "damage": self.damage,
            "block": self.block,
            "magic_number": self.magic_number,
        }


@dataclass(frozen=True)
class RawIntent:
    id: str
    hits: int
    base_damage: int
    adjusted_damage: int
    text: str

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawIntent":
        return RawIntent(
            id=str(_d(d, "id", "UNKNOWN")),
            hits=int(_d(d, "hits", 0)),
            base_damage=int(_d(d, "base_damage", 0)),
            adjusted_damage=int(_d(d, "adjusted_damage", 0)),
            text=str(_d(d, "text", "")),
        )


@dataclass(frozen=True)
class RawMonster:
    index: int
    id: str
    name: str
    hp: int
    max_hp: int
    block: int
    half_dead: bool
    is_gone: bool
    powers: tuple[RawPower, ...]
    intent: RawIntent | None
    move_history: tuple[int, ...] = ()
    upcoming_moves: tuple[int, ...] = ()

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawMonster":
        intent = d.get("intent")
        return RawMonster(
            index=int(_d(d, "index", 0)),
            id=str(d["id"]),
            name=str(_d(d, "name", d["id"])),
            hp=int(_d(d, "hp", 0)),
            max_hp=int(_d(d, "max_hp", 0)),
            block=int(_d(d, "block", 0)),
            half_dead=bool(_d(d, "half_dead", False)),
            is_gone=bool(_d(d, "is_gone", False)),
            powers=_tuple_of(d.get("powers"), RawPower.from_dict),
            intent=RawIntent.from_dict(intent) if intent else None,
            move_history=tuple(int(x) for x in _d(d, "move_history", [])),
            upcoming_moves=tuple(int(x) for x in _d(d, "upcoming_moves", [])),
        )


@dataclass(frozen=True)
class RawPlayer:
    character: str
    hp: int
    max_hp: int
    block: int
    energy: int
    gold: int
    powers: tuple[RawPower, ...]
    relics: tuple[RawRelic, ...]
    potions: tuple[RawPotion | None, ...]
    potion_slots: int = 0

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawPlayer":
        raw_potions = d.get("potions") or []
        potions: list[RawPotion | None] = []
        for p in raw_potions:
            potions.append(RawPotion.from_dict(p) if p else None)
        return RawPlayer(
            character=str(_d(d, "character", "IRONCLAD")),
            hp=int(_d(d, "hp", 0)),
            max_hp=int(_d(d, "max_hp", 0)),
            block=int(_d(d, "block", 0)),
            energy=int(_d(d, "energy", 0)),
            gold=int(_d(d, "gold", 0)),
            powers=_tuple_of(d.get("powers"), RawPower.from_dict),
            relics=_tuple_of(d.get("relics"), RawRelic.from_dict),
            potions=tuple(potions),
            potion_slots=int(_d(d, "potion_slots", len(potions))),
        )


@dataclass(frozen=True)
class RawCombat:
    turn: int
    hand: tuple[RawCard, ...]
    draw_pile: tuple[RawCard, ...]
    discard_pile: tuple[RawCard, ...]
    exhaust_pile: tuple[RawCard, ...]
    monsters: tuple[RawMonster, ...]
    cards_discarded_this_turn: int = 0
    times_damaged: int = 0

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawCombat":
        return RawCombat(
            turn=int(_d(d, "turn", 1)),
            hand=_tuple_of(d.get("hand"), RawCard.from_dict),
            draw_pile=_tuple_of(d.get("draw_pile"), RawCard.from_dict),
            discard_pile=_tuple_of(d.get("discard_pile"), RawCard.from_dict),
            exhaust_pile=_tuple_of(d.get("exhaust_pile"), RawCard.from_dict),
            monsters=_tuple_of(d.get("monsters"), RawMonster.from_dict),
            cards_discarded_this_turn=int(_d(d, "cards_discarded_this_turn", 0)),
            times_damaged=int(_d(d, "times_damaged", 0)),
        )

    def alive_monsters(self) -> tuple[RawMonster, ...]:
        return tuple(m for m in self.monsters if not m.is_gone and m.hp > 0)


@dataclass(frozen=True)
class RawMapNode:
    id: str
    x: int
    y: int
    type: str
    children: tuple[str, ...] = ()

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawMapNode":
        return RawMapNode(
            id=str(d["id"]),
            x=int(_d(d, "x", 0)),
            y=int(_d(d, "y", 0)),
            type=str(_d(d, "type", ROOM_UNKNOWN)),
            children=tuple(str(c) for c in _d(d, "children", [])),
        )


@dataclass(frozen=True)
class RawMap:
    act: int
    nodes: tuple[RawMapNode, ...]
    current: str | None
    reachable: tuple[str, ...]
    boss: str | None = None
    boss_relic_taken: bool = False

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawMap":
        return RawMap(
            act=int(_d(d, "act", 1)),
            nodes=_tuple_of(d.get("nodes"), RawMapNode.from_dict),
            current=d.get("current"),
            reachable=tuple(str(c) for c in _d(d, "reachable", [])),
            boss=d.get("boss"),
            boss_relic_taken=bool(_d(d, "boss_relic_taken", False)),
        )

    def node_by_id(self, nid: str) -> RawMapNode | None:
        for n in self.nodes:
            if n.id == nid:
                return n
        return None


@dataclass(frozen=True)
class ZonedCard:
    """带区域标记的牌。选牌界面（GRID）用得到：候选 id 是 `card:<zone>:<index>`。"""

    zone: str
    card: RawCard

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ZonedCard":
        return ZonedCard(zone=str(_d(d, "zone", ZONE_HAND)), card=RawCard.from_dict(d))


@dataclass(frozen=True)
class RawShopItem:
    index: int
    kind: str           # card | relic | potion | purge
    id: str
    price: int
    affordable: bool
    name: str = ""
    text: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawShopItem":
        return RawShopItem(
            index=int(_d(d, "index", 0)),
            kind=str(_d(d, "kind", "card")),
            id=str(_d(d, "id", "")),
            price=int(_d(d, "price", 0)),
            affordable=bool(_d(d, "affordable", False)),
            name=str(_d(d, "name", "")),
            text=str(_d(d, "text", "")),
        )


@dataclass(frozen=True)
class RewardDetail:
    """`COMBAT_REWARD` 界面里某一条奖励的**已揭晓**内容。

    为什么必须给：卡牌奖励的条目在"看一眼-不满意-退出"之后回到奖励界面时，
    如果选项文案只有 "Card reward" 四个字，模型就永远不知道那三张值不值得再
    点进去，真机上它就陷在 `点进去→看一眼→退出→再点进去` 的死循环里。
    这些内容都是玩家在界面上真的看得到的（公平信息），所以可以进 state。
    """

    kind: str                    # card | relic | potion | gold | stolen_gold | ...
    text: str = ""
    cards: tuple[RawCard, ...] = ()
    id: str = ""
    amount: int = 0

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> "RewardDetail":
        d = d or {}
        return RewardDetail(
            kind=str(_d(d, "kind", "")),
            text=str(_d(d, "text", "")),
            cards=_tuple_of(d.get("cards"), RawCard.from_dict),
            id=str(_d(d, "id", "")),
            amount=int(_d(d, "amount", 0)),
        )


@dataclass(frozen=True)
class ScreenState:
    """界面相关状态。字段按需增长，未用到的界面留空即可。"""

    options: tuple[str, ...] = ()
    option_ids: tuple[str, ...] = ()
    min_select: int = 0
    max_select: int = 0
    select_cards: tuple[ZonedCard, ...] = ()
    reward_cards: tuple[RawCard, ...] = ()
    reward_relics: tuple[RawRelic, ...] = ()
    reward_details: tuple[RewardDetail, ...] = ()
    shop_items: tuple[RawShopItem, ...] = ()
    rest_options: tuple[str, ...] = ()
    neow_options: tuple[str, ...] = ()
    # 选牌界面是**有状态**的：同一块 "选一张牌" 界面对应升级 / 删除 / 变形 /
    # 事件等完全不同的题目。`origin` 是执行侧判定的来由，`event_*` 是事件上下文。
    origin: str = ""
    event_name: str = ""
    event_text: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> "ScreenState":
        d = d or {}
        return ScreenState(
            options=tuple(str(o) for o in _d(d, "options", [])),
            option_ids=tuple(str(o) for o in _d(d, "option_ids", [])),
            min_select=int(_d(d, "min_select", 0)),
            max_select=int(_d(d, "max_select", 0)),
            select_cards=_tuple_of(d.get("select_cards"), ZonedCard.from_dict),
            reward_cards=_tuple_of(d.get("reward_cards"), RawCard.from_dict),
            reward_relics=_tuple_of(d.get("reward_relics"), RawRelic.from_dict),
            reward_details=_tuple_of(d.get("reward_details"), RewardDetail.from_dict),
            shop_items=_tuple_of(d.get("shop_items"), RawShopItem.from_dict),
            rest_options=tuple(str(o) for o in _d(d, "rest_options", [])),
            neow_options=tuple(str(o) for o in _d(d, "neow_options", [])),
            origin=str(_d(d, "origin", "")),
            event_name=str(_d(d, "event_name", "")),
            event_text=str(_d(d, "event_text", "")),
        )


@dataclass(frozen=True)
class RawObservation:
    """mod 上报的完整观测。**含隐藏信息**，绝不直接进 state。"""

    game_version: str
    screen: str
    in_combat: bool
    act: int
    floor: int
    node: int
    room_type: str
    ascension: int
    run_seed: int
    player: RawPlayer
    # 游戏界面语言（`ENG` / `ZHS` / ...）。序列化契约要求英文，agent 会拿它告警。
    language: str = ""
    screen_state: ScreenState = field(default_factory=ScreenState)
    combat: RawCombat | None = None
    deck: tuple[RawCard, ...] = ()
    map: RawMap | None = None

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RawObservation":
        room = d.get("room") or {}
        combat = d.get("combat")
        mapd = d.get("map")
        return RawObservation(
            game_version=str(_d(d, "game_version", "2.3.4")),
            screen=str(_d(d, "screen", SCREEN_NONE)),
            in_combat=bool(_d(d, "in_combat", False)),
            act=int(_d(room, "act", _d(d, "act", 1))),
            floor=int(_d(room, "floor", _d(d, "floor", 0))),
            node=int(_d(room, "node", 0)),
            room_type=str(_d(room, "type", ROOM_UNKNOWN)),
            ascension=int(_d(d, "ascension", 0)),
            run_seed=int(_d(d, "run_seed", 0)),
            player=RawPlayer.from_dict(d.get("player") or {}),
            language=str(_d(d, "language", "")),
            screen_state=ScreenState.from_dict(d.get("screen_state")),
            combat=RawCombat.from_dict(combat) if combat else None,
            deck=_tuple_of(d.get("deck"), RawCard.from_dict),
            map=RawMap.from_dict(mapd) if mapd else None,
        )


# --------------------------------------------------------------------------- #
# Fair：公平视图
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CardStack:
    """同一区域内同名牌的折叠：`Strike ×4`。"""

    id: str
    upgrades: int
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "up": self.upgrades, "n": self.count}


@dataclass(frozen=True)
class Intent:
    """公平意图：只有玩家在界面上看得到的部分。"""

    id: str
    hits: int
    adjusted_damage: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "hits": self.hits,
            "damage": self.adjusted_damage,
            "text": self.text,
        }


@dataclass(frozen=True)
class Monster:
    index: int
    id: str
    name: str
    hp: int
    max_hp: int
    block: int
    powers: tuple[RawPower, ...]
    intent: Intent | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": f"m{self.index}",
            "name": self.name,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "block": self.block,
            "powers": [
                {"id": p.id, "amount": p.amount, "name": p.name, "text": p.text}
                for p in self.powers
            ],
            "intent": self.intent.text if self.intent else "",
            "intent_damage": self.intent.adjusted_damage if self.intent else 0,
            "intent_hits": self.intent.hits if self.intent else 0,
        }


@dataclass(frozen=True)
class Zones:
    """四个区域的折叠视图。全部按多重集处理，**不含顺序信息**。"""

    draw: tuple[CardStack, ...]
    discard: tuple[CardStack, ...]
    exhaust: tuple[CardStack, ...]
    deck: tuple[CardStack, ...]

    def counts(self) -> dict[str, int]:
        return {
            "draw": sum(s.count for s in self.draw),
            "discard": sum(s.count for s in self.discard),
            "exhaust": sum(s.count for s in self.exhaust),
            "deck": sum(s.count for s in self.deck),
        }

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, stacks in (
            ("draw", self.draw),
            ("discard", self.discard),
            ("exhaust", self.exhaust),
            ("deck", self.deck),
        ):
            out[name] = {
                "total": sum(s.count for s in stacks),
                "stacks": [s.to_dict() for s in stacks],
            }
        return out


@dataclass(frozen=True)
class ScreenFair:
    """公平版的界面状态。"""

    options: tuple[str, ...] = ()
    min_select: int = 0
    max_select: int = 0
    select_cards: tuple[ZonedCard, ...] = ()
    reward_cards: tuple[RawCard, ...] = ()
    reward_relics: tuple[RawRelic, ...] = ()
    reward_details: tuple[RewardDetail, ...] = ()
    shop_items: tuple[RawShopItem, ...] = ()
    rest_options: tuple[str, ...] = ()
    neow_options: tuple[str, ...] = ()
    origin: str = ""
    event_name: str = ""
    event_text: str = ""


@dataclass(frozen=True)
class FairObservation:
    """唯一允许进入 `serialize.state` 的观测。"""

    game_version: str
    screen: str
    in_combat: bool
    act: int
    floor: int
    node: int
    room_type: str
    ascension: int
    player: RawPlayer
    hand: tuple[RawCard, ...]
    monsters: tuple[Monster, ...]
    zones: Zones
    combat_turn: int
    cards_discarded_this_turn: int
    times_damaged: int
    screen_state: ScreenFair
    map: RawMap | None = None

    # ---- 决策点识别与候选枚举用到的便捷访问 ----

    def alive_monsters(self) -> tuple[Monster, ...]:
        return tuple(m for m in self.monsters if m.hp > 0)

    def potions(self) -> tuple[tuple[int, RawPotion], ...]:
        """(槽位, 药水) 列表，**跳过空槽**。

        空槽不是药水。早先只有 `p is not None`，于是"空槽"被当成一种可用的药水，
        候选里出现 `potion:p0`、地图描述里写着"你带着 3 瓶药水"，而玩家其实一瓶
        都没有。详见 docs/04-fairness.md#药水。
        """
        return tuple(
            (i, p)
            for i, p in enumerate(self.player.potions)
            if p is not None and not p.is_empty
        )

    def playable_hand(self) -> tuple[RawCard, ...]:
        return tuple(c for c in self.hand if c.is_playable)

    def zone_cards(self, zone: str) -> tuple[RawCard, ...]:
        if zone != ZONE_HAND:
            # 非手牌区域在公平视图里只有折叠信息，选牌界面单独带 select_cards
            return ()
        return self.hand


# 供外部引用，避免拼错字符串
__all__ = [
    "ATTACK_LIKE",
    "CARD_TYPE_ATTACK",
    "CARD_TYPE_CURSE",
    "CARD_TYPE_POWER",
    "CARD_TYPE_SKILL",
    "CARD_TYPE_STATUS",
    "CardStack",
    "EMPTY_POTION_ID",
    "FairObservation",
    "Intent",
    "Monster",
    "ROOM_BOSS",
    "ROOM_ELITE",
    "ROOM_EVENT",
    "ROOM_MONSTER",
    "ROOM_REST",
    "ROOM_SHOP",
    "ROOM_TREASURE",
    "ROOM_UNKNOWN",
    "RawCard",
    "RawCombat",
    "RawIntent",
    "RawMap",
    "RawMapNode",
    "RawMonster",
    "RawObservation",
    "RawPlayer",
    "RawPotion",
    "RawPower",
    "RawRelic",
    "RawShopItem",
    "RewardDetail",
    "SCREEN_BOSS_RELIC",
    "SCREEN_CARD_REWARD",
    "SCREEN_COMBAT_REWARD",
    "SCREEN_EVENT",
    "SCREEN_GAME_OVER",
    "SCREEN_GRID",
    "SCREEN_MAP",
    "SCREEN_NEOW",
    "SCREEN_NONE",
    "SCREEN_REST",
    "SCREEN_SHOP_ROOM",
    "SKILL_LIKE",
    "ScreenFair",
    "ScreenState",
    "TARGET_ALL_ENEMY",
    "TARGET_ENEMY",
    "TARGET_NONE",
    "TARGET_SELF",
    "ZONE_DECK",
    "ZONE_DISCARD",
    "ZONE_EXHAUST",
    "ZONE_HAND",
    "ZonedCard",
]
