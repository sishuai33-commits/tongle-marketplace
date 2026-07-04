#!/bin/bash
# Memory 慢环深度健康检查
# 用法: bash memory-health.sh          → 完整报告
#       bash memory-health.sh --json   → JSON 供 AI 消费
#       bash memory-health.sh --quick  → 仅预警

: "${CC_MEMORY_DIR:=$HOME/.claude/projects/${HOME//\//-}/memory}"
MEMORY_DIR="$CC_MEMORY_DIR"
INDEX_FILE="$MEMORY_DIR/MEMORY.md"
HEALTH_LOG="$MEMORY_DIR/.memory-health-log.md"
TMPDIR="${TMPDIR:-/tmp}/memory-health-$$"
mkdir -p "$TMPDIR"
NOW=$(date +%s)
JSON=0; QUICK=0
[[ "$*" == *"--json"* ]] && JSON=1
[[ "$*" == *"--quick"* ]] && QUICK=1

# === 数据采集 ===
total=0; total_size=0
new_week=0; new_month=0; old_30=0; old_60=0; old_90=0

for f in "$MEMORY_DIR"/*.md; do
  [ -e "$f" ] || continue
  name=$(basename "$f")
  [ "$name" = "MEMORY.md" ] && continue
  [[ "$name" == .* ]] && continue

  total=$((total + 1))
  size=$(stat -f%z "$f" 2>/dev/null || echo 0)
  mtime=$(stat -f%m "$f" 2>/dev/null || echo 0)
  age_days=$(( (NOW - mtime) / 86400 ))
  total_size=$((total_size + size))

  # Age
  if [ "$age_days" -le 7 ]; then new_week=$((new_week + 1))
  elif [ "$age_days" -le 30 ]; then new_month=$((new_month + 1))
  elif [ "$age_days" -le 60 ]; then old_30=$((old_30 + 1))
  elif [ "$age_days" -le 90 ]; then old_60=$((old_60 + 1))
  else old_90=$((old_90 + 1)); fi

  # Type
  type=$(sed -n '/^---$/,/^---$/p' "$f" | grep '  type:' | head -1 | awk '{print $2}')
  [ -z "$type" ] && type="unknown"
  echo "$type" >> "$TMPDIR/types"

  # Size + age + name for analysis
  echo "$size $age_days $name" >> "$TMPDIR/files"

  # Prefix (for merge detection)
  prefix=$(echo "$name" | sed 's/-[0-9][0-9-]*//' | sed 's/\.md$//')
  echo "$prefix" >> "$TMPDIR/prefixes"
done

# === 膨胀预警 (>5KB) ===
bloat=""
while read -r sz ag nm; do
  [ "$sz" -gt 5000 ] && bloat="$bloat  $nm:$((sz/1024))K"
done < "$TMPDIR/files"

# === 日落候选 ===
sunset=""
while read -r sz ag nm; do
  if [ "$ag" -gt 60 ] && [ "$sz" -lt 300 ]; then
    sunset="$sunset  $nm:${ag}d:${sz}B"
  elif [ "$ag" -gt 90 ]; then
    sunset="$sunset  $nm:${ag}d:${sz}B"
  fi
done < "$TMPDIR/files"

# === 合并候选 (同前缀>=3) ===
merge=""
sort "$TMPDIR/prefixes" | uniq -c | sort -rn | while read -r cnt pfx; do
  [ "$cnt" -ge 3 ] && echo "  $pfx:${cnt}个" >> "$TMPDIR/merges"
done
[ -f "$TMPDIR/merges" ] && merge=$(cat "$TMPDIR/merges")

# === 毕业候选 ===
graduate=""
while read -r sz ag nm; do
  type=$(sed -n '/^---$/,/^---$/p' "$MEMORY_DIR/$nm" | grep '  type:' | head -1 | awk '{print $2}')
  # 稳定 >30天 + >1KB 的 project 类文件
  if [ "$ag" -gt 30 ] && [ "$sz" -gt 1000 ] && [ "$type" = "project" ]; then
    if ! grep -q '完整内容已迁移至 Wiki' "$MEMORY_DIR/$nm" 2>/dev/null; then
      graduate="$graduate  $nm:${ag}d:$((sz/1024))K:project"
    fi
  fi
  # 稳定 >30天 + >1KB 的 reference 类文件（技术配置/操作手册等）
  if [ "$ag" -gt 30 ] && [ "$sz" -gt 1000 ] && [ "$type" = "reference" ]; then
    if ! grep -q '完整内容已迁移至 Wiki' "$MEMORY_DIR/$nm" 2>/dev/null; then
      graduate="$graduate  $nm:${ag}d:$((sz/1024))K:reference"
    fi
  fi
done < "$TMPDIR/files"

