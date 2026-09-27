"""SL 回滚状态机。见 docs/08-dataset.md#sl-语义 与 docs/11-testing.md#sl-回滚。"""

from __future__ import annotations

from spire_core.sl import (
    EVENT_NEW_ROOM,
    EVENT_REWIND,
    EVENT_SAME_ROOM,
    RoomToken,
    RoomTracker,
    Snapshot,
)


def snap(turn=1, hp=70, block=0, monsters=(40,), in_combat=True):
    return Snapshot(
        turn=turn,
        player_hp=hp,
        player_block=block,
        monster_hp=tuple(monsters),
        in_combat=in_combat,
    )


ROOM_A = RoomToken(act=1, floor=7, node=5)
ROOM_B = RoomToken(act=1, floor=8, node=6)
# 局外观测：主菜单 / 选角色 / 结算画面都是 `(0,0,0)`
NON_ROOM = RoomToken(act=0, floor=0, node=0)


def test_only_in_run_tokens_count_as_rooms():
    assert ROOM_A.is_room is True
    assert RoomToken(act=1, floor=0, node=3).is_room is False
    assert NON_ROOM.is_room is False


def test_out_of_run_screens_are_never_revisited():
    """真机回归：`(0,0,0)` 在一局里反复出现，不能当成"重访节点"（= 读档）。

    旧实现把它记进 `_seen`，于是"回主菜单再开一局"就丢掉整房数据。真机上一局里
    这样误报了 9 次。
    """
    t = RoomTracker()
    assert t.observe(NON_ROOM, snap(in_combat=False, monsters=())).kind == EVENT_NEW_ROOM
    t.observe(ROOM_A, snap())

    ev = t.observe(NON_ROOM, snap(in_combat=False, monsters=()))
    assert ev.kind == EVENT_NEW_ROOM
    assert t.post_sl is False
    assert t.combat_instance == 1

    # 再回一次主菜单也只是同一个局外画面，永远不是读档
    for _ in range(3):
        assert t.observe(NON_ROOM, snap(in_combat=False, monsters=())).kind == EVENT_SAME_ROOM
    assert t.post_sl is False


def test_first_room_is_new_room():
    t = RoomTracker()
    ev = t.observe(ROOM_A, snap())
    assert ev.kind == EVENT_NEW_ROOM
    assert ev.combat_instance == 1
    assert ev.post_sl is False
    assert ev.room_key == "a1_f7_n5_c1"


def test_same_room_progress_is_same_room():
    t = RoomTracker()
    t.observe(ROOM_A, snap(turn=1, hp=70))
    ev = t.observe(ROOM_A, snap(turn=2, hp=61))
    assert ev.kind == EVENT_SAME_ROOM
    assert ev.combat_instance == 1
    assert ev.post_sl is False


def test_next_room_resets_instance_and_post_sl():
    t = RoomTracker()
    t.observe(ROOM_A, snap())
    ev = t.observe(ROOM_B, snap(turn=1, hp=52))
    assert ev.kind == EVENT_NEW_ROOM
    assert ev.combat_instance == 1
    assert ev.post_sl is False
    assert t.post_sl is False


def test_rewind_when_state_regresses_in_same_room():
    t = RoomTracker()
    t.observe(ROOM_A, snap(turn=1, hp=70, monsters=(40,)))
    t.observe(ROOM_A, snap(turn=3, hp=55, monsters=(20,)))
    ev = t.observe(ROOM_A, snap(turn=1, hp=70, monsters=(40,)))
    assert ev.kind == EVENT_REWIND
    assert ev.combat_instance == 2
    assert ev.post_sl is True
    assert "turn 1 < 3" in ev.detail


def test_in_combat_healing_is_not_a_rewind():
    """战斗内治疗会让 HP 合法上升（血瓶 / Regen / 战后回血遗物），不能当读档。"""
    t = RoomTracker()
    t.observe(ROOM_A, snap(turn=1, hp=70))
    t.observe(ROOM_A, snap(turn=2, hp=50))
    ev = t.observe(ROOM_A, snap(turn=2, hp=70))
    assert ev.kind == EVENT_SAME_ROOM
    assert ev.post_sl is False


