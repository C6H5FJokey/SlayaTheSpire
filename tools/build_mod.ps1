<#
  打包模组：javac --release 8 + jar，零 Gradle、零第三方依赖。

  用法：
    powershell -File tools/build_mod.ps1                 # 构建 mod/build/spireagent.jar
    powershell -File tools/build_mod.ps1 -SkipSelfTest   # 跳过纯逻辑自检

  产物（<STS>\mods\SlayaTheSpireAgent.jar）由 tools/install_mod.ps1 复制。
#>
[CmdletBinding()]
param(
    [string]$StsDir = 'C:\Program Files (x86)\Steam\steamapps\common\SlayTheSpire',
    [string]$WorkshopDir = 'C:\Program Files (x86)\Steam\steamapps\workshop\content\646570',
    [string]$JdkHome = 'C:\Program Files\Java\jdk-21',
    [string]$OutJar = 'mod\build\spireagent.jar',
    [switch]$SkipSelfTest
)

$ErrorActionPreference = 'Stop'

$javac = Join-Path $JdkHome 'bin\javac.exe'
$jar = Join-Path $JdkHome 'bin\jar.exe'
$java = Join-Path $JdkHome 'bin\java.exe'
foreach ($tool in @($javac, $jar, $java)) {
    if (-not (Test-Path -LiteralPath $tool)) { throw "missing JDK tool: $tool" }
}

$gameJar = Join-Path $StsDir 'desktop-1.0.jar'
$mtsJar = Join-Path $WorkshopDir '1605060445\ModTheSpire.jar'
$baseModJar = Join-Path $WorkshopDir '1605833019\BaseMod.jar'
foreach ($dep in @($gameJar, $mtsJar, $baseModJar)) {
    if (-not (Test-Path -LiteralPath $dep)) { throw "missing dependency: $dep" }
}
$classpath = "$gameJar;$mtsJar;$baseModJar"

$root = (Get-Location).Path
$srcFiles = Get-ChildItem -Recurse (Join-Path $root 'mod\src\main\java') -Filter *.java |
    ForEach-Object { $_.FullName }
$outDir = Join-Path $root 'mod\build\classes'
Remove-Item -Recurse -Force $outDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

Write-Host "[build] javac --release 8 ($($srcFiles.Count) files)" -ForegroundColor Cyan
& $javac --release 8 -Xlint:-options -encoding UTF-8 -classpath $classpath -d $outDir $srcFiles
if ($LASTEXITCODE -ne 0) { throw "javac failed (exit $LASTEXITCODE)" }

# ModTheSpire.json 与其它资源必须放在 jar 根目录
$resDir = Join-Path $root 'mod\src\main\resources'
if (Test-Path -LiteralPath $resDir) {
    Copy-Item -Recurse -Force (Join-Path $resDir '*') $outDir
}

if (-not $SkipSelfTest) {
    Write-Host '[build] self-test (pure logic)' -ForegroundColor Cyan
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
        'mod\src\main\java\spireagent\obs\ZoneGuess.java',
        'mod\src\main\java\spireagent\obs\Eng.java',
        'mod\src\main\java\spireagent\obs\ShopSlots.java',
        'mod\src\main\java\spireagent\obs\CampfireSlots.java',
        'mod\src\main\java\spireagent\Watchdog.java',
        'mod\src\main\java\spireagent\Reflect.java',
        'mod\src\main\java\spireagent\Log.java',
        'mod\src\main\java\spireagent\SlDetector.java',
        'mod\src\test\java\spireagent\SelfTest.java'
    ) | ForEach-Object { Join-Path $root $_ }
    $pureOut = Join-Path $root 'mod\build\pure'
    Remove-Item -Recurse -Force $pureOut -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $pureOut | Out-Null
    & $javac --release 8 -Xlint:-options -encoding UTF-8 -classpath $gameJar -d $pureOut $pureSrc
    if ($LASTEXITCODE -ne 0) { throw "javac (self-test) failed (exit $LASTEXITCODE)" }
    # 带上 $outDir + MTS/BaseMod 只是为了 checkPatches 能反射真实签名，不影响自检结果
    & $java -cp "$pureOut;$outDir;$classpath" spireagent.SelfTest
    if ($LASTEXITCODE -ne 0) { throw "self-test failed (exit $LASTEXITCODE)" }
}

$jarPath = Join-Path $root $OutJar
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $jarPath) | Out-Null
Remove-Item -Force $jarPath -ErrorAction SilentlyContinue
& $jar --create --file $jarPath -C $outDir .
if ($LASTEXITCODE -ne 0) { throw "jar failed (exit $LASTEXITCODE)" }

Write-Host "[build] ok -> $jarPath ($((Get-Item $jarPath).Length) bytes)" -ForegroundColor Green
