#!/usr/bin/env bash
# gp 一键安装（Mac / Linux）：
#   curl -fsSL https://raw.githubusercontent.com/dikaiyang6-maker/stock-desk/main/tools/install.sh | bash
# 装什么：独立的 Python 虚拟环境（~/.stock-tools/venv）里的 akshare、easyquotation、mootdx、pywencai、akquant，
#         有 Node.js 时再装 stock-api（~/.stock-tools/node），最后把 gp 命令放进 ~/.local/bin。
# 重复运行 = 升级到最新版。卸载：rm -rf ~/.stock-tools ~/.local/bin/gp
set -euo pipefail

RAW="https://raw.githubusercontent.com/dikaiyang6-maker/stock-desk/main"
DIR="${STOCK_TOOLS_HOME:-$HOME/.stock-tools}"
BIN="${STOCK_TOOLS_BIN:-$HOME/.local/bin}"
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m注意：\033[0m%s\n' "$*"; }

# 1. 找 Python 3.11+（akshare 要求 3.11 以上）
PY=""
for c in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  echo "需要 Python 3.11 或更高版本。Mac：brew install python@3.12（或到 https://www.python.org/downloads/ 下载）；Ubuntu：sudo apt install python3.12 python3.12-venv" >&2
  exit 1
fi
say "使用 $PY（$("$PY" --version 2>&1)）"

# 2. 虚拟环境 + Python 库
mkdir -p "$DIR" "$BIN"
if [ ! -x "$DIR/venv/bin/python" ]; then
  say "创建虚拟环境 $DIR/venv"
  "$PY" -m venv "$DIR/venv" || { echo "创建虚拟环境失败。Ubuntu / Debian 先运行：sudo apt install python3-venv" >&2; exit 1; }
fi
VPY="$DIR/venv/bin/python"
say "安装 / 升级 Python 库（第一次要几分钟）"
"$VPY" -m pip install -q -U pip setuptools wheel
"$VPY" -m pip install -q -U --use-pep517 PyExecJS easyutils        # 这两个只有源码包，要用新式构建
"$VPY" -m pip install -q -U akshare easyquotation mootdx pywencai akquant

# 3. stock-api（Node.js）
if command -v npm >/dev/null 2>&1; then
  say "安装 / 升级 stock-api（Node.js $(node --version 2>/dev/null)）"
  npm install --prefix "$DIR/node" --no-audit --no-fund --no-update-notifier --loglevel=error stock-api@latest >/dev/null
else
  warn "没找到 Node.js（npm），跳过 stock-api；问财（pywencai）也要用 Node.js。装好 Node.js 18+ 后重新运行本脚本即可。"
fi

# 4. gp 命令和 Claude Code 技能说明
SRC="${BASH_SOURCE[0]:-}"; HERE=""
if [ -n "$SRC" ] && [ -f "$SRC" ]; then HERE="$(cd "$(dirname "$SRC")" && pwd)"; fi     # curl | bash 时没有本地文件
fetch() {  # fetch 仓库内路径 目标文件：在仓库里运行时用本地文件，用 curl | bash 运行时从 GitHub 下载
  if [ -n "$HERE" ] && [ -f "$HERE/../$1" ]; then cp "$HERE/../$1" "$2"; else curl -fsSL "$RAW/$1" -o "$2"; fi
}
fetch tools/gp.py "$DIR/gp.py"
cat > "$BIN/gp" <<EOF
#!/usr/bin/env bash
export PATH="$DIR/node/node_modules/.bin:\$PATH"
exec "$VPY" "$DIR/gp.py" "\$@"
EOF
chmod +x "$BIN/gp"
say "已安装 gp → $BIN/gp"

if [ -d "$HOME/.claude" ] && [ -z "${STOCK_TOOLS_NO_SKILL:-}" ]; then
  mkdir -p "$HOME/.claude/skills/a-share-gp"
  fetch .claude/skills/a-share-gp/SKILL.md "$HOME/.claude/skills/a-share-gp/SKILL.md"
  say "已给本机的 Claude Code 加上技能说明（~/.claude/skills/a-share-gp），新开的 Claude Code 会话会知道怎么用 gp"
fi

# 5. PATH
case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    line="export PATH=\"$BIN:\$PATH\""
    for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
      if [ -f "$rc" ] || [ "$rc" = "$HOME/.zshrc" -a "$(uname)" = "Darwin" ]; then
        grep -qsF "$line" "$rc" || printf '\n# gp（A 股数据命令行）\n%s\n' "$line" >> "$rc"
      fi
    done
    warn "已把 $BIN 加进 PATH（写在 shell 配置文件里），请新开一个终端窗口再用 gp。"
    ;;
esac

say "体检："
"$BIN/gp" doctor || true
echo
echo "试试：gp q 600519    gp k 600519 -n 10    gp zt    gp lhb    gp bt 600519    gp --help"
