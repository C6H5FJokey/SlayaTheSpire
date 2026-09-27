"""测试夹具：构造 raw 观测。

刻意手写而不是从游戏抓取 —— 这样测试能在没有游戏的机器上跑，
并且每个边界（0 能量、单敌人、已死敌人…）都能精确构造。
"""

from __future__ import annotations

from typing import Any


def card(
    index: int = 0,
    cid: str = "Strike_R",
    name: str = "Strike",
    ctype: str = "ATTACK",
    cost: int = 1,
    cost_for_turn: int | None = None,
    upgrades: int = 0,
    playable: bool = True,
    target: str = "ENEMY",
    text: str = "Deal 6 damage.",
    damage: int = 6,
    block: int = 0,
    uuid: str | None = None,
) -> dict[str, Any]:
    return {
        "index": index,
        "id": cid,
        "name": name,
        "type": ctype,
        "cost": cost,
        "cost_for_turn": cost if cost_for_turn is None else cost_for_turn,
        "upgrades": upgrades,
        "rarity": "BASIC",
        "exhausts": False,
        "ethereal": False,
        "is_playable": playable,
        "target_type": target,
        "uuid": uuid or f"uuid-{cid}-{index}-{upgrades}",
        "text": text,
        "damage": damage,
        "block": block,
        "magic_number": 0,
    }


def monster(
    index: int = 0,
    mid: str = "JawWorm",
    name: str = "Jaw Worm",
    hp: int = 42,
    max_hp: int = 46,
    block: int = 0,
    intent_damage: int = 12,
    base_damage: int = 12,
    intent_text: str = "Chomp for 12 damage.",
    is_gone: bool = False,
) -> dict[str, Any]:
    return {
        "index": index,
        "id": mid,
        "name": name,
        "hp": hp,
        "max_hp": max_hp,
        "block": block,
        "half_dead": False,
        "is_gone": is_gone,
        "powers": [],
        "intent": {
            "id": "ATTACK",
            "hits": 1,
            "base_damage": base_damage,
            "adjusted_damage": intent_damage,
            "text": intent_text,
        },
        "move_history": [1, 4, 1],
        "upcoming_moves": [4, 1, 4],
    }


