# SlayaTheSpire

用 **Laya**（非自回归判断模型：`choice` / `score` / `noul` 三类题型，单次前向出概率）
全自动打完一局的《杀戮尖塔》**一代（Slay the Spire 1，v2.3.4 Steam 版）**智能体，
并且从设计阶段就把数据采集做成**能直接喂 Laya 微调**的形态。

数据单元与 Laya 的可训单元严格对齐：`(英文 state, typed questions) -> answer`。
采集的不是"游戏录像"，而是**每个决策点的完整构题现场 + 标签**。

## 三段式架构

```
自研 Java 模组（观测 + 执行）  ──NDJSON/TCP──▶  Python 智能体  ──HTTP──▶  远程 Laya 服务
  稳定态 → 原始观测             agent 主动连入    公平过滤 → 英文序列化 → 决策点识别 → 候选枚举
                                                    → 构题 → 解析 → 裁决 → 动作校验 → 执行 → 落盘
```

- **模组**：挂在主更新循环上，只在"稳定态"（动作队列空闲、无动画）上报观测；
  执行走游戏自身的 `GameAction` / 动作队列，**不模拟鼠标**。协议是 loopback TCP + NDJSON，
  Java 侧零第三方依赖。
- **智能体**：公平过滤、序列化、构题、裁决、对局记录、观战面板。
  过滤责任全部在这边，模组不做过滤。
- **Laya 后端**：`POST /v1/systemone`，可部署在另一台有 GPU 的机器上。

## 两种运行模式

| 模式 | 谁决策 | 产出 |
|---|---|---|
| `agent`（默认） | Laya | 弱标签（`label_source=agent`），可整局自动跑 |
| `observe_human` | 人类正常玩 | **强标签**（`label_source=human`），可选同时问一次模型做对照 |

两种模式共用同一套观测 / 构题 / 记录管线，`(state, questions)` 逐字节相同 ——
所以人类标签可以直接拿来微调，不需要任何格式转换。

## 怎么区分"人类在玩"还是"智能体在控制"

**只有一个开关：agent 的 `mode`。** 模组没有自己的模式 —— 它在握手后接受 agent
推来的 `configure{mode, watchdog_sec}`，在那之前是哑的（不发观测、不接管、不报人类动作）。

| | 怎么开 | 谁决策 |
|---|---|---|
| 智能体控制 | `mode = "agent"`（默认） | Laya |
| 人类玩、采集数据 | `mode = "observe_human"` | 人类 |

```powershell
.\.venv\Scripts\python.exe -m spire_agent run                          # 智能体控制（默认）
.\.venv\Scripts\python.exe -m spire_agent run --mode observe_human     # 人类玩、只记录
```

切模式**不用碰游戏、不用重启游戏**：重跑 agent 时新的 `mode` 会随握手下发。
模组侧唯一需要配的是监听地址（`host` / `port`，默认 `127.0.0.1:17777`），
而它也有默认值 —— 除非要改端口，不然那个 properties 文件根本不用建。

开局时 agent 会把当前模式打在屏幕上（`运行模式：agent —— 智能体控制，人类不参与`）。
事后查落盘：`runs/<run_id>/meta.json` 的 `mode` / `mod_observe_human`、
`summary.json` 的 `by_source`（`agent` vs `human`）、每行的 `source` 字段。

## 模型连不上时不保底

默认 `[laya] on_error = "stop"`：**拿不到模型决策就停跑报错**（`preflight` 在启动时先探一次，
失败退出码 3、根本不连游戏；决策中途失败退出码 4），绝不偷偷用规则策略替模型下判断 ——
那样只会白烧一局真实对局、产出一批要丢掉的数据。

| 退出码 | 含义 |
|---|---|
| `0` | 正常结束 |
| `2` | 连不上模组（游戏没起 / 没勾选模组） |
| `3` | Laya 启动预检失败（最常见的原因是根本没起 Laya，见「快速开始」第 3 步；`--skip-preflight` 可跳过） |
| `4` | 模型不可用 / 解析失败，按 `on_error=stop` 停跑 |
| `5` | 模组对不上话（协议版本不符 / 拒绝 `configure`）—— 通常是装的是旧 jar |
| `130` | Ctrl-C |

