#!/bin/bash
# Memory 体系守卫
# 每次 Write/Bash/Edit 后自动检查 memory 目录的完整性
# 仅在发现违规时输出，无违规则静默
# 同时写状态文件供 SessionStart 消费

# 环境变量兜底（默认值按 $HOME 推导 CC 的 per-project memory 编码，可由 CC_MEMORY_DIR 覆盖）
: "${CC_MEMORY_DIR:=$HOME/.claude/projects/${HOME//\//-}/memory}"

MEMORY_DIR="$CC_MEMORY_DIR"
INDEX_FILE="$MEMORY_DIR/MEMORY.md"
CLAUDE_MD="$HOME/.claude/CLAUDE.md"
GUARD_STATE="$HOME/.claude/instincts/.memory-guard-state.json"
VIOLATIONS=0
MAX_INDEX_LINES=150
MAX_CLAUDE_LINES=200
ORPHANS=()
DANGLING=()
FRONTMATTER_BAD=0
INDEX_OVER=0
CLAUDE_OVER=0

# === 快速检查（轻量，秒级完成）===

# 1. 目录存在性
if [ ! -d "$MEMORY_DIR" ]; then
  echo "🚨 Memory 守卫: memory 目录不存在: $MEMORY_DIR"
  exit 1
fi

# 2. 索引文件存在
if [ ! -f "$INDEX_FILE" ]; then
  echo "🚨 Memory 守卫: MEMORY.md 索引文件缺失"
  VIOLATIONS=$((VIOLATIONS + 1))
fi

# 3. 索引行数超限
index_lines=$(wc -l < "$INDEX_FILE" 2>/dev/null || echo 0)
if [ "$index_lines" -gt "$MAX_INDEX_LINES" ]; then
  echo "🚨 Memory 守卫: MEMORY.md $index_lines 行，超过 $MAX_INDEX_LINES 行上限"
  VIOLATIONS=$((VIOLATIONS + 1))
  INDEX_OVER=1
fi

# 4. CLAUDE.md 行数超限
claude_lines=0  # 初始化：朋友无 ~/.claude/CLAUDE.md 时不进 if 块，避免 heredoc 引用未定义变量致 Python SyntaxError
if [ -f "$CLAUDE_MD" ]; then
  claude_lines=$(wc -l < "$CLAUDE_MD" 2>/dev/null || echo 0)
  if [ "$claude_lines" -gt "$MAX_CLAUDE_LINES" ]; then
    echo "🚨 Memory 守卫: CLAUDE.md $claude_lines 行，超过 $MAX_CLAUDE_LINES 行上限"
    VIOLATIONS=$((VIOLATIONS + 1))
    CLAUDE_OVER=1
  fi
fi

