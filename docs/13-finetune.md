# 13 微调

把 `dataset/` 里的 `(state, questions) -> answer` 行**编译成 Laya 训练项**、按官方 RLCD 配方微调、
在留出记录上离线评测，再用**同一个虚拟环境**把微调后的 checkpoint 起成服务 —— agent 侧只改
`[laya] base_url` 与 `model` 两个字段，客户端一行不用动。

本页是**实现契约**：脚本参数、产物格式、默认值与已知坑。动机、采集与迭代节奏见
[12-roadmap §3](12-roadmap.md#3-微调流程)，数据本身的格式见 [08-dataset](08-dataset.md)。

## 定位：为什么训练脚本单独一层

`dataset/` 是**数据契约**（人读的、与模型无关），训练项是**模型契约**（分词后的 ids、选项下标、
目标分布）。两者之间有一条**必须唯一、可复现、可校验**的翻译规则，所以它单独成 `build.py`
一层，而不是散在训练循环里：

- 同一份 `dataset/` 编译两次必须**逐字节相同**（`--verify` 就是查这个）；
- 训练与部署必须走**同一个** `laya.common.build_sequence`，否则训练时看到的概率
  不再是线上给的概率；
- 任何"跳过的行"都要有**具名理由**并计入 `build_manifest.json`，不做静默丢弃。

## 脚本一览

| 脚本 | 作用 | 依赖 |
|---|---|---|
| `finetune/ft_data.py` | 纯逻辑：转换 / 跳过规则 / 切分 / 长度统计 / checkpoint 解析（被下面几个脚本共用，不直接跑） | 无 torch |
| `finetune/build.py` | 数据集契约 -> 模型契约（分词、选项下标、目标分布、长度统计） | `.venv-laya` |
| `finetune/train.py` | RLCD 微调 + 训练后校准，产出可直接部署的 checkpoint | `.venv-laya` + GPU（CPU 也能跑） |
| `finetune/eval.py` | 在留出记录上评「基座 / 微调后 / 采集时落盘分布」 | `.venv-laya` |
| `finetune/serve.py` | 用**微调 checkpoint 目录**起 `/v1/systemone` | `.venv-laya` |
| `finetune/run_all.sh` | Linux 一条龙：build -> train -> eval + 部署提示 | bash |
| `tools\finetune_laya.ps1` | Windows 一条龙（与 `run_all.sh` 等价，参数一一对应） | `.venv-laya` |
| `finetune/tests/` | 离线测试：纯逻辑 + 小 checkpoint 端到端（见下「测试」） | 见下 |

所有 Python 都用**解释器全路径**调用（`.\.venv-laya\Scripts\python.exe`，Linux 是
`.venv-laya/bin/python`），不需要激活、不需要 `PYTHONPATH`。

## 一条龙

Windows（仓库根目录）：

```powershell
# 0) 数据先导出（见 docs/08-dataset.md）
.\.venv\Scripts\python.exe -m spire_agent export --runs runs --out dataset

# 1) 一条龙：build -> train -> eval -> 打印部署命令
powershell -File tools\finetune_laya.ps1
powershell -File tools\finetune_laya.ps1 -Step build                  # 只 build（all|build|train|eval）
powershell -File tools\finetune_laya.ps1 -World 2                     # 多卡（torchrun）
powershell -File tools\finetune_laya.ps1 -TrainArgs --freeze-encoder,--epochs,8
```

Linux / 远端：

```bash
bash finetune/run_all.sh
STEP=build bash finetune/run_all.sh
WORLD=2 bash finetune/run_all.sh
bash finetune/run_all.sh --freeze-encoder --epochs 8      # 多余参数原样传给 train.py
```

`run_all.sh` 的环境变量覆盖（默认值在括号里）：`PY`（`.venv-laya/bin/python`）、`DATA`（`dataset`）、
`OUT`（`finetune/out`）、`CKPT`（`finetune/checkpoints/laya-spire-v1`）、`STEP`（`all`）、`WORLD`（`1`）、
`BASE`（`convaiinnovations/laya`）、`HF_HOME`（仓库内 `.cache/huggingface`）、
`HF_HUB_OFFLINE`（`1`；设 `0` 才允许联网解析 checkpoint）。

两个脚本都只做**最小限度的前置检查**：`build` 前查 `dataset/train.jsonl` 在不在，`train` 前查
`finetune/out/train_items.jsonl` 在不在，`eval` 前查 `dev_records.jsonl` 在不在（不在就跳过并告警）。
缺东西时给出**该先跑哪条命令**，而不是抛一堆 traceback。

## 训练与部署共用同一个 venv

**硬规则**：微调与部署都用 `.venv-laya`，版本不许漂移。

理由不是"省事"，而是**概率必须对得上**：`build.py` 的 `build_sequence`、`train.py` 的校准、
`serve.py` 的服务端解码，三者都调用**同一个 `laya 0.3.20` 实现**。温度夹取区间（`[0.5, 5]`）、
选项渲染、`state` 序列化只要有一处版本不同，训练时算出来的分布就不是线上给的那个。

`.venv`（agent 侧）**不装 torch/laya**：它只要 `httpx` 就能跑。两边互不干扰，
`tools\setup_laya.ps1` 负责建 `.venv-laya`（CUDA torch + `laya[serve]` + `pytest`）。

本仓库实测版本组合（`finetune_report.json` 的 `environment` 字段会照抄一份）：

| 组件 | 版本 |
|---|---|
| CPython | 3.12 |
| torch | 2.9.1+cu128 |
| laya | 0.3.20 |
| transformers | 5.17.0（随 laya 而定） |
| pytest | 9.1.1（跑 `finetune/tests` 用） |

checkpoint 缓存落在**仓库内** `.cache\huggingface`（gitignore）：迁移到远端只要整个目录拷过去，
再把 `HF_HOME` 指到它，`HF_HUB_OFFLINE=1` 就能完全离线训练（脚本默认就是离线）。

## 1. build：数据集契约 -> 模型契约

```powershell
.\.venv-laya\Scripts\python.exe finetune\build.py --dataset dataset --out finetune\out
.\.venv-laya\Scripts\python.exe finetune\build.py --dataset dataset --out finetune\out --verify
```

### 产物（每行一个 JSON，无缩进、键序固定）

| 文件 | 内容 |
|---|---|
| `{split}_items.jsonl` | 训练项：`ids` / `markers` / `qtype` / `target` / `weight` / `seq_len` / `state_tokens` / `state_kept` |
| `{split}_records.jsonl` | 记录：`state` + `questions`（Jev 形态，逐字节等于采集时的那一份）+ `gold`（含选项 key 顺序） |
| `build_manifest.json` | 参数 / 输入 sha256 / 计数 / 跳过理由 / 长度统计 / 产物 sha256 / 告警 |

`{split}` 有四个：`train` / `val` / `test` 来自**行内写死的 `split` 字段**（按 run 切分，见
[08-dataset](08-dataset.md)）；`dev` 是 `build` 从 `train` 里**按房间**再切出来的一份训练期留出
（`--dev-frac`，默认 0.15，`--dev-seed` 固定）。切在**房间**上而不是行上，否则同一间房的
相邻近重复状态会漏进 dev。

`train.py` 只吃 `train_items.jsonl`；`eval.py` 通常吃 `dev_records.jsonl`。

### 跳过规则（每条都记进 `manifest.counts.skipped`）

| 理由码 | 什么时候跳 |
|---|---|
| `source_not_selected` | `source` 不在 `--sources` 里（默认只收 `human`） |
| `agent_fallback` | 该行是规则兜底产物（`meta.agent_fallback`） |
| `unmatched` | 人类动作没匹配上任何候选（`matched=false`） |
| `post_sl_room` | 读档重记的房间，标签被非公平信息污染 |
| `no_label` | 没有标签 |
| `label_not_in_options` | 标签不在候选里（枚举缺口，值得回查） |
| `too_few_options` | 候选数 < `--min-options`（默认 2；每题 softmax 恒为 1，没有梯度） |
| `no_question` | 该行没有可用问题 |
| `multi_label` | 标签是真·多选（一次动作选多张），一道 `choice` 表达不了 —— 宁可跳过也不给错的答案空间 |

正常导出的 `dataset/` 已经把 `post_sl` / `agent_fallback` / `unmatched` 过滤过一遍（见
[08-dataset](08-dataset.md)），这层是**防御性复查**：直接喂未过滤的行也不会静默进训练集。

### 权重与软标签

- `--weight-human`（1.0）/ `--weight-agent`（0.25）：弱标签降权，默认值就是按"人类一局的价值
  高于自博弈一局"定的；
- `--soft-mix x > 0`：目标 = `(1-x) * 人类 one-hot + x * 采集时落盘的基础模型分布`
  （分布对不齐 key 时**静默退回 one-hot**，并记 `gold[qid].soft_mix_used`）。默认 `0`，纯人类 one-hot。
  agent 采的数据建议开一点（弱标签本身就是分布）。

### 预算：`--max-len` / `--head-max-len` 必须与部署一致

- `--max-len`（默认 2048）= 部署时的序列总长；`--head-max-len`（默认 320）= 题面 + 选项的 token 预算。
- 超长时**只截 state，不截选项**：选项被截就是把答案空间切掉，那是错题。
- `train.py` 会拿 `build_manifest.json` 里的这两个值**对比自己的参数，不一致就拒跑**（除非 `--force`）——
  这是防止"用 2048 训、用 512 部署"这类静默错配的第一道闸。

**为什么默认是 2048 而不是 512**：Laya 的 `english` checkpoint 自带 `max_len = 512` / `head_max_len = 192`
（见 [07-laya-contract](07-laya-contract.md)），但本项目的 `state` 单独就已经 **99% 超过 512 token**
（中位数 1725）。512 窗口等于"模型看不到大半张场面"，所以编译训练项与部署统一按 2048 / 320 走，
微调时把这两个值写进新的 `rl_agent_config.json`。

ModernBERT 的 `max_position_embeddings` 本来就是 8192，所以这不是架构限制，只是**把 checkpoint 的
部署配置从 512 提到 2048 并让模型在这个长度上适配**；想更省心也可以直接用 `multilingual` /
`typed-decisions`（自带 1024）。这是**有意的取舍**，不是漏配。

### 确定性（`--verify`）

`build` 从头重算一遍，与磁盘上的产物**逐字节比对**（含 `manifest` 里的输入 sha256）。用于：
排查"是不是手改过 jsonl"、确认换机器后结果一致、CI 里挡住不确定的改动。
本仓库实测：737 行 -> 698 记录 -> 698 项，重跑 sha256 完全一致。

## 2. train：RLCD + 训练后校准

```powershell
.\.venv-laya\Scripts\python.exe finetune\train.py --dry-run                    # 只看计划，不训练
.\.venv-laya\Scripts\python.exe finetune\train.py --freeze-encoder `
    --out finetune\checkpoints\laya-spire-v1
```

### 配方（与 Laya 官方 finetune 文档对齐）

1. **RLCD**：每题采样 G 个「加噪 logit」（`--samples`，默认 4；噪声 σ 随 epoch 从
   `--sigma-start` 0.4 退火到 `--sigma-end` 0.1），用**严格恰当评分规则**（log + spherical + RPS，
   默认 `--w-sph 0.75 --w-rps 1.0`）当奖励做 GRPO 式策略梯度，再叠一份**全权重 soft cross-entropy**。
2. **优化器**：AdamW，编码器 lr 2.5e-5 / 决策头 lr 1e-4，cosine，梯度裁剪 1.0，默认 4 epoch。
3. **可部署**：产物就是一份普通 Laya checkpoint，`laya.load(path)` 直接能载。

### 校准（训练**前**留出，训练**后**拟合）

- 训练**开始前**先从 `train_items` 里留一份校准片（默认 `--calib-frac 0.1`，上限 `--calib-max 400`，
  `--calib-seed` 固定）——**必须在训练前切**，否则这 400 项已经进过梯度，拟出来的温度是过拟合的；
- 训练后在它上面按 `(题型, 候选数档)` 拟合温度，写进 `rl_agent_config.json`；
- 某档样本少于 `--min-bucket-n`（默认 30）就**退回题型温度**（记为 `used_type_temperature: true`）；
- 拟合值用 `laya.common.clamp_temperature` 夹到 **`[0.5, 5]`** —— 服务端也夹这一区间，训练侧写什么就生效什么；
- **旧 checkpoint 自带的 `temperature_by_options` 一律清掉**：它在推理端优先级**高于**题型温度，
  不清就会静默顶掉新拟合值。这是本项目踩过的坑，`train.py` 现在无条件清空并写新的。

### 产物

```
finetune/checkpoints/<name>/
  rl_agent_config.json     部署读的那份（max_len / head_max_len / model_name / 温度 / fine_tuned）
  model.safetensors        权重
  encoder/                 编码器 config（尺寸/层数）
  tokenizer/               分词器（训练/服务两侧必须同一份）
  finetune_report.json     参数、校准明细、build_params、环境版本、耗时（可追溯，训练**不读**它）
  resume.pt                续跑点（只有中断/`--save-every` 时在；正常跑完最后一步会删掉）
```

### 资源与建议

| 项 | 数字（本仓库实测） |
|---|---|
| 参数量 | 421.3M（`english` / ModernBERT-large + 2 层决策头） |
| 单卡（RTX 4070 SUPER 12 GB） | bf16 + `--grad-checkpoint encoder`（默认）；显存吃紧就调小 `--micro-batch` / `--max-tokens-per-batch` |
| 有效 micro-batch | `min(--micro-batch, --max-tokens-per-batch / --max-len)`；默认 2048 序列下是 `min(8, 4) = 4`，等价批 `4 * 4 = 16` 题 |
| 本仓库训练计划（`--dry-run`） | 训练项 521（校准片 57），每 epoch 33 步，4 epoch 共 132 步 |
| 冒烟 | `--limit-items 16 --epochs 2`（2 步）：约 19 秒，显存 < 8 GB |

**数据量小（本项目目前只有一局 ~700 行）时建议 `--freeze-encoder`**：只训决策头
（实测 **26.5M / 421.3M** 可训练），别拿几百个样本去动 3.95 亿参数的编码器。

### 续跑与 Windows 上的重命名坑

- `--save-every N` 每 N 步写一次 `resume.pt`，配 `--resume` 从它续跑；
- **Windows 特有**：`resume.pt` 有 5 GB 量级时，`os.replace` 落盘可能撞
  `WinError 32/5`（刚写完的文件被搜索索引/杀毒短暂持有句柄）。`train.py` 的处理是
  **指数退避重试 + 失败就跳过这一次存盘并继续训练**，绝不因为存续跑点失败而弄丢整个训练；
- 跑完之后**不再写**最后一步的续跑点（同一次崩溃窗口里没必要），正常结束会清掉 `resume.pt`。

### 多卡（DDP）

```bash
WORLD=2 bash finetune/run_all.sh                     # torchrun --standalone --nproc_per_node=2
python -m torch.distributed.run --standalone --nproc_per_node=2 finetune/train.py \
    --items finetune/out/train_items.jsonl --out finetune/checkpoints/laya-spire-v1
```

`train.py` 自己读 `WORLD_SIZE` / `RANK` / `LOCAL_RANK`：**只有 rank0 写 checkpoint**，
等价批 = `micro * accum * world`，backend 默认 `cuda -> nccl` / 其它 `gloo`。
`--device cpu --backend gloo` 是纯 CPU 的多进程路径（测试就是这么跑的）。

## 3. eval：基座 vs 微调 vs 采集时分布

```powershell
.\.venv-laya\Scripts\python.exe finetune\eval.py `
    --records finetune\out\dev_records.jsonl `
    --checkpoint base=convaiinnovations/laya `
    --checkpoint ft=finetune\checkpoints\laya-spire-v1 `
    --json finetune\out\eval.json
```

`--checkpoint` 两种写法：`NAME=PATH`（加载 checkpoint，目录或能被解析的 HF repo id）、
裸 `recorded`（用数据集行里 `answers` 落下的**采集时基础模型**概率，不占显存）。不给 `--checkpoint`
时默认跑 `base=<--base>`（有 recorded 就再加一个 `recorded` 变体）。

序列与温度都按**部署口径**算：构题走 `laya.common.build_sequence`，温度按 `(题型, 候选数档)`
取该 checkpoint 的 `temperature_by_options`（与 `serve.py` / `laya` 服务端解码同一条规则），
所以这里报的概率就是线上 `/v1/systemone` 会给的概率。序列长度默认从同目录的
`build_manifest.json` 读；**读不到就直接报错让你显式传 `--max-len/--head-max-len`**，从不猜。

| 指标 | 含义 |
|---|---|
| 准确率 | argmax 是否落在人类选择上 |
| NLL / Brier | 概率质量有没有放在正确答案上，越小越好 |
| ECE(`answer_confidence`) | 置信度与正确率的偏差（15 桶）；**统计一律用这个量** |
| 平均置信度 | 明显偏高就是过自信，要和 ECE 一起看 |
| NLL@T=1 -> NLL@T | checkpoint 自带的温度这一层到底有没有用（仅对有 logits 的变体） |
| p50/p95 延迟 | 逐题端到端（含分词），给部署预算用 |
| 与 recorded 的一致率 | 与「采集时基础模型的选择」相同的比例 —— 路线图里 `agreement_rate` 的离线版 |

分层结果（`by_decision_point` / `by_bucket` / `by_qtype`）只在 `--json` 里给，够细但不糊屏幕。

## 4. serve：用微调后的 checkpoint 起服务

```powershell
# 自检：加载 checkpoint，发一道最小题，确认能答（不启服务，退出码即结果）
.\.venv-laya\Scripts\python.exe finetune\serve.py --model finetune\checkpoints\laya-spire-v1 --check

# 起服务：只监听 loopback；跨机必须配 --api-key
.\.venv-laya\Scripts\python.exe finetune\serve.py --model finetune\checkpoints\laya-spire-v1 --api-key <key>
```

**为什么不用 `laya-serve`**：`laya.serve` 的 Router 只认内置 checkpoint 名
（`english` / `multilingual` / `typed-decisions`），**喂不进一个本地目录**。`serve.py` 直接
`laya.load(<目录>)`，HTTP 外壳、错误码、请求预算守卫全部对齐 `laya.serve`（同一份
`_check_request_limits` / `MAX_BODY_BYTES`，单 worker 线程池 + 一把锁串行推理）。

两条硬规则：

1. **只服务本地目录**：`--model` 必须是含 `rl_agent_config.json` 的**已存在目录**，路径不会被
   当成 HF repo id，也就不会在启动时偷偷联网下载；非环回地址而没配 key 时**在解析模型之前**就拒绝；
2. **与训练同一个 venv**：见上「单 venv 规则」。

环境变量（尽量与 `laya-serve` 同名，运维不用记两套）：`LAYA_MODEL` / `LAYA_HOST` / `LAYA_PORT` /
`LAYA_DEVICE` / `LAYA_API_KEY` / `LAYA_THREADS` / `LAYA_PRELOAD` / `LAYA_LOG_LEVEL` /
`LAYA_MAX_LEN` / `LAYA_HEAD_MAX_LEN`；命令行参数优先于环境变量。

> 实现注意：`serve.py` **故意不写** `from __future__ import annotations` —— PEP 563 会让 FastAPI
> 解析不了 `Request` 注入，每个 POST 都 422。这个坑有专门的回归测试（`tests/test_serve.py`）。

agent 侧切过去只要两个字段（见 [10-deployment](10-deployment.md#agent-侧配置)）：

```toml
[laya]
base_url = "http://<host>:8000"
api_key  = "…"
model    = "laya-spire"      # 与 serve 的 --name / checkpoint 里的 model_name 一致
```

## 本项目当前数据的实况（只有一局）

| 事实 | 数字 | 后果 |
|---|---|---|
| 行 / 记录 / 训练项 | 737 / 698 / 698 | 一局人类对局（`run-0001`），全部 `choice` 题 |
| train / dev | 578 / 120 | `dataset/val.jsonl`、`test.jsonl` **是空的** |
| 丢弃 | `too_few_options: 39` | 只有 1 个候选的题（softmax 恒为 1） |
| state 字符数 | p50 4407 / max 8001；42 行 > 6000 | 超过 `prefer_multilingual_over_chars` 的自动切换阈值 |
| state token 数 | p50 1725 / p90 2211 / max 2970 | **99% 超过 512**；`--max-len 2048` 下仍截掉 83986 token（保留 93%，**271 题 state 不完整**） |
| state 含非 ASCII | 33 / 737 行（train 分片 22） | 里面是 `[对话]` 之类的中文占位 —— **英文 backbone 读不了中文**，建议先在序列化层清掉 |
| 决策点分布 | `combat_play` 616，其余 8 类共 82 | 战斗样本压倒性多，商店/事件/篝火严重不足 |
| 基座在 dev 上的准确率（`eval.py --checkpoint recorded`） | 0.217（NLL 2.367，ECE 0.246，平均置信度 0.423） | **微调要打败的起点**；ECE 远大于 0 说明基座明显过自信 |

由此有两条**必须记住**的结论：

1. **`dev` 不是泛化上界**。它是同一局、同一角色、同一牌风里切出来的房间，数字偏乐观；
   `val/test` 为空意味着**没有跨局留出**。想拿真数字，再跑一局（换个 seed / 角色）重导即可，
   `build` 会自动把它切进 `val`/`test`。
2. **截断是真的在丢信息**。先看 `build_manifest.json` 的 `item_stats.state_kept_mean`
   （现在 0.95，最少 0.60）：要么调大 `--max-len`，要么压 state 序列化
   （见 [05-state-schema](05-state-schema.md#预算与压缩)）。

## 测试

`finetune/tests/` 与外部环境解耦，不连游戏、不联网（HF 缓存指向仓库内、`HF_HUB_OFFLINE=1`），
用一份**缩小的真 checkpoint**（ModernBERT-large 的 config，hidden 64 / 2 层）跑端到端。

```powershell
# 全量（需要 torch / laya / pytest，即 .venv-laya）
.\.venv-laya\Scripts\python.exe -m pytest finetune\tests -q

# 只跑纯逻辑（.venv 里也行：torch 相关的会自动 skip）
.\.venv\Scripts\python.exe -m pytest finetune\tests -q -p no:cacheprovider
```

在仓库根直接跑 `pytest` 会把 `packages/` 与 `finetune/` 一起收（`testpaths` 写在根目录 `pytest.ini`
里；finetune 的测试文件名不与 `packages/**/tests` 重名，否则会撞 import mode 的「import file mismatch」）。

覆盖：转换与跳过规则、软标签、dev 切分、JSONL 确定性、sha256、长度统计、checkpoint 解析、
`build --verify`、`train` 的校准与「max_len 不一致就拒跑」、`eval` 的 JSON 口径与缺 manifest 报错、
`serve` 的 health/auth/400/413/500 不泄漏内部信息/路由标记/环境变量优先级/非环回必须配 key、
以及 DDP 的「只有 rank0 写盘」。

已知 **skip**：真两进程 `gloo` 训练在这台 Windows 上跑不起来
（`makeDeviceForHostname(): unsupported gloo device`，本机 hostname 解析到 28.0.0.0）——
测试会自动跳过并在 Linux 上真正执行；另有**不依赖 gloo** 的 in-process 版本在两边都跑。

## 相关文档

- 数据契约与采集 -> [08-dataset](08-dataset.md)
- 路线图与迭代节奏 -> [12-roadmap](12-roadmap.md#3-微调流程)
- 部署与 agent 侧配置 -> [10-deployment](10-deployment.md)
- 调用契约（题型 / 预算 / 错误码） -> [07-laya-contract](07-laya-contract.md)
- 验收清单 -> [11-testing](11-testing.md)