想恢复"规则兜底把这一局打完"的旧行为：`[laya] on_error = "fallback"`（产出的行
`agent_fallback=true`，导出时默认排除）。

## SL（Save & Load）

- **默认禁止**：动作白名单里根本没有读档/重开，agent 永不主动触发。
- **交易边界 = 当前房间节点**（STS 的自动存档发生在进入房间时，读档回到房间开头）。
  行先写 `runs/<run_id>/pending/<room_key>.jsonl`，离开节点才提交进 `decisions.jsonl`。
- **检测到 SL**：当前房间的 pending **整体丢弃**、`combat_instance` 自增、从房间开头重记；
  前序房间的已提交数据不受影响。重记的那间房打 `post_sl=true`（导出时默认排除，
  因为此时人类已知抽牌顺序与敌人行动，标签被非公平信息污染）。

## 快速开始

前置：

- Steam 订阅并启用工坊 mod **ModTheSpire `1605060445`** 与 **BaseMod `1605833019`**；
- JDK 21（只用 `javac --release 8`）、Python 3.11+。

**所有命令都在仓库根目录跑**——`--config` 的相对路径、`runs/`、`dataset/` 都相对当前目录解析。

```powershell
# 0) 一次性：建 .venv 并以可编辑模式装 spire-core / spire-agent（唯一外部依赖 httpx）
pwsh -File tools/setup_dev.ps1

# 1) 构建 + 安装模组
pwsh -File tools/build_mod.ps1          # -> mod/build/spireagent.jar（含纯逻辑自检）
pwsh -File tools/install_mod.ps1        # 复制到 <STS>\mods\

# 2) 用 ModTheSpire 启动游戏，勾选 SlayaTheSpire Agent
#    模组配置走 MTS 的 SpireConfig("spireagent", "SlayaTheSpire")，只读 host / port（默认 127.0.0.1:17777）；
#    不改端口就不用建这个文件。模式与 watchdog 由 agent 握手时推送，不需要配。

# 3) 起 Laya —— agent 不会替你起它：base_url 那个端口上必须真有个进程在监听
#    ★ 自己开一个 PowerShell 窗口跑，别塞进当下的临时 shell —— 服务归启动它的那个终端，
#      终端一关端口就空了，症状就是 agent 预检报 WinError 10061（连接被拒绝）。
#    真模型（本机 GPU、离线、首次加载几十秒；没装过先跑 tools/setup_laya.ps1）：
pwsh -File tools/serve_laya.ps1
#    确认真在监听：netstat -ano | Select-String ':8000'     # 要看到 LISTENING
#    没有 GPU / 只想先验链路：见下面「没有远端 Laya 时…」（假 Laya，不是模型）

# 4) 准备配置：API Key 走环境变量，不要写进文件
Copy-Item packages/spire-agent/config.example.toml spire.local.toml
$env:LAYA_API_KEY = "..."
$env:LAYA_BASE_URL = "http://<你的 Laya 主机>:8000"   # 本机 Laya 就是 http://127.0.0.1:8000

# 5) 自检 + 开跑（用 venv 里的解释器：不需要激活，也不需要 PYTHONPATH）
.\.venv\Scripts\python.exe -m spire_agent doctor --config spire.local.toml
.\.venv\Scripts\python.exe -m spire_agent run    --config spire.local.toml
```

不想建 venv 也可以退回免安装跑法（但跑的是全局 Python，容易和别的项目串味）：

```powershell
$env:PYTHONPATH = "packages/spire-core/src;packages/spire-agent/src"
python -m spire_agent run --config spire.local.toml
```

观战面板：`http://127.0.0.1:8788/`（只读、仅 loopback）。

没有远端 Laya 时可先用假的跑通链路：

