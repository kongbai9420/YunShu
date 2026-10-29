<#
.SYNOPSIS
  在 GitHub Actions 的一次性 Windows 运行器上测试 MSI：安装 → 服务 → 就绪 → 权限 → 维护命令 → 故障恢复 → 升级 → 卸载。

.DESCRIPTION
  只给 .github/workflows/windows-package.yml 用。它会真的安装 MSI、注册并启动服务、（升级时）添加防火墙规则，
  所以只在 GitHub Actions 里运行，本机直接执行会拒绝。只用 127.0.0.1、伪造口令和伪造数据。

.PARAMETER Msi           要测试的安装包
.PARAMETER UpgradeMsi    同一程序、版本号调高的安装包（测试升级保留数据与配置）
.PARAMETER Database      sqlite（默认）或 postgres（使用运行器自带的 PostgreSQL，先从 SQLite 迁过去）
#>
param(
    [Parameter(Mandatory = $true)] [string] $Msi,
    [Parameter(Mandatory = $true)] [string] $UpgradeMsi,
    [ValidateSet("sqlite", "postgres")] [string] $Database = "sqlite"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
# yunshu.exe 输出重定向时用 UTF-8（见 app/winservice.py），这里按 UTF-8 解读
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($env:GITHUB_ACTIONS -ne "true" -or $env:RUNNER_ENVIRONMENT -ne "github-hosted") {
    throw "这个脚本会安装 MSI 并注册服务，只在 GitHub 托管的一次性运行器上运行"
}

$Programs = Join-Path $env:ProgramFiles "Yunshu"
$Root = Join-Path $env:ProgramData "Yunshu"
$Exe = Join-Path $Programs "yunshu.exe"
$Logs = Join-Path $env:RUNNER_TEMP "yunshu-install-logs"
New-Item -ItemType Directory -Force $Logs | Out-Null
$Base = "http://127.0.0.1:8080"
$ServiceAccount = "NT SERVICE\Yunshu"
$Allowed = @("NT AUTHORITY\SYSTEM", "BUILTIN\Administrators", $ServiceAccount)

function Step([string] $Text) { Write-Output "::group::$Text" }
function EndStep { Write-Output "::endgroup::" }
# 不限定 [bool]：对数组（如 sc.exe 的多行输出）用 -match 得到的是匹配的行，非空即通过
function Assert($Condition, [string] $Message) { if (-not $Condition) { throw "检查失败：$Message" } }

function Invoke-Msi([string[]] $Arguments, [string] $LogName) {
    $log = Join-Path $Logs $LogName
    $p = Start-Process msiexec.exe -ArgumentList ($Arguments + @("/qn", "/norestart", "/l*v", "`"$log`"")) -Wait -PassThru
    Assert ($p.ExitCode -in 0, 3010) "msiexec $($Arguments -join ' ') 退出码 $($p.ExitCode)（日志 $log）"
}

function Invoke-Yunshu([string[]] $Arguments) {
    # 只返回标准输出（日志在标准错误，带时间戳，单独显示）
    $err = Join-Path $Logs "yunshu-stderr.txt"
    $output = (& $Exe @Arguments 2>$err | Out-String).Trim()
    $code = $LASTEXITCODE
    Write-Host "> yunshu $($Arguments -join ' ')"
    Write-Host $output
    if (Test-Path $err) { Get-Content $err | Write-Host }
    Assert ($code -eq 0) "yunshu $($Arguments -join ' ') 退出码 $code"
    return $output
}

function Wait-Ready([int] $Seconds = 240) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri "$Base/api/ready" -TimeoutSec 5 -SkipHttpErrorCheck
            if ($r.StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Seconds 2
    }
    Get-ChildItem (Join-Path $Root "logs") -ErrorAction SilentlyContinue | ForEach-Object { Get-Content -Tail 60 $_.FullName }
    throw "$Seconds 秒内 /api/ready 没有返回 200"
}

function Wait-ServiceState([string] $State, [int] $Seconds = 120) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $s = Get-Service Yunshu -ErrorAction SilentlyContinue
        if ($s -and $s.Status -eq $State) { return }
        Start-Sleep -Seconds 1
    }
    throw "服务 Yunshu 没有在 $Seconds 秒内进入 $State"
}

function Assert-RestrictedAcl([string] $Path) {
    & icacls.exe $Path | Write-Host
    $acl = Get-Acl -LiteralPath $Path
    Assert $acl.AreAccessRulesProtected "$Path 不应继承上级权限"
    $who = $acl.Access | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique
    foreach ($w in $who) { Assert ($w -in $Allowed) "$Path 不应授权给 $w" }
    Assert ($ServiceAccount -in $who) "$Path 应授权给 $ServiceAccount"
}

function Get-ServicePid { return (Get-CimInstance Win32_Service -Filter "Name='Yunshu'").ProcessId }

function Get-InstalledVersion {
    $keys = Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall" |
        ForEach-Object { Get-ItemProperty $_.PSPath } | Where-Object { $_.DisplayName -eq "云枢运维平台" }
    return @($keys | ForEach-Object { $_.DisplayVersion })
}

try {
    Step "安装（默认选项，静默）"
    Invoke-Msi @("/i", "`"$Msi`"") "install.log"
    Assert ((Get-InstalledVersion).Count -eq 1) "应登记一个已安装的版本"
    EndStep

    Step "服务配置"
    Wait-ServiceState Running
    $qc = & sc.exe qc Yunshu | Out-String; Write-Output $qc
    Assert ($qc -match [regex]::Escape($ServiceAccount)) "服务应以 $ServiceAccount 运行"
    Assert ($qc -match "DELAYED") "服务应为延迟自动启动"
    Assert ($qc -match [regex]::Escape('yunshu.exe" service')) "服务命令行应为 yunshu.exe service"
    $qf = & sc.exe qfailure Yunshu | Out-String; Write-Output $qf
    Assert ($qf -match "RESTART") "失败后应自动重启"
    $flag = & sc.exe qfailureflag Yunshu | Out-String; Write-Output $flag
    Assert ($flag -match "TRUE") "服务以错误码停止时也应触发恢复"
    $display = (Get-Service Yunshu).DisplayName
    Assert ($display -eq "云枢运维平台") "显示名称应为 云枢运维平台，实际 $display"
    EndStep

    Step "就绪与首次登录"
    Wait-Ready
    $login = Invoke-RestMethod -Method Post -Uri "$Base/api/auth/login" -ContentType "application/json" `
        -Body '{"username":"admin","password":"admin123"}'
    Assert ($login.user.require_password_change -eq $true) "首次登录应要求改密"
    EndStep

    Step "目录与权限"
    foreach ($d in @("data", "keys", "logs")) { Assert (Test-Path (Join-Path $Root $d)) "缺少 $Root\$d" }
    $envFile = Join-Path $Root "yunshu.env"
    Assert (Test-Path $envFile) "缺少默认配置 $envFile"
    $key = Join-Path $Root "keys\master.key"
    Assert (Test-Path $key) "服务启动后应生成 $key"
    Assert-RestrictedAcl (Join-Path $Root "keys")
    Assert-RestrictedAcl $key
    & icacls.exe $Root | Write-Host
    $rootWho = (Get-Acl $Root).Access | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique
    foreach ($w in $rootWho) { Assert ($w -in $Allowed) "$Root 不应授权给 $w（yunshu.env 可能含口令）" }
    Assert (Test-Path (Join-Path $Root "logs\yunshu-service.log")) "服务日志应写到 $Root\logs"
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    Assert ($machinePath -split ";" | Where-Object { $_.TrimEnd("\") -eq $Programs }) "系统 PATH 应包含 $Programs"
    Assert (-not (Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "云枢运维平台*" })) "防火墙规则默认不应安装"
    EndStep

    if ($Database -eq "postgres") {
        Step "切换到运行器自带的 PostgreSQL（先从 SQLite 迁移）"
        Assert ([bool]$env:PGBIN) "运行器没有预装 PostgreSQL（缺少 PGBIN）"
        $pg = Get-Service -Name "postgresql*" | Select-Object -First 1
        Assert ([bool]$pg) "运行器没有 PostgreSQL 服务"
        Set-Service $pg.Name -StartupType Manual
        Start-Service $pg.Name
        $psql = Join-Path $env:PGBIN "psql.exe"
        & $psql -h 127.0.0.1 -U postgres -v ON_ERROR_STOP=1 -c "CREATE ROLE yunshu LOGIN PASSWORD 'fake-ci-password'" -c "CREATE DATABASE yunshu OWNER yunshu"
        Assert ($LASTEXITCODE -eq 0) "创建测试库失败"
        $url = "postgresql+psycopg://yunshu:fake-ci-password@127.0.0.1:5432/yunshu"
        Stop-Service Yunshu
        Invoke-Yunshu @("upgrade_to_pg", "run", "--target", $url, "--yes") | Out-Null
        Add-Content -LiteralPath $envFile -Value "`nYUNSHU_DATABASE_URL=$url`nYUNSHU_PG_BIN_DIR=$env:PGBIN`n" -Encoding utf8
        Start-Service Yunshu
        Wait-Ready
        $health = Invoke-RestMethod "$Base/api/ready"
        Write-Output ($health | ConvertTo-Json -Depth 5)
        EndStep
    }

    Step "维护命令（管理员命令行）"
    Invoke-Yunshu @("version") | Out-Null
    Invoke-Yunshu @("worker", "--check") | Out-Null
    Invoke-Yunshu @("backup", "create") | Out-Null
    $backupsBefore = Invoke-Yunshu @("backup", "list")
    $fingerprint = Invoke-Yunshu @("keytool", "fingerprint")
    EndStep

    Step "故障恢复：强制结束服务进程后自动重启"
    $oldPid = Get-ServicePid
    Stop-Process -Id $oldPid -Force
    $deadline = (Get-Date).AddSeconds(120)
    do { Start-Sleep -Seconds 2; $newPid = Get-ServicePid } while (($newPid -eq 0 -or $newPid -eq $oldPid) -and (Get-Date) -lt $deadline)
    Assert ($newPid -ne 0 -and $newPid -ne $oldPid) "服务进程被结束后没有按恢复策略重启"
    Wait-Ready
    EndStep

    Step "升级（版本号调高的同一程序，同时打开防火墙功能）"
    Add-Content -LiteralPath $envFile -Value "# fake-ci-marker：升级不能覆盖管理员改过的配置" -Encoding utf8
    Invoke-Msi @("/i", "`"$UpgradeMsi`"", "ADDLOCAL=ALL") "upgrade.log"
    $versions = Get-InstalledVersion
    Write-Output "已安装版本：$($versions -join ', ')"
    Assert ($versions.Count -eq 1) "升级后应只剩一个已安装版本"
    Wait-ServiceState Running
    Wait-Ready
    Assert ((Get-Content -Raw $envFile) -match "fake-ci-marker") "升级覆盖了 yunshu.env"
    Assert ((Invoke-Yunshu @("keytool", "fingerprint")) -eq $fingerprint) "升级后主密钥变了"
    $backupsAfter = Invoke-Yunshu @("backup", "list")
    foreach ($line in ($backupsBefore -split "`n" | Where-Object { $_.Trim() })) {
        Assert ($backupsAfter.Contains($line.Trim())) "升级后丢失了备份：$line"
    }
    Assert-RestrictedAcl $key
    $rule = Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "云枢运维平台*" }
    Assert ([bool]$rule) "选择防火墙功能后应有入站规则"
    ($rule | Get-NetFirewallPortFilter) | Format-List | Out-String | Write-Output
    EndStep

    Step "卸载"
    Invoke-Msi @("/x", "`"$UpgradeMsi`"") "uninstall.log"
    Assert (-not (Get-Service Yunshu -ErrorAction SilentlyContinue)) "卸载后服务应已删除"
    Assert (-not (Test-Path $Exe)) "卸载后程序文件应已删除"
    Assert ((Get-InstalledVersion).Count -eq 0) "卸载后不应再登记已安装版本"
    Assert (Test-Path $envFile) "卸载应保留 yunshu.env"
    Assert (Test-Path $key) "卸载应保留主密钥"
    Assert (Test-Path (Join-Path $Root "data")) "卸载应保留数据目录"
    if ($Database -eq "sqlite") { Assert (Test-Path (Join-Path $Root "data\yunshu.db")) "卸载应保留数据库" }
    Assert (-not (Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "云枢运维平台*" })) "卸载后防火墙规则应已删除"
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    Assert (-not ($machinePath -split ";" | Where-Object { $_.TrimEnd("\") -eq $Programs })) "卸载后 PATH 不应再包含 $Programs"
    EndStep

    Step "重新安装：沿用保留的数据与密钥"
    Invoke-Msi @("/i", "`"$Msi`"") "reinstall.log"
    Wait-ServiceState Running
    Wait-Ready
    Assert ((Invoke-Yunshu @("keytool", "fingerprint")) -eq $fingerprint) "重新安装后应沿用原来的主密钥"
    Invoke-Msi @("/x", "`"$Msi`"") "uninstall-2.log"
    EndStep
    Write-Output "Windows 安装测试（$Database）全部通过"
} finally {
    Copy-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $Root "logs") (Join-Path $Logs "service-logs")
}
