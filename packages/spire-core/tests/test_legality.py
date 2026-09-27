"""合法性判定 + "观测事实"的回归测试。

这里钉的全是真机上踩到的坑：

- 空药水槽的 `canUse()` 返回 true（游戏自己判空槽看的是 id），照抄之后模型会
  反复选"用药水"，被拒一次、局面不变，于是无限空转；
- 界面语言是中文时 `AbstractCard.name` / `rawDescription` 都是中文，
  实测写出过 `"Play 打击 (1E): 造成 6 点伤害"`；
- 奖励界面上的卡没跑过 `applyPowers()`，`damage` 是 -1，实测写出过
  "造成 -1 点伤害"；
- 指令里写死 "as the Ironclad"，而那一局其实是 Watcher（A5）。

见 docs/04-fairness.md、docs/05-state-schema.md、docs/06-decision-points.md。
"""

from __future__ import annotations

import pytest

from fixtures import (
    combat_observation,
    grid_observation,
    map_observation,
    reward_observation,
)

from spire_core import actions as A
from spire_core import fairness, legality, pipeline, serialize
from spire_core.candidates import enumerate_candidates
from spire_core.decision import (
    CARD_REWARD,
    COMBAT_PLAY,
    EVENT_OPTION,
    MAP_NODE,
    RELIC_SELECT,
    REST_SITE,
    SELECT_CARD_ANY,
    SELECT_CARD_MUST_K,
    SHOP,
)
from spire_core.model import RawObservation

# 空药水槽：id 就是 "Potion Slot"，而且 canUse() 返回 true。
EMPTY_SLOT = {"id": "Potion Slot", "can_use": True, "requires_target": False}
FIRE_POTION = {
    "id": "Fire Potion",
    "can_use": True,
    "requires_target": True,
    "name": "Fire Potion",
    "text": "Deal 20 damage.",
}


def fair(raw):
    return fairness.filter_(RawObservation.from_dict(raw))


def screen_observation(screen, screen_state, **overrides):
    """借 `reward_observation` 的骨架造一个非战斗界面观测。"""
    raw = reward_observation()
    raw["screen"] = screen
    raw["screen_state"] = screen_state
    raw.update(overrides)
    return raw


# --------------------------------------------------------------------------- #
# 空药水槽
# --------------------------------------------------------------------------- #


def test_empty_potion_slots_are_not_potions():
    f = fair(combat_observation(potions=[EMPTY_SLOT, EMPTY_SLOT, EMPTY_SLOT]))
    assert f.potions() == ()
    ids = [c.cid for c in enumerate_candidates(f, COMBAT_PLAY)]
    assert not [cid for cid in ids if cid.startswith("potion:")]


def test_empty_potion_slot_is_illegal_even_though_the_game_says_usable():
    f = fair(combat_observation(potions=[EMPTY_SLOT]))
    action = A.Action(A.KIND_USE_POTION, {"potion_index": 0})
    assert legality.check(f, "potion:p0", action) is not None


def test_potion_slot_count_survives_but_the_items_do_not():
    st = serialize.state(fair(combat_observation(potions=[EMPTY_SLOT, EMPTY_SLOT, None])))
    assert st["player"]["potions"] == []
    assert st["player"]["potion_slots"] == 3


# --------------------------------------------------------------------------- #
# 动作合法性
# --------------------------------------------------------------------------- #


def test_real_potion_with_target_is_legal():
    f = fair(combat_observation(potions=[FIRE_POTION]))
    action = A.Action(A.KIND_USE_POTION, {"potion_index": 0, "target": "m0"})
    assert legality.check(f, "potion:p0->m0", action) is None
    # 需要目标的药水不给目标
    assert legality.check(f, "potion:p0", A.Action(A.KIND_USE_POTION, {"potion_index": 0})) is not None
    # 槽位越界
    assert legality.check(f, "potion:p7", A.Action(A.KIND_USE_POTION, {"potion_index": 7})) is not None


def test_play_card_legality_edges():
    f = fair(combat_observation())
    play = A.KIND_PLAY_CARD
    assert legality.check(f, "play:h0->m0", A.Action(play, {"hand_index": 0, "target": "m0"})) is None
    assert legality.check(f, "play:h9", A.Action(play, {"hand_index": 9})) is not None
    # 需要单体目标的攻击牌不给目标
    assert legality.check(f, "play:h0", A.Action(play, {"hand_index": 0})) is not None
    # 打不出来的牌
    raw = combat_observation()
    raw["combat"]["hand"][0]["is_playable"] = False
    f2 = fair(raw)
    assert legality.check(f2, "play:h0->m0", A.Action(play, {"hand_index": 0, "target": "m0"})) is not None
    # 不需要目标的技能牌，给了目标
    assert legality.check(f, "play:h2->m0", A.Action(play, {"hand_index": 2, "target": "m0"})) is not None
    assert legality.check(f, "play:h2", A.Action(play, {"hand_index": 2})) is None
    # 打向不存在的敌人
    assert legality.check(f, "play:h0->m4", A.Action(play, {"hand_index": 0, "target": "m4"})) is not None