```powershell
# a) 只验证客户端：进程内起个假 Laya，断言重试/缓存/兜底
.\.venv\Scripts\python.exe tools\laya_health.py --selftest

# b) 整条链路冒烟：另开一个终端常驻假 Laya（127.0.0.1:8000，回答是确定性的），
#    然后把 spire.local.toml 的 [laya] base_url 指向它，再跑 doctor / run
.\.venv\Scripts\python.exe tools\fake_laya_server.py --port 8000 --api-key ""
```

`fake_laya_server.py` **不是模型**（每题按 `criteria` 字典序取最小 key，`score` 题固定 `good`），
只用来验证 桥接→构题→请求→仲裁→执行→落盘→面板 这条链路；换真模型见 [docs/07-laya-contract.md](docs/07-laya-contract.md)。

### 采集人类数据（`observe_human`）

人类正常玩、agent 只观察，把真实选择记成**强标签**（完整 runbook 见 [docs/08-dataset.md](docs/08-dataset.md)）：

```powershell
# 0) 先离线看一眼数据长什么样（不连游戏、不连真 Laya），跑完读 .pytest-tmp/demo/
.\.venv\Scripts\python.exe tools\demo_observe_session.py --dump-row

# 1) 直接以 observe_human 开跑（不用改模组、不用重启游戏）
.\.venv\Scripts\python.exe -m spire_agent run --config spire.local.toml --mode observe_human

#    等价于把 spire.local.toml 里 [agent] mode 改成 "observe_human" 再起

# 2) 收工后导出
.\.venv\Scripts\python.exe -m spire_agent export --runs runs --out dataset
```

`[observe_human] also_query_model = true` 时每步会顺手问一次模型，落 `model_answer` / `agreement`
做对照；没有真 Laya 就用上面的 `fake_laya_server.py` 顶上（**只验证管线通了，不是真模型对照**）。

## 数据集导出

```powershell
.\.venv\Scripts\python.exe -m spire_agent export --runs runs --out dataset          # 生成 train/val/test.jsonl + manifest.json
.\.venv\Scripts\python.exe -m spire_agent export --runs runs --out dataset --verify # 确定性校验，只比对不重写
.\.venv\Scripts\python.exe -m spire_agent replay --run runs/run-0001                # 用已提交行重放构题，验证可复现
```

- `split` 按 **run/seed** 切分并写在行内（绝不按行随机切，否则相邻近重复状态会泄漏到 val）。
- 默认排除 `post_sl` 房间、`agent_fallback` 行、`matched=false` 的未命中行；
  加 `--keep-post-sl / --keep-fallback / --keep-unmatched` 即可保留（`spire_agent export` 与
  `dataset/export.py` 参数名一致）。原始记录永远留在 `runs/`，排除只影响导出。
- `--expand-noul` 能把一行 `choice` 无损展开成 N 行 `noul`（选中 true、其余 false），
  免费得到均衡的二分类样本。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest packages/spire-core/tests packages/spire-agent/tests -q
pwsh -File tools/test_mod.ps1     # 模组纯逻辑自检（无需游戏）
```

## 目录结构

```
docs/                    设计文档（阶段 0 交付物，全项目契约来源）
mod/                     Java 8 模组（ModTheSpire + BaseMod）
packages/spire-core/     纯逻辑：公平过滤 / 序列化 / 候选 / 构题 / 裁决（零依赖，可整包搬远端）
packages/spire-agent/    主循环 / 桥接 / Laya 客户端 / 采集 / 观战面板
tools/                   setup_dev / build_mod / install_mod / test_mod / decompile / javap_api /
                         laya_health（远端健康检查）/ fake_laya_server（本地假 Laya）
dataset/export.py        确定性导出脚本（产物 gitignore）
runs/                    对局记录（gitignore）
ref/                     反编译参考源（gitignore）
```

## v1 边界

- 目标 **STS1 v2.3.4 Steam 版**；默认 Ironclad / Ascension 0；**严格公平**（`fairness_mode="strict"`）。
- 不含 OCR 通道（只预留抽象接口）；不在线微调，只产数据集。
- 单机单局串行；不做 TLS / 不做并发多局。
- 微调流程与后续路线（虚拟思考链、OCR、评测）见 `docs/12-roadmap.md`。
