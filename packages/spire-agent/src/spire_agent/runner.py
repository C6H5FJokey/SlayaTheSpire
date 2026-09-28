"""agent 主循环（见 docs/02-architecture.md#一次决策的完整时序）。

一次观测的完整链路，全部在这个文件里可见：

    观测 -> 公平过滤 -> 房间/SL 判定 -> 识别决策点 -> 枚举候选 -> 构题
         -> (observe_human: 等人类动作 | agent: 问 Laya) -> 裁决 -> 动作校验
         -> 发给 mod -> 收到 ok -> 落 pending -> 离开房间时提交

三条纪律：
- **不猜**：识别不出决策点就什么都不做（让看门狗兜底），绝不静默产生垃圾数据；
- **先确认再记录**：训练行在 mod 回 `ok` 之后才写 pending；SL 来了整间房丢弃；
- **同分布**：observe_human 造的 `(state, questions)` 与 agent 模式逐字节相同 ——
  连"必选 k 张"连问 k 次的现场也逐次记录下来，所以人类标签可以直接喂给模型。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from spire_core import actions as A
from spire_core import fairness, pipeline, serialize
from spire_core.arbitrate import selected_answer
from spire_core.config import MODE_OBSERVE_HUMAN, AgentConfig
from spire_core.dataset import (
    LABEL_AGENT,
    LABEL_HUMAN,
    SOURCE_AGENT,
    SOURCE_HUMAN,
    make_row,
    split_of,
)
from spire_core.decision import SELECT_CARD_ANY, SELECT_CARD_MUST_K
from spire_core.errors import UnknownDecisionPoint
from spire_core.model import FairObservation, RawObservation
from spire_core.pipeline import Plan, RunOver
from spire_core.sl import (
    EVENT_NEW_ROOM,
    EVENT_REWIND,
    RoomEvent,
    RoomToken,
    RoomTracker,
    Snapshot,
)
from spire_core.types import Decision, LayaResult

from .bridge import (
    FRAME_ACTION_RESULT,
    FRAME_HUMAN_ACTION,
    FRAME_OBSERVATION,
    IncompatibleMod,
    Message,
    ModBridge,
)
from .laya_client import LayaClient, canonical_hash
from .recorder import RunRecorder

log = logging.getLogger(__name__)

# 同一个 state 上的动作被模组连着拒绝这么多次、而 state 一直没变，就不再重发
# （真机那次是空药水槽：候选里有游戏执行不了的东西，见 `note_rejection`）。
REJECTION_LIMIT = 3

# 这些决策点即使只剩一个候选也不算"没得选"：`select_card_any` 的"一张都不选"
# 本身就是一个选项（见 `forced_choice`）。
FORCED_EXEMPT = (SELECT_CARD_ANY,)

PanelSink = Callable[[dict[str, Any]], None]
LABEL_FALLBACK = "fallback"
CARD_SELECT_POINTS = (SELECT_CARD_MUST_K, SELECT_CARD_ANY)


class ModelUnavailable(RuntimeError):
    """拿不到可用的模型决策（连不上 / 解析失败 / 答案为空 / 子选择没凑够）。

    默认策略是**停跑并报错**（`laya.on_error = "stop"`）—— 不替模型猜，也不静默产出一局
    没有模型参与的废数据。只有显式配置 `on_error = "fallback"` 才退回规则策略。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Inflight:
    """已发出、等 `action_result` 的动作。"""

    message_id: int
    seq: int
    room_key: str
    room: dict[str, Any]
    rows: list[dict[str, Any]] = field(default_factory=list)
    tried: list[str] = field(default_factory=list)
    retried: bool = False
    state_key: str = ""


@dataclass
class Armed:
    """"这一屏轮到谁"的构题现场。observe_human 靠它给人类动作配对。"""

    seq: int
    fair: FairObservation
    plan: Plan
    room: dict[str, Any]
    result: LayaResult | None = None


