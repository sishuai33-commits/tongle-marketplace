#!/bin/bash
# env-check.sh — 朋友部署前环境评估（设计要求1：环境评估前置）
#
# 目的：安装前先评估朋友环境是否具备条件，缺啥引导装，不假设就绪。
# 纯检测脚本，不改任何东西。跑通 manage.sh install 前先跑这个。
#
# 用法: bash scripts/env-check.sh
set -uo pipefail

PASS=0; WARN=0; FAIL=0
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
warn() { printf "  \033[33m⚠️\033[0m %s\n" "$1"; WARN=$((WARN+1)); }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=$((FAIL+1)); }
hint() { printf "      %s\n" "$1"; }

CLAUDE_DIR="$HOME/.claude"
# CC 的 per-project memory 按 $HOME 路径编码命名（与 hook 默认值同一推导）
HOME_CODED="${HOME//\//-}"
CC_MEMORY_DEFAULT="$CLAUDE_DIR/projects/$HOME_CODED/memory"
VAULT_DEFAULT="$HOME/Documents/Obsidian Vault"

echo "# tongle 环境评估"
echo "  推导: HOME=$HOME → memory 编码=$HOME_CODED"
echo

# ── 0. 平台识别（ke 的 hook 是 bash 脚本，Windows 原生无 bash 是命门）──
echo "## 0. 平台与 bash 运行环境"
IS_WINDOWS=0
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1; PLATFORM="Windows (Git Bash/MSYS)" ;;
  Darwin) PLATFORM="macOS" ;;
  Linux)  PLATFORM="Linux" ;;
  *)      PLATFORM="未知: $(uname -s 2>/dev/null || echo '无法识别')" ;;
esac
ok "运行平台: $PLATFORM"
# 能跑到这里说明当前 shell 有 bash；但 Windows 原生上 CC 触发 hook 时是否用 bash 是另一回事
if [ "$IS_WINDOWS" -eq 1 ]; then
  warn "Windows 原生检测到 — ke 的 hook 是 bash 脚本，CC 在 Windows 原生上执行 hook 需要 bash 环境"
  hint "若你在 WSL 或 Git Bash 里跑此脚本（PATH 有 bash），install 能装，但 CC 将来触发 hook 也要同环境"
  hint "推荐: 在 WSL 2 里装 claude + ke（最稳，linux 全兼容）"
  hint "或: 装 Git for Windows，确保 CC 用 Git Bash 执行 hook（python3/路径编码仍可能有坑，见 INSTALL）"
fi

# ── 1. 核心依赖：Claude Code 本体 ──
echo "## 1. Claude Code（核心依赖）"
if command -v claude >/dev/null 2>&1; then
  ok "claude CLI 已安装: $(claude --version 2>/dev/null || echo '版本未知')"
else
  fail "未检测到 claude CLI — tongle 是 CC 插件，必须先装 Claude Code"
  hint "安装: npm install -g @anthropic-ai/claude-code"
fi

# ── 2. 运行时：python3（hook 脚本硬依赖，Windows 上注意命令名）──
echo "## 2. Python3（hook 脚本依赖）"
if command -v python3 >/dev/null 2>&1; then
  ok "python3: $(python3 --version 2>&1)"
else
  fail "未检测到 python3 — 多数 hook 脚本（注入链/守卫）依赖 python3 命令"
  if [ "$IS_WINDOWS" -eq 1 ]; then
    hint "Windows 上 Python 装后命令通常是 python 而非 python3"
    hint "WSL: sudo apt install python3  ｜  原生: 装 Python 后建 python3 别名或复制 python.exe → python3.exe"
  else
    hint "macOS: brew install python3  /  或装 Xcode Command Line Tools"
  fi
fi

# ── 3. 运行时：node（必需，pre-security 守卫每次工具调用都跑 node）──
echo "## 3. Node.js（必需，pre-security 守卫依赖）"
if command -v node >/dev/null 2>&1; then
  ok "node: $(node --version 2>&1)"
else
  fail "未检测到 node — pre-security 守卫（PreToolUse 每次工具调用都跑 node）会失效"
  hint "安装: macOS brew install node / Windows https://nodejs.org/ （装过 Claude Code 即自带 node）"
fi

# ── 4. CC 配置目录 ──
echo "## 4. Claude Code 配置目录"
if [ -d "$CLAUDE_DIR" ]; then
  ok "CC 配置目录存在: $CLAUDE_DIR"
else
  warn "CC 配置目录不存在: $CLAUDE_DIR"
  hint "首次启动 claude CLI 会自动创建，可先跑一次 claude 再回来"
fi

# ── 5. skills 目录（plugin 安装目标）──
SKILLS_DIR="$CLAUDE_DIR/skills"
if [ -d "$SKILLS_DIR" ]; then
  ok "skills 目录存在: $SKILLS_DIR"