# === 毕业不一致检测 ===
# 检查 MEMORY.md 中已标记毕业（~~删除线~~）但 memory 文件仍存在的条目
stale_graduation=""
if [ -f "$INDEX_FILE" ]; then
  while IFS= read -r line; do
    # 匹配 ~~...~~ 格式的毕业条目
    name=$(echo "$line" | grep -oE '~~[^~]+~~' | head -1 | sed 's/^~~//;s/~~$//')
    if [ -n "$name" ]; then
      # 尝试找到对应的 memory 文件
      for f in "$MEMORY_DIR"/*.md; do
        fname=$(basename "$f")
        # 模糊匹配：从条目名推断文件名
        fname_norm=$(echo "$fname" | sed 's/-[0-9-]*\.md$//')
        entry_norm=$(echo "$name" | sed 's/[[:space:]]//g' | tr '[:upper:]' '[:lower:]')
        # 如果 MEMORY.md 中标记了 ~~XX~~ 但对应文件还在 → 毕业不完整
        if echo "$fname_norm" | grep -qi "$entry_norm" 2>/dev/null; then
          if [ -f "$MEMORY_DIR/$fname" ]; then
            stale_graduation="$stale_graduation  $fname"
          fi
        fi
      done
    fi
  done < <(grep '~~' "$INDEX_FILE" 2>/dev/null)
fi

# === 微小文件 ===
tiny=""
while read -r sz ag nm; do
  [ "$sz" -lt 300 ] && tiny="$tiny  $nm:${sz}B"
done < "$TMPDIR/files"

# === 类型统计 ===
type_summary=$(sort "$TMPDIR/types" | uniq -c | sort -rn | awk '{printf "  %s: %s\n", $2, $1}')

# === 最大文件 ===
max_info=$(sort -rn "$TMPDIR/files" | head -1 | awk '{printf "%s (%sK)", $3, $1/1024}')

# === 输出 ===

if [ "$JSON" -eq 1 ]; then
  echo "{"
  echo "  \"total\": $total,"
  echo "  \"age_week\": $new_week, \"age_month\": $new_month, \"age_30d\": $old_30, \"age_60d\": $old_60, \"age_90d+\": $old_90,"
  echo "  \"total_kb\": $((total_size / 1024)),"
  echo "  \"largest\": \"$max_info\","
  echo "  \"bloat\": \"$(echo $bloat | xargs)\","
  echo "  \"sunset\": \"$(echo $sunset | xargs)\","
  echo "  \"graduate\": \"$(echo $graduate | xargs)\","
  echo "  \"stale_graduation\": \"$(echo $stale_graduation | xargs)\","
  echo "  \"merge\": \"$(echo $merge | tr '\n' ' ' | xargs)\","
  echo "  \"tiny\": \"$(echo $tiny | xargs)\""
  echo "}"
  rm -rf "$TMPDIR"
  exit 0
fi

if [ "$QUICK" -eq 1 ]; then
  w=0
  [ -n "$bloat" ] && { echo "⚠️  膨胀:$bloat"; w=$((w+1)); }
  [ -n "$sunset" ] && { echo "⚠️  日落:$sunset"; w=$((w+1)); }
  [ -n "$merge" ] && { echo "💡 合并:$merge"; w=$((w+1)); }
  [ -n "$graduate" ] && { echo "💡 毕业:$graduate"; w=$((w+1)); }
  [ -n "$stale_graduation" ] && { echo "⚠️  毕业残留:$stale_graduation"; w=$((w+1)); }
  [ "$w" -eq 0 ] && echo "✅ 无预警"
  rm -rf "$TMPDIR"
  exit 0
fi

# === 完整报告 ===
cat << EOF
## Memory 慢环深度检查 — $(date +%Y-%m-%d)

### 📊 概览
- 文件总数: **$total** | 总大小: $((total_size / 1024))KB | 最大: $max_info
- 本周: **$new_week** | 月内: $new_month | >30天: $old_30 | >60天: $old_60 | >90天: $old_90

### 📂 类型分布
$type_summary

### 📏 膨胀预警 (>5KB)
EOF
if [ -z "$bloat" ]; then echo "  ✅ 无"; else echo "$bloat"; fi

echo ""
echo "### 🔀 合并候选 (同前缀≥3个文件)"
if [ -z "$merge" ]; then echo "  ✅ 无"; else echo "$merge"; fi

echo ""
echo "### 🌅 日落候选 (>60天+<300B 或 >90天)"
if [ -z "$sunset" ]; then echo "  ✅ 无"; else echo "$sunset"; fi

echo ""
echo "### 🎓 毕业候选 (project/reference类, 稳定>30天, >1KB)"
if [ -z "$graduate" ]; then echo "  ✅ 无"; else echo "$graduate"; fi

echo ""
echo "### ⚠️ 毕业残留 (MEMORY.md已标记~~毕业~~但文件仍在)"
if [ -z "$stale_graduation" ]; then echo "  ✅ 无"; else echo "$stale_graduation"; fi

echo ""
echo "### 🔬 微小文件 (<300B)"
if [ -z "$tiny" ]; then echo "  ✅ 无"; else echo "$tiny"; fi

# === 写健康日志 ===
{
  echo "## $(date +%Y-%m-%d) | 文件:$total | 本周:$new_week | >30d:$old_30"
  [ -n "$bloat" ] && echo "- 膨胀:$(echo $bloat | xargs)"
  [ -n "$sunset" ] && echo "- 日落:$(echo $sunset | xargs)"
  [ -n "$graduate" ] && echo "- 毕业:$(echo $graduate | xargs)"
  [ -n "$merge" ] && echo "- 合并:$(echo $merge | xargs | tr '\n' ' ')"
  [ -z "$bloat" ] && [ -z "$sunset" ] && [ -z "$graduate" ] && [ -z "$merge" ] && echo "- ✅ 健康"
  echo ""
} >> "$HEALTH_LOG" 2>/dev/null

rm -rf "$TMPDIR"
