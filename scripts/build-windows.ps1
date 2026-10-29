<#
.SYNOPSIS
  构建云枢运维平台 Windows 服务版：控制台程序 yunshu.exe（PyInstaller）+ MSI 安装包（WiX 5.0.2）。

.DESCRIPTION
  1. 没有 frontend/dist 时先 npm ci / npm run build。
  2. 在 artifacts/windows/build_venv 用哈希锁安装依赖（标准版锁含 PostgreSQL 驱动 + 打包锁含 PyInstaller）。
  3. PyInstaller 按 scripts/yunshu-windows.spec 打包，再用 packaging/windows/smoke_test.py 隔离自检
     （临时数据目录与密钥、127.0.0.1 高位端口；api 与 service --foreground 两种方式启动、就绪、首次登录改密、
     worker --check、Ctrl+Break 停止后锁已释放、backup / keytool）。
  4. 下载固定版本并核对 SHA-256 的 WiX 工具包（只放在 artifacts/windows/cache，不做任何全局安装），
     生成文件清单与许可协议 RTF，构建 MSI 并做 ICE 校验（只读校验，不安装）。
  5. 输出 artifacts/windows/Yunshu-<版本>-windows-amd64.msi 与 .sha256。
  -Desktop 时再用同一个 yunshu 目录构建免安装 ZIP（scripts/build-desktop.ps1，ZIP 的 cli\ 里带 yunshu.exe）。

  本脚本不安装 MSI、不注册服务、不改防火墙；安装测试在 GitHub Actions（.github/workflows/windows-package.yml）里做。

.PARAMETER Python
  用来创建构建环境的 Python 3.11（默认 PATH 上的 python）。
.PARAMETER SkipApp
  复用已有的 artifacts/windows/dist/yunshu，只重新生成 MSI（CI 用它打升级测试包）。
.PARAMETER ProductVersion
  MSI 的数字版本，默认取 backend/app/__init__.py 的版本去掉预发布标记（4.4.0-beta.1 → 4.4.0）。
.PARAMETER OutputName
  MSI 文件名，默认 Yunshu-<完整版本>-windows-amd64.msi。
#>
param(
    [string] $Python = "python",
    [switch] $SkipApp,
    [string] $ProductVersion = "",
    [string] $OutputName = "",
    [switch] $Desktop
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$out = [System.IO.Path]::GetFullPath((Join-Path $repo "artifacts\windows"))
if (-not $out.StartsWith($repo + [System.IO.Path]::DirectorySeparatorChar)) { throw "Unexpected output directory" }
New-Item -ItemType Directory -Force $out | Out-Null

# WiX 5.0.2（MS-RL）：只作构建工具。SHA-256 已与 nuget.org 目录里登记的包哈希核对过
$WixVersion = "5.0.2"
$WixPackages = [ordered]@{
    "wix"                        = "f30ef0c74e2a986126539c5780be93ac24e8136eaf723b1937b26272703ae173"
    "wixtoolset.util.wixext"     = "dda1cc1b4d3b2305b1246f167c6af6fcded5000be84b3bbff3dda4f442bf4740"
    "wixtoolset.ui.wixext"       = "5ef2c707614b9f70b6bbadd2d4abcb4124efee215e9b16bfbc80113079a604c7"
    "wixtoolset.firewall.wixext" = "c9a1c99afab82225127b34036b719dea6c98c451e21b341ae1c62a674d0ba5ef"
}

function Invoke-Checked([string] $What, [scriptblock] $Block) {
    & $Block
    if ($LASTEXITCODE -ne 0) { throw "$What 失败（退出码 $LASTEXITCODE）" }
}

function Get-AppVersion {
    $init = Get-Content -Raw -Encoding UTF8 (Join-Path $repo "backend\app\__init__.py")
    if ($init -notmatch '__version__\s*=\s*"([^"]+)"') { throw "读不到 backend/app/__init__.py 的版本" }
    return $Matches[1]
}

function Get-ServiceSid([string] $Name) {
    # 虚拟账号 NT SERVICE\<服务名> 的 SID：S-1-5-80- 加服务名大写后 UTF-16LE 的 SHA-1（与 sc showsid 相同）
    $sha = [System.Security.Cryptography.SHA1]::Create()
    $hash = $sha.ComputeHash([System.Text.Encoding]::Unicode.GetBytes($Name.ToUpperInvariant()))
    $parts = 0..4 | ForEach-Object { [System.BitConverter]::ToUInt32($hash, $_ * 4) }
    return "S-1-5-80-" + ($parts -join "-")
}

function Get-WixTools {
    $cache = Join-Path $out "cache\wix-$WixVersion"
    $tools = @{}
    foreach ($id in $WixPackages.Keys) {
        $dir = Join-Path $cache $id
        $nupkg = Join-Path $cache "$id.$WixVersion.nupkg"
        if (-not (Test-Path (Join-Path $dir ".verified"))) {
            New-Item -ItemType Directory -Force $cache | Out-Null
            if (-not (Test-Path $nupkg)) {
                Invoke-WebRequest -UseBasicParsing -Uri "https://api.nuget.org/v3-flatcontainer/$id/$WixVersion/$id.$WixVersion.nupkg" -OutFile $nupkg
            }
            $actual = (Get-FileHash -Algorithm SHA256 $nupkg).Hash.ToLowerInvariant()
            if ($actual -ne $WixPackages[$id]) { Remove-Item -LiteralPath $nupkg; throw "$id 的 SHA-256 不符：$actual" }
            if (Test-Path $dir) { Remove-Item -Recurse -Force -LiteralPath $dir }
            $zip = "$nupkg.zip"
            Copy-Item -LiteralPath $nupkg $zip -Force
            Expand-Archive -LiteralPath $zip -DestinationPath $dir -Force
            Remove-Item -LiteralPath $zip
            Set-Content -LiteralPath (Join-Path $dir ".verified") -Value $actual
        }
        $tools[$id] = $dir
    }
    $wix = Get-ChildItem -Recurse -Filter wix.dll (Join-Path $tools["wix"] "tools") | Select-Object -First 1
    $ext = foreach ($id in @("wixtoolset.util.wixext", "wixtoolset.ui.wixext", "wixtoolset.firewall.wixext")) {
        (Get-ChildItem -Recurse -Filter "*.wixext.dll" (Join-Path $tools[$id] "wixext5") | Select-Object -First 1).FullName
    }
    if (-not $wix -or @($ext).Count -ne 3) { throw "WiX 工具包内容不完整" }
    return @{ Wix = $wix.FullName; Extensions = @($ext) }
}

function ConvertTo-Rtf([string] $Text) {
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append("{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0\fnil Consolas;}}\viewkind4\uc1\pard\f0\fs18 ")
    foreach ($ch in $Text.ToCharArray()) {
        $code = [int]$ch
        if ($ch -eq "`r") { continue }
        elseif ($ch -eq "`n") { [void]$sb.Append("\par`r`n") }
        elseif ($ch -eq "\" -or $ch -eq "{" -or $ch -eq "}") { [void]$sb.Append("\" + $ch) }
        elseif ($code -gt 127) { [void]$sb.Append("\u" + $(if ($code -gt 32767) { $code - 65536 } else { $code }) + "?") }
        else { [void]$sb.Append($ch) }
    }
    [void]$sb.Append("}")
    return $sb.ToString()
}

$appVersion = Get-AppVersion
if (-not $ProductVersion) {
    if ($appVersion -notmatch '^(\d+\.\d+\.\d+)') { throw "版本 $appVersion 不是 x.y.z 开头" }
    $ProductVersion = $Matches[1]
}
if ($ProductVersion -notmatch '^\d+\.\d+\.\d+$') { throw "MSI 版本必须是纯数字 x.y.z：$ProductVersion" }
if (-not $OutputName) { $OutputName = "Yunshu-$appVersion-windows-amd64.msi" }
$app = Join-Path $out "dist\yunshu"
$venvPython = Join-Path $out "build_venv\Scripts\python.exe"

Push-Location $repo
try {
    if (-not $SkipApp) {
        if (-not (Test-Path frontend/dist/index.html)) {
            Push-Location frontend
            try {
                Invoke-Checked "npm ci" { npm ci --no-audit --no-fund }
                Invoke-Checked "前端构建" { npm run build }
            } finally { Pop-Location }
        }
        if (-not (Test-Path $venvPython)) {
            Invoke-Checked "创建构建环境" { & $Python -m venv (Join-Path $out "build_venv") }
        }
        Invoke-Checked "安装哈希锁依赖" {
            & $venvPython -m pip install --disable-pip-version-check --require-hashes `
                -r backend/requirements-standard.lock -r backend/requirements-package.lock
        }
        Invoke-Checked "PyInstaller 构建" {
            & $venvPython -m PyInstaller scripts/yunshu-windows.spec --noconfirm --distpath (Join-Path $out "dist") --workpath (Join-Path $out "build")
        }
        $env:PYTHONIOENCODING = "utf-8"
        Invoke-Checked "yunshu.exe 自检" {
            & $venvPython packaging/windows/smoke_test.py (Join-Path $app "yunshu.exe") (Join-Path $out "smoke.json")
        }
    }
    if (-not (Test-Path (Join-Path $app "yunshu.exe"))) { throw "没有 $app\yunshu.exe，请先完整构建（不加 -SkipApp）" }

    # ---- MSI ----
    $tools = Get-WixTools
    $obj = Join-Path $out "obj"
    if (Test-Path $obj) { Remove-Item -Recurse -Force -LiteralPath $obj }
    New-Item -ItemType Directory -Force $obj | Out-Null
    $harvestPython = if (Test-Path $venvPython) { $venvPython } else { $Python }
    Invoke-Checked "生成文件清单" { & $harvestPython packaging/windows/harvest.py $app (Join-Path $obj "AppFiles.wxs") }
    $rtf = Join-Path $obj "License.rtf"
    [System.IO.File]::WriteAllText($rtf, (ConvertTo-Rtf (Get-Content -Raw -Encoding UTF8 LICENSE)), [System.Text.Encoding]::ASCII)
    $msi = Join-Path $out $OutputName
    $wixArgs = @("build", "-nologo", "-arch", "x64", "-culture", "zh-CN",
        "-d", "ProductVersion=$ProductVersion", "-d", "AppDir=$app", "-d", "LicenseRtf=$rtf",
        "-d", "ServiceSid=$(Get-ServiceSid 'Yunshu')", "-intermediatefolder", $obj, "-o", $msi)
    foreach ($e in $tools.Extensions) { $wixArgs += @("-ext", $e) }
    $wixArgs += @("-loc", "packaging/windows/zh-CN.wxl", "packaging/windows/Yunshu.wxs", (Join-Path $obj "AppFiles.wxs"))
    Invoke-Checked "WiX 构建" { dotnet $tools.Wix @wixArgs }
    # ICE 校验只读打开 MSI，不安装。ICE38 / ICE43 / ICE57 把“所有用户”的开始菜单也当成个人目录，
    # 对按机器安装（ALLUSERS=1）的快捷方式是已知的误报，跳过；其余 ICE 错误一律让构建失败
    Invoke-Checked "MSI ICE 校验" { dotnet $tools.Wix msi validate -nologo -sice ICE38 -sice ICE43 -sice ICE57 $msi }
    $hash = (Get-FileHash -Algorithm SHA256 $msi).Hash.ToLowerInvariant()
    [System.IO.File]::WriteAllText("$msi.sha256", "$hash  $OutputName`n", (New-Object System.Text.UTF8Encoding $false))
    Write-Output "MSI：$msi"
    Write-Output "SHA-256：$hash"

    if ($Desktop) {
        Invoke-Checked "免安装 ZIP 构建" { & (Join-Path $PSScriptRoot "build-desktop.ps1") -Python $venvPython -ConsoleDir $app }
        $zip = Join-Path $repo "artifacts\desktop\Yunshu-windows-amd64.zip"
        $zipHash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLowerInvariant()
        [System.IO.File]::WriteAllText("$zip.sha256", "$zipHash  Yunshu-windows-amd64.zip`n", (New-Object System.Text.UTF8Encoding $false))
        Write-Output "免安装 ZIP：$zip（SHA-256 $zipHash）"
    }
} finally { Pop-Location }
