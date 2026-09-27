<#
  一次性开发环境：在仓库里建 .venv，并以**可编辑模式**装上 spire-core / spire-agent。

  用法（仓库根目录，或任意目录都行）：
    pwsh -File tools/setup_dev.ps1              # 首次；已存在则只补装
    pwsh -File tools/setup_dev.ps1 -Recreate    # 推倒重建

  装完之后**不需要**激活、也不需要 PYTHONPATH，直接用 venv 里的解释器就行：
    .\.venv\Scripts\python.exe -m spire_agent run --config spire.local.toml
    .\.venv\Scripts\python.exe -m pytest packages/spire-core/tests packages/spire-agent/tests -q

  需要联网（装 httpx / pytest）。可编辑模式意味着改 src 立刻生效，不用重装。
#>
[CmdletBinding()]
param(
    [string]$Python = 'python',
    [switch]$Recreate
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$py = Join-Path $venv 'Scripts\python.exe'

if ($Recreate -and (Test-Path -LiteralPath $venv)) {
    Write-Host "[dev] remove $venv" -ForegroundColor Yellow
    Remove-Item -Recurse -Force -LiteralPath $venv
}

if (-not (Test-Path -LiteralPath $py)) {
    Write-Host "[dev] venv -> $venv" -ForegroundColor Cyan
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed (exit $LASTEXITCODE)" }
}

Write-Host '[dev] pip install -e spire-core -e spire-agent pytest' -ForegroundColor Cyan
& $py -m pip install -e (Join-Path $root 'packages\spire-core') -e (Join-Path $root 'packages\spire-agent') pytest
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

Write-Host "[dev] ok -> $py" -ForegroundColor Green
Write-Host '[dev] 在仓库根目录跑：.\.venv\Scripts\python.exe -m spire_agent doctor --config spire.local.toml' -ForegroundColor Cyan
