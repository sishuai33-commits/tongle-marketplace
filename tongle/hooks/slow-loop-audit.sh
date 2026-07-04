#!/bin/bash
# slow-loop-audit.sh — 慢环结构审计脚本
# 全面扫描 Wiki vault + Memory 体系的结构问题
# 输出 JSON 供 CC 消费
# 用法: bash slow-loop-audit.sh [--json]

# 环境变量默认值兜底
: "${CC_MEMORY_DIR:=$HOME/.claude/projects/${HOME//\//-}/memory}"
: "${WIKI_VAULT_PATH:=$HOME/Documents/Obsidian Vault}"

# hooks 目录解析：plugin 调起时用 CLAUDE_PLUGIN_ROOT，否则回退脚本自身目录
HOOKS_DIR="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"
if [ -z "$HOOKS_DIR" ]; then
    HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

VAULT="$WIKI_VAULT_PATH"
MEMORY_DIR="$CC_MEMORY_DIR"
CLAUDE_MD="$HOME/.claude/CLAUDE.md"

output_json() { echo "$1" >> "$tmp_json"; }
tmp_json=$(mktemp)
echo "{" > "$tmp_json"

# ============================================================
# 1. VAULT 结构检查
# ============================================================
output_json '"vault_structure": {'

# 1a. 目录分裂：检查是否有内容目录在 vault 根而非 wiki/ 下
root_dirs=$(find "$VAULT" -maxdepth 1 -type d -not -path "$VAULT" -not -name ".git" -not -name ".obsidian" -not -name ".claude" -not -name ".trash" -not -name "wiki" | sed 's|.*/||')
if [ -n "$root_dirs" ]; then
    output_json "  \"root_dirs_outside_wiki\": \"$(echo $root_dirs | tr '\n' ' ')\","
else
    output_json '  "root_dirs_outside_wiki": null,'
fi

# 1b. 重复文件名（排除结构性约定）
structural_dupes="synthesis.md|SKILL.md|_narrative.md|index.md|方法论.md|观点演化.md|投资哲学.md|指标词典.md"
real_dupes=$(find "$VAULT" -name "*.md" -type f | sed 's|.*/||' | sort | uniq -d | grep -vE "$structural_dupes")
if [ -n "$real_dupes" ]; then
    output_json "  \"duplicate_filenames\": \"$(echo $real_dupes | tr '\n' ' ')\","
else
    output_json '  "duplicate_filenames": null,'
fi

# 1c. 空文件
empty_files=$(find "$VAULT" -name "*.md" -type f -size 0)
if [ -n "$empty_files" ]; then
    output_json "  \"empty_files\": $(echo "$empty_files" | sed 's|.*/||' | jq -R -s 'split("\n")[:-1]'),"
else
    output_json '  "empty_files": [],'
fi

# 1d. 空目录（排除 .git）
empty_dirs=$(find "$VAULT" -type d -empty -not -path "*/.git/*" 2>/dev/null)
if [ -n "$empty_dirs" ]; then
    output_json "  \"empty_dirs\": $(echo "$empty_dirs" | sed "s|$VAULT/||g" | jq -R -s 'split("\n")[:-1]'),"
else
    output_json '  "empty_dirs": [],'
fi

output_json "},"

# ============================================================
# 2. WIKI 内容健康
# ============================================================
output_json '"wiki_health": {'

# 2a. 断链
broken_links=$(grep -roh '\[\[[^]]*\]\]' "$VAULT/wiki/" --include="*.md" 2>/dev/null | \
    sed 's/\[\[//;s/\]\]//;s/|.*//;s/#.*//' | sort -u | while read link; do
    # Skip external/memory references
    [[ "$link" == memory:* ]] && continue
    [[ "$link" == http* ]] && continue
    # Try to resolve the link
    found=$(find "$VAULT" -name "${link}.md" -type f 2>/dev/null | head -1)
    # Also try with relative path resolution
    if [ -z "$found" ]; then
        found=$(find "$VAULT/wiki" -name "${link}.md" -type f 2>/dev/null | head -1)
    fi
    if [ -z "$found" ]; then
        echo "$link"
    fi
done | head -50)
if [ -n "$broken_links" ]; then
    output_json "  \"broken_links_count\": $(echo "$broken_links" | wc -l | tr -d ' '),"
    output_json "  \"broken_links_sample\": $(echo "$broken_links" | jq -R -s 'split("\n")[:-1][:20]'),"
else
    output_json '  "broken_links_count": 0,'
    output_json '  "broken_links_sample": [],'
fi

