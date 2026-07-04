#!/bin/bash
# Skills 目录结构守卫
# 检测 ~/.claude/skills/ 下的违规项（非软连接、软连接断链、指向规范外路径）
# 仅在发现违规时输出，无违规则静默

SKILLS_DIR="$HOME/.claude/skills"
VIOLATIONS=0

# 允许的软连接目标前缀
# 环境变量 SKILLS_ALLOWED_PREFIXES 可覆盖（冒号分隔），默认含现有三项 + 机制层 plugin 开发根
if [ -n "${SKILLS_ALLOWED_PREFIXES:-}" ]; then
  IFS=':' read -r -a ALLOWED_PREFIXES <<< "$SKILLS_ALLOWED_PREFIXES"
else
  ALLOWED_PREFIXES=(
    "$HOME/Documents/My_Skills_Library"
    "$HOME/.agents/skills"
    "$HOME/.claude/plugins"
    "$HOME/Documents/My_Code_Projects"
  )
fi

# 检查目录是否存在
if [ ! -d "$SKILLS_DIR" ]; then
  exit 0
fi

# 遍历 skills 目录下所有条目
for entry in "$SKILLS_DIR"/*; do
  # -e 对断链软连接返回 false，加 -L 确保断链也能被检测到
  [ -e "$entry" ] || [ -L "$entry" ] || continue
  name=$(basename "$entry")

  # 跳过非 skill 文件（如 .DS_Store）
  [[ "$name" == .* ]] && continue

  # 规则1：必须是软连接
  if [ ! -L "$entry" ]; then
    echo "🚨 Skills 守卫: '$name' 是目录/文件而非软连接！"
    echo "   位置: $entry"
    echo "   修复: mv 到 ~/Documents/My_Skills_Library/技能库_自建/ 然后 ln -s 回来"
    VIOLATIONS=$((VIOLATIONS + 1))
    continue
  fi

  # 规则2：软连接目标必须存在
  target=$(readlink "$entry")
  # 解析相对路径
  if [[ "$target" != /* ]]; then
    target="$(cd "$(dirname "$entry")" && realpath "$target" 2>/dev/null || echo "$(dirname "$entry")/$target")"
  fi
  if [ ! -e "$entry" ]; then
    echo "🚨 Skills 守卫: '$name' 软连接断链！"
    echo "   目标: $target"
    echo "   修复: 检查源文件是否被移动/删除，重新创建软连接"
    VIOLATIONS=$((VIOLATIONS + 1))
    continue
  fi

  # 规则3：软连接目标必须在允许的路径范围内
  ok=0
  for prefix in "${ALLOWED_PREFIXES[@]}"; do
    if [[ "$target" == "$prefix"* ]]; then
      ok=1
      break
    fi
  done
  if [ "$ok" -eq 0 ]; then
    echo "🚨 Skills 守卫: '$name' 指向规范外路径！"
    echo "   目标: $target"
    echo "   允许前缀: ${ALLOWED_PREFIXES[*]}"
    VIOLATIONS=$((VIOLATIONS + 1))
  fi
done

# 有违规时以非零退出码返回（触发 CC 注意）
if [ "$VIOLATIONS" -gt 0 ]; then
  echo ""
  echo "⚠️  共 $VIOLATIONS 项违规。请执行 /skills-management 修复。"
  exit 1
fi

# ── Lint 门禁（仅在 skills 目录有变更时运行）──
STATE_FILE="$HOME/.claude/skills/.guard-state"
current_state=$(for d in "$SKILLS_DIR"/*/; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  [[ "$name" == .* ]] && continue
  target=$(readlink "$d" 2>/dev/null || echo "DIR:$d")
  echo "$name|$target"
done | sort | md5 2>/dev/null || md5sum 2>/dev/null || echo "no-md5")

saved_state=$(cat "$STATE_FILE" 2>/dev/null || echo "")

if [ "$current_state" != "$saved_state" ]; then
  LINT_SCRIPT="$HOME/.claude/skills/skills-management/skills-lint.sh"
  # 排除的 skill（plugin 形态非 skill，如 tongle 自身），可由 SKILLS_LINT_EXCLUDE 覆盖
  # 默认 tongle（release 安装目录名）；指挥官自用 knowledge-engine 目录请 export SKILLS_LINT_EXCLUDE=knowledge-engine
  EXCLUDE="${SKILLS_LINT_EXCLUDE:-tongle}"
  if [ -x "$LINT_SCRIPT" ] || [ -f "$LINT_SCRIPT" ]; then
    lint_output=$(bash "$LINT_SCRIPT" --json 2>/dev/null || true)
    if [ -n "$lint_output" ]; then
      lint_summary=$(echo "$lint_output" | SKILLS_LINT_EXCLUDE="$EXCLUDE" python3 -c "
import sys, json, os
text = sys.stdin.read()
idx = text.find('{')
if idx < 0: sys.exit(0)
exclude = [e.strip() for e in os.environ.get('SKILLS_LINT_EXCLUDE', '').split(',') if e.strip()]
try:
    d = json.loads(text[idx:])
    fails = [x for x in d.get('results', {}).get('fail', []) if not any(x.startswith('FAIL|'+e+'|') for e in exclude)]
    warns = [x for x in d.get('results', {}).get('warn', []) if not any(x.startswith('WARN|'+e+'|') for e in exclude)]
    if fails or warns:
        print(f'{len(fails)} FAIL, {len(warns)} WARN')
except: pass
" 2>/dev/null)
      if [ -n "$lint_summary" ]; then
        echo ""
        echo "🔍 Skills Lint: $lint_summary — 执行 /skills-management 检查"
      fi
    fi
  fi
  echo "$current_state" > "$STATE_FILE"
fi

exit 0
