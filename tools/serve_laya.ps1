<#
  在本机起 Laya HTTP 服务（用 tools\setup_laya.ps1 装好的 .venv-laya）。

  用法：
    powershell -File tools\serve_laya.ps1                          # 127.0.0.1:8000，只预加载 english
    powershell -File tools\serve_laya.ps1 -Models english,multilingual
    powershell -File tools\serve_laya.ps1 -ApiKey <key>             # 设了之后 agent 侧要填同一个 key
    powershell -File tools\serve_laya.ps1 -AllowDownload            # 允许联网解析 checkpoint

  默认只监听 loopback。要跨机访问必须自己改 -Bind，并且一定配上 -ApiKey。
  checkpoint 在 setup 阶段就整仓下好了，所以默认 **离线** 起（HF_HUB_OFFLINE=1）：
  启动快、不吃代理，网络抖动也不会影响服务。
#>
[CmdletBinding()]
param(
    [string]$Bind     = '127.0.0.1',
    [int]$Port        = 8000,
    [string]$Models   = 'english',
    [string]$ApiKey   = '',
    [string]$Device   = 'cuda',
    [string]$LogLevel = 'info',
    [switch]$NoPreload,
    [switch]$AllowDownload
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $root '.venv-laya\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
    throw "还没装本机 Laya：先跑 powershell -File tools\setup_laya.ps1"
}

$env:HF_HOME        = Join-Path $root '.cache\huggingface'
$env:LAYA_HOST      = $Bind
$env:LAYA_PORT      = "$Port"
$env:LAYA_DEVICE    = $Device
$env:LAYA_MODELS    = $Models
$env:LAYA_PRELOAD   = if ($NoPreload) { '0' } else { '1' }
$env:LAYA_LOG_LEVEL = $LogLevel
if ($ApiKey) { $env:LAYA_API_KEY = $ApiKey }

if ($AllowDownload) {
    Remove-Item Env:HF_HUB_OFFLINE -ErrorAction SilentlyContinue
} else {
    $env:HF_HUB_OFFLINE = '1'
}

Write-Host "[laya] $Device  $Bind`:$Port  models=$Models  offline=$(-not $AllowDownload)  HF_HOME=$env:HF_HOME" -ForegroundColor Cyan
& $py -m laya.serve
