#!/bin/bash
# session-end-wiki-check.sh — 会话结束时检查 working memory [synthesized] 主题是否已同步 Wiki
# 发现未同步项 → 写入 marker 文件，下次 SessionStart 消费并提醒
set -euo pipefail

# 环境变量默认值兜底（默认值按 $HOME 推导 CC 的 per-project memory 编码，可被环境变量覆盖）
: "${CC_MEMORY_DIR:=$HOME/.claude/projects/${HOME//\//-}/memory}"

MARKER_FILE="$HOME/.claude/instincts/.pending-wiki-sync"
WM_FILE="$CC_MEMORY_DIR/working-memory.md"

if [ ! -f "$WM_FILE" ]; then
    exit 0
fi

# 解析 working-memory.md，找出 [synthesized] 但缺少 wiki: 引用的主题
UNSYNCED=$(python3 -c "
import re, json

with open('$WM_FILE') as f:
    wm = f.read()

topics = []
current = None
for line in wm.split('\n'):
    if line.startswith('## Topic:'):
        if current:
            topics.append(current)
        current = {
            'title': line.replace('## Topic:', '').strip(),
            'lines': [],
            'has_wiki_ref': False
        }
    elif current:
        current['lines'].append(line)
        if line.strip().startswith('wiki:') or line.strip().startswith('- wiki:'):
            current['has_wiki_ref'] = True
if current:
    topics.append(current)

# 只检查 [synthesized] 且无 wiki 引用的主题
unsynced = [t['title'] for t in topics
            if '[synthesized]' in t['title'] and not t['has_wiki_ref']]

if unsynced:
    print(json.dumps({'unsynced': unsynced, 'count': len(unsynced)}))
" 2>/dev/null)

if [ -n "$UNSYNCED" ]; then
    mkdir -p "$(dirname "$MARKER_FILE")"
    echo "$UNSYNCED" > "$MARKER_FILE"
fi

exit 0
