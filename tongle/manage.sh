#!/bin/bash
# manage.sh — tongle 生命周期管理
# 用法: bash manage.sh [install|uninstall|upgrade|status]
#   install   — 环境评估 + symlink 安装 + 验证
#   uninstall — 删 symlink + 清 ke 运行时状态（保留你填的 config/assets）
#   upgrade   — zip 分发：提示重新下载 + reinstall
#   status    — 显示安装状态 + ke 在本环境写的文件清单
set -uo pipefail

RELEASE_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
SKILLS_DIR="$CLAUDE_DIR/skills"
TARGET="$SKILLS_DIR/tongle"
INSTINCTS_DIR="$CLAUDE_DIR/instincts"

# ke 产生的 instincts 运行时状态（uninstall 清理用，完整清单）
# 注：patterns.yaml / .alert-snooze 是朋友手填资产（instinct/静音规则），不在此列 → uninstall 保留
KE_STATE_FILES=(
  "active-context.md"          # session-start 写
  ".pending-wiki-sync"         # session-end 写/session-start 消费
  ".memory-guard-state.json"   # memory-guard 写
  ".maintenance-due"           # maintenance-guard 写
  ".ke-plugin-probe"           # 探针 marker
  "observations.jsonl"         # observe 写
  ".observer-signal-counter"   # observe 计数
  ".observer-pending"           # observe 待处理
  ".security-violations.json"  # pre-security 写（含凭证片段，卸载必清）
)
# 目录形态的 ke 运行时状态（rm -rf 清理；rm -f 对目录无效会留垃圾）
KE_STATE_DIRS=( ".cost-state" "observations.archive" )  # cost-guard 状态 / observe 归档

