<#
.SYNOPSIS
  在本机一键预览云枢 4.0（后端 FastAPI + 前端 Vite 开发服务器，改代码即时刷新）。

.DESCRIPTION
  不带参数：预览脚本所在的这份检出。
  带分支名或 PR 编号：在 ..\yunshu-preview\<分支> 建立（或复用）独立 git worktree，
  不会切换、重置你当前检出的分支或本地 main。该分支若已在别的 worktree 里检出，直接复用那一份。
  只监听 127.0.0.1，不对局域网开放。按 Ctrl+C 停止。

.EXAMPLE
  scripts\preview.bat            # 预览当前检出
  scripts\preview.bat 5          # 预览 PR #5
  scripts\preview.bat p0-scaffold
  scripts\preview.bat -Reset    # 清空预览数据库后重新开始（预览密钥保留）
  scripts\preview.bat -NoSeed   # 预览库为空时不自动导入示例数据

  预览库为空时，会把 scripts\preview-sample.ini（伪造的旧版配置，地址都在 192.0.2.0/24）
  导入预览库，资产列表里就有设备可看；仍用 admin / admin123 登录。
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)] [string] $Target = "",
  [int] $BackendPort = 8000,
  [int] $FrontendPort = 5173,
  [switch] $NoBrowser,
  [switch] $Reset,
  [switch] $NoSeed
)

# 原生命令（git、uv）会往 stderr 写进度，用 Continue 并逐个检查 $LASTEXITCODE
$ErrorActionPreference = "Continue"
$Remote = "origin"

function Info([string] $msg) { Write-Host "[预览] $msg" -ForegroundColor Cyan }
function Fail([string] $msg) { Write-Host "[预览] $msg" -ForegroundColor Red; exit 1 }

function Invoke-Git([string[]] $GitArgs) {
  $out = & git @GitArgs 2>&1
  if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') 失败：$out" }
  return $out
}

function Test-PortFree([int] $port) {
  $listener = $null
  try {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $port)
    $listener.Start()
    return $true
  } catch { return $false } finally { if ($listener) { $listener.Stop() } }
}

function Get-FreePort([int] $start) {
  for ($p = $start; $p -lt $start + 50; $p++) { if (Test-PortFree $p) { return $p } }
  Fail "从 $start 起找不到空闲端口"
}

# 是否有 node / python 进程（开发服务器、测试等）正在使用该目录
function Test-InUse([string] $dir) {
  $needle = $dir.TrimEnd('\')
  $procs = Get-CimInstance Win32_Process -Filter "Name='node.exe' or Name='python.exe' or Name='uvicorn.exe'" -ErrorAction SilentlyContinue
  foreach ($p in $procs) {
    if ($p.CommandLine -and $p.CommandLine.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
  }
  return $false
}

# 不用 Get-FileHash：从 PowerShell 7 里调起 5.1 时模块路径会串，找不到该命令
function Get-FileHashText([string] $path) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try { return [BitConverter]::ToString($sha.ComputeHash([System.IO.File]::ReadAllBytes($path))) -replace '-', '' }
  finally { $sha.Dispose() }
}

function Wait-Http([string] $url, [int] $seconds, $proc) {
  $deadline = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $deadline) {
    if ($proc -and $proc.HasExited) { return $false }
    try {
      $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop $url
      if ($r.StatusCode -lt 500) { return $true }
    } catch { Start-Sleep -Milliseconds 500 }
  }
  return $false
}

function Stop-Tree($proc) {
  if ($proc -and -not $proc.HasExited) { & taskkill /T /F /PID $proc.Id 2>&1 | Out-Null }
}

