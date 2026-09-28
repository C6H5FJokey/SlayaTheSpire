"""选牌界面的上下文（`origin` / 事件正文）与奖励明细。

这两组字段是本轮修的"选牌/领奖没语义"问题的数据契约，见
docs/06-decision-points.md 与 docs/05-state-schema.md#screen_state。
"""

from __future__ import annotations

from fixtures import card, combat_observation, grid_observation

from spire_core import candidates as C
from spire_core import fairness, questions, serialize
from spire_core.decision import CARD_REWARD, SELECT_CARD_ANY, SELECT_CARD_MUST_K
from spire_core.model import RawObservation


def fair(raw):
    return fairness.filter_(RawObservation.from_dict(raw))


def grid(*, min_select: int, max_select: int, **screen):
    raw = grid_observation(min_select=min_select, max_select=max_select)
    raw["screen_state"].update(screen)
    return fair(raw)


# --------------------------------------------------------------- origin / 事件上下文


def test_origin_and_event_context_survive_the_fair_filter():
    """公平过滤是白名单式构造，新字段必须显式透传，否则会静默消失。"""
    f = grid(
        min_select=1,
        max_select=1,
        origin="event",
        event_name="Upgrade Shrine",
        event_text="You come across an old shrine.",
    )
    assert f.screen_state.origin == "event"
    assert f.screen_state.event_name == "Upgrade Shrine"
    state = serialize.state(f)
    assert state["screen"]["origin"] == "event"
    assert state["screen"]["event_name"] == "Upgrade Shrine"
    assert state["screen"]["event_text"] == "You come across an old shrine."


def test_missing_origin_is_omitted_not_defaulted():
    """旧版模组不报 `origin`：state 里就不该凭空多一个字段。"""
    state = serialize.state(grid(min_select=1, max_select=1))
    assert "origin" not in state["screen"]
    assert "event_name" not in state["screen"]


def test_selection_candidates_carry_the_purpose():
    f = grid(min_select=0, max_select=3, origin="purge")
    cands = C.enumerate_candidates(f, SELECT_CARD_ANY)
    assert cands
    assert all("remove a card from your deck" in c.description for c in cands)


def test_selection_candidates_name_the_event():
    f = grid(min_select=0, max_select=3, origin="event", event_name="Upgrade Shrine")
    cands = C.enumerate_candidates(f, SELECT_CARD_ANY)
    assert all("event Upgrade Shrine" in c.description for c in cands)


def test_select_card_instruction_carries_the_purpose():
    f = grid(min_select=1, max_select=1, origin="rest_smith")
    cands = C.enumerate_candidates(f, SELECT_CARD_MUST_K)
    _state, qs = questions.build(f, SELECT_CARD_MUST_K, cands)
    assert "rest site smith" in qs["q_pick"]["instructions"]


def test_event_placeholder_filled_with_event_name():
    f = grid(min_select=0, max_select=3, event_name="Big Fish")
    out = questions.fill_placeholders("Choose how to resolve this event ({event_name}).", f)
    assert out == "Choose how to resolve this event (Big Fish)."


def test_event_placeholder_collapses_when_there_is_no_event():
    """拿不到事件名时连括号一起吞掉，不留 "event ()." 这种残句。"""
    f = grid(min_select=0, max_select=3)
    out = questions.fill_placeholders("Choose how to resolve this event ({event_name}).", f)
    assert out == "Choose how to resolve this event."


# --------------------------------------------------------------------- 奖励明细


def test_reward_details_reach_state():
    raw = combat_observation()
    raw["screen"] = "COMBAT_REWARD"
    raw["in_combat"] = False
    raw["combat"] = None
    raw["screen_state"] = {
        "options": ["Card reward (Anger / Cleave)", "Gold (25)", "Proceed"],
        "option_ids": ["card", "gold", "proceed"],
        "reward_details": [
            {
                "kind": "card",
                "text": "Card reward (Anger / Cleave)",
                "cards": [
                    card(index=0, cid="Anger", name="Anger", text="Deal 6 damage.", damage=6),
                    card(index=1, cid="Cleave", name="Cleave", cost=1,
                         text="Deal 8 damage to ALL enemies.", damage=8),
                ],
            },
            {"kind": "gold", "text": "Gold (25)", "amount": 25},
        ],
    }
    state = serialize.state(fair(raw))
    details = state["screen"]["reward_details"]
    assert [d["kind"] for d in details] == ["card", "gold"]
    # 卡牌奖励必须带牌名 —— 这就是"要不要再点进去"的全部依据。
    assert [c["card"] for c in details[0]["cards"]] == ["Anger", "Cleave"]
    assert details[1]["amount"] == 25


def test_reward_details_absent_for_plain_screen():
    state = serialize.state(grid(min_select=1, max_select=1))
    assert "reward_details" not in state["screen"]


def test_card_reward_skip_wording_is_explicit():
    raw = combat_observation()
    raw["screen"] = "CARD_REWARD"
    raw["in_combat"] = False
    raw["combat"] = None
    raw["screen_state"] = {
        "reward_cards": [card(index=0, cid="Anger", name="Anger", text="Deal 6 damage.", damage=6)],
    }
    cands = C.enumerate_candidates(fair(raw), CARD_REWARD)
    skip = [c for c in cands if c.cid == "skip"]
    assert len(skip) == 1
    assert "none of these three cards" in skip[0].description

# ---------------------------------------------------- 检索类选牌（头槌 / 预见）

# 头槌（DiscardPileToTopOfDeckAction）与观者的预见（ScryAction）都会**开着选牌
# 界面停在动作队列里**，模组因此曾经一个观测都不发（见 docs/03 的稳定性判定）。
# 这两条路的数据契约在 core 侧就是"zone + reason + 预见的 draw_order"。


def test_reason_reaches_state_and_is_omitted_when_absent():
    f = grid(min_select=1, max_select=1, origin="combat_select",
             reason="Choose a Card to Put on Top of Your Draw Pile.")
    assert f.screen_state.reason == "Choose a Card to Put on Top of Your Draw Pile."
    state = serialize.state(f)
    assert state["screen"]["reason"] == "Choose a Card to Put on Top of Your Draw Pile."
    assert "reason" not in serialize.state(grid(min_select=1, max_select=1))["screen"]


def test_combat_retrieval_purpose_is_named():
    f = grid(min_select=1, max_select=1, origin="combat_select")
    cands = C.enumerate_candidates(f, SELECT_CARD_MUST_K)
    assert all("in-combat card retrieval" in c.description for c in cands)


def test_scry_cards_carry_draw_order_and_keep_their_id():
    """预见：`draw_order=1` 是下一张会抽到的牌；候选 id 仍然是 `<zone>:<index>`。"""
    raw = grid_observation(min_select=0, max_select=3)
    for i, entry in enumerate(raw["screen_state"]["select_cards"]):
        entry["zone"] = "draw"
        entry["draw_order"] = i + 1
    raw["screen_state"]["origin"] = "scry"
    raw["screen_state"]["reason"] = "Choose any number of cards to discard."
    f = fair(raw)

    assert [zc.draw_order for zc in f.screen_state.select_cards] == [1, 2, 3]
    state = serialize.state(f)
    selectable = state["screen"]["selectable"]
    assert [e["draw_order"] for e in selectable] == [1, 2, 3]
    assert [e["id"] for e in selectable] == ["draw:0", "draw:1", "draw:2"]
    assert [c.cid for c in C.enumerate_candidates(f, SELECT_CARD_ANY)] == [
        "card:draw:0",
        "card:draw:1",
        "card:draw:2",
    ]
    assert all("scry" in c.description for c in C.enumerate_candidates(f, SELECT_CARD_ANY))


def test_draw_order_absent_for_plain_selection():
    """非预见的选牌界面不能凭空多出 draw_order（那是顺序信息）。"""
    state = serialize.state(grid(min_select=1, max_select=1))
    assert all("draw_order" not in e for e in state["screen"]["selectable"])


def test_select_reason_placeholder_is_a_whole_sentence_or_nothing():
    with_reason = grid(min_select=1, max_select=1, reason="Choose a Card to Upgrade.")
    out = questions.fill_placeholders("Pick one ({select_purpose}).{select_reason}", with_reason)
    assert out == 'Pick one (card selection). The screen says: "Choose a Card to Upgrade."'
    without = questions.fill_placeholders(
        "Pick one ({select_purpose}).{select_reason}", grid(min_select=1, max_select=1)
    )
    assert without == "Pick one (card selection)."