# 5. 检查孤儿文件（存在但未在 MEMORY.md 中索引）
for f in "$MEMORY_DIR"/*.md; do
  [ -e "$f" ] || continue
  name=$(basename "$f")
  [ "$name" = "MEMORY.md" ] && continue

  if ! grep -q "$name" "$INDEX_FILE" 2>/dev/null; then
    echo "🚨 Memory 守卫: '$name' 未在 MEMORY.md 中索引（孤儿文件）"
    VIOLATIONS=$((VIOLATIONS + 1))
    ORPHANS+=("$name")
  fi
done

# 6. 检查毕业不一致：MEMORY.md 标记 ~~已毕业~~ 但 memory 文件仍存在
STALE_GRADUATION=()
if [ -f "$INDEX_FILE" ]; then
  while IFS= read -r line; do
    # 提取 ~~名称~~（中文条目名）
    entry_name=$(echo "$line" | grep -oE '~~[^~]+~~' | head -1 | sed 's/^~~//;s/~~$//' 2>/dev/null)
    if [ -n "$entry_name" ]; then
      # 从 build-asset-manifest.py 等已知映射检查——简化版：检查常见对应关系
      # 直接检查是否有同名（或相近名）的 md 文件仍存在
      norm=$(echo "$entry_name" | sed 's/[[:space:]/]//g' | tr '[:upper:]' '[:lower:]')
      for f in "$MEMORY_DIR"/*.md; do
        [ -e "$f" ] || continue
        fname=$(basename "$f" .md)
        fname_norm=$(echo "$fname" | tr '[:upper:]' '[:lower:]' | sed 's/-[0-9][0-9-]*$//')
        # 如果条目名包含在文件名中（模糊匹配），文件仍存在 → 毕业不完整
        if [ ${#norm} -gt 4 ] && echo "$fname_norm" | grep -q "$norm" 2>/dev/null; then
          STALE_GRADUATION+=("$fname.md")
          break
        fi
      done
    fi
  done < <(grep '~~' "$INDEX_FILE" 2>/dev/null)
fi

if [ ${#STALE_GRADUATION[@]} -gt 0 ]; then
  for sg in "${STALE_GRADUATION[@]}"; do
    echo "⚠️  Memory 守卫: MEMORY.md 标记 ~~已毕业~~ 但文件仍存在: ${sg}（应删除或取消标记）"
    VIOLATIONS=$((VIOLATIONS + 1))
  done
fi

# 7. 检查索引中引用但不存在的文件
if [ -f "$INDEX_FILE" ]; then
  while IFS= read -r ref; do
    # 跳过 wiki/ 引用和非 .md 引用
    [[ "$ref" == wiki/* ]] && continue
    [[ "$ref" != *.md ]] && continue

    if [ ! -f "$MEMORY_DIR/$ref" ]; then
      echo "🚨 Memory 守卫: MEMORY.md 引用 '$ref' 但文件不存在"
      VIOLATIONS=$((VIOLATIONS + 1))
      DANGLING+=("$ref")
    fi
  done < <(grep -oE '\]\([^)]+\.md\)' "$INDEX_FILE" 2>/dev/null | sed -E 's/^\]\(//; s/\)$//')
fi

# 8. 检查 frontmatter 健康（快速抽样：检查 type 字段）
for f in "$MEMORY_DIR"/*.md; do
  [ -e "$f" ] || continue
  [ "$(basename "$f")" = "MEMORY.md" ] && continue

  # 检查是否有 frontmatter
  if ! head -1 "$f" | grep -q '^---$'; then
    FRONTMATTER_BAD=$((FRONTMATTER_BAD + 1))
    continue
  fi

  # 检查 type 字段
  type_count=$(sed -n '/^---$/,/^---$/p' "$f" | grep -c '^  type:')
  if [ "$type_count" -gt 1 ]; then
    FRONTMATTER_BAD=$((FRONTMATTER_BAD + 1))
  fi
done

if [ "$FRONTMATTER_BAD" -gt 0 ]; then
  echo "🚨 Memory 守卫: $FRONTMATTER_BAD 个文件 frontmatter 异常（缺 type 或重复 type）"
  VIOLATIONS=$((VIOLATIONS + 1))
fi

# === 判定严重级别 ===
ORPHAN_COUNT=${#ORPHANS[@]}
DANGLING_COUNT=${#DANGLING[@]}

SEVERITY="green"
SEVERITY_REASON=""

# 🔴 必须处理
STALE_GRADUATION_COUNT=${#STALE_GRADUATION[@]}
if [ "$ORPHAN_COUNT" -ge 5 ] || [ "$INDEX_OVER" -eq 1 ] || [ "$DANGLING_COUNT" -gt 0 ] || [ "$CLAUDE_OVER" -eq 1 ] || [ "$STALE_GRADUATION_COUNT" -gt 0 ]; then
  SEVERITY="red"
  SEVERITY_REASON="孤儿≥5/索引超限/断链引用/CLAUDE.md超限/毕业残留"
# 🟡 该看看了
elif [ "$ORPHAN_COUNT" -ge 3 ] || [ "$FRONTMATTER_BAD" -ge 3 ] || [ "$STALE_GRADUATION_COUNT" -gt 0 ]; then
  SEVERITY="yellow"
  SEVERITY_REASON="孤儿3-4/frontmatter异常≥3/毕业残留"
fi

# === 写状态文件 ===
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
NOW_EPOCH=$(date +%s)

# 将数组写入临时文件（避免嵌套引号地狱）
ORPHANS_FILE=$(mktemp)
DANGLING_FILE=$(mktemp)
printf '%s\n' "${ORPHANS[@]:-}" > "$ORPHANS_FILE"
printf '%s\n' "${DANGLING[@]:-}" > "$DANGLING_FILE"
TOTAL_FILES=$(ls "$MEMORY_DIR"/*.md 2>/dev/null | wc -l | tr -d ' ')
	STALE_GRADUATION_FILE=$(mktemp)
	printf '%s\n' "${STALE_GRADUATION[@]:-}" > "$STALE_GRADUATION_FILE"

python3 << PYEOF
import json

# 读取孤儿和断链列表
orphans = []
try:
    with open('$ORPHANS_FILE') as f:
        orphans = [l.strip() for l in f if l.strip()]
except: pass


stale_graduation = []
try:
    with open('$STALE_GRADUATION_FILE') as f:
        stale_graduation = [l.strip() for l in f if l.strip()]
except: pass
dangling = []
try:
    with open('$DANGLING_FILE') as f:
        dangling = [l.strip() for l in f if l.strip()]
except: pass

# 读取旧状态（保留 last_reminded 用于节流）
last_reminded = 0
reminder_count = 0
try:
    with open('$GUARD_STATE') as f:
        old = json.load(f)
        last_reminded = old.get('last_reminded_epoch', 0)
        reminder_count = old.get('reminder_count', 0)
except: pass

state = {
    'last_run': '$NOW',
    'last_run_epoch': $NOW_EPOCH,
    'severity': '$SEVERITY',
    'severity_reason': '$SEVERITY_REASON',
    'total_violations': $VIOLATIONS,
    'orphans': orphans,
    'orphan_count': len(orphans),
    'dangling_refs': dangling,
    'dangling_count': len(dangling),
    'stale_graduation': stale_graduation,
    'stale_graduation_count': len(stale_graduation),
    'frontmatter_bad': $FRONTMATTER_BAD,
    'index_over_limit': $INDEX_OVER,
    'index_lines': $index_lines,
    'index_max': $MAX_INDEX_LINES,
    'claude_md_over_limit': $CLAUDE_OVER,
    'claude_md_lines': $claude_lines,
    'claude_md_max': $MAX_CLAUDE_LINES,
    'total_files': $TOTAL_FILES,
    'last_reminded_epoch': last_reminded,
    'reminder_count': reminder_count
}
with open('$GUARD_STATE', 'w') as f:
    json.dump(state, f, indent=2)
PYEOF

rm -f "$ORPHANS_FILE" "$DANGLING_FILE" "$STALE_GRADUATION_FILE"

# === 输出结果 ===
if [ "$VIOLATIONS" -gt 0 ]; then
  echo ""
  echo "⚠️  共 $VIOLATIONS 项违规 [${SEVERITY}]。建议执行 memory 体系维护。"
  exit 1
fi

exit 0
