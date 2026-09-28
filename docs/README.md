# 文档索引

SlayaTheSpire 的契约文档。**代码是文档的实现，文档是代码的依据**；两者冲突时以文档为准并提 issue 修正。

## 阅读顺序

| # | 文档 | 读者 | 什么时候读 |
|---|---|---|---|
| 01 | [总览](01-overview.md) | 全体 | 先读这个 |
| 02 | [架构](02-architecture.md) | 开发 | 想知道"一次决策发生了什么" |
| 03 | [模组线协议](03-mod-protocol.md) | 模组开发 | 改 mod 或桥接层 |
| 04 | [公平视图](04-fairness.md) | 全体 | 涉及"模型能看到什么" |
| 05 | [状态 schema](05-state-schema.md) | 构题开发 | 改序列化 |
| 06 | [决策点](06-decision-points.md) | 构题开发 | 加/改决策点 |
| 07 | [Laya 契约](07-laya-contract.md) | 全体 | 调模型或改客户端 |
| 08 | [数据集规范](08-dataset.md) | 数据/微调 | 改采集或做训练（含 observe_human 采集 runbook） |
| 09 | [可观测性](09-observability.md) | 开发/调试 | 看面板或加指标 |
| 10 | [部署](10-deployment.md) | 运维 | 部署远程 Laya |
| 11 | [测试与验收](11-testing.md) | 全体 | 提 PR / 验收 |
| 12 | [路线图](12-roadmap.md) | 全体 | 规划下一步 |
| 13 | [微调](13-finetune.md) | 数据/运维 | 训练、评测、部署微调后的 checkpoint |

## 术语

| 术语 | 含义 |
|---|---|
| **Laya** | 非自回归「System 1 判断模型」。输入 `state` + 一组类型化问题，单次前向输出带概率的答案。**不会生成文本或动作序列**。 |
| **类型化问题** | `choice`（多选一）/ `score`（有序分级）/ `noul`（yes-no-unknown）三类。 |
| **Jev 协议** | TypeSafe Jev 的 `/v1/systemone` 线协议。Laya 的 `predict()` 输出与之 schema 兼容，`laya.serve` 直接暴露该路由。 |
| **state** | 送给 Laya 的一份状态描述（本项目用紧凑 JSON，内容为英文）。 |
| **公平视图** | 只含「游戏内玩家可见信息」的状态视图，见 04。 |
| **原始观测** | mod 上报的未过滤观测，含隐藏信息；仅用于落盘备查，绝不进入 state。 |
| **候选** | 一个决策点上枚举出的一个可执行选项，用候选 id 表示（如 `play:h2->m0`）。 |
| **pending** | 当前房间尚未提交的采集行。SL 回滚的作用域。 |
| **SL** | Save & Load / save-scum：退出并读档，回到当前房间开头。默认禁止。 |
| **post_sl** | 该房间的数据是「读档后重记」的，标签可能被非公平信息污染。 |

## 上游依赖

- Laya：`pip install laya`，checkpoints `convaiinnovations/laya`、`laya-multilingual`、`laya-typed-decisions`。
- Laya 微调 / 微调后部署：`finetune/`（`pip install "laya[serve]" pytest`，**训练与部署共用一个 venv**，见 [13-finetune](13-finetune.md)）。
- ModTheSpire（Steam 工坊 `1605060445`）、BaseMod（`1605833019`）。
- 参考实现：CommunicationMod（协议与状态 schema 的先例，本项目不依赖它运行）。

## 当前状态

- 阶段 0（本目录）已定稿；阶段 1-4 已落地：`spire-core` / `spire-agent` / 模组均有测试覆盖
  （`.\.venv\Scripts\python.exe -m pytest packages -q`、`powershell -File tools\test_mod.ps1`）。
- 采集 -> 导出的完整链路可离线跑通：`python tools/demo_observe_session.py`。
- 微调链路（编译训练项 -> RLCD 微调 -> 离线评测 -> 用微调后的 checkpoint 起服务）已落地并可离线跑通：
  `finetune/`（Windows 一条龙 `tools\finetune_laya.ps1`，Linux `finetune/run_all.sh`），契约见 [13-finetune](13-finetune.md)。
- 剩余工作（观战面板细化、端到端验收）见 [12-roadmap](12-roadmap.md)
  与 [11-testing](11-testing.md) 的验收清单。
