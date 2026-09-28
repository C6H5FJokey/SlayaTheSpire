<#
  Laya 微调一条龙（Windows）：build -> train -> eval -> 部署提示（见 docs\13-finetune.md）。

  用法（仓库根目录）：
    powershell -File tools\finetune_laya.ps1                        # 单卡，全部跑完
    powershell -File tools\finetune_laya.ps1 -Step build            # 只跑到某一步（all|build|train|eval）
    powershell -File tools\finetune_laya.ps1 -World 2               # 多卡：torchrun --nproc_per_node=2
    powershell -File tools\finetune_laya.ps1 -TrainArgs --freeze-encoder,--epochs,8

  前提：**训练与部署共用同一个 venv**（.venv-laya，见 docs\10-deployment.md）：
    powershell -File tools\setup_laya.ps1        # 建 .venv-laya（CUDA torch + laya[serve] + pytest）

  数据来源：先跑 `spire_agent export --runs runs --out dataset`（见 docs\08-dataset.md）。
  默认走仓库内的 HF 缓存（离线）；要联网解析 checkpoint 就加 -AllowDownload。

  和 Linux 的 finetune/run_all.sh 等价：同样读/写 dataset、finetune\out、
  finetune\checkpoints，参数一一对应（Python 侧脚本两份平台共用）。
#>
[CmdletBinding()]
param(
    [ValidateSet('all', 'build', 'train', 'eval')][string]$Step = 'all',
    [int]$World    = 1,
    [string]$Dataset    = 'dataset',
    [string]$Out        = 'finetune\out',
    [string]$Checkpoint = 'finetune\checkpoints\laya-spire-v1',
    [string]$Base       = 'convaiinnovations/laya',
    [string[]]$TrainArgs = @(),
    [switch]$AllowDownload
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $root '.venv-laya\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
    throw "还没装本机 Laya：先跑 powershell -File tools\setup_laya.ps1"
}

# checkpoint 统一放仓库内（和 tools\setup_laya.ps1 一致）：迁移/备份只要拷目录。
$env:HF_HOME = Join-Path $root '.cache\huggingface'
$env:PYTHONIOENCODING = 'utf-8'
if ($AllowDownload) {
    Remove-Item Env:HF_HUB_OFFLINE -ErrorAction SilentlyContinue
} else {
    $env:HF_HUB_OFFLINE = '1'
}

# 每个 python 调用都走这里：退出码非 0 立刻抛，不给"后面步骤拿着半成品继续跑"的机会。
function Invoke-Py {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$PyArgs)
    & $py @PyArgs
    if ($LASTEXITCODE -ne 0) { throw "python 失败（exit $LASTEXITCODE）：$($PyArgs -join ' ')" }
}

function Test-Step([string]$Name) { $Step -eq 'all' -or $Step -eq $Name }

Push-Location $root
try {
    Write-Host '== 环境' -ForegroundColor Cyan
    Invoke-Py -c "import sys, torch; print('python', sys.version.split()[0], 'torch', torch.__version__, 'cuda', torch.cuda.is_available(), 'gpus', torch.cuda.device_count())"

    if (Test-Step 'build') {
        $train = Join-Path $Dataset 'train.jsonl'
        if (-not (Test-Path -LiteralPath $train)) {
            throw "找不到 $train —— 先 spire_agent export --runs runs --out $Dataset（见 docs\08-dataset.md）"
        }
        Write-Host "== build：$Dataset -> $Out" -ForegroundColor Cyan
        Invoke-Py finetune\build.py --dataset $Dataset --out $Out --base $Base
    }

    if (Test-Step 'train') {
        $items = Join-Path $Out 'train_items.jsonl'
        if (-not (Test-Path -LiteralPath $items)) {
            throw "找不到 $items —— 先跑 -Step build（或把 -Out 指到已有产物）"
        }
        Write-Host "== train -> $Checkpoint（World=$World）" -ForegroundColor Cyan
        if ($World -gt 1) {
            # 多卡：torchrun 起 N 个进程；train.py 自己读 WORLD_SIZE/RANK/LOCAL_RANK，
            # 只有 rank0 写 checkpoint，等价批 = micro * accum * world。
            Invoke-Py -m torch.distributed.run --standalone "--nproc_per_node=$World" `
                finetune\train.py --items $items --out $Checkpoint --base $Base @TrainArgs
        } else {
            Invoke-Py finetune\train.py --items $items --out $Checkpoint --base $Base @TrainArgs
        }
    }

    if (Test-Step 'eval') {
        $dev = Join-Path $Out 'dev_records.jsonl'
        if (-not (Test-Path -LiteralPath $dev)) {
            Write-Warning "找不到 $dev，跳过 eval（用 -Step build 先产出记录）"
        } else {
            Write-Host '== eval：基座 vs 微调后（dev 片）' -ForegroundColor Cyan
            Invoke-Py finetune\eval.py --records $dev --base $Base `
                --checkpoint "base=$Base" --checkpoint "ft=$Checkpoint" `
                --json (Join-Path $Out 'eval.json')
        }
    }

    Write-Host ''
    Write-Host '== 部署（同一个 venv，同一个 checkpoint）' -ForegroundColor Green
    Write-Host "   .\.venv-laya\Scripts\python.exe finetune\serve.py --model $Checkpoint --check         # 先自检"
    Write-Host "   .\.venv-laya\Scripts\python.exe finetune\serve.py --model $Checkpoint --api-key <key>   # 起服务"
    Write-Host '   agent 侧：[laya] base_url = http://<host>:8000, model = laya-spire'
}
finally {
    Pop-Location
}