# 上一次预览没停干净留下的后端进程。后端是三层：venv 启动器 → 基础 Python 的重载进程 → spawn 出的服务进程
# （内嵌 Worker 在里面）。后两层的命令行里没有项目路径，Test-InUse 认不出；启动器先被结束时，它们会继续
# 连着预览库、拿着调度锁、跑旧代码。按预览库 worker_heartbeats 里本机记录的 pid 找到服务进程，
# 再往上找同属这次 uvicorn 的重载进程，整棵结束。调用前须确认没有正在运行的预览（Test-InUse 为假）。
function Stop-PreviewOrphans([string] $venvPy, [string] $dbFile) {
  if (-not (Test-Path $dbFile) -or -not (Test-Path $venvPy)) { return }
  $query = "import socket, sqlite3, sys`n" +
           "try:`n" +
           "    c = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True, timeout=5)`n" +
           "    rows = c.execute('select pid from worker_heartbeats where lower(host) = ?', (socket.gethostname().lower(),)).fetchall()`n" +
           "except sqlite3.Error:`n" +
           "    rows = []`n" +
           "print(' '.join(str(r[0]) for r in rows if r[0]))"
  $pids = (& $venvPy -I -c $query $dbFile 2>$null) -split ' ' | Where-Object { $_ -match '^\d+$' }
  foreach ($procId in $pids) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    # pid 可能已被别的程序复用：只认 Python 的 multiprocessing 子进程或 uvicorn 预览进程
    if (-not $p -or $p.Name -ne "python.exe" -or
        ($p.CommandLine -notmatch 'multiprocessing\.spawn' -and $p.CommandLine -notmatch 'uvicorn app\.main:create_app')) { continue }
    $top = $p
    while ($true) {
      $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($top.ParentProcessId)" -ErrorAction SilentlyContinue
      if (-not $parent -or $parent.Name -ne "python.exe" -or $parent.CommandLine -notmatch 'uvicorn app\.main:create_app' -or
          $parent.CreationDate -gt $top.CreationDate) { break }
      $top = $parent
    }
    if ($top.ProcessId -eq $p.ProcessId) { Info "结束残留的预览后端进程 $procId（内嵌 Worker 在其中）" }
    else { Info "结束残留的预览后端进程 $($top.ProcessId) 及其子进程（内嵌 Worker 在 $procId）" }
    & taskkill /T /F /PID $top.ProcessId 2>&1 | Out-Null
  }
}

foreach ($tool in "git", "node") {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "找不到 $tool，请先安装并加入 PATH" }
}

$scriptRepo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

# ---------- 1. 确定要预览的目录 ----------
if ($Target -eq "") {
  $workDir = $scriptRepo
  $branch = (& git -C $workDir rev-parse --abbrev-ref HEAD).Trim()
  Info "预览当前检出：$branch"
} else {
  Push-Location $scriptRepo
  try {
    if ($Target -match '^#?(\d+)$') {
      $pr = $Matches[1]
      $branch = $null
      if (Get-Command gh -ErrorAction SilentlyContinue) {
        $branch = (& gh pr view $pr --json headRefName -q .headRefName 2>$null)
      }
      if ($branch) {
        Info "PR #$pr 对应分支 $branch"
        Invoke-Git @("fetch", $Remote, $branch) | Out-Null
        $remoteRef = "$Remote/$branch"
      } else {
        $branch = "pr-$pr"
        Info "没有可用的 gh，按 pull/$pr/head 取 PR #$pr 到本地分支 $branch"
        Invoke-Git @("fetch", $Remote, "+pull/$pr/head:refs/remotes/$Remote/$branch") | Out-Null
        $remoteRef = "$Remote/$branch"
      }
    } else {
      $branch = $Target
      Invoke-Git @("fetch", $Remote, $branch) | Out-Null
      $remoteRef = "$Remote/$branch"
    }

    # 该分支是否已在某个 worktree 中检出
    $workDir = $null
    $current = $null
    foreach ($line in (& git worktree list --porcelain)) {
      if ($line -like "worktree *") { $current = $line.Substring(9) }
      elseif ($line -eq "branch refs/heads/$branch") { $workDir = $current }
    }

    $safeName = $branch -replace '[\\/:*?"<>|]', '-'
    $previewRoot = Join-Path (Split-Path $scriptRepo -Parent) "yunshu-preview"
    New-Item -ItemType Directory -Force $previewRoot | Out-Null

    if ($workDir) { $workDir = (Resolve-Path $workDir).Path }
    if ($workDir -and (Test-InUse $workDir)) {
      # 别的进程（例如正在那里开发的开发服务器）在用这份 worktree：不碰它，另开一份只读用途的分离检出
      $workDir = Join-Path $previewRoot "$safeName-detached"
      if (-not (Test-Path $workDir)) {
        Invoke-Git @("worktree", "add", "--detach", $workDir, $remoteRef) | Out-Null
      } elseif (-not (Test-InUse $workDir)) {
        & git -C $workDir checkout -q --detach $remoteRef 2>&1 | Out-Null
      }
      Info "分支已在另一个正在使用的 worktree 中检出，改用分离检出预览远端最新提交：$workDir"
    } elseif ($workDir) {
      Info "复用已有 worktree：$workDir"
      $dirty = & git -C $workDir status --porcelain --untracked-files=no
      if ($dirty) {
        Info "该 worktree 有未提交改动，按现状预览，不拉取远端更新"
      } else {
        & git -C $workDir merge --ff-only $remoteRef 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Info "本地分支与远端已分叉，按本地现状预览" }
      }
    } else {
      $workDir = Join-Path $previewRoot $safeName
      & git show-ref --verify --quiet "refs/heads/$branch"
      if ($LASTEXITCODE -eq 0) {
        Invoke-Git @("worktree", "add", $workDir, $branch) | Out-Null
        & git -C $workDir merge --ff-only $remoteRef 2>&1 | Out-Null
      } else {
        Invoke-Git @("worktree", "add", "--track", "-b", $branch, $workDir, $remoteRef) | Out-Null
      }
      Info "已建立 worktree：$workDir"
    }
  } finally { Pop-Location }
}