def test_end_turn_needs_combat():
    f = fair(combat_observation())
    assert legality.check(f, A.END_TURN, A.Action(A.KIND_END_TURN, {})) is None
    f2 = fair(map_observation())
    assert legality.check(f2, A.END_TURN, A.Action(A.KIND_END_TURN, {})) is not None


def test_map_node_must_be_reachable():
    f = fair(map_observation())
    reach = A.Action(A.KIND_SELECT_MAP_NODE, {"node": "n4_3"})
    other = A.Action(A.KIND_SELECT_MAP_NODE, {"node": "n2_4"})
    assert legality.check(f, "node:n4_3", reach, MAP_NODE) is None
    assert legality.check(f, "node:n2_4", other, MAP_NODE) is not None


def test_shop_bounds_and_the_minus_one_leave():
    raw = screen_observation(
        "SHOP_ROOM",
        {
            "shop_items": [
                {"index": 0, "kind": "card", "id": "Cleave", "price": 50, "affordable": True}
            ]
        },
    )
    f = fair(raw)
    buy = A.Action(A.KIND_SELECT_CHOICE, {"index": 0})
    assert legality.check(f, "buy:0", buy, SHOP) is None
    assert legality.check(f, "buy:5", A.Action(A.KIND_SELECT_CHOICE, {"index": 5}), SHOP) is not None
    leave = A.Action(A.KIND_SELECT_CHOICE, {"index": -1})
    assert legality.check(f, "leave", leave, SHOP) is None
    # -1 只有在商店里才是"离开"
    assert legality.check(f, "leave", leave, MAP_NODE) is not None


def test_card_select_must_be_on_the_screen():
    f = fair(grid_observation(min_select=2, max_select=2))
    assert legality.check(f, "card:hand:0", None, SELECT_CARD_MUST_K) is None
    assert legality.check(f, "card:hand:9", None, SELECT_CARD_MUST_K) is not None


def test_legal_only_reports_what_it_dropped():
    f = fair(combat_observation(potions=[EMPTY_SLOT]))
    kept, dropped = legality.legal_only(f, enumerate_candidates(f, COMBAT_PLAY), COMBAT_PLAY)
    # 枚举器本来就不会产出空槽候选，所以这里只验证"接口能报出被丢的东西"。
    assert dropped == []
    assert kept


# --------------------------------------------------------------------------- #
# 不变式：枚举器产出的候选**全部合法**（否则模型会选到游戏执行不了的动作）
# --------------------------------------------------------------------------- #


def _cases():
    combat_dark = combat_observation(energy=0, potions=None)
    for c in combat_dark["combat"]["hand"]:
        c["is_playable"] = False
    shop = screen_observation(
        "SHOP_ROOM",
        {
            "shop_items": [
                {"index": 0, "kind": "card", "id": "Cleave", "price": 50, "affordable": True,
                 "name": "Cleave", "text": "Deal 8 damage to ALL enemies."},
                {"index": 1, "kind": "relic", "id": "Anchor", "price": 150, "affordable": False,
                 "name": "Anchor", "text": "Start each combat with 10 Block."},
            ]
        },
    )
    rest = screen_observation("REST", {"rest_options": ["Rest (heal)", "Smith (upgrade a card)"]})
    event = screen_observation("EVENT", {"options": ["Take the gold.", "Leave."]})
    return {
        COMBAT_PLAY: [
            combat_observation(),
            combat_observation(potions=[EMPTY_SLOT, EMPTY_SLOT]),
            combat_dark,
            combat_observation(hand=[]),
        ],
        MAP_NODE: [map_observation()],
        CARD_REWARD: [reward_observation()],
        SELECT_CARD_MUST_K: [grid_observation(min_select=2, max_select=2)],
        SELECT_CARD_ANY: [grid_observation(min_select=0, max_select=2)],
        RELIC_SELECT: [screen_observation("BOSS_RELIC", {
            "reward_relics": [{"id": "Anchor", "counter": -1, "name": "Anchor",
                               "text": "Start each combat with 10 Block."}]})],
        REST_SITE: [rest],
        EVENT_OPTION: [event],
        SHOP: [shop],
    }


