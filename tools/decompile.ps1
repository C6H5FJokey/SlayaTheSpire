<#
  把 desktop-1.0.jar 解到 ref/game-src/（class 文件 + javap 反汇编文本）。

  不做真正的反编译：本机没有 CFR/procyon，也不联网下载。`javap -p -c` 已经够用 ——
  STS 的 jar 未混淆，类名/方法名/字段名全在，字节码也很直白（见 ref/javap/*.txt）。

  产物（全部在 ref/ 下，已 gitignore）：
    ref/game-src/           解压后的 .class 树，供 `rg --text` 搜常量与字符串
    ref/javap/*.txt         逐个类的 javap -p 输出（签名一览）
    ref/javap_bc_<cls>.txt  需要细看字节码的类的 javap -p -c 输出

  用法：
    powershell -File tools/decompile.ps1                     # 解压 + 常用类的签名表
    powershell -File tools/decompile.ps1 -Class com.megacrit.cardcrawl.dungeons.AbstractDungeon -Bytecode
#>
[CmdletBinding()]
param(
    [string]$StsDir = 'C:\Program Files (x86)\Steam\steamapps\common\SlayTheSpire',
    [string]$WorkshopDir = 'C:\Program Files (x86)\Steam\steamapps\workshop\content\646570',
    [string]$JdkHome = 'C:\Program Files\Java\jdk-21',
    [string[]]$Class = @(),
    [switch]$Bytecode,
    [switch]$SignatureOnly
)

$ErrorActionPreference = 'Stop'
$root = (Get-Location).Path
$javap = Join-Path $JdkHome 'bin\javap.exe'
$jar = Join-Path $JdkHome 'bin\jar.exe'
$gameJar = Join-Path $StsDir 'desktop-1.0.jar'
$mtsJar = Join-Path $WorkshopDir '1605060445\ModTheSpire.jar'
$baseModJar = Join-Path $WorkshopDir '1605833019\BaseMod.jar'
$classpath = "$gameJar;$mtsJar;$baseModJar"

$gameSrc = Join-Path $root 'ref\game-src'
if (-not (Test-Path -LiteralPath $gameSrc) -or -not (Get-ChildItem -LiteralPath $gameSrc -ErrorAction SilentlyContinue)) {
    New-Item -ItemType Directory -Force -Path $gameSrc | Out-Null
    Write-Host "[decompile] expanding $gameJar" -ForegroundColor Cyan
    Push-Location $gameSrc
    try { & $jar -xf $gameJar } finally { Pop-Location }
}

if ($Class.Count -gt 0) {
    $javapDir = Join-Path $root 'ref\javap'
    New-Item -ItemType Directory -Force -Path $javapDir | Out-Null
    foreach ($c in $Class) {
        $safe = $c -replace '[.$]', '_'
        if ($Bytecode) {
            $target = Join-Path $root "ref\javap_bc_$safe.txt"
            Write-Host "[decompile] javap -p -c $c -> $target" -ForegroundColor Cyan
            & $javap -p -c -constants -classpath $classpath $c 2>&1 | Out-File -Encoding utf8 $target
        } else {
            $target = Join-Path $javapDir "$safe.txt"
            Write-Host "[decompile] javap -p $c -> $target" -ForegroundColor Cyan
            & $javap -p -constants -classpath $classpath $c 2>&1 | Out-File -Encoding utf8 $target
        }
    }
    return
}

if ($SignatureOnly) { return }
Write-Host '[decompile] done；用 -Class <fqn> 生成某个类的签名表，-Bytecode 换字节码' -ForegroundColor Green