def player(
    character: str = "IRONCLAD",
    hp: int = 68,
    max_hp: int = 75,
    block: int = 0,
    energy: int = 3,
    gold: int = 142,
    potions: list[dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    return {
        "character": character,
        "hp": hp,
        "max_hp": max_hp,
        "block": block,
        "energy": energy,
        "gold": gold,
        "powers": [{"id": "Strength", "amount": 2}],
        "relics": [{"id": "BurningBlood", "counter": -1}],
        "potions": potions
        if potions is not None
        else [{"id": "FirePotion", "can_use": True, "requires_target": True}, None],
    }


def combat_observation(
    *,
    hand: list[dict[str, Any]] | None = None,
    monsters: list[dict[str, Any]] | None = None,
    energy: int = 3,
    turn: int = 3,
    draw_pile: list[dict[str, Any]] | None = None,
    deck: list[dict[str, Any]] | None = None,
    potions: list[dict[str, Any] | None] | None = None,
    hp: int = 68,
) -> dict[str, Any]:
    hand = hand if hand is not None else [
        card(index=0, cid="Strike_R", damage=6),
        card(index=1, cid="Bash", name="Bash", cost=2, text="Deal 8 damage. Apply 2 Vulnerable.", damage=8),
        card(index=2, cid="Defend_R", name="Defend", ctype="SKILL", target="NONE",
             text="Gain 5 Block.", damage=0, block=5),
    ]
    monsters = monsters if monsters is not None else [monster(index=0)]
    # 抽牌堆刻意给出顺序：公平视图必须把它变成多重集
    draw_pile = draw_pile if draw_pile is not None else [
        card(index=0, cid="Strike_R", uuid="u1"),
        card(index=1, cid="Defend_R", name="Defend", ctype="SKILL", target="NONE",
             text="Gain 5 Block.", damage=0, block=5, uuid="u2"),
        card(index=2, cid="Strike_R", uuid="u3"),
        card(index=3, cid="AscendersBane", name="Ascender's Bane", ctype="CURSE",
             cost=-2, playable=False, target="NONE", text="Unplayable.", damage=0, uuid="u4"),
    ]
    deck = deck if deck is not None else [
        card(index=i, uuid=f"d{i}") for i in range(10)
    ]
    return {
        "game_version": "2.3.4",
        "screen": "NONE",
        "screen_state": {},
        "in_combat": True,
        "room": {"act": 1, "floor": 7, "node": 5, "type": "MONSTER"},
        "ascension": 0,
        "in_combat_flag": True,
        "run_seed": -3047511808784702860,
        "player": player(energy=energy, potions=potions, hp=hp),
        "combat": {
            "turn": turn,
            "hand": hand,
            "draw_pile": draw_pile,
            "discard_pile": [],
            "exhaust_pile": [],
            "monsters": monsters,
            "cards_discarded_this_turn": 0,
            "times_damaged": 0,
        },
        "deck": deck,
    }


def map_observation(*, reachable: list[str] | None = None) -> dict[str, Any]:
    nodes = [
        {"id": "n3_3", "x": 3, "y": 3, "type": "MONSTER", "children": ["n2_4", "n3_4"]},
        {"id": "n4_3", "x": 4, "y": 3, "type": "MONSTER", "children": ["n4_4", "n5_4"]},
        {"id": "n2_4", "x": 2, "y": 4, "type": "EVENT", "children": ["n2_5"]},
        {"id": "n3_4", "x": 3, "y": 4, "type": "ELITE", "children": ["n3_5"]},
        {"id": "n4_4", "x": 4, "y": 4, "type": "REST", "children": ["n4_5"]},
        {"id": "n5_4", "x": 5, "y": 4, "type": "SHOP", "children": ["n5_5"]},
        {"id": "n2_5", "x": 2, "y": 5, "type": "MONSTER", "children": []},
        {"id": "n3_5", "x": 3, "y": 5, "type": "MONSTER", "children": []},
        {"id": "n4_5", "x": 4, "y": 5, "type": "MONSTER", "children": []},
        {"id": "n5_5", "x": 5, "y": 5, "type": "TREASURE", "children": []},
        {"id": "n4_0", "x": 4, "y": 0, "type": "BOSS", "children": []},
    ]
    return {
        "game_version": "2.3.4",
        "screen": "MAP",
        "screen_state": {},
        "in_combat": False,
        "room": {"act": 1, "floor": 7, "node": 5, "type": "MONSTER"},
        "ascension": 0,
        "run_seed": 12345,
        "player": player(),
        "deck": [card(index=i, uuid=f"d{i}") for i in range(11)],
        "map": {
            "act": 1,
            "nodes": nodes,
            "current": "n3_3",
            "reachable": reachable if reachable is not None else ["n4_3"],
            "boss": "n4_0",
            "boss_relic_taken": False,
        },
    }


def grid_observation(*, min_select: int, max_select: int) -> dict[str, Any]:
    cards = [
        {"zone": "hand", "index": 0, "id": "Strike_R", "name": "Strike", "type": "ATTACK",
         "cost": 1, "cost_for_turn": 1, "upgrades": 0, "rarity": "BASIC",
         "exhausts": False, "ethereal": False, "is_playable": True, "target_type": "ENEMY",
         "uuid": "g0", "text": "Deal 6 damage.", "damage": 6, "block": 0, "magic_number": 0},
        {"zone": "hand", "index": 1, "id": "Defend_R", "name": "Defend", "type": "SKILL",
         "cost": 1, "cost_for_turn": 1, "upgrades": 0, "rarity": "BASIC",
         "exhausts": False, "ethereal": False, "is_playable": True, "target_type": "NONE",
         "uuid": "g1", "text": "Gain 5 Block.", "damage": 0, "block": 5, "magic_number": 0},
        {"zone": "hand", "index": 2, "id": "Bash", "name": "Bash", "type": "ATTACK",
         "cost": 2, "cost_for_turn": 2, "upgrades": 0, "rarity": "BASIC",
         "exhausts": False, "ethereal": False, "is_playable": True, "target_type": "ENEMY",
         "uuid": "g2", "text": "Deal 8 damage. Apply 2 Vulnerable.", "damage": 8,
         "block": 0, "magic_number": 2},
    ]
    return {
        "game_version": "2.3.4",
        "screen": "GRID",
        "screen_state": {
            "min_select": min_select,
            "max_select": max_select,
            "select_cards": cards,
            "options": ["Confirm"] if max_select == 0 else [],
        },
        "in_combat": False,
        "room": {"act": 1, "floor": 7, "node": 5, "type": "EVENT"},
        "ascension": 0,
        "run_seed": 99,
        "player": player(),
        "deck": [card(index=i, uuid=f"d{i}") for i in range(6)],
    }


def reward_observation() -> dict[str, Any]:
    return {
        "game_version": "2.3.4",
        "screen": "CARD_REWARD",
        "screen_state": {
            "reward_cards": [
                card(index=0, cid="Anger", name="Anger", text="Deal 6 damage.", damage=6),
                card(index=1, cid="Cleave", name="Cleave", cost=1, text="Deal 8 damage to ALL enemies.", damage=8),
                card(index=2, cid="PommelStrike", name="Pommel Strike", cost=1,
                     text="Deal 9 damage. Draw 1 card.", damage=9),
            ],
        },
        "in_combat": False,
        "room": {"act": 1, "floor": 7, "node": 5, "type": "MONSTER"},
        "ascension": 0,
        "run_seed": 99,
        "player": player(),
        "deck": [card(index=i, uuid=f"d{i}") for i in range(6)],
    }