@pytest.mark.parametrize("decision_point", sorted(_cases()))
def test_enumerators_only_produce_legal_candidates(decision_point):
    for raw in _cases()[decision_point]:
        f = fair(raw)
        candidates = enumerate_candidates(f, decision_point)
        kept, dropped = legality.legal_only(f, candidates, decision_point)
        assert dropped == [], f"{decision_point}: 枚举器产出了非法候选 {dropped}"
        assert kept == candidates


# --------------------------------------------------------------------------- #
# 英文文本与新增字段
# --------------------------------------------------------------------------- #


def test_state_carries_names_descriptions_type_and_rarity():
    raw = combat_observation(potions=[FIRE_POTION, EMPTY_SLOT, None])
    raw["player"]["relics"] = [
        {"id": "BurningBlood", "counter": -1, "name": "Burning Blood",
         "text": "At the end of combat, heal 6 HP."}
    ]
    raw["player"]["powers"] = [
        {"id": "Strength", "amount": 2, "name": "Strength",
         "text": "Attacks deal 2 additional damage."}
    ]
    st = serialize.state(fair(raw))
    p = st["player"]
    assert [x["slot"] for x in p["potions"]] == [0]
    assert p["potions"][0]["name"] == "Fire Potion"
    assert p["potions"][0]["usable"] is True
    assert p["relics"][0]["name"] == "Burning Blood"
    assert p["relics"][0]["counter"] == -1
    assert p["powers"][0]["text"] == "Attacks deal 2 additional damage."

    hand = st["hand"][0]
    assert hand["type"] == "ATTACK"
    assert hand["rarity"] == "BASIC"


def test_monster_powers_carry_name_and_text():
    raw = combat_observation()
    raw["combat"]["monsters"][0]["powers"] = [
        {"id": "Vulnerable", "amount": 2, "name": "Vulnerable",
         "text": "Takes 50% more damage."}
    ]
    st = serialize.state(fair(raw))
    power = st["monsters"][0]["powers"][0]
    assert power["name"] == "Vulnerable"
    assert power["text"] == "Takes 50% more damage."


def test_shop_candidates_include_the_name_and_the_effect():
    raw = screen_observation(
        "SHOP_ROOM",
        {
            "shop_items": [
                {"index": 0, "kind": "card", "id": "Cleave", "price": 50, "affordable": True,
                 "name": "Cleave", "text": "Deal 8 damage to ALL enemies."},
                {"index": 1, "kind": "relic", "id": "Anchor", "price": 150, "affordable": False,
                 "name": "Anchor", "text": "Start each combat with 10 Block."},
            ]
        },
    )
    plan = pipeline.make_plan(fair(raw))
    criteria = plan.questions["q_shop"]["criteria"]
    assert "Cleave" in criteria["buy:0"]
    assert "8 damage" in criteria["buy:0"]
    # 买不起的不进候选 —— 不合法的动作根本不给模型
    assert "buy:1" not in criteria
    assert criteria["leave"] == "Leave the shop."
    # state 里也要带上名字与效果
    assert plan.state["screen"]["shop"][0]["name"] == "Cleave"


def test_relic_candidate_includes_name_and_effect():
    raw = screen_observation("BOSS_RELIC", {
        "reward_relics": [{"id": "Anchor", "counter": -1, "name": "Anchor",
                           "text": "Start each combat with 10 Block."}]})
    plan = pipeline.make_plan(fair(raw))
    criteria = plan.questions["q_relic"]["criteria"]
    assert "Anchor" in criteria["relic:0"]
    assert "10 Block" in criteria["relic:0"]


def test_instructions_use_the_actual_character_and_ascension():
    raw = combat_observation()
    raw["player"]["character"] = "WATCHER"
    raw["ascension"] = 5
    plan = pipeline.make_plan(fair(raw))
    ins = plan.questions["q_action"]["instructions"]
    assert "Watcher" in ins
    assert "Ascension 5" in ins
    assert "Ironclad" not in ins


def test_map_candidate_potion_count_ignores_empty_slots():
    raw = map_observation()
    raw["player"]["potions"] = [EMPTY_SLOT, EMPTY_SLOT, EMPTY_SLOT]
    plan = pipeline.make_plan(fair(raw))
    desc = plan.questions["q_map"]["criteria"]["node:n4_3"]
    assert "0 potions" in desc
