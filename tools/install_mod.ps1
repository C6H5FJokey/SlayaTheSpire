<#
  把构建好的模组 jar 复制进 <STS>\mods\（会先备份同名旧文件）。

  用法：
    pwsh -File tools/install_mod.ps1
    pwsh -File tools/install_mod.ps1 -Jar mod\build\spireagent.jar
#>
[CmdletBinding()]
param(
    [string]$StsDir = 'C:\Program Files (x86)\Steam\steamapps\common\SlayTheSpire',
    [string]$Jar = 'mod\build\spireagent.jar'
)

$ErrorActionPreference = 'Stop'
$root = (Get-Location).Path
$source = Join-Path $root $Jar
if (-not (Test-Path -LiteralPath $source)) {
    throw "jar not found: $source (run tools/build_mod.ps1 first)"
}
$modsDir = Join-Path $StsDir 'mods'
New-Item -ItemType Directory -Force -Path $modsDir | Out-Null
$target = Join-Path $modsDir 'SlayaTheSpireAgent.jar'

if (Test-Path -LiteralPath $target) {
    $backup = "$target.bak"
    Move-Item -Force -LiteralPath $target -Destination $backup
    Write-Host "[install] previous jar backed up -> $backup" -ForegroundColor Yellow
}
Copy-Item -LiteralPath $source -Destination $target -Force
Write-Host "[install] ok -> $target" -ForegroundColor Green
Write-Host '[install] 需要在 ModTheSpire 启动器里勾选 SlayaTheSpire Agent 后启动游戏' -ForegroundColor Cyan