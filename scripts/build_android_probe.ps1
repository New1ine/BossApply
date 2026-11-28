[CmdletBinding()]
param(
    [string] $SdkPath = $env:ANDROID_HOME,
    [string] $JavaPath = $env:JAVA_HOME,
    [string] $BuildToolsVersion = '35.0.0',
    [ValidateRange(35, 99)]
    [int] $PlatformApi = 35
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$cache = Join-Path $repo 'build\android-probe-toolchain'

function Invoke-BuildTool {
    param([string] $Executable, [string[]] $Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$(Split-Path -Leaf $Executable) failed (exit $LASTEXITCODE)."
    }
}

if (-not $SdkPath) {
    $SdkPath = $env:ANDROID_SDK_ROOT
}
if (-not $SdkPath) {
    $standardSdk = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
    $SdkPath = if (Test-Path -LiteralPath $standardSdk -PathType Container) {
        $standardSdk
    } else { Join-Path $cache 'sdk' }
}
if (-not $JavaPath) {
    $compiler = Get-Command javac.exe -ErrorAction SilentlyContinue
    if ($compiler) {
        $JavaPath = Split-Path -Parent (Split-Path -Parent $compiler.Source)
    }
}
if (-not $JavaPath -and (Test-Path -LiteralPath $cache -PathType Container)) {
    $cachedJdk = Get-ChildItem -LiteralPath $cache -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bin\javac.exe') -PathType Leaf } |
        Select-Object -First 1
    if ($cachedJdk) { $JavaPath = $cachedJdk.FullName }
}
if (-not $JavaPath -or -not (Test-Path -LiteralPath $JavaPath -PathType Container)) {
    throw 'Set JAVA_HOME or pass -JavaPath pointing to a JDK 17+ installation.'
}

$java = Join-Path $JavaPath 'bin\java.exe'
$javac = Join-Path $JavaPath 'bin\javac.exe'
$jar = Join-Path $JavaPath 'bin\jar.exe'
$keytool = Join-Path $JavaPath 'bin\keytool.exe'
$platform = Join-Path $SdkPath "platforms\android-$PlatformApi\android.jar"
$toolsPath = Join-Path $SdkPath "build-tools\$BuildToolsVersion"
$aapt2 = Join-Path $toolsPath 'aapt2.exe'
$zipalign = Join-Path $toolsPath 'zipalign.exe'
$d8 = Join-Path $toolsPath 'lib\d8.jar'
$apksigner = Join-Path $toolsPath 'lib\apksigner.jar'
foreach ($tool in @($java, $javac, $jar, $keytool, $platform, $aapt2, $zipalign, $d8, $apksigner)) {
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) {
        throw "Build dependency missing at $tool."
    }
}

$buildDir = Join-Path $repo 'build'
$probeBuild = Join-Path $buildDir 'android-probe'
$androidOutput = Join-Path $buildDir 'android'
foreach ($path in @($buildDir, $probeBuild, $androidOutput)) {
    $dir = New-Item -Path $path -ItemType Directory -Force
    if ($dir.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Build path is a reparse point at $path."
    }
}
$sources = @(Get-ChildItem -LiteralPath (Join-Path $repo 'android-probe\src') -Filter '*.java' -File -Recurse |
    Sort-Object FullName | Select-Object -ExpandProperty FullName)
if (-not $sources.Count) { throw 'No Android component Java sources found.' }
$stage = Join-Path $probeBuild ([Guid]::NewGuid().ToString('N'))
$classDir = Join-Path $stage 'classes'
$dexDir = Join-Path $stage 'dex'
New-Item -Path $classDir, $dexDir -ItemType Directory -Force | Out-Null
$manifest = Join-Path $repo 'android-probe\AndroidManifest.xml'
$classesJar = Join-Path $stage 'classes.jar'
$unaligned = Join-Path $stage 'unaligned.apk'
$aligned = Join-Path $stage 'aligned.apk'
$signed = Join-Path $stage 'signed.apk'
$key = Join-Path $probeBuild 'signing.keystore'
$output = Join-Path $androidOutput 'BossApplyUiProbe.apk'

try {
    Invoke-BuildTool $javac (@('--release', '8', '-encoding', 'UTF-8', '-classpath', $platform, '-d', $classDir) + $sources)
    Invoke-BuildTool $jar @("-J-Djava.io.tmpdir=$stage", 'cf', $classesJar, '-C', $classDir, '.')
    Invoke-BuildTool $java @("-Djava.io.tmpdir=$stage", '-cp', $d8, 'com.android.tools.r8.D8', '--lib', $platform, '--min-api', '24',
        '--output', $dexDir, $classesJar)
    Invoke-BuildTool $aapt2 @('link', '-I', $platform, '--manifest', $manifest, '-o', $unaligned)
    Invoke-BuildTool $jar @("-J-Djava.io.tmpdir=$stage", 'uf', $unaligned, '-C', $dexDir, 'classes.dex')
    Invoke-BuildTool $zipalign @('-f', '4', $unaligned, $aligned)

    if (-not (Test-Path -LiteralPath $key -PathType Leaf)) {
        # Reuse the local signing key on rebuilds so the installed probe can be updated in place.
        Invoke-BuildTool $keytool @('-genkeypair', '-keystore', $key, '-storetype', 'PKCS12',
            '-alias', 'bossapply-ui-probe', '-keyalg', 'RSA', '-keysize', '2048', '-validity', '10000',
            '-dname', 'CN=BossApply UI Probe', '-storepass', 'android', '-keypass', 'android')
    }
    Invoke-BuildTool $java @('-jar', $apksigner, 'sign', '--ks', $key, '--ks-key-alias', 'bossapply-ui-probe',
        '--ks-pass', 'pass:android', '--key-pass', 'pass:android', '--out', $signed, $aligned)
    Invoke-BuildTool $java @('-jar', $apksigner, 'verify', '--verbose', $signed)
    Move-Item -LiteralPath $signed -Destination $output -Force
    Write-Output "Android probe built at $output"
} finally {
    $stageDir = Get-Item -LiteralPath $stage -ErrorAction SilentlyContinue
    if ($stageDir -and $stageDir.FullName -eq $stage -and $stageDir.Parent.FullName -eq $probeBuild -and
            -not ($stageDir.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Remove-Item -LiteralPath $stageDir.FullName -Recurse -Force
    }
}
