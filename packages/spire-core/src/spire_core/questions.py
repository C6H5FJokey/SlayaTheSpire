"""题面构造。

模板从 `decision_points.toml` 读（stdlib tomllib，core 保持零依赖）。
构题结果是**确定性**的：同样的 (fair, decision_point, candidates) 必然得到
逐字节相同的 questions。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from . import serialize
from .candidates import Candidate, selection_purpose
from .decision import (
    CARD_REWARD,
    COMBAT_PLAY,
    EVENT_OPTION,
    GENERIC_CHOICE,
    MAP_NODE,
    NEOW_BONUS,
    RELIC_SELECT,
    REST_SITE,
    SELECT_CARD_ANY,
    SELECT_CARD_MUST_K,
    SELECT_TARGET,
    SHOP,
)
from .describe import monster_brief
from .model import FairObservation

TEMPLATE_PATH = Path(__file__).with_name("decision_points.toml")

DEFAULT_LEVELS = ["very bad", "bad", "neutral", "good", "very good"]

# 角色 id -> 人类读法。指令里写死角色是实际发生过的 bug（Watcher 的局面上写着
# "as the Ironclad"），所以一律用 `{character}` 占位、构题时再填。
CHARACTER_LABEL = {
    "IRONCLAD": "Ironclad",
    "THE_SILENT": "Silent",
    "SILENT": "Silent",
    "DEFECT": "Defect",
    "WATCHER": "Watcher",
}

DEFAULT_TEMPLATES: dict[str, dict[str, Any]] = {
    COMBAT_PLAY: {
        "question_id": "q_action",
        "type": "choice",
        "instructions": "Choose the single best action for this turn.",
        "prelude": [],
    },
    SELECT_TARGET: {
        "question_id": "q_target",
        "type": "choice",
        "instructions": "Choose which enemy to target.",
        "prelude": [],
    },
    SELECT_CARD_MUST_K: {
        "question_id": "q_pick",
        "type": "choice",
        "instructions": "Choose one card to select ({select_purpose}).{select_reason}",
        "prelude": [],
    },
    SELECT_CARD_ANY: {
        "question_id": "q_card",
        "type": "score",
        "instructions": "Rate how good it is to select this card ({select_purpose}).{select_reason}",
        "levels": DEFAULT_LEVELS,
        "prelude": [],
    },
    MAP_NODE: {
        "question_id": "q_map",
        "type": "choice",
        "instructions": "Choose which node to travel to next.",
        "prelude": [],
    },
    CARD_REWARD: {
        "question_id": "q_reward",
        "type": "choice",
        "instructions": "Choose a card reward, or skip.",
        "prelude": [],
    },
    RELIC_SELECT: {
        "question_id": "q_relic",
        "type": "choice",
        "instructions": "Choose which relic to take.",
        "prelude": [],
    },
    EVENT_OPTION: {
        "question_id": "q_event",
        "type": "choice",
        "instructions": "Choose how to resolve this event.",
        "prelude": [],
    },
    SHOP: {
        "question_id": "q_shop",
        "type": "choice",
        "instructions": "Decide what to buy, or leave the shop.",
        "prelude": [],
    },
    REST_SITE: {
        "question_id": "q_rest",
        "type": "choice",
        "instructions": "Choose what to do at the rest site.",
        "prelude": [],
    },
    NEOW_BONUS: {
        "question_id": "q_neow",
        "type": "choice",
        "instructions": "Choose your starting bonus from Neow.",
        "prelude": [],
    },
    GENERIC_CHOICE: {
        "question_id": "q_choice",
        "type": "choice",
        "instructions": "Choose one of the available options.",
        "prelude": [],
    },
}


def load_templates(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """读取模板；文件缺失或解析失败时回退到内置默认（保证 core 可用）。"""
    p = path or TEMPLATE_PATH
    templates = dict(DEFAULT_TEMPLATES)
    try:
        with open(p, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return templates
    for name, cfg in (data.get("decision_points") or {}).items():
        merged = dict(templates.get(name, {}))
        merged.update(cfg)
        templates[name] = merged
    return templates


def _criteria_for(
    fair: FairObservation, candidates: list[Candidate]
) -> dict[str, str]:
    return {c.cid: c.description for c in candidates}


def fill_placeholders(template: str, fair: FairObservation) -> str:
    """把指令模板里的占位换成当前局面的值。

    支持 `{character}` / `{ascension}` / `{origin}` / `{event_name}` /
    `{event_text}` / `{select_purpose}` / `{select_reason}`。

    `{select_reason}` 是**整句或空串**：界面上的提示语拿不到时就整个消失，
    不会留下 `The screen says: ""` 这种残句（选牌界面在离线测试里也会出现
    reason 为空的情况）。

    用最朴素的 replace 而不是 `str.format`：指令里可能出现别的大括号（例如
    JSON 片段），`format` 会直接把构题炸掉。
    """
    if not template:
        return template
    raw_character = str(fair.player.character or "")
    label = CHARACTER_LABEL.get(raw_character.upper(), raw_character)
    s = fair.screen_state
    # 先处理"可有可无"的组合占位：事件名拿不到时连括号一起吞掉，免得留下
    # "Choose how to resolve this event ()." 这种残句。
    if not s.event_name:
        template = template.replace(" ({event_name})", "")
    if not s.event_text:
        template = template.replace(" ({event_text})", "")
    reason_line = ""
    reason = s.reason.strip()
    if reason:
        # 游戏自己的提示语大多自带句号，别拼出 "...Pile.."。
        if not reason.endswith("."):
            reason += "."
        reason_line = f' The screen says: "{reason}"'
    return (
        template.replace("{character}", label)
        .replace("{ascension}", str(fair.ascension))
        .replace("{origin}", s.origin)
        .replace("{event_name}", s.event_name)
        .replace("{event_text}", s.event_text)
        .replace("{select_purpose}", selection_purpose(fair))
        .replace("{select_reason}", reason_line)
    )


def _selection_context(fair: FairObservation, context: dict) -> None:
    """把"已选"信息拼进 state（必选 k 张的连问需要）。"""
    picked = context.get("selection_picked") or []
    if not picked:
        return
    from .describe import card_brief

    lines = []
    for zc in fair.screen_state.select_cards:
        cid = f"card:{zc.zone}:{zc.card.index}"
        if cid in picked:
            lines.append(f"- ALREADY SELECTED: {card_brief(zc.card)} ({cid})")
    if lines:
        fair_state = context.setdefault("_extra_state", {})
        fair_state["already_selected"] = lines


def build(
    fair: FairObservation,
    decision_point: str,
    candidates: list[Candidate],
    *,
    context: dict | None = None,
    templates: dict[str, dict[str, Any]] | None = None,
    compact_deck: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """构造 (state, questions)。

    返回的 state 与 questions 是**送给 Laya 的最后形态**（预算压缩由
    `budget.enforce` 在其后做）。
    """
    context = dict(context or {})
    templates = templates or load_templates()
    tpl = templates.get(decision_point, DEFAULT_TEMPLATES[GENERIC_CHOICE])

    state = serialize.state(fair, compact_deck=compact_deck)

    # 选目标时，把候选敌人的摘要也放进 state，帮助模型选
    if decision_point == SELECT_TARGET:
        state["target_candidates"] = [
            monster_brief(m) for m in fair.alive_monsters()
        ]

    _selection_context(fair, context)
    extra = context.get("_extra_state")
    if extra:
        state.update(extra)

    qid = str(tpl.get("question_id", "q_action"))
    qtype = str(tpl.get("type", "choice"))
    instructions = fill_placeholders(str(tpl.get("instructions", "")), fair)

    if qtype == "score":
        levels = list(tpl.get("levels") or DEFAULT_LEVELS)
        questions: dict[str, Any] = {}
        for c in candidates:
            questions[f"{qid}_{_slug(c.cid)}"] = {
                "type": "score",
                "instructions": f"{instructions} Candidate: {c.description}",
                "criteria": levels,
            }
    else:
        criteria = _criteria_for(fair, candidates)
        questions = {
            qid: {
                "type": "choice",
                "instructions": instructions,
                "criteria": criteria,
            }
        }

    # 先导判断题（虚拟思考链，v1 默认为空）
    preludes = tpl.get("prelude") or []
    for i, pre in enumerate(preludes):
        pid = str(pre.get("id") or f"q_prelude_{i}")
        ptype = str(pre.get("type", "noul"))
        q: dict[str, Any] = {
            "type": ptype,
            "instructions": fill_placeholders(str(pre.get("instructions", "")), fair),
        }
        if ptype in ("choice", "score"):
            crit = pre.get("criteria")
            if isinstance(crit, dict):
                q["criteria"] = {str(k): str(v) for k, v in crit.items()}
            elif isinstance(crit, list):
                q["criteria"] = [str(x) for x in crit]
            else:
                q["criteria"] = {
                    f"m{m.index}": monster_brief(m) for m in fair.alive_monsters()
                }
                if ptype == "score":
                    q["criteria"] = list(DEFAULT_LEVELS)
        questions[pid] = q

    return state, questions


def _slug(cid: str) -> str:
    """把候选 id 变成合法的题目后缀：只保留字母数字。"""
    return "".join(ch if ch.isalnum() else "_" for ch in cid)


__all__ = ["DEFAULT_LEVELS", "DEFAULT_TEMPLATES", "build", "load_templates"]

def score_question_ids(
    candidates: list[Candidate],
    decision_point: str,
    templates: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    """返回 `{question_id: candidate_id}`，供 score 类题目的裁决使用。

    必须与 `build()` 生成题目时用的规则完全一致，否则裁决会找不到题。
    """
    templates = templates or load_templates()
    tpl = templates.get(decision_point, DEFAULT_TEMPLATES[GENERIC_CHOICE])
    qid = str(tpl.get("question_id", "q_card"))
    return {f"{qid}_{_slug(c.cid)}": c.cid for c in candidates}