else
  warn "skills 目录不存在 — manage.sh install 会自动创建"
fi

# ── 6. memory 目录（注入链核心依赖）──
echo "## 5. Memory 目录（注入链核心依赖）"
if [ -d "$CC_MEMORY_DEFAULT" ]; then
  MEM_COUNT=$(ls "$CC_MEMORY_DEFAULT"/*.md 2>/dev/null | wc -l | tr -d ' ')
  ok "memory 目录存在: $CC_MEMORY_DEFAULT ($MEM_COUNT 个 md 文件)"
  if [ "$MEM_COUNT" -eq 0 ]; then
    warn "memory 目录为空 — 首次对话会积累，working-memory.md 用 examples/ 模板初始化"
  fi
else
  warn "默认 memory 目录不存在: $CC_MEMORY_DEFAULT"
  # HOME 推导指向空目录时，扫描已有 per-project memory 帮朋友定位真路径
  FOUND_MEMS=$(find "$CLAUDE_DIR/projects" -maxdepth 2 -type d -name memory 2>/dev/null | head -5)
  if [ -n "$FOUND_MEMS" ]; then
    hint "扫描到以下 memory 目录（你的 CC memory 可能在这里）："
    echo "$FOUND_MEMS" | while read -r m; do hint "  $m"; done
    hint "memory 路径由你启动 claude 的目录决定。若上面有你的，安装后设:"
    hint "  export CC_MEMORY_DIR=<上面你那个路径>"
  else
    hint "暂无 memory 目录 — 与 CC 对话后会在对应项目目录下生成"
    hint "CC memory 路径编码: ~/.claude/projects/<启动目录路径(斜杠转短横)>/memory"
  fi
fi

# ── 7. Obsidian Vault（wiki 知识域，可选）──
echo "## 6. Obsidian Vault（wiki 知识域，可选）"
if [ -d "$VAULT_DEFAULT" ]; then
  ok "默认 Vault 存在: $VAULT_DEFAULT"
  if [ -f "$VAULT_DEFAULT/wiki/.ai-vocab.md" ]; then
    ok "wiki/.ai-vocab.md 存在（注入链 wiki 侧就绪）"
  else
    warn "Vault 存在但缺 wiki/.ai-vocab.md — 用 examples/min-knowledge-base/ 模板初始化"
  fi
else
  warn "默认 Vault 不存在: $VAULT_DEFAULT"
  hint "若用 Obsidian 且 Vault 在别处，安装后设: export WIKI_VAULT_PATH=你的Vault路径"
  hint "若不用 Obsidian，wiki 注入空转（不影响 memory 注入与守卫）"
fi

# ── 8. 源1 扫描根 SOURCE_ROOT（采集环 local_file 源，可选但推荐）──
echo "## 7. 源1 扫描根 SOURCE_ROOT（采集环 local_file 源，可选）"
SOURCE_DEFAULT="$HOME/Documents/My_Code_Projects"
if [ -n "${SOURCE_ROOT:-}" ]; then
  ok "已设 SOURCE_ROOT=$SOURCE_ROOT（采集环扫这里找文件变更）"
elif [ -d "$SOURCE_DEFAULT" ]; then
  warn "默认扫描根存在: $SOURCE_DEFAULT（指挥官路径，你的项目若不在这需覆盖）"
  hint "采集环源1 扫描这里的文件变更做判别候选。你的项目根若在别处，安装后设:"
  hint "  export SOURCE_ROOT=你的项目根目录（如 ~/code 或 ~/projects）"
else
  warn "默认扫描根不存在: $SOURCE_DEFAULT"
  hint "采集环源1 会扫不到文件变更（源2 transcript / 源3 ima 仍工作）"
  hint "你的项目根在哪？安装后设: export SOURCE_ROOT=你的项目根目录"
  hint "不设也行——源1 可选，不影响注入链与守卫"
fi

# ── 9. 推导的配置建议 ──
echo
echo "## 推导的配置（非默认位置时 manage.sh install 会提示设置环境变量）"
echo "  CC_MEMORY_DIR  = $CC_MEMORY_DEFAULT"
echo "  WIKI_VAULT_PATH = $VAULT_DEFAULT"
echo "  SOURCE_ROOT    = ${SOURCE_ROOT:-$SOURCE_DEFAULT}"

echo
echo "## 评估结果: 通过=$PASS  警告=$WARN  失败=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf "\033[31m✗ 有 %d 项失败，请先解决再安装\033[0m\n" "$FAIL"
  exit 1
elif [ "$WARN" -gt 0 ]; then
  printf "\033[33m⚠️ 有 %d 项警告，不阻塞安装但部分功能可能不可用\033[0m\n" "$WARN"
  exit 0
else
  printf "\033[32m✓ 环境就绪，可运行 ./manage.sh install\033[0m\n"
  exit 0
fi