class AgentRunner:
    def __init__(
        self,
        config: AgentConfig,
        *,
        bridge: ModBridge | None = None,
        laya: LayaClient | None = None,
        recorder: RunRecorder | None = None,
        panel: PanelSink | None = None,
        templates: dict[str, dict[str, Any]] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.config = config
        self.bridge = bridge or ModBridge(
            config.mod, mode=config.mode, watchdog_sec=config.watchdog_sec
        )
        self.laya = laya
        self.recorder = recorder or RunRecorder(
            config.record.runs_dir, run_id, save_raw_states=config.record.save_raw_states
        )
        self.panel = panel
        self.templates = templates
        self.tracker = RoomTracker()
        self.observe_mode = config.mode == MODE_OBSERVE_HUMAN
        self.current_room_key: str | None = None
        self.room_post_sl = False
        # 本会话是否已经进过一个**局内**房间。用来区分"模组报了一次读档"到底是
        # 真的读档，还是"从主菜单开一局"（见 apply_room_event）。
        self.session_room_seen = False
        # 跨会话读档续玩：下一个**局内**房间整体要标 post_sl（那间房的抽牌顺序、
        # 敌人后续行动人类都已经知道了，标签被非公平信息污染）。
        self.post_sl_next_room = False
        self.row_seq = 0
        self.inflight: dict[int, Inflight] = {}
        self.armed: Armed | None = None
        # observe_human：按观测 seq 保存"构题现场"。
        #
        # 曾经的写法是一个 `self.armed` 槽：观测来了就覆盖，人类动一下就清空。
        # 那在真机上全线崩：一轮里人类会连打几张牌、模组对同一次出牌可能重复
        # 上报、界面事件又会来抢这个槽，结果是 `hand_index` 全都对不上、行大量
        # 丢失。现在改成"动作自带它属于哪个 seq"，于是（seq, kind, args）就是
        # 天然幂等键：重复上报被丢弃，同一次出牌不会写两行，也不会串到别人头上。
        self.plans: dict[int, Armed] = {}
        self.seen_actions: set[tuple[Any, ...]] = set()
        self.plan_history = 64
        self.action_history = 512
        self.obs_context: dict[str, Any] = {}
        self.last_obs_seq = -1
        self.matched = 0
        self.unmatched = 0
        self.rows_written = 0
        # 被模组拒绝的 state 与次数：同一个 state 攒够 REJECTION_LIMIT 次就停发，
        # 把这一屏交给看门狗（见 note_rejection / decide_and_send）。
        self.rejected_state: str | None = None
        self.rejected_strikes = 0
        # 面板要能"点开看这一次决策到底发了什么、拿回了什么"。钩子挂在 client 上
        # （它是唯一知道线上原样字节的地方），runner 只负责贴上 seq 与决策点。
        self._exchange_ctx: dict[str, Any] = {}
        if self.laya is not None:
            self.laya.on_exchange = self._on_exchange

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        hello = self.bridge.hello
        self.recorder.meta.mod_version = str(hello.get("mod_version", ""))
        self.recorder.meta.game_version = str(hello.get("game_version", ""))
        self.recorder.meta.protocol = int(hello.get("protocol", 0) or 0)
        self.recorder.meta.mode = self.config.mode
        self.recorder.meta.character = self.config.character
        self.recorder.meta.ascension = self.config.ascension
        self.recorder.meta.fairness_mode = self.config.fairness_mode
        self.recorder.meta.laya_base_url = self.config.laya.base_url
        self.recorder.meta.laya_model = self.config.laya.model
        self.recorder.meta.laya_on_error = self.config.laya.on_error
        # 模组的模式只有一个来源：我们刚推过去的 configure。这里核对回执。
        acked = getattr(self.bridge, "configured", None) or {}
        mod_mode = str(acked.get("mode") or "")
        if not mod_mode:
            raise IncompatibleMod(
                "模组没有确认 configure —— 大概是旧 jar，重新构建安装后重启游戏"
            )
        if mod_mode != self.config.mode:
            raise IncompatibleMod(
                f"模组确认的模式 {mod_mode} 与 agent 的 mode={self.config.mode} 不一致"
            )
        self.recorder.meta.mod_observe_human = mod_mode == MODE_OBSERVE_HUMAN
        self.recorder.write_meta()
        log.info("run %s recording to %s", self.recorder.run_id, self.recorder.dir)
        if not self.observe_mode and self.laya is None:
            raise ModelUnavailable("agent 模式没有可用的 Laya 客户端（检查 [laya] 配置）")
        # 上一次会话（agent 被杀 / 崩溃）在这个 run 目录里留下的 pending：那一局的
        # 轨迹已经断了，行不可信，而且同一个 room_key 会让新会话的行追加进同一个
        # 文件（两段轨迹混在一起）。整批作废。
        leftovers = self.recorder.pending_keys()
        if leftovers:
            dropped = self.drop_pending_rooms(
                seam="agent_start", detail="pending left by an interrupted session"
            )
            # meta 还是 in_progress ⇒ 这次很可能是"游戏里续玩"，重记的房间要标 post_sl
            self.post_sl_next_room = self.recorder.meta.result == "in_progress"
            log.warning(
                "[sl] agent start: dropped %d rows left pending by an interrupted session (%s)",
                dropped,
                ", ".join(leftovers),
            )
            self.publish_sl(
                room_key=",".join(leftovers),
                dropped_rows=dropped,
                seam="agent_start",
                detail="pending left by an interrupted session",
                combat_instance=0,
            )

    def stop(self, *, result: str = "aborted") -> None:
        if self.current_room_key:
            self.recorder.commit_room(self.current_room_key)
            self.current_room_key = None
        self.recorder.finish(result=result, floors_reached=self.recorder.meta.floors_reached)
        self.bridge.close()

    def run(self) -> int:
        """主循环。返回进程退出码。"""
        try:
            if not self.bridge.connect(retry=True):
                log.error("cannot connect to the mod bridge; is the game running with the mod?")
                # 别留一个 result=in_progress 的孤儿目录：明确记成"没连上模组"。
                self.recorder.meta.mode = self.config.mode
                self.stop(result="no_mod")
                return 2
            self.start()
            while True:
                if not self.bridge.connected:
                    # 模组那头断了：关游戏、换 jar、模组崩溃。旧代码只会在空队列上
                    # 无限 recv，真机上表现为"玩家重开游戏，agent 装死"。
                    if not self.reconnect():
                        self.stop(result="no_mod")
                        return 2
                    continue
                msg = self.bridge.recv(timeout=0.2)
                if msg is None:
                    continue
                self.handle(msg)
        except IncompatibleMod as exc:
            log.error(
                "模组与 agent 对不上话：%s。重试没有意义，停跑。", exc
            )
            self.recorder.meta.mode = self.config.mode
            self.stop(result="mod_incompatible")
            return 5
        except ModelUnavailable as exc:
            log.error(
                "拿不到模型决策（%s）：按 laya.on_error=stop 停跑，不发兜底动作。"
                " 先修好 Laya 再重跑；游戏侧由模组看门狗接管。",
                exc.reason,
            )
            self.stop(result="model_unavailable")
            return 4
        except KeyboardInterrupt:
            log.info("interrupted")
            self.stop(result="aborted")
            return 130

    # ------------------------------------------------------------ 分发

    def reconnect(self) -> bool:
        """断线后重新握手；成功返回 True。

        模组每次接受连接都会把 `seq` 归零并重新 configure，所以重连后的第一条观测
        会自然走 `restart_session`（换 RoomTracker、作废旧 pending），这里不用额外重置。
        """
        log.warning(
            "mod 连接已断，重连中（等 %s:%s 重新监听）",
            self.config.mod.host,
            self.config.mod.port,
        )
        if not self.bridge.connect(retry=True):
            log.error("重连被中止，停跑")
            return False
        log.info("重连成功，已重新握手 mod")
        return True

    def handle(self, msg: Message) -> None:
        if msg.type == FRAME_OBSERVATION:
            self.handle_observation(msg)
        elif msg.type == FRAME_ACTION_RESULT:
            self.handle_action_result(msg)
        elif msg.type == FRAME_HUMAN_ACTION:
            self.handle_human_action(msg)

    def handle_observation(self, msg: Message) -> None:
        payload = msg.payload
        seq = int(payload.get("seq", -1) or -1)
        raw = payload.get("raw") or {}
        sl = payload.get("sl")
        if payload.get("watchdog_events"):
            self.recorder.note_watchdog(int(payload["watchdog_events"]))
        self.recorder.write_raw(seq, raw, sl=sl)

        observation = RawObservation.from_dict(raw)

        # mod 重连/重开局会把 seq 归零；此时旧的构题现场必须整体作废，
        # 否则人类的动作会匹配到上一局的同号 seq 上。
        restarted = seq <= self.last_obs_seq
        previous_context = dict(self.obs_context)
        if restarted:
            log.info("observation seq went backwards (%s -> %s); dropping plan history", self.last_obs_seq, seq)
            self.plans.clear()
            self.seen_actions.clear()
        self.last_obs_seq = seq
        self.note_observation_context(observation)
        if restarted:
            self.restart_session(previous_context)

        event = self.tracker.observe(
            RoomToken(act=observation.act, floor=observation.floor, node=observation.node),
            Snapshot.from_observation(raw),
        )
        self.apply_room_event(event, sl)
        self.recorder.note_floor(observation.floor, observation.player.hp, observation.player.max_hp)

        fair = fairness.filter_(observation, self.config.fairness_mode)
        room = self.room_dict(event, observation)
        # 面板显示的就是**送给模型的那份公平局面**（serialize.state 的原样输出），
        # 这样观战者看到的不会比模型多，也不会比模型少。构建失败不影响主循环。
        try:
            board = serialize.state(fair)
        except Exception:  # noqa: BLE001 - 面板只是观测，不能拖垮决策
            board = {}
        self.publish(
            "observation",
            {
                "seq": seq,
                "screen": observation.screen,
                "room": room,
                "hp": observation.player.hp,
                "max_hp": observation.player.max_hp,
                "gold": observation.player.gold,
                "act": observation.act,
                "floor": observation.floor,
                "board": board,
            },
        )

        if observation.screen == "GAME_OVER":
            self.finish_run(raw, observation)
            return

        plan = self.make_plan(fair, seq)
        if plan is None:
            # 认不出决策点就**不要**留着旧的构题现场：否则下一个界面事件会被
            # 拿去匹配上一个决策点的候选集，写出一行状态与动作完全不搭的数据。
            self.armed = None
            return
        if self.observe_mode:
            self.arm_human(seq, fair, plan, room)
        else:
            self.decide_and_send(seq, fair, plan, room)

    def note_observation_context(self, observation: RawObservation) -> None:
        """把"这一局到底是什么"记进 run meta（角色/进阶/种子/语言/画质）。

        以前这些字段直接抄 `spire.local.toml`，真机上跑的是 Watcher A5 却写成
        IRONCLAD A0，训练集的 context 全是错的。
        """
        player = observation.player
        character = getattr(player, "character", "") or self.config.character
        self.obs_context = {
            "character": str(character),
            "ascension": int(observation.ascension),
            "seed": int(observation.run_seed),
            "game_language": str(observation.language or ""),
        }
        meta = self.recorder.meta
        changed = (
            meta.character != self.obs_context["character"]
            or meta.ascension != self.obs_context["ascension"]
            or meta.seed != self.obs_context["seed"]
            or meta.game_language != self.obs_context["game_language"]
        )
        meta.character = self.obs_context["character"]
        meta.ascension = self.obs_context["ascension"]
        meta.seed = self.obs_context["seed"]
        meta.game_language = self.obs_context["game_language"]
        if changed:
            self.recorder.write_meta()

    def restart_session(self, previous: dict[str, Any]) -> None:
        """观测 `seq` 归零 = 游戏（模组）重启了，换一茬会话状态。

        三件事，缺一不可：

        1. **换新 `RoomTracker`**。它的 `_seen` 只对同一个会话有意义：跨会话留着，
           "重开一局又走到同一个节点"就成了"重访节点"→ 误判读档。真机上结算画面
           停在 act4 boss 节点上，这样反复误报了 9 次。
        2. **上一会话没提交的 pending 一律作废**。留着的话新一局会把行追加进同一个
           `pending/<room_key>.jsonl`，两局的行混在一个文件里（room_key 只由
           act/floor/node/combat_instance 决定）。
        3. **种子没变且上一局没结束 ⇒ 这是读档续玩**。STS 的自动存档发生在进入房间
           时，所以"退出游戏再继续"必然退回房间开头，按 SL 处理并把新房间标
           `post_sl`（导出时默认排除）。
        """
        key = self.current_room_key
        resumed = bool(
            key
            and int(previous.get("seed") or 0)
            and int(previous.get("seed") or 0) == int(self.obs_context.get("seed") or 0)
            and self.recorder.meta.result == "in_progress"
        )
        self.tracker = RoomTracker()
        dropped = self.drop_pending_rooms(
            seam="session_restart",
            detail="run resumed after restart (same seed)" if resumed else "room of an abandoned run",
        )
        if dropped or resumed:
            log.warning(
                "[sl] session restart: %s, dropped %d pending rows from %s",
                "resumed the same run" if resumed else "no resume (new run or finished)",
                dropped,
                key,
            )
            self.publish_sl(
                room_key=key or "",
                dropped_rows=dropped,
                seam="session_restart",
                detail="run resumed after restart (same seed)" if resumed else "room of an abandoned run",
                combat_instance=self.tracker.combat_instance,
            )
        self.current_room_key = None
        self.room_post_sl = False
        self.post_sl_next_room = resumed
        self.session_room_seen = False

    def drop_pending_rooms(self, *, seam: str, detail: str) -> int:
        """把有行的 pending 桶全部丢掉，返回丢掉的行数。
        正常只有一个桶（= 正在记录的房间）。**不能**只看 `current_room_key`：
        玩家中途回主菜单时观测 token 会变成局外的 `(0,0,0)`，`current_room_key`
        就漂到那个空桶上了，于是真正有行的那间房反而没被丢掉。
        """
        dropped = 0
        for key in self.recorder.pending_keys():
            dropped += self.recorder.rollback_room(
                key,
                seam=seam,
                detail=detail,
                combat_instance=self.tracker.combat_instance,
            )
        return dropped

    def publish_sl(
        self, *, room_key: str, dropped_rows: int, seam: str, detail: str, combat_instance: int
    ) -> None:
        """把一次 SL 推到观战面板（docs/09-observability.md#事件流）。"""
        self.publish(
            "sl_event",
            {
                "room_key": room_key,
                "dropped_rows": dropped_rows,
                "combat_instance": combat_instance,
                "seam": seam,
                "detail": detail,
            },
        )

    def context_for(self, state: dict[str, Any]) -> dict[str, Any]:
        """训练行的 `context`：只放**游戏自己说的**事实，不抄配置文件。

        `seed` 刻意不放进来 —— 它是"不公平"信息（见 docs/04-fairness.md），
        留在 `meta.json` 里够重放用就行，不混进训练行。
        """
        player = state.get("player") or {}
        character = player.get("character") or self.obs_context.get("character")
        ascension = state.get("ascension")
        if ascension is None:
            ascension = self.obs_context.get("ascension", self.config.ascension)
        return {
            "character": str(character or self.config.character),
            "ascension": int(ascension),
            "fairness_mode": self.config.fairness_mode,
        }

    def make_plan(self, fair: FairObservation, seq: int) -> Plan | None:
        """构题。识别不出决策点就返回 None：宁可不决策，也不产生垃圾数据。"""
        try:
            return pipeline.make_plan(fair, templates=self.templates)
        except RunOver:
            log.info("seq %s needs no decision", seq)
        except UnknownDecisionPoint as exc:
            log.warning("unknown decision point at seq %s; watchdog may take over: %s", seq, exc)
        except Exception as exc:  # noqa: BLE001 - 构题失败不能拖垮整局
            log.error("make_plan failed at seq %s: %s", seq, exc, exc_info=True)
        return None
    # ------------------------------------------------------------ 房间 / SL

    def room_dict(
        self, event: RoomEvent, observation: RawObservation | None = None
    ) -> dict[str, Any]:
        token = event.token
        return {
            "act": token.act,
            "floor": token.floor,
            "node": token.node,
            "type": observation.room_type if observation else "UNKNOWN",
            "combat_instance": event.combat_instance,
            "post_sl": bool(event.post_sl or self.room_post_sl),
        }

    def apply_room_event(self, event: RoomEvent, sl: dict[str, Any] | None) -> None:
        """房间事件 -> 提交 / 回滚。提交点是**离开节点**。"""
        in_room = event.token.is_room
        had_room = self.session_room_seen
        if event.kind == EVENT_NEW_ROOM:
            # 只有"真的走进了另一个房间"才算离开节点。回主菜单、结算、加载中这些
            # 局外观测（act/floor = 0）**不能**提交 —— 玩家在那儿退出游戏再继续，
            # 存档还在房间入口，那一间房的行必须留住等着被 SL 回滚掉。
            if in_room and self.current_room_key and self.current_room_key != event.room_key:
                self.recorder.commit_room(self.current_room_key)
            self.current_room_key = event.room_key
            # 跨会话读档续玩之后重记的第一间房要保留 post_sl；处理完就消费掉。
            self.room_post_sl = bool(in_room and self.post_sl_next_room)
            if in_room:
                self.post_sl_next_room = False
        elif event.kind == EVENT_REWIND:
            if self.current_room_key:
                dropped = self.recorder.rollback_room(
                    self.current_room_key,
                    seam="state_rewind",
                    detail=event.detail,
                    combat_instance=event.combat_instance,
                )
                self.publish_sl(
                    room_key=self.current_room_key,
                    dropped_rows=dropped,
                    seam="state_rewind",
                    detail=event.detail,
                    combat_instance=event.combat_instance,
                )
            self.current_room_key = event.room_key
            self.room_post_sl = bool(event.post_sl)
        if in_room:
            self.session_room_seen = True

        loads = int((sl or {}).get("count", 0) or 0)
        if loads <= 0:
            return
        seam = str((sl or {}).get("seam") or "mod")
        if had_room and event.kind != EVENT_REWIND:
            # 本会话已经进过房间，现在模组报了一次读档：以模组为准，丢弃**所有**
            # 有行的 pending 桶（正常只有一个 = 当前房间；玩家中途回主菜单时
            # `current_room_key` 可能已经漂到那个空桶上，所以要按"有行"来找），
            # 并把这一间房剩下的行打上 post_sl（导出时默认排除）。
            dropped = self.drop_pending_rooms(
                seam=f"mod:{seam}", detail="mod observed a save load"
            )
            self.room_post_sl = True
            self.publish_sl(
                room_key=self.current_room_key or "",
                dropped_rows=dropped,
                seam=f"mod:{seam}",
                detail="mod observed a save load",
                combat_instance=self.tracker.combat_instance,
            )
            log.warning(
                "[sl] mod load signal (%s): dropped %d pending rows, rest of the room marked post_sl",
                seam,
                dropped,
            )
        else:
            # 这个会话还没进过任何房间：`CardCrawlGame.loadPlayerSave` 就是
            # "从主菜单开始或继续一局"的那条路径，每次启动游戏都会响一次，所以
            # 这里**不能**记成 SL —— 真机上 10 次会话启动就记了 10 笔假 SL
            # （见 docs/08-dataset.md#sl-语义）。
            log.info(
                "[sl] mod load signal (%s) before any room: session start, not a save-scum",
                seam,
            )

    def finish_run(self, raw: dict[str, Any], observation: RawObservation) -> None:
        won = bool(raw.get("victory"))
        self.recorder.finish(
            result="victory" if won else "death",
            floors_reached=observation.floor,
        )
        log.info(
            "run %s finished: %s at floor %s (%d rows committed)",
            self.recorder.run_id,
            "victory" if won else "death",
            observation.floor,
            len(self.recorder.committed_rows),
        )
        if self.config.auto_restart:
            log.info("auto_restart=true：请在游戏回到主菜单后重启 agent 开始新一局")

    # ------------------------------------------------------------ agent 决策

    def _pace(self) -> None:
        """演示用的节流：把两个动作之间的间隔拉到人能看清的长度。

        只影响**执行**：观测、构题、落盘仍然全速，模型调用也不受影响。
        默认 0，留给测试与强化训练跑批。
        """
        delay = self.config.decision_delay_sec
        if delay > 0:
            time.sleep(delay)

    def decide_and_send(
        self, seq: int, fair: FairObservation, plan: Plan, room: dict[str, Any]
    ) -> None:
        # 面板要把"这一次请求"和"走到哪了"对上：client 的钩子只认识 payload，
        # 不认识 seq 与决策点，所以在这里贴上下文。
        self._exchange_ctx = {"seq": seq, "decision_point": plan.decision_point}
        state_key = canonical_hash(plan.state)
        if state_key != self.rejected_state:
            self.rejected_state = state_key
            self.rejected_strikes = 0
        elif self.rejected_strikes >= REJECTION_LIMIT:
            log.error(
                "%s: %d actions in a row were rejected by the mod and the state never changed "
                "(seq %s); not sending again — the watchdog takes this screen. This normally "
                "means the candidate list holds something the game cannot execute.",
                plan.decision_point,
                self.rejected_strikes,
                seq,
            )
            return
        # 只有一个合法动作时**不问模型**：没有可决策的东西。这既省 budget，
        # 也避免模型在没得选的局面上乱答（见 docs/06#强制决策短路）。
        forced = self.forced_choice(plan)
        if forced is not None:
            decision, rows = self.build_forced_decision(seq, fair, plan, room, forced)
        else:
            decision, rows = self.build_decision(seq, fair, plan, room)
        if decision is None or decision.chosen_action is None:
            log.warning(
                "no executable action for %s; leaving it to the watchdog", plan.decision_point
            )
            return
        wire = decision.chosen_action
        self._pace()
        message_id = self.bridge.send_action(
            seq=seq, kind=wire["kind"], args=wire.get("args") or {}
        )
        if message_id < 0:
            log.error("failed to send the action; a fresh observation will trigger a re-decide")
            return
        self.inflight[message_id] = Inflight(
            message_id=message_id,
            seq=seq,
            room_key=self.current_room_key or "unknown_room",
            room=room,
            rows=rows,
            tried=_labels_of(rows),
            state_key=state_key,
        )
        # 记住这次构题现场：动作被拒时要用它挑"次优候选"重试。
        self.armed = Armed(seq=seq, fair=fair, plan=plan, room=room)
        self.publish(
            "decision",
            {
                "seq": seq,
                "decision_point": plan.decision_point,
                "chosen": decision.chosen,
                "chosen_ids": decision.chosen_ids,
                "confidence": decision.confidence,
                "fallback": decision.fallback,
                "candidates": plan.candidate_ids,
                "action": wire,
                "forced": forced is not None,
            },
        )

    # ------------------------------------------------------------ 强制决策短路

    def forced_choice(self, plan: Plan):
        """只有一个合法动作时返回它，否则 None。

        `select_card_any` 例外：它的"什么都不选"（空选）本身就是一个真实选项，
        一个候选不等于没有选择余地。
        """
        if len(plan.candidates) == 1 and plan.candidates[0].cid == A.EMPTY_SELECTION:
            return plan.candidates[0]
        if len(plan.candidates) != 1 or plan.decision_point in FORCED_EXEMPT:
            return None
        # 选牌界面要求必选 k 张时，"只有一个候选"是界面异常，不该由这里拍板。
        if plan.decision_point in CARD_SELECT_POINTS and plan.k_min > 1:
            return None
        return plan.candidates[0]

    def build_forced_decision(
        self,
        seq: int,
        fair: FairObservation,
        plan: Plan,
        room: dict[str, Any],
        candidate,
    ) -> tuple[Decision | None, list[dict[str, Any]]]:
        """把"唯一合法动作"直接变成一次决策，不经过 Laya。

        产出的行 `label_source` 仍是 `agent`（不是 fallback）—— 它不是"模型
        挂了"的应急产物，而是"这个局面不需要模型"，所以**不排除**出训练集。
        为了能事后分辨，行的 meta 里带 `forced: true`。
        """
        action = candidate.action
        if action is None and plan.decision_point in CARD_SELECT_POINTS:
            action = A.select_cards([candidate.cid])
        decision = Decision(
            seq=seq,
            decision_point=plan.decision_point,
            state=plan.state,
            questions=plan.questions,
            candidate_ids=plan.candidate_ids,
            chosen=candidate.cid,
            chosen_ids=[candidate.cid],
            chosen_action=action.wire() if action is not None else None,
            confidence=1.0,
            probabilities={candidate.cid: 1.0},
            fallback=False,
            fallback_reason=None,
            meta={"forced": True},
        )
        return decision, [self.row_for(
            plan, decision, room, SOURCE_AGENT, LABEL_AGENT,
            meta_extra={"forced": True},
        )]

    def no_model_decision(self, plan: Plan, fair: FairObservation, reason: str) -> Decision:
        """拿不到模型决策时的唯一出口。

        默认（`laya.on_error = "stop"`）**抛异常让整局停下来** —— 不替模型猜。
        只有显式配了 `fallback` 才退回规则策略，且产出的行带 `agent_fallback=true`，
        导出时默认被排除（见 docs/08）。
        """
        if self.config.laya.on_error != "fallback":
            raise ModelUnavailable(f"{reason} @ {plan.decision_point}")
        log.error("no model decision (%s); falling back to the rule policy", reason)
        return pipeline.fallback_decision(plan, fair, reason=reason)

    def build_decision(
        self, seq: int, fair: FairObservation, plan: Plan, room: dict[str, Any]
    ) -> tuple[Decision | None, list[dict[str, Any]]]:
        if plan.decision_point == SELECT_CARD_MUST_K and plan.k_min > 1:
            return self.sequential_card_pick(seq, fair, plan, room)
        if self.laya is None:
            decision = self.no_model_decision(plan, fair, "no laya client")
            return decision, [self.row_for(plan, decision, room, SOURCE_AGENT, LABEL_FALLBACK)]

        result = self.laya.ask(plan)
        if result is None:
            decision = self.no_model_decision(plan, fair, "laya unavailable")
            return decision, [self.row_for(plan, decision, room, SOURCE_AGENT, LABEL_FALLBACK)]

        try:
            decision = pipeline.resolve(plan, result)
        except Exception as exc:  # noqa: BLE001 - 解析失败要么停跑要么兜底，由 on_error 决定
            log.error("resolve failed for %s: %s", plan.decision_point, exc)
            decision = self.no_model_decision(plan, fair, f"resolve failed: {exc}")
            return decision, [self.row_for(plan, decision, room, SOURCE_AGENT, LABEL_FALLBACK)]

        if decision.chosen_action is None:
            # 选牌类决策点的答案是"若干张牌"，由决策点层聚合提交；
            # 一个都没选（k_min==0）也是合法答案 -> 提交空列表。
            if plan.decision_point in CARD_SELECT_POINTS:
                action = A.select_cards(decision.chosen_ids)
                decision = _with_action(decision, action)
            else:
                decision = self.no_model_decision(plan, fair, "empty answer")

        label_source = LABEL_FALLBACK if decision.fallback else LABEL_AGENT
        return decision, [self.row_for(plan, decision, room, SOURCE_AGENT, label_source, result)]

    def sequential_card_pick(
        self, seq: int, fair: FairObservation, plan: Plan, room: dict[str, Any]
    ) -> tuple[Decision | None, list[dict[str, Any]]]:
        """必选 k 张：同一屏连问 k 次（每次排除已选），最后一次性提交。

        每次提问各自的 `state`/`questions` 都记一行 —— 这正是模型推理时看到的东西，
        数据集与推理因此严格同分布。
        """
        assert self.laya is not None
        picked: list[str] = []
        rows: list[dict[str, Any]] = []
        current = plan
        result: LayaResult | None = None
        confidences: list[float] = []
        probabilities: dict[str, float] = {}
        k = max(1, plan.k_min)

        for _ in range(k):
            result = self.laya.ask(current)
            if result is None:
                break
            step = pipeline.resolve(current, result)
            if not step.chosen_ids:
                break
            cid = step.chosen_ids[0]
            picked.append(cid)
            confidences.append(step.confidence)
            probabilities.update(step.probabilities)
            rows.append(self.row_for(current, step, room, SOURCE_AGENT, LABEL_AGENT, result))
            try:
                current = pipeline.make_plan(
                    fair, templates=self.templates, context={"selection_picked": picked}
                )
            except RunOver:
                break
            if not current.candidate_ids:
                break

        if len(picked) < k:
            decision = self.no_model_decision(plan, fair, "card selection incomplete")
            return decision, [self.row_for(plan, decision, room, SOURCE_AGENT, LABEL_FALLBACK)]

        merged = _with_action(
            Decision(
                seq=seq,
                decision_point=plan.decision_point,
                state=plan.state,
                questions=plan.questions,
                candidate_ids=plan.candidate_ids,
                chosen=picked[0],
                chosen_ids=picked,
                chosen_action=None,
                confidence=round(sum(confidences) / len(confidences), 4) if confidences else 0.0,
                probabilities=probabilities,
                fallback=False,
                fallback_reason=None,
                meta={"picks": picked},
            ),
            A.select_cards(picked),
        )
        return merged, rows
    # ------------------------------------------------------------ 训练行

    def row_for(
        self,
        plan: Plan,
        decision: Decision,
        room: dict[str, Any],
        source: str,
        label_source: str,
        result: LayaResult | None = None,
        meta_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        labels = {plan.question_id: _label_value(plan, decision)}
        meta: dict[str, Any] = {"matched": True}
        if meta_extra:
            meta.update(meta_extra)
        if label_source == LABEL_AGENT and result is not None:
            meta.update(
                {
                    "model_answer": decision.chosen,
                    "model_picks": list(decision.chosen_ids),
                    "model_confidence": decision.confidence,
                    "checkpoint": result.checkpoint,
                    "latency_ms": result.latency_ms,
                    "usage": result.usage,
                }
            )
        if decision.fallback:
            meta["fallback_reason"] = decision.fallback_reason
        return self.make_row(
            plan, room, source, labels, label_source, meta, decision=decision, result=result
        )

    def human_row(
        self,
        plan: Plan,
        room: dict[str, Any],
        labels: dict[str, Any],
        action: dict[str, Any],
        matched: bool,
        *,
        result: LayaResult | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "matched": matched,
            "human_action": action,
        }
        if note:
            meta["note"] = note
        if result is not None:
            model_answer = _model_answer(result, plan.question_id)
            meta.update(
                {
                    "model_answer": model_answer,
                    "model_confidence": _model_confidence(result, plan.question_id),
                    "agreement": _agreement(plan, model_answer, labels),
                    "checkpoint": result.checkpoint,
                    "latency_ms": result.latency_ms,
                    "usage": result.usage,
                }
            )
        return self.make_row(
            plan, room, SOURCE_HUMAN, labels, LABEL_HUMAN, meta, result=result
        )

    def make_row(
        self,
        plan: Plan,
        room: dict[str, Any],
        source: str,
        labels: dict[str, Any],
        label_source: str,
        meta: dict[str, Any],
        *,
        state: dict[str, Any] | None = None,
        questions: dict[str, Any] | None = None,
        decision: Decision | None = None,
        result: LayaResult | None = None,
    ) -> dict[str, Any]:
        self.row_seq += 1
        merged_meta = {
            "label_source": label_source,
            "agent_fallback": label_source == LABEL_FALLBACK,
        }
        merged_meta.update(meta)
        row = make_row(
            run_id=self.recorder.run_id,
            seq=self.row_seq,
            decision_point=plan.decision_point,
            room=room,
            context=self.context_for(state if state is not None else plan.state),
            source=source,
            split=split_of(
                self.recorder.run_id,
                split_seed=self.config.dataset.split_seed,
                val_ratio=self.config.dataset.val_ratio,
                test_ratio=self.config.dataset.test_ratio,
            ),
            state=plan.state if state is None else state,
            questions=plan.questions if questions is None else questions,
            candidate_ids=plan.candidate_ids,
            labels=labels,
            meta=merged_meta,
            outcome={
                "run_won": None,
                "hp_after": None,
                "hp_delta_room": None,
                "floor_reached": room.get("floor"),
            },
        )
        # 附加字段：让 `spire_core.replay` 能脱离游戏复现"同一批答案 -> 同一个动作"。
        # 它们不影响训练（Laya 只读 state/questions），但排障时非常值钱。
        row["chosen"] = decision.chosen if decision else None
        row["chosen_ids"] = list(decision.chosen_ids) if decision else []
        row["k_min"] = plan.k_min
        row["k_max"] = plan.k_max
        row["answers"] = dict(result.answers) if result is not None else {}
        return row

    # ------------------------------------------------------------ 动作结果

    def handle_action_result(self, msg: Message) -> None:
        payload = msg.payload
        inflight = self.match_inflight(msg)
        ok = bool(payload.get("ok"))
        if inflight is None:
            log.debug(
                "action_result for unknown action (id=%s reply_to=%s seq=%s ok=%s)",
                msg.id,
                msg.reply_to,
                payload.get("seq"),
                ok,
            )
            return
        if payload.get("watchdog"):
            log.warning("mod executed a watchdog default for seq %s", inflight.seq)
            return
        if ok:
            for row in inflight.rows:
                self.recorder.add_pending(inflight.room_key, row)
            self.rows_written += len(inflight.rows)
            log.info(
                "seq %s ok: %d row(s) pending in %s",
                inflight.seq,
                len(inflight.rows),
                inflight.room_key,
            )
            return

        log.warning(
            "seq %s rejected by mod: %s %s", inflight.seq, payload.get("code"), payload.get("error")
        )
        if inflight.retried:
            log.error("the second attempt was rejected too; letting the watchdog act")
            self.note_rejection(inflight)
            return
        self.retry_action(inflight)

    def match_inflight(self, msg: Message) -> Inflight | None:
        """把一条 `action_result` 找回它对应的 inflight 动作。

        协议（见 docs/03-mod-protocol.md#action_result）：动作 id 在 **`reply_to`** 里，
        `id` 是模组自己的出站序号。看门狗那条更极端——它压根没有 `reply_to`，只有 `seq`。

        曾经只按 `msg.id` 取，于是真机上一条都匹配不上（落的是 DEBUG 日志，肉眼看不见），
        表现是"agent 玩了一整局，`runs/*/pending/` 一行都没有"：动作执行了、也没人记录。

        `id` 与 `seq` 两条兜底是给假模组（测试桩）和旧实现的，不影响真机路径。
        """
        if msg.reply_to is not None:
            found = self.inflight.pop(msg.reply_to, None)
            if found is not None:
                return found
        found = self.inflight.pop(msg.id, None)
        if found is not None:
            return found
        seq = msg.payload.get("seq")
        if seq is None:
            return None
        for key, entry in list(self.inflight.items()):
            if entry.seq == int(seq):
                del self.inflight[key]
                return entry
        return None

    def note_rejection(self, inflight: Inflight) -> None:
        """记一次"两个候选都被模组拒了"。

        同一个 state 上攒够 `REJECTION_LIMIT` 次，就说明游戏不认我们给的候选、而 state 又
        一直没变（真机那次是空药水槽）：再发只会无限空转，还把模组看门狗压着不触发。
        `decide_and_send` 见到同一个 state 就不再发，这一屏交给看门狗；state 一变
        （看门狗结束回合、换回合、换房间）计数自然清零。
        """
        if inflight.state_key and inflight.state_key == self.rejected_state:
            self.rejected_strikes += 1
        else:
            self.rejected_state = inflight.state_key or None
            self.rejected_strikes = 1

    def retry_action(self, inflight: Inflight) -> None:
        """按 docs/03 的约定用次优候选重试**一次**。"""
        if self.armed is None:
            log.error("cannot retry: the armed plan is gone")
            return
        plan = self.armed.plan
        # 不把出牌候选排除在外：docs/03 的约定是"次优候选"，不是"次优的非出牌候选"。
        # 之前这里会把被拒的出牌换成 end_turn —— 模型答了牌、模组拒了牌、agent 却直接
        # 结束回合，看上去就是"模型返回了结果但 agent 不执行"，而且白扔一个回合。
        remaining = [c for c in plan.candidate_ids if c not in inflight.tried]
        if not remaining:
            log.error("no alternative candidate left; letting the watchdog act")
            return
        cid = remaining[0]
        action = A.parse_candidate(cid)
        message_id = self.bridge.send_action(
            seq=inflight.seq, kind=action.kind, args=action.args
        )
        if message_id < 0:
            return
        self.inflight[message_id] = Inflight(
            message_id=message_id,
            seq=inflight.seq,
            room_key=inflight.room_key,
            room=inflight.room,
            rows=inflight.rows,
            tried=inflight.tried + [cid],
            retried=True,
            state_key=inflight.state_key,
        )
        log.info("retrying seq %s with candidate %s", inflight.seq, cid)

    # ------------------------------------------------------------ observe_human

    def arm_human(self, seq: int, fair: FairObservation, plan: Plan, room: dict[str, Any]) -> None:
        """人类接管这一屏：构题照做（与模型逐字节同构），但不发动作。"""
        result: LayaResult | None = None
        if self.config.observe_human.also_query_model and self.laya is not None:
            self._exchange_ctx = {"seq": seq, "decision_point": plan.decision_point}
            result = self.laya.ask(plan)
        self.remember_plan(seq, fair, plan, room, result)
        self.publish(
            "human_prompt",
            {
                "seq": seq,
                "decision_point": plan.decision_point,
                "candidates": plan.candidate_ids,
                "questions": plan.questions,
                "model_answer": _model_answer(result, plan.question_id),
                "model_confidence": _model_confidence(result, plan.question_id),
            },
        )

    def remember_plan(
        self, seq: int, fair: FairObservation, plan: Plan, room: dict[str, Any],
        result: LayaResult | None = None,
    ) -> None:
        """记下"第 seq 次观测时该怎么做题"，等人类动作带着同一个 seq 回来对齐。"""
        self.plans[seq] = Armed(seq=seq, fair=fair, plan=plan, room=room, result=result)
        while len(self.plans) > self.plan_history:
            del self.plans[min(self.plans)]

    def handle_human_action(self, msg: Message) -> None:
        payload = msg.payload
        action = {
            "kind": str(payload.get("kind") or ""),
            "args": payload.get("args") or {},
        }
        seq = int(payload.get("seq", -1) or -1)

        # 幂等：模组对同一次提交可能发不止一次（见 handle_human_action 的历史注释）。
        key = (seq, action["kind"], json.dumps(action["args"], sort_keys=True, ensure_ascii=False))
        if key in self.seen_actions:
            log.info("duplicate human action ignored: seq=%s %s", seq, action["kind"])
            return
        self.seen_actions.add(key)
        while len(self.seen_actions) > self.action_history:
            self.seen_actions.discard(next(iter(self.seen_actions)))

        armed = self.plans.get(seq)
        if armed is None:
            # 人类做了我们没枚举到的事（主菜单、宝箱、或者模组没为这次提交发观测）。
            # 宁可丢弃也不编：没有对应的构题现场就没有合法的 (state, questions)。
            log.warning(
                "human action at seq %s has no plan (dropped): %s",
                seq,
                json.dumps(action, ensure_ascii=False),
            )
            self.unmatched += 1
            return

        matched_id = self.match_candidate(armed.plan, action)
        if matched_id is None:
            self.unmatched += 1
            log.warning(
                "human action %s matched no candidate at %s",
                json.dumps(action, ensure_ascii=False),
                armed.plan.decision_point,
            )
        else:
            self.matched += 1
            log.info(
                "human seq %s %s -> %s (%s)",
                seq,
                action["kind"],
                matched_id,
                armed.plan.decision_point,
            )

        rows = self.human_rows(seq, armed, action, matched_id)
        for row in rows:
            self.recorder.add_pending(self.current_room_key or "unknown_room", row)
        self.rows_written += len(rows)
        self.publish(
            "human_action",
            {
                "seq": seq,
                "decision_point": armed.plan.decision_point,
                "action": action,
                "matched": matched_id is not None,
                "candidate": matched_id,
                "rows": len(rows),
            },
        )

    def human_rows(
        self, seq: int, armed: Armed, action: dict[str, Any], matched_id: str | None
    ) -> list[dict[str, Any]]:
        picks = self.human_cards(action)
        if armed.plan.decision_point in CARD_SELECT_POINTS and picks:
            return self.human_card_rows(seq, armed, picks, action)
        labels = {armed.plan.question_id: matched_id}
        return [
            self.human_row(
                armed.plan,
                armed.room,
                labels,
                action,
                matched_id is not None,
                result=armed.result,
            )
        ]

    def human_card_rows(
        self, seq: int, armed: Armed, picks: list[str], action: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """必选 k 张时人类一次性提交 k 张 -> 拆成 k 行，逐次提问的现场与模型一致。"""
        rows: list[dict[str, Any]] = []
        picked: list[str] = []
        current = armed.plan
        total = len(picks)
        for index, cid in enumerate(picks):
            matched = cid in current.candidate_ids
            labels = {current.question_id: [cid]}
            rows.append(
                self.human_row(
                    current,
                    armed.room,
                    labels,
                    action,
                    matched,
                    result=armed.result,
                    note=f"card select pick {index + 1}/{total}",
                )
            )
            picked.append(cid)
            try:
                current = pipeline.make_plan(
                    armed.fair, templates=self.templates, context={"selection_picked": picked}
                )
            except RunOver:
                break
        return rows

    def human_cards(self, action: dict[str, Any]) -> list[str]:
        """`select_cards{indices:[[zone, index], ...]}` -> 候选 id 列表（保序）。"""
        if action.get("kind") != A.KIND_SELECT_CARDS:
            return []
        indices = (action.get("args") or {}).get("indices")
        if not isinstance(indices, list):
            return []
        out: list[str] = []
        for item in indices:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out.append(A.format_card(str(item[0]), int(item[1])))
        return out

    def match_candidate(self, plan: Plan, action: dict[str, Any]) -> str | None:
        """人类动作 -> 候选 id。用枚举器本身当唯一事实来源，不重新推导。"""
        kind = action.get("kind")
        args = action.get("args") or {}
        for candidate in plan.candidates:
            # 选牌类候选没有独立动作（action is None），由下面的 select_cards 分支处理
            if candidate.action is None or candidate.action.kind != kind:
                continue
            if _same_args(candidate.action.args or {}, args):
                return candidate.cid
        if kind == A.KIND_SELECT_CARDS:
            picks = self.human_cards(action)
            if len(picks) == 1 and picks[0] in plan.candidate_ids:
                return picks[0]
        return None

    # ------------------------------------------------------------ 面板

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        if self.panel is None:
            return
        try:
            self.panel({"kind": kind, **payload})
        except Exception as exc:  # noqa: BLE001 - 面板永远不能影响决策
            log.warning("panel sink failed: %s", exc)

    def _on_exchange(self, record: dict[str, Any]) -> None:
        """LayaClient 每完成一次往返就调这里（成功 / 缓存 / 失败 / 超预算都算）。

        `record` 里带的是**原样的请求体与返回体**。这里只补上 seq 与决策点，
        然后交给面板 —— 面板把大块内容留在自己的环形缓冲里，时间线上只放摘要。
        """
        payload = dict(self._exchange_ctx)
        payload.update(record)
        self.publish("exchange", payload)


# --------------------------------------------------------------------------- #
# 纯函数助手（单测直接打这些）
# --------------------------------------------------------------------------- #


def _same_args(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if set(left) != set(right):
        return False
    for key, value in left.items():
        other = right[key]
        if isinstance(value, bool) or isinstance(other, bool):
            if bool(value) != bool(other):
                return False
        elif value != other:
            return False
    return True


def _label_value(plan: Plan, decision: Decision) -> Any:
    if plan.decision_point in CARD_SELECT_POINTS:
        return list(decision.chosen_ids)
    return decision.chosen


def _with_action(decision: Decision, action: A.Action) -> Decision:
    return Decision(
        seq=decision.seq,
        decision_point=decision.decision_point,
        state=decision.state,
        questions=decision.questions,
        candidate_ids=decision.candidate_ids,
        chosen=decision.chosen,
        chosen_ids=decision.chosen_ids,
        chosen_action=action.wire(),
        confidence=decision.confidence,
        probabilities=decision.probabilities,
        fallback=decision.fallback,
        fallback_reason=decision.fallback_reason,
        meta=decision.meta,
    )


def _labels_of(rows: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        for value in (row.get("labels") or {}).values():
            if isinstance(value, list):
                out.extend(v for v in value if isinstance(v, str))
            elif isinstance(value, str):
                out.append(value)
    return out


def _first_label_value(labels: dict[str, Any]) -> Any:
    for value in labels.values():
        return value
    return None


def _model_answer(result: LayaResult | None, question_id: str) -> Any:
    """模型对某题的原始答案（`answers[qid]` 里题型对应的那个键）。没问过/畸形 -> None。"""
    raw = (result.answers or {}).get(question_id) if result else None
    return selected_answer(raw) if isinstance(raw, dict) else None


def _model_confidence(result: LayaResult | None, question_id: str) -> float:
    raw = (result.answers or {}).get(question_id) if result else None
    if not isinstance(raw, dict):
        return 0.0
    try:
        return float(raw.get("answer_confidence", raw.get("confidence")))
    except (TypeError, ValueError):
        return 0.0


def _agreement(plan: Plan, model_answer: Any, labels: dict[str, Any]) -> bool | None:
    """`model_answer == labels`（见 docs/08）。只在两边答案空间一致时给结论。

    `score` 题模型答的是等级下标、`labels` 是候选 id，直接比会得到恒假的脏数据，
    所以那类题返回 None（"不可比"），由 `dataset.py` 按 `model_answer is None` 之外的口径统计。
    """
    question = (plan.questions or {}).get(plan.question_id)
    if not isinstance(question, dict) or question.get("type") != "choice":
        return None
    if model_answer is None:
        return None
    return model_answer == _first_label_value(labels)
