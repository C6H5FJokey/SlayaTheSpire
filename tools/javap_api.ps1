<#
  查一个类的公开 API（模组写代码时的"字典"）。

  用法：
    powershell -File tools/javap_api.ps1 com.megacrit.cardcrawl.characters.AbstractPlayer
    powershell -File tools/javap_api.ps1 com.megacrit.cardcrawl.ui.buttons.ProceedButton -All
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Class,
    [string]$StsDir = 'C:\Program Files (x86)\Steam\steamapps\common\SlayTheSpire',
    [string]$WorkshopDir = 'C:\Program Files (x86)\Steam\steamapps\workshop\content\646570',
    [string]$JdkHome = 'C:\Program Files\Java\jdk-21',
    [switch]$All,
    [switch]$Save
)

$ErrorActionPreference = 'Stop'
$javap = Join-Path $JdkHome 'bin\javap.exe'
$classpath = "$(Join-Path $StsDir 'desktop-1.0.jar');$(Join-Path $WorkshopDir '1605060445\ModTheSpire.jar');$(Join-Path $WorkshopDir '1605833019\BaseMod.jar')"

$flags = if ($All) { @('-p', '-constants') } else { @('-constants') }
$out = & $javap @flags -classpath $classpath $Class 2>&1
if ($Save) {
    $root = (Get-Location).Path
    $dir = Join-Path $root 'ref\javap'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $target = Join-Path $dir (($Class -replace '[.$]', '_') + '.txt')
    $out | Out-File -Encoding utf8 $target
    Write-Host "[javap] -> $target" -ForegroundColor Green
} else {
    $out
}