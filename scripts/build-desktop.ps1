# -ConsoleDir：已构建好的 yunshu 控制台程序目录（scripts/build-windows.ps1 传入）；不给时用 scripts/yunshu-windows.spec 现场构建
param([string] $Python = "python", [switch] $NativeWindowTest, [string] $ConsoleDir = "")
$ErrorActionPreference = "Stop"
$repo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$output = [System.IO.Path]::GetFullPath((Join-Path $repo "artifacts\desktop"))
if (-not $output.StartsWith($repo + [System.IO.Path]::DirectorySeparatorChar)) { throw "Unexpected output directory" }
Push-Location $repo
try {
    if (-not (Test-Path frontend/dist/index.html)) { throw "请先在 frontend 运行 npm ci 和 npm run build" }
    & $Python -m PyInstaller scripts/desktop.spec --noconfirm --distpath "$output/dist" --workpath "$output/build"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }
    & $Python scripts/desktop_notices.py
    if ($LASTEXITCODE -ne 0) { throw "依赖许可收集失败" }
    $exe = Join-Path $output "dist/Yunshu/Yunshu.exe"
    $report = Join-Path $output "smoke.json"
    $smokeFlag = if ($NativeWindowTest) { "--smoke-window" } else { "--smoke-test" }
    $check = Start-Process -FilePath $exe -ArgumentList @($smokeFlag, "`"$report`"") -Wait -PassThru -WindowStyle Hidden
    if ($check.ExitCode -ne 0 -or -not (Test-Path $report)) { throw "打包程序自检失败，请查看 dist/Yunshu/logs/desktop.log" }
    if ((Get-Content $report -Raw | ConvertFrom-Json).status -ne "ok") { throw "自检结果不正确" }
    # 测试日志不随包交付；自检数据库只在临时目录，正常退出自动清理。
    $checkLog = Join-Path $output "dist/Yunshu/logs/desktop.log"
    if (Test-Path $checkLog) { Remove-Item -LiteralPath $checkLog }
    Copy-Item scripts/desktop-guide.md "$output/dist/Yunshu/使用说明.md"
    # 维护命令行 cli\yunshu.exe（backup / keytool / legacy_import 等）。标记文件让它按桌面版目录运行：
    # 数据默认 <解压目录>\data，不读服务版的 %ProgramData%\Yunshu\yunshu.env
    if (-not $ConsoleDir) {
        & $Python -m PyInstaller scripts/yunshu-windows.spec --noconfirm --distpath "$output/console-dist" --workpath "$output/console-build"
        if ($LASTEXITCODE -ne 0) { throw "yunshu.exe 构建失败" }
        $ConsoleDir = Join-Path $output "console-dist/yunshu"
    }
    $cli = Join-Path $output "dist/Yunshu/cli"
    if (Test-Path $cli) { Remove-Item -Recurse -Force -LiteralPath $cli }
    Copy-Item -Recurse -LiteralPath $ConsoleDir $cli
    [System.IO.File]::WriteAllText((Join-Path $cli "desktop-portable.env"), "# 免安装包的维护命令行：有这个文件时，yunshu.exe 使用桌面版的数据目录（..\data）和主密钥（%APPDATA%\yunshu\master.key）。`r`n# 桌面版用环境变量改过数据目录时，在下面写同样的值，例如：`r`n# YUNSHU_DATA_DIR=D:\yunshu-data`r`n", (New-Object System.Text.UTF8Encoding $false))
    $cliVersion = & (Join-Path $cli "yunshu.exe") version
    if ($LASTEXITCODE -ne 0 -or -not $cliVersion) { throw "cli\yunshu.exe 无法运行" }
    $archive = Join-Path $output "Yunshu-windows-amd64.zip"
    Compress-Archive -Path "$output/dist/Yunshu" -DestinationPath $archive -Force
    Get-FileHash -Algorithm SHA256 $archive | Format-List
    Write-Output "免安装包：$archive"
} finally { Pop-Location }