$backendDir = Join-Path $workDir "backend"
$frontendDir = Join-Path $workDir "frontend"
if (-not (Test-Path (Join-Path $backendDir "app")) -or -not (Test-Path (Join-Path $frontendDir "package.json"))) {
  Fail "分支 $branch 里没有 backend/ 或 frontend/（4.0 代码从 p0-scaffold 起才有），无法预览"
}

# ---------- 2. 后端依赖（锁文件变了才重装） ----------
$venvPy = Join-Path $backendDir ".venv\Scripts\python.exe"
$lock = Join-Path $backendDir "requirements.lock"
$stamp = Join-Path $backendDir ".venv\.preview-lock-sha256"
$useUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)
if (-not (Test-Path $venvPy)) {
  Info "创建后端虚拟环境（Python 3.11）"
  if ($useUv) { & uv venv (Join-Path $backendDir ".venv") --python 3.11 -q }
  elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3.11 -m venv (Join-Path $backendDir ".venv") }
  else { & python -m venv (Join-Path $backendDir ".venv") }
  if ($LASTEXITCODE -ne 0) { Fail "创建虚拟环境失败，需要 Python 3.11（或安装 uv）" }
}
$lockHash = Get-FileHashText $lock
if (-not (Test-Path $stamp) -or (Get-Content $stamp) -ne $lockHash) {
  if (Test-InUse $backendDir) { Fail "后端依赖需要更新，但有进程正在使用 $backendDir，请先停止它再运行" }
  Info "安装后端依赖（requirements.lock，校验哈希）"
  if ($useUv) { & uv pip install -q --require-hashes -r $lock --python $venvPy }
  else { & $venvPy -m pip install -q --disable-pip-version-check --require-hashes -r $lock }
  if ($LASTEXITCODE -ne 0) { Fail "后端依赖安装失败" }
  Set-Content -Path $stamp -Value $lockHash
}

# ---------- 3. 前端依赖（package-lock 变了才 npm ci） ----------
$pkgLock = Join-Path $frontendDir "package-lock.json"
$nodeStamp = Join-Path $frontendDir "node_modules\.preview-lock-sha256"
$pkgHash = Get-FileHashText $pkgLock
$npmHidden = Join-Path $frontendDir "node_modules\.package-lock.json"
if (-not (Test-Path $nodeStamp) -and (Test-Path (Join-Path $frontendDir "node_modules\vite\bin\vite.js")) -and
    (Test-Path $npmHidden) -and (Get-Item $npmHidden).LastWriteTime -ge (Get-Item $pkgLock).LastWriteTime) {
  # 已经由别处装好且比 package-lock 新：直接沿用，不重装
  Set-Content -Path $nodeStamp -Value $pkgHash
}
if (-not (Test-Path $nodeStamp) -or (Get-Content $nodeStamp) -ne $pkgHash) {
  # npm ci 会先删掉整个 node_modules；有开发服务器在用时删到一半会把它弄坏
  if (Test-InUse $frontendDir) { Fail "前端依赖需要更新，但有进程正在使用 $frontendDir，请先停止它再运行" }
  Info "安装前端依赖（npm ci）"
  Push-Location $frontendDir
  try { & npm ci --no-audit --no-fund --loglevel=error } finally { Pop-Location }
  if ($LASTEXITCODE -ne 0) { Fail "npm ci 失败" }
  Set-Content -Path $nodeStamp -Value $pkgHash
}

