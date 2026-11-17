# 安装包编译入口（只编译，不安装任何工具）
# 前置：出包前先跑 python package_portable.py（三值断言 + 便携 zip）。
# 缺少 Inno Setup 6 时本脚本只报告、不会下载或安装，退出码 5 便于门禁识别。
param(
    [string]$Version = ''
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$root = Split-Path -Parent $here

if (-not $Version) {
    $Version = (Get-Content (Join-Path $root 'VERSION') -Raw).Trim()
}

$exe = Join-Path $root 'dist\CuktechPanel\CuktechPanel.exe'
if (-not (Test-Path $exe)) {
    Write-Output "INSTALLER_BLOCKED: 产物不存在 $exe（先构建或先跑 package_portable.py）"
    exit 4
}

# 出包前断言 dist 内《第三方组件与开源许可》自述数字 = 实际组件/目录数，
# 防止「复制早于文档定稿」的旧文本被压进安装包。
$internal = Join-Path $root 'dist\CuktechPanel\_internal'
& python (Join-Path $root 'legal_gate.py') packaged $internal
if ($LASTEXITCODE -ne 0) {
    Write-Output 'INSTALLER_BLOCKED: 包内许可清单数字与实物不一致（legal_gate packaged 失败）'
    exit 7
}

$iscc = @(
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe'
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    Write-Output 'INSTALLER_BLOCKED: 未找到 ISCC.exe（Inno Setup 6 未安装）。未安装、未编译，等待确认。'
    exit 5
}

$iss = Join-Path $here 'cuktech_setup.iss'
$license = Join-Path $here 'license.txt'
$eula = Join-Path $root 'legal\用户协议.md'
if (-not (Test-Path $eula)) {
    Write-Output "INSTALLER_BLOCKED: 缺少用户协议 $eula"
    exit 4
}
Copy-Item -LiteralPath $eula -Destination $license -Force
Write-Output "INSTALLER_LICENSE $license"
Write-Output "INSTALLER_COMPILE version=$Version iscc=$iscc"
try {
    & $iscc "/DAppVersion=$Version" $iss
} finally {
    Remove-Item -LiteralPath $license -Force -ErrorAction SilentlyContinue
}
if ($LASTEXITCODE -ne 0) {
    Write-Output "INSTALLER_FAIL rc=$LASTEXITCODE"
    exit $LASTEXITCODE
}

$setup = Join-Path $root "release\CuktechPanel-$Version-setup-win64.exe"
if (-not (Test-Path $setup)) {
    Write-Output "INSTALLER_FAIL: 编译成功但未找到 $setup"
    exit 6
}
$sha = (Get-FileHash -Algorithm SHA256 -LiteralPath $setup).Hash.ToLower()
Write-Output "INSTALLER_OK $setup $((Get-Item $setup).Length) sha256=$sha"
