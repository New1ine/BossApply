[CmdletBinding()]
param(
    [string] $SdkPath = $env:ANDROID_HOME,
    [string] $JavaPath = $env:JAVA_HOME,
    [string] $BuildToolsVersion = '35.0.0',
    [ValidateRange(24, 99)]
    [int] $PlatformApi = 35,
    [string] $Output
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

if (-not $SdkPath) { $SdkPath = $env:ANDROID_SDK_ROOT }
if (-not $SdkPath) {
    $standardSdk = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
    $SdkPath = if (Test-Path -LiteralPath $standardSdk -PathType Container) {
        $standardSdk
    } else { Join-Path $cache 'sdk' }
}
if (-not $JavaPath) {
    $compiler = Get-Command javac.exe -ErrorAction SilentlyContinue
    if ($compiler) { $JavaPath = Split-Path -Parent (Split-Path -Parent $compiler.Source) }
}
if (-not $JavaPath -and (Test-Path -LiteralPath $cache -PathType Container)) {
    $cachedJdk = Get-ChildItem -LiteralPath $cache -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bin\javac.exe') -PathType Leaf } |
        Select-Object -First 1
    if ($cachedJdk) { $JavaPath = $cachedJdk.FullName }
}
if (-not $JavaPath) { throw 'No existing JDK found. Set JAVA_HOME or pass -JavaPath.' }
$java = Join-Path $JavaPath 'bin\java.exe'
$javac = Join-Path $JavaPath 'bin\javac.exe'
$jar = Join-Path $JavaPath 'bin\jar.exe'
$platform = Join-Path $SdkPath "platforms\android-$PlatformApi\android.jar"
$d8 = Join-Path $SdkPath "build-tools\$BuildToolsVersion\lib\d8.jar"
foreach ($tool in @($java, $javac, $jar, $platform, $d8)) {
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw "Build dependency missing: $tool" }
}

$work = Join-Path $repo 'build\scroll-gesture'
$androidOutput = Join-Path $repo 'build\android'
if (-not $Output) { $Output = Join-Path $androidOutput 'BossApplyScrollGesture.jar' }
if (-not [IO.Path]::IsPathRooted($Output)) { $Output = Join-Path $repo $Output }
$outputPath = [IO.Path]::GetFullPath($Output)
$outputDirectory = Split-Path -Parent $outputPath
foreach ($path in @($work, $androidOutput, $outputDirectory)) {
    $dir = New-Item -Path $path -ItemType Directory -Force
    if ($dir.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Build path is a reparse point: $path" }
}
$stage = Join-Path $work ([Guid]::NewGuid().ToString('N'))
$classes = Join-Path $stage 'classes'
$dex = Join-Path $stage 'dex'
New-Item -Path $classes, $dex -ItemType Directory -Force | Out-Null
$classesJar = Join-Path $stage 'classes.jar'
$gestureSources = @(
    (Join-Path $repo 'android-probe\src\com\bossapply\scrolltest\GestureEngine.java'),
    (Join-Path $repo 'android-probe\src\com\bossapply\scrolltest\ScrollGesture.java')
)
try {
    Invoke-BuildTool $javac (@('--release', '8', '-encoding', 'UTF-8', '-classpath', $platform,
        '-d', $classes) + $gestureSources)
    Invoke-BuildTool $jar @("-J-Djava.io.tmpdir=$stage", 'cf', $classesJar, '-C', $classes, '.')
    Invoke-BuildTool $java @("-Djava.io.tmpdir=$stage", '-cp', $d8, 'com.android.tools.r8.D8', '--lib', $platform, '--min-api', '24',
        '--output', $dex, $classesJar)
    Invoke-BuildTool $jar @("-J-Djava.io.tmpdir=$stage", 'cf', $outputPath, '-C', $dex, 'classes.dex')
    Write-Output "Uniform scroll gesture built at $outputPath"
} finally {
    $stageDir = Get-Item -LiteralPath $stage -ErrorAction SilentlyContinue
    if ($stageDir -and $stageDir.FullName -eq $stage -and $stageDir.Parent.FullName -eq $work -and
            -not ($stageDir.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Remove-Item -LiteralPath $stageDir.FullName -Recurse -Force
    }
}