# ---------- 4. 预览数据（独立目录，不碰正式位置 %APPDATA%\yunshu） ----------
$previewData = Join-Path $backendDir ".preview\data"
$env:YUNSHU_DATA_DIR = $previewData
$env:YUNSHU_MASTER_KEY_FILE = Join-Path $backendDir ".preview\keys\master.key"
# 预览时 Worker 跑在后端进程里（不用另开窗口）
$env:YUNSHU_EMBEDDED_WORKER = "1"
$previewDb = Join-Path $previewData "yunshu.db"
if (-not (Test-InUse $backendDir)) { Stop-PreviewOrphans $venvPy $previewDb }
if ($Reset -and (Test-Path $previewData)) {
  if (Test-InUse $backendDir) { Fail "有进程正在使用 $backendDir（可能是另一个预览），先停止它再用 -Reset" }
  Remove-Item -Recurse -Force $previewData
  # 首次启动向导保存的数据库选择在密钥目录里（4.1 P12），一并清掉，重置后会再次显示向导
  $dbChoice = Join-Path $backendDir ".preview\keys\database.json"
  if (Test-Path $dbChoice) { Remove-Item -Force $dbChoice }
  Info "已清空预览数据库"
}
Info "预览数据目录：$previewData"
$legacyConfig = Join-Path $workDir "scripts\preview-sample.ini"
if (-not $NoSeed -and (Test-Path $legacyConfig)) {
  # 只读导入伪造的示例配置；库里已有资产就跳过
  # 直接输出到窗口（不捕获，避免中文按代码页转换出错）
  Push-Location $backendDir
  try { & $venvPy -m app.legacy_import --config $legacyConfig --apply --if-empty --brief }
  finally { Pop-Location }
  if ($LASTEXITCODE -ne 0) { Info "自动导入旧配置失败（不影响预览），见上方提示" }
}

# ---------- 5. 启动 ----------
$BackendPort = Get-FreePort $BackendPort
$FrontendPort = Get-FreePort $FrontendPort
$apiUrl = "http://127.0.0.1:$BackendPort"
$webUrl = "http://127.0.0.1:$FrontendPort/"

$viteConfig = Get-Content -Raw (Join-Path $frontendDir "vite.config.ts")
if ($viteConfig -notmatch '"/api"') { Info "注意：该分支的 vite.config.ts 还没有 /api 代理，前端调用接口会失败（页面本身不受影响）" }

$backend = $null
$frontend = $null
try {
  Info "启动后端 $apiUrl（改 backend/app 自动重载）"
  # 首次登录 admin / admin123，按提示改密码
  $backend = Start-Process -PassThru -NoNewWindow -WorkingDirectory $backendDir -FilePath $venvPy `
    -ArgumentList @("-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1",
                    "--port", "$BackendPort", "--reload", "--reload-dir", "app")

  Info "启动前端 $webUrl（改 frontend/src 页面即时刷新）"
  $env:YUNSHU_API_URL = $apiUrl
  $viteBin = Join-Path $frontendDir "node_modules\vite\bin\vite.js"
  $frontend = Start-Process -PassThru -NoNewWindow -WorkingDirectory $frontendDir -FilePath "node" `
    -ArgumentList @("`"$viteBin`"", "--host", "127.0.0.1", "--port", "$FrontendPort", "--strictPort")

  if (-not (Wait-Http "$apiUrl/api/health" 60 $backend)) { Fail "后端没有就绪（已退出或 60 秒超时），见上方日志" }
  if (-not (Wait-Http $webUrl 60 $frontend)) { Fail "前端没有就绪（已退出或 60 秒超时），见上方日志" }

  Write-Host ""
  Write-Host "  预览已就绪：$webUrl" -ForegroundColor Green
  Write-Host "  分支：$branch"
  Write-Host "  目录：$workDir"
  Write-Host "  后端：$apiUrl/docs"
  Write-Host "  按 Ctrl+C 停止"
  Write-Host ""
  if (-not $NoBrowser) { Start-Process $webUrl }

  while (-not $backend.HasExited -and -not $frontend.HasExited) { Start-Sleep -Seconds 1 }
  Info "有一个服务退出了，正在停止另一个"
} finally {
  Stop-Tree $frontend
  Stop-Tree $backend
  # 启动器若比子进程先退出，taskkill /T 找不到下面两层；按心跳再清一次
  if (-not (Test-InUse $backendDir)) { Stop-PreviewOrphans $venvPy $previewDb }
  Info "已停止"
}
