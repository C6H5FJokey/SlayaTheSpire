<#
  一次性环境：在仓库里建 .venv-laya，装「本机 Laya 服务」（CUDA torch + laya[serve] + pytest）。

  为什么和 .venv 分开：agent 侧的 .venv 只需要 httpx；torch 的 CUDA 轮子约 2.5 GB，
  混在一起只会让 agent 的环境又大又慢。两边依赖互不相干，分开升级互不影响。

  为什么优先用 uv：**不要**拿 Anaconda 的 python 建这个 venv。Anaconda 的 python312.dll 会把
  C:\ProgramData\anaconda3 里那份**旧的** VCRUNTIME140.dll 先载进进程（98 KB，System32 是
  123 KB），torch 的 c10.dll 在它上面初始化直接失败：
      OSError: [WinError 1114] ... Error loading "...\torch\lib\c10.dll"
  uv 的独立 CPython 自带匹配的 MSVC 运行库，没有这个坑。没有 uv 才回退到 `python -m venv`。

  用法（仓库根目录）：
    powershell -File tools/setup_laya.ps1                # 首次；已存在则只补装
    powershell -File tools/setup_laya.ps1 -Recreate      # 推倒重建
    powershell -File tools/setup_laya.ps1 -Cuda cu130    # 换 CUDA 轮子（默认 cu128）
    powershell -File tools/setup_laya.ps1 -NoModel       # 只装包，不下模型

  模型落在仓库内 .cache\huggingface（已 gitignore）。起服务见 tools\serve_laya.ps1。
  pytest 是给 finetune\tests 用的：**微调与部署共用这一个 venv**（见 docs\13-finetune.md）。
#>
[CmdletBinding()]
param(
    [string]$Python = 'python',
    [switch]$Recreate,
    [string]$Cuda   = 'cu128',
    [string]$Torch  = '2.9.1',
    [string]$Proxy  = '',
    [switch]$NoModel
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv-laya'
$py   = Join-Path $venv 'Scripts\python.exe'

# 沙箱/CI 会注入 PIP_NO_INDEX=1 和一个指向死端口的代理；先清掉，再换成能用的代理。
Remove-Item Env:PIP_NO_INDEX -ErrorAction SilentlyContinue
Remove-Item Env:PIP_DISABLE_PIP_VERSION_CHECK -ErrorAction SilentlyContinue

if (-not $Proxy) {
    $Proxy = $env:SLASPIRE_PROXY
    if (-not $Proxy) {
        $reg = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
        if ($reg -and $reg.ProxyServer) { $Proxy = $reg.ProxyServer }
    }
    if ($Proxy -and $Proxy -notmatch '^https?://') { $Proxy = "http://$Proxy" }
}
if ($Proxy) {
    $env:HTTP_PROXY = $Proxy
    $env:HTTPS_PROXY = $Proxy
    $env:ALL_PROXY = $Proxy
    Write-Host "[laya] proxy -> $Proxy" -ForegroundColor Cyan
}

function Invoke-Pip {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$PipArgs)
    $cmd = @('-m', 'pip', 'install', '--disable-pip-version-check') + $PipArgs
    if ($Proxy) { $cmd += @('--proxy', $Proxy) }
    & $py @cmd
    if ($LASTEXITCODE -ne 0) { throw "pip failed (exit $LASTEXITCODE): $($PipArgs -join ' ')" }
}

if ($Recreate -and (Test-Path -LiteralPath $venv)) {
    Write-Host "[laya] remove $venv" -ForegroundColor Yellow
    Remove-Item -Recurse -Force -LiteralPath $venv
}
if (-not (Test-Path -LiteralPath $py)) {
    $uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if ($uv) {
        # 解释器和它的运行库都放进仓库内，彻底绕开 Anaconda
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $root '.cache\uv-python'
        $env:UV_CACHE_DIR = Join-Path $root '.cache\uv-cache'
        Write-Host "[laya] uv venv（独立 CPython 3.12）-> $venv" -ForegroundColor Cyan
        & $uv venv $venv --python 3.12 --python-preference only-managed --seed
    } else {
        Write-Warning 'uv 不在 PATH 上，回退到 python -m venv。'
        Write-Warning '若解释器来自 Anaconda，torch 可能报 WinError 1114（c10.dll 初始化失败）：'
        Write-Warning '那是 Anaconda 的旧 VCRUNTIME140.dll 抢先载入所致，请改用 uv 或独立 CPython。'
        & $Python -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) { throw "创建 venv 失败（exit $LASTEXITCODE）" }
}

# CUDA 版 torch 只在 download.pytorch.org 上；PyPI 的 win_amd64 轮子是 CPU-only（124 MB）。
Write-Host "[laya] torch $Torch+$Cuda" -ForegroundColor Cyan
Invoke-Pip "torch==$Torch" '--index-url' "https://download.pytorch.org/whl/$Cuda"

Write-Host '[laya] laya[serve]' -ForegroundColor Cyan
Invoke-Pip 'laya[serve]'

# pytest 只给 finetune\tests 用；微调与部署共用这个 venv，装在这里省得两头版本对不上。
Write-Host '[laya] pytest（跑 finetune\tests）' -ForegroundColor Cyan
Invoke-Pip 'pytest'

if (-not $NoModel) {
    $env:HF_HOME = Join-Path $root '.cache\huggingface'
    New-Item -ItemType Directory -Force -Path $env:HF_HOME | Out-Null
    Write-Host "[laya] checkpoint -> $env:HF_HOME" -ForegroundColor Cyan
    & $py -c "from huggingface_hub import snapshot_download; print(snapshot_download('convaiinnovations/laya'))"
    if ($LASTEXITCODE -ne 0) { throw "checkpoint download failed (exit $LASTEXITCODE)" }
}

# 立刻验一次：这一句就是用来挡住"包装上了但 torch 根本起不来"的。
& $py -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"
if ($LASTEXITCODE -ne 0) { throw "torch 起不来（见上面的报错）" }

Write-Host "[laya] ok -> $py" -ForegroundColor Green
Write-Host '[laya] 起服务：powershell -File tools\serve_laya.ps1' -ForegroundColor Cyan
