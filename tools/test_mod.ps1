<#
  只跑模组的纯逻辑自检（不需要启动游戏）。CI 与快速迭代用这个。

  用法：
    powershell -File tools/test_mod.ps1
#>
[CmdletBinding()]
param(
    [string]$StsDir = 'C:\Program Files (x86)\Steam\steamapps\common\SlayTheSpire',
    [string]$WorkshopDir = 'C:\Program Files (x86)\Steam\steamapps\workshop\content\646570',
    [string]$JdkHome = 'C:\Program Files\Java\jdk-21'
)

$ErrorActionPreference = 'Stop'
$root = (Get-Location).Path
$javac = Join-Path $JdkHome 'bin\javac.exe'
$java = Join-Path $JdkHome 'bin\java.exe'
$gameJar = Join-Path $StsDir 'desktop-1.0.jar'
$mtsJar = Join-Path $WorkshopDir '1605060445\ModTheSpire.jar'
$baseModJar = Join-Path $WorkshopDir '1605833019\BaseMod.jar'

$pureSrc = @(
    'mod\src\main\java\spireagent\bridge\Json.java',
    'mod\src\main\java\spireagent\bridge\Envelope.java',
    'mod\src\main\java\spireagent\bridge\Configure.java',
    'mod\src\main\java\spireagent\proto\Errors.java',
    'mod\src\main\java\spireagent\proto\ActionContext.java',
    'mod\src\main\java\spireagent\proto\ActionSpec.java',
    'mod\src\main\java\spireagent\obs\StabilityGate.java',
    'mod\src\main\java\spireagent\obs\EchoGate.java',
    'mod\src\main\java\spireagent\obs\PotionFacts.java',
    'mod\src\main\java\spireagent\obs\Eng.java',
    'mod\src\main\java\spireagent\obs\ShopSlots.java',
    'mod\src\main\java\spireagent\obs\CampfireSlots.java',
    'mod\src\main\java\spireagent\Watchdog.java',
    'mod\src\main\java\spireagent\Reflect.java',
    'mod\src\main\java\spireagent\Log.java',
    'mod\src\main\java\spireagent\SlDetector.java',
    'mod\src\test\java\spireagent\SelfTest.java'
) | ForEach-Object { Join-Path $root $_ }

$out = Join-Path $root 'mod\build\pure'
Remove-Item -Recurse -Force $out -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $out | Out-Null
& $javac --release 8 -Xlint:-options -encoding UTF-8 -classpath $gameJar -d $out $pureSrc
if ($LASTEXITCODE -ne 0) { throw "javac failed (exit $LASTEXITCODE)" }
# $mainOut 存在时 SelfTest.checkPatches 才能反射 HumanActionPatches 校验 patch 形状；
# 不存在就自动跳过（见 SelfTest 的说明）。
$mainOut = Join-Path $root 'mod\build\classes'
$runCp = @($out)
if (Test-Path -LiteralPath $mainOut) { $runCp += $mainOut }
foreach ($dep in @($gameJar, $mtsJar, $baseModJar)) {
    if (Test-Path -LiteralPath $dep) { $runCp += $dep }
}
& $java -cp ($runCp -join ';') spireagent.SelfTest
if ($LASTEXITCODE -ne 0) { throw "self-test failed (exit $LASTEXITCODE)" }