red()    { printf "\033[31m%s\033[0m\n" "$1"; }
green()  { printf "\033[32m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }

# ── install ──
do_install() {
  echo "# 安装 tongle"
  echo "  源目录: ${RELEASE_DIR}"
  echo "  目标:   ${TARGET} (symlink)"
  echo
  echo "## 步骤 1/4: 环境评估"
  if [ -f "${RELEASE_DIR}/scripts/env-check.sh" ]; then
    bash "${RELEASE_DIR}/scripts/env-check.sh"
    rc=$?
    if [ "$rc" -ne 0 ]; then
      echo; red "✗ 环境评估未通过，安装中止。请按提示解决后重试。"; exit 1
    fi
  else
    yellow "  (env-check.sh 不存在，跳过环境评估)"
  fi
  echo
  echo "## 步骤 2/4: 准备 skills 目录"
  mkdir -p "${SKILLS_DIR}"
  green "  skills 目录就绪: ${SKILLS_DIR}"
  echo
  echo "## 步骤 3/4: 安装 plugin (symlink)"
  if [ -L "${TARGET}" ]; then
    OLD=$(readlink "${TARGET}")
    yellow "  已存在 symlink → ${OLD}，将替换"
    rm "${TARGET}"
  elif [ -e "${TARGET}" ]; then
    BAK="${TARGET}.bak.$(date +%s)"
    yellow "  已存在 ${TARGET}（非 symlink），备份为 ${BAK}"
    mv "${TARGET}" "${BAK}"
  fi
  ln -s "${RELEASE_DIR}" "${TARGET}"
  green "  已安装: ${TARGET} → ${RELEASE_DIR}"
  echo
  echo "## 步骤 4/4: 验证 + 环境变量"
  ok=1
  [ -f "${TARGET}/.claude-plugin/plugin.json" ] && green "  plugin.json 可读" || { red "  plugin.json 缺失"; ok=0; }
  [ -f "${TARGET}/hooks/hooks.json" ] && green "  hooks.json 存在（hook 注册入口）" || { red "  hooks.json 缺失"; ok=0; }
  HOME_CODED="${HOME//\//-}"
  CC_MEM_DEFAULT="${CLAUDE_DIR}/projects/${HOME_CODED}/memory"
  VAULT_DEFAULT="${HOME}/Documents/Obsidian Vault"
  SOURCE_DEFAULT="${HOME}/Documents/My_Code_Projects"
  echo
  echo "## 环境变量检查（默认位置无需设置）"
  NEED_ENV=0
  if [ ! -d "${CC_MEM_DEFAULT}" ]; then
    yellow "  ⚠️ 默认 memory 目录不存在: ${CC_MEM_DEFAULT}"
    echo "    若你的 memory 在别处，在 ~/.zshrc 加: export CC_MEMORY_DIR=你的memory路径"
    NEED_ENV=1
  fi
  if [ ! -d "${VAULT_DEFAULT}" ]; then
    yellow "  ⚠️ 默认 Vault 不存在: ${VAULT_DEFAULT}"
    echo "    若用 Obsidian 且 Vault 在别处，在 ~/.zshrc 加: export WIKI_VAULT_PATH=你的Vault路径"
    NEED_ENV=1
  fi
  if [ ! -d "${SOURCE_DEFAULT}" ] || [ -z "${SOURCE_ROOT:-}" ] && [ ! -d "${SOURCE_DEFAULT}" ]; then
    yellow "  ⚠️ 默认源1 扫描根不存在: ${SOURCE_DEFAULT}"
    echo "    采集环源1 扫这里找文件变更。你的项目根若在别处，在 ~/.zshrc 加: export SOURCE_ROOT=你的项目根目录"
    echo "    （源1 可选，不设不影响注入链与守卫，源2 transcript / 源3 ima 仍工作）"
    NEED_ENV=1
  fi
  [ "$NEED_ENV" -eq 0 ] && green "  默认位置均存在，无需设置环境变量"
  # 生成 .ke-env：固化推导路径，供 QUICKSTART 复制示例库前 source
  # （cp 命令依赖 $CC_MEMORY_DIR/$WIKI_VAULT_PATH，未导出则展开为空写根目录 → 朋友必崩）
  KE_MEM="${CC_MEMORY_DIR:-${CC_MEM_DEFAULT}}"
  KE_VAULT="${WIKI_VAULT_PATH:-${VAULT_DEFAULT}}"
  KE_SOURCE="${SOURCE_ROOT:-${SOURCE_DEFAULT}}"
  cat > "${RELEASE_DIR}/.ke-env" <<ENVEOF
# tongle 环境变量（manage.sh install 自动生成）
# 用法: source .ke-env  （之后 CC_MEMORY_DIR / WIKI_VAULT_PATH / SOURCE_ROOT 可用于 cp 等命令）
# 若 memory/Vault/项目根 不在以下位置，改这里或 ~/.zshrc export 后重跑 install
export CC_MEMORY_DIR="${KE_MEM}"
export WIKI_VAULT_PATH="${KE_VAULT}"
export SOURCE_ROOT="${KE_SOURCE}"
ENVEOF
  green "  已生成 .ke-env（QUICKSTART 复制示例库前先 source .ke-env）"
  echo
  if [ "$ok" -eq 1 ]; then
    green "## ✓ 安装完成"
    echo "  下次启动 claude，SessionStart hook 自动加载（注入链 / 守卫 / 记忆健康）。"
    echo "  hook 会话级加载：本会话不生效，新开 claude 会话才触发。"
    echo "  首次使用：参考 docs/QUICKSTART.md"
    [ "$NEED_ENV" -eq 1 ] && yellow "  提醒：设置环境变量后 source ~/.zshrc 再启动 claude。"
    exit 0
  else
    red "## ✗ 安装可能不完整，请检查上方报错"; exit 1
  fi
}

# ── uninstall ──
do_uninstall() {
  echo "# 卸载 tongle"
  echo "  保留：你填的 config/ + assets/ + instincts/patterns.yaml + .alert-snooze（数据/规则不动）"
  echo "  清理：symlink + ke 自动产生的运行时状态（instincts/ 下 ke 写的文件/目录）"
  echo "  不动：~/.claude/settings.json（ke 不写它）+ 你的 CC_MEMORY_DIR/WIKI_VAULT_PATH"
  echo
  echo "## 步骤 1/2: 删 symlink"
  if [ -L "${TARGET}" ]; then
    OLD=$(readlink "${TARGET}")
    rm "${TARGET}"
    green "  已删 symlink: ${TARGET} → ${OLD}"
  elif [ -e "${TARGET}" ]; then
    yellow "  ${TARGET} 存在但非 symlink（可能手动复制安装），跳过删除"
    echo "    如需删除请手动: rm -rf ${TARGET}"
  else
    yellow "  ${TARGET} 不存在（未安装或已卸载）"
  fi
  echo
  echo "## 步骤 2/2: 清 ke 运行时状态（instincts/）"
  if [ -d "${INSTINCTS_DIR}" ]; then
    cleaned=0
    for f in "${KE_STATE_FILES[@]}"; do
      if [ -e "${INSTINCTS_DIR}/${f}" ]; then
        rm -f "${INSTINCTS_DIR}/${f}"
        echo "    清理 ${f}"
        cleaned=$((cleaned+1))
      fi
    done
    for d in "${KE_STATE_DIRS[@]}"; do
      if [ -d "${INSTINCTS_DIR}/${d}" ]; then
        rm -rf "${INSTINCTS_DIR}/${d}"
        echo "    清理 ${d}/"
        cleaned=$((cleaned+1))
      fi
    done
    green "  清理 ${cleaned} 项 ke 状态"
  else
    green "  instincts/ 不存在，无需清理"
  fi
  echo
  green "## ✓ 卸载完成"
  echo "  保留（你的数据/规则）:"
  echo "    - 源目录 config/ + assets/（持仓阈值/方法论）"
  echo "    - instincts/patterns.yaml + .alert-snooze（你手填的 instinct/静音规则）"
  echo "  如确认不再用，可手动删除源目录: ${RELEASE_DIR}"
  echo "  ke 不修改 settings.json，无需恢复配置。"
}

# ── upgrade ──
do_upgrade() {
  echo "# 升级 tongle（zip 分发）"
  echo
  if [ -L "${TARGET}" ]; then
    OLD=$(readlink "${TARGET}")
    echo "  当前安装指向: ${OLD}"
    echo
    echo "## 升级步骤："
    echo "  1. 向作者索取最新 release zip"
    echo "  2. 解压到新目录（建议保留旧目录直到确认新版正常）"
    echo "  3. 迁移你填的资产（旧目录 config/、assets/ 里你改过的文件）到新目录"
    echo "     新版 config/assets 文件已含示例注释，你的值需手动迁移（diff 比对）"
    echo "  4. 进入新目录运行: bash manage.sh install"
    echo "     install 检测旧 symlink → 提示替换 → 自动指向新源"
    echo "  5. 重启 claude（hook 会话级加载，新会话才生效）"
    echo
    yellow "  ⚠️ 升级前务必备份你填的 config/assets"
  else
    yellow "  未检测到安装（${TARGET} 不存在）"
    echo "  直接 bash manage.sh install 即可"
  fi
  echo
  echo "  查看当前状态: bash manage.sh status"
}

# ── status ──
do_status() {
  echo "# tongle 状态"
  echo
  echo "## 安装"
  if [ -L "${TARGET}" ]; then
    OLD=$(readlink "${TARGET}")
    green "  已安装: ${TARGET} → ${OLD}"
    [ -f "${TARGET}/.claude-plugin/plugin.json" ] && green "  plugin.json 可读" || red "  plugin.json 缺失"
    [ -f "${TARGET}/hooks/hooks.json" ] && green "  hooks.json 存在" || red "  hooks.json 缺失"
  else
    yellow "  未安装（${TARGET} 不存在）"
    echo "    安装: bash manage.sh install"
  fi
  echo
  HOME_CODED="${HOME//\//-}"
  CC_MEM_DEFAULT="${CLAUDE_DIR}/projects/${HOME_CODED}/memory"
  VAULT_DEFAULT="${HOME}/Documents/Obsidian Vault"
  echo "## 环境变量"
  echo "  CC_MEMORY_DIR  = ${CC_MEM_DEFAULT} $([ -d "${CC_MEM_DEFAULT}" ] && echo '✓' || echo '✗ 不存在')"
  echo "  WIKI_VAULT_PATH = ${VAULT_DEFAULT} $([ -d "${VAULT_DEFAULT}" ] && echo '✓' || echo '✗ 不存在')"
  echo
  echo "## ke 运行时状态（instincts/，卸载会清这些）"
  if [ -d "${INSTINCTS_DIR}" ]; then
    cnt=0
    for f in "${KE_STATE_FILES[@]}"; do
      [ -e "${INSTINCTS_DIR}/${f}" ] && { echo "  ${f}"; cnt=$((cnt+1)); }
    done
    for d in "${KE_STATE_DIRS[@]}"; do
      [ -d "${INSTINCTS_DIR}/${d}" ] && { echo "  ${d}/"; cnt=$((cnt+1)); }
    done
    echo "  共 ${cnt} 项"
  else
    echo "  instincts/ 不存在（未运行过）"
  fi
}

# ── main ──
CMD="${1:-install}"
case "${CMD}" in
  install)   do_install ;;
  uninstall) do_uninstall ;;
  upgrade)   do_upgrade ;;
  status)    do_status ;;
  *) echo "用法: bash manage.sh [install|uninstall|upgrade|status]"; exit 1 ;;
esac