# 2b. 孤立页面（no incoming links）
output_json '  "orphan_pages": ['
first=true
for f in $(find "$VAULT/wiki/concepts" "$VAULT/wiki/projects" "$VAULT/wiki/entities" "$VAULT/wiki/procedures" -name "*.md" -type f 2>/dev/null); do
    basename=$(basename "$f" .md)
    count=$(grep -rl "\[\[$basename\]\]" "$VAULT/wiki/" --include="*.md" 2>/dev/null | wc -l | tr -d ' ')
    # Count self-reference too — 0 means truly orphaned
    if [ "$count" -eq 0 ]; then
        relpath=$(echo "$f" | sed "s|$VAULT/||")
        if [ "$first" = true ]; then first=false; else output_json ','; fi
        output_json "    \"$relpath\""
    fi
done
output_json '  ],'

# 2c. Stale 页面
output_json "  \"stale_pages_count\": $(grep -rl 'staleness: stale' "$VAULT/wiki/" --include="*.md" 2>/dev/null | wc -l | tr -d ' ')"

output_json "},"

# ============================================================
# 3. MEMORY 健康
# ============================================================
output_json '"memory_health": {'

# 3a. 孤儿文件
orphans=$(bash "$HOOKS_DIR/memory-guard.sh" 2>&1 | grep "孤儿" | wc -l | tr -d ' ')
output_json "  \"orphan_files\": $orphans,"

# 3b. 行数检查
mem_md_lines=$(wc -l < "$MEMORY_DIR/MEMORY.md" | tr -d ' ')
claude_md_lines=$(wc -l < "$CLAUDE_MD" | tr -d ' ')
output_json "  \"MEMORY_md_lines\": $mem_md_lines,"
output_json "  \"MEMORY_md_limit\": 150,"
output_json "  \"CLAUDE_md_lines\": $claude_md_lines,"
output_json "  \"CLAUDE_md_limit\": 200,"

# 3c. 文件总数
file_count=$(ls "$MEMORY_DIR"/*.md 2>/dev/null | wc -l | tr -d ' ')
output_json "  \"total_files\": $file_count,"

# 3d. 断链引用（CLAUDE.md 和 MEMORY.md 中引用但已删除的文件）
output_json '  \"broken_refs_in_claude_md\": ['
grep -oP 'memory/[a-zA-Z0-9_\-]+\.md' "$CLAUDE_MD" 2>/dev/null | sed 's|memory/||' | sort -u | while read f; do
    if [ ! -f "$MEMORY_DIR/$f" ]; then
        output_json "    \"$f\","
    fi
done
output_json '    null],'

output_json '  \"broken_refs_in_memory_md\": ['
grep -oP '[a-zA-Z0-9_\-]+\.md' "$MEMORY_DIR/MEMORY.md" 2>/dev/null | sort -u | while read f; do
    if [ ! -f "$MEMORY_DIR/$f" ] && [ "$f" != "MEMORY.md" ] && [ "$f" != "CLAUDE.md" ]; then
        output_json "    \"$f\","
    fi
done
output_json '    null]'

output_json "},"

# ============================================================
# 4. 跨体系一致性
# ============================================================
output_json '"cross_system": {'

# 4a. MEMORY.md 索引 vs vault wiki 页面同步
output_json '  "memory_wiki_sync_notes": ['
# Check if memory files with wiki: references actually have corresponding wiki pages
grep -l 'wiki:' "$MEMORY_DIR"/*.md 2>/dev/null | while read mf; do
    grep 'wiki:' "$mf" 2>/dev/null | grep -oP 'wiki/\S+' | sed 's|[),]||g' | while read wref; do
        wpath=$(echo "$wref" | sed 's|^wiki/||')
        if [ ! -f "$VAULT/wiki/$wpath" ]; then
            output_json "    \"memory:$(basename $mf) → wiki:$wpath (MISSING)\","
        fi
    done
done
output_json '    null],'

# 4b. Wiki 中引用的 memory 文件是否存在
output_json '  \"wiki_memory_refs_broken\": ['
grep -roh '\[\[[^]]*\]\]' "$VAULT/wiki/" --include="*.md" 2>/dev/null | \
    sed 's/\[\[//;s/\]\]//;s/|.*//' | grep '^memory/' | sed 's|^memory/||' | sort -u | while read mf; do
    if [ ! -f "$MEMORY_DIR/$mf" ]; then
        output_json "    \"$mf\","
    fi
done
output_json '    null]'

output_json "}"

# 收尾
output_json "}"
cat "$tmp_json" | jq . 2>/dev/null || cat "$tmp_json"
rm "$tmp_json"
