# gp 一键安装（Windows PowerShell）：
#   irm https://raw.githubusercontent.com/dikaiyang6-maker/stock-desk/main/tools/install.ps1 | iex
# 装什么：独立的 Python 虚拟环境（%USERPROFILE%\.stock-tools\venv）里的 akshare、easyquotation、mootdx、pywencai（增强版 pywencai-enhanced）、akquant，
#         有 Node.js 时再装 stock-api（%USERPROFILE%\.stock-tools\node），最后把 gp 命令加进用户 PATH。
# 重复运行 = 升级到最新版。卸载：删除 %USERPROFILE%\.stock-tools，并从用户环境变量 Path 里去掉 ...\.stock-tools\bin

function Install-StockTools {
    $ErrorActionPreference = "Stop"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $Raw = "https://raw.githubusercontent.com/dikaiyang6-maker/stock-desk/main"
    $Dir = if ($env:STOCK_TOOLS_HOME) { $env:STOCK_TOOLS_HOME } else { Join-Path $env:USERPROFILE ".stock-tools" }
    $Bin = Join-Path $Dir "bin"
    New-Item -ItemType Directory -Force -Path $Dir, $Bin | Out-Null

    # 1. 找 Python 3.11+（akshare 要求 3.11 以上）
    $py = $null
    foreach ($c in @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("python"), @("python3"))) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $rest = @($c | Select-Object -Skip 1)
        $ok = & $c[0] @rest -c "import sys; print(int(sys.version_info >= (3, 11)))" 2>$null
        if ("$ok".Trim() -eq "1") { $py = $c; break }
    }
    if (-not $py) {
        Write-Host "需要 Python 3.11 或更高版本：https://www.python.org/downloads/ （安装时勾选 Add python.exe to PATH），装好后重新运行。" -ForegroundColor Yellow
        return
    }
    $rest = @($py | Select-Object -Skip 1)
    Write-Host "==> 使用 $($py -join ' ')（$(& $py[0] @rest --version)）" -ForegroundColor Cyan

    # 2. 虚拟环境 + Python 库
    $vpy = Join-Path $Dir "venv\Scripts\python.exe"
    if (-not (Test-Path $vpy)) {
        Write-Host "==> 创建虚拟环境 $Dir\venv" -ForegroundColor Cyan
        & $py[0] @rest -m venv (Join-Path $Dir "venv")
        if ($LASTEXITCODE -ne 0) { Write-Host "创建虚拟环境失败" -ForegroundColor Red; return }
    }
    Write-Host "==> 安装 / 升级 Python 库（第一次要几分钟）" -ForegroundColor Cyan
    & $vpy -m pip install -q -U pip setuptools wheel
    & $vpy -m pip install -q -U --use-pep517 easyutils
    & $vpy -m pip install -q -U akshare easyquotation mootdx akquant
    & $vpy -m pip uninstall -q -y pywencai 2>$null
    & $vpy -m pip install -q -U "https://github.com/HeRiki/pywencai-enhanced/archive/883f968ea6e34fd0f68f2a32ad4c21cc2dd15945.zip"
    if ($LASTEXITCODE -ne 0) { Write-Host "Python 库安装失败，看上面的报错；网络不稳时重新运行即可" -ForegroundColor Red; return }

    # 3. stock-api（Node.js）
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        Write-Host "==> 安装 / 升级 stock-api" -ForegroundColor Cyan
        & npm install --prefix (Join-Path $Dir "node") --no-audit --no-fund --no-update-notifier --loglevel=error stock-api@latest | Out-Null
    } else {
        Write-Host "注意：没找到 Node.js，跳过 stock-api；问财（pywencai）也要用 Node.js。装好 Node.js 18+（https://nodejs.org）后重新运行本脚本即可。" -ForegroundColor Yellow
    }

    # 4. gp 命令（gp.cmd 用相对路径，用户名里有中文也没关系）
    Invoke-WebRequest "$Raw/tools/gp.py" -OutFile (Join-Path $Dir "gp.py") -UseBasicParsing
    $cmd = "@echo off`r`nsetlocal`r`nset `"PATH=%~dp0..\node\node_modules\.bin;%PATH%`"`r`n`"%~dp0..\venv\Scripts\python.exe`" `"%~dp0..\gp.py`" %*`r`n"
    [IO.File]::WriteAllText((Join-Path $Bin "gp.cmd"), $cmd, [Text.Encoding]::ASCII)
    Write-Host "==> 已安装 gp → $Bin\gp.cmd" -ForegroundColor Cyan

    $claude = Join-Path $env:USERPROFILE ".claude"
    if ((Test-Path $claude) -and -not $env:STOCK_TOOLS_NO_SKILL) {
        $sk = Join-Path $claude "skills\a-share-gp"
        New-Item -ItemType Directory -Force -Path $sk | Out-Null
        Invoke-WebRequest "$Raw/.claude/skills/a-share-gp/SKILL.md" -OutFile (Join-Path $sk "SKILL.md") -UseBasicParsing
        Write-Host "==> 已给本机的 Claude Code 加上技能说明（.claude\skills\a-share-gp），新开的 Claude Code 会话会知道怎么用 gp" -ForegroundColor Cyan
    }

    # 5. PATH
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $userPath) { $userPath = "" }
    if (-not (($userPath -split ";") -contains $Bin)) {
        [Environment]::SetEnvironmentVariable("Path", (($userPath.TrimEnd(";") + ";" + $Bin).TrimStart(";")), "User")
        Write-Host "注意：已把 $Bin 加进用户 PATH，新开的终端窗口里可以直接用 gp。" -ForegroundColor Yellow
    }
    $env:Path = "$Bin;$env:Path"

    Write-Host "==> 体检：" -ForegroundColor Cyan
    & (Join-Path $Bin "gp.cmd") doctor
    Write-Host ""
    Write-Host "试试：gp q 600519    gp k 600519 -n 10    gp zt    gp lhb    gp bt 600519    gp --help"
}

Install-StockTools
