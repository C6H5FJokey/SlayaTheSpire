#!/usr/bin/env bash
# 远端一条龙：build -> train -> eval（见 docs/13-finetune.md）。
#
#   bash finetune/run_all.sh                 # 默认：单卡，全部跑完
#   WORLD=2 bash finetune/run_all.sh         # 多卡：走 torchrun --nproc_per_node=2
#   STEP=build bash finetune/run_all.sh      # 只跑到某一步（build|train|eval）
#   bash finetune/run_all.sh --freeze-encoder --epochs 8   # 额外参数原样传给 train.py
#
# 前提：**训练与部署共用同一个 venv**（默认 .venv-laya，见 docs/10-deployment.md）：
#   python3 -m venv .venv-laya && . .venv-laya/bin/activate
#   pip install torch --index-url https://download.pytorch.org/whl/cu128
#   pip install "laya[serve]" pytest
#
# 默认路径（都可用环境变量覆盖）：
#   PY=.venv-laya/bin/python  DATA=dataset  OUT=finetune/out  CKPT=finetune/checkpoints/laya-spire-v1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv-laya/bin/python}"
DATA="${DATA:-dataset}"
OUT="${OUT:-finetune/out}"
CKPT="${CKPT:-finetune/checkpoints/laya-spire-v1}"
STEP="${STEP:-all}"
WORLD="${WORLD:-1}"
BASE="${BASE:-convaiinnovations/laya}"

# checkpoint 统一放仓库内（跟 Windows 侧一致）：HF_HOME 落在仓库里，迁移/备份只要拷目录。
export HF_HOME="${HF_HOME:-$ROOT/.cache/huggingface}"
# 默认离线（权重已经在 HF_HOME 里）：挡住任何"偷偷联网"；要联网解析 checkpoint 就 HF_HUB_OFFLINE=0
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export PYTHONIOENCODING=utf-8

if [ ! -x "$PY" ]; then
  echo "找不到解释器 $PY —— 先把 .venv-laya 建好（见 docs/10-deployment.md）" >&2
  exit 1
fi

step_wanted() { [ "$STEP" = "all" ] || [ "$STEP" = "$1" ]; }

echo "== 环境"
"$PY" - <<'PYEOF'
import os, torch
print("python", os.sys.version.split()[0], "torch", torch.__version__, "cuda", torch.cuda.is_available(),
      "gpus", torch.cuda.device_count())
PYEOF

if step_wanted build; then
  echo "== build：$DATA -> $OUT"
  "$PY" finetune/build.py --dataset "$DATA" --out "$OUT" --base "$BASE"
fi

if step_wanted train; then
  echo "== train -> $CKPT（WORLD=$WORLD）"
  if [ "$WORLD" -gt 1 ]; then
    # 多卡：torchrun 起 N 个进程；train.py 自己读 WORLD_SIZE/RANK/LOCAL_RANK，
    # 只有 rank0 写 checkpoint，等价批 = micro * accum * world。
    "$PY" -m torch.distributed.run --standalone --nproc_per_node="$WORLD" \
      finetune/train.py --items "$OUT/train_items.jsonl" --out "$CKPT" --base "$BASE" "$@"
  else
    "$PY" finetune/train.py --items "$OUT/train_items.jsonl" --out "$CKPT" --base "$BASE" "$@"
  fi
fi

if step_wanted eval; then
  echo "== eval：基座 vs 微调后（dev 片）"
  "$PY" finetune/eval.py --records "$OUT/dev_records.jsonl" --base "$BASE" \
    --checkpoint "base=$BASE" --checkpoint "ft=$CKPT" --json "$OUT/eval.json"
fi

echo
echo "== 部署（同一个 venv，同一个 checkpoint）"
echo "   $PY finetune/serve.py --model $CKPT --check            # 先自检"
echo "   $PY finetune/serve.py --model $CKPT --api-key <key>    # 起服务"
echo "   agent 侧：[laya] base_url = http://<host>:8000, model = laya-spire"