def test_summoned_monsters_are_not_a_rewind():
    """召唤类敌人会让怪物血量上升，也不能当读档。"""
    t = RoomTracker()
    t.observe(ROOM_A, snap(turn=2, monsters=(20, 30)))
    ev = t.observe(ROOM_A, snap(turn=2, monsters=(20, 45, 12)))
    assert ev.kind == EVENT_SAME_ROOM


def test_combat_end_turn_reset_is_not_a_rewind():
    """真机回归：战斗结束那一帧游戏把 `combat.turn` 重置回 1，怪物全 0 血。

    这次误判曾经把整局 41 行数据当 SL 丢掉（见 docs/08-dataset.md#sl-语义）。
    """
    t = RoomTracker()
    t.observe(ROOM_A, snap(turn=1, hp=72, monsters=(43,)))
    t.observe(ROOM_A, snap(turn=2, hp=72, monsters=(10,)))
    t.observe(ROOM_A, snap(turn=3, hp=72, monsters=(10,)))
    ev = t.observe(ROOM_A, snap(turn=1, hp=72, monsters=(0,)))
    assert ev.kind == EVENT_SAME_ROOM
    assert ev.combat_instance == 1
    assert ev.post_sl is False
    assert t.events[-1].kind == EVENT_SAME_ROOM


def test_turn_reset_outside_combat_is_not_a_rewind():
    """奖励界面里 `in_combat=False`、turn 仍是 1，也不能当读档。"""
    t = RoomTracker()
    t.observe(ROOM_A, snap(turn=3, hp=70, monsters=(0,)))
    ev = t.observe(ROOM_A, snap(turn=1, hp=70, monsters=(0,), in_combat=False))
    assert ev.kind == EVENT_SAME_ROOM


def test_revisiting_an_older_node_is_rewind():
    t = RoomTracker()
    t.observe(ROOM_A, snap())
    t.observe(ROOM_B, snap(turn=1, hp=90, monsters=(10,)))
    ev = t.observe(ROOM_A, snap(turn=1, hp=70, monsters=(40,)))
    assert ev.kind == EVENT_REWIND
    assert "revisited node" in ev.detail
    assert t.current == ROOM_A


def test_repeated_rewinds_increment_combat_instance():
    t = RoomTracker()
    t.observe(ROOM_A, snap(turn=1, hp=70))
    t.observe(ROOM_A, snap(turn=4, hp=40))
    assert t.observe(ROOM_A, snap(turn=1, hp=70)).combat_instance == 2
    t.observe(ROOM_A, snap(turn=5, hp=30))
    ev = t.observe(ROOM_A, snap(turn=1, hp=70))
    assert ev.combat_instance == 3
    assert ev.room_key == "a1_f7_n5_c3"


def test_room_key_scope_is_per_combat_instance():
    t = RoomTracker()
    t.observe(ROOM_A, snap(turn=1, hp=70))
    first_key = t.make_key(ROOM_A)
    t.observe(ROOM_A, snap(turn=3, hp=40))
    t.observe(ROOM_A, snap(turn=1, hp=70))
    assert t.make_key(ROOM_A) != first_key


def test_snapshot_from_observation_dict():
    s = Snapshot.from_observation(
        {"player": {"hp": 61, "block": 5},
         "in_combat": True,
         "combat": {"turn": 2, "monsters": [{"hp": 33}, {"hp": 12}]}}
    )
    assert (s.turn, s.player_hp, s.player_block, s.monster_hp) == (2, 61, 5, (33, 12))
    assert s.in_combat is True
    assert Snapshot.from_observation({}).in_combat is False


def test_events_are_accumulated_in_order():
    t = RoomTracker()
    t.observe(ROOM_A, snap())
    t.observe(ROOM_A, snap(turn=3, hp=40))
    t.observe(ROOM_A, snap())
    t.observe(ROOM_B, snap())
    assert [e.kind for e in t.events] == [
        EVENT_NEW_ROOM,
        EVENT_SAME_ROOM,
        EVENT_REWIND,
        EVENT_NEW_ROOM,
    ]
