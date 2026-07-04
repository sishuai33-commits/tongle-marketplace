#!/bin/bash
# wiki-track-read.sh — PostToolUse: 记录 Wiki 页面读取
# CC hook 通过 stdin 传入 JSON 事件数据。
set -euo pipefail

# 路径环境变量默认值兜底（daemon 内部用 WIKI_VAULT_PATH 定位 Vault 数据资产）
: "${WIKI_VAULT_PATH:=$HOME/Documents/Obsidian Vault}"
export WIKI_VAULT_PATH

# daemon 位置（P1.2 迁入项目 adapters/obsidian/ 机制层）：走 CLAUDE_PLUGIN_ROOT 定位
# plugin 调起时用 CLAUDE_PLUGIN_ROOT，否则回退脚本自身目录（测试/手动场景）
ADAPTER_DIR="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/adapters/obsidian}"
if [ -z "$ADAPTER_DIR" ]; then
    ADAPTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../adapters/obsidian" && pwd)"
fi
DAEMON="$ADAPTER_DIR/.wiki-daemon.py"

# --batch mode: consume pending access queue (called from session-start.sh)
if [ "${1:-}" = "--batch" ]; then
    START_TS=$(python3 -c "import time; print(int(time.time()*1000))")
    python3 "$DAEMON" access-update
    NOW_MS=$(python3 -c "import time; print(int(time.time()*1000))")
    ELAPSED=$(( NOW_MS - START_TS ))
    echo "[wiki-track] batch access-update done in ${ELAPSED}ms" >&2
    exit 0
fi

# 从 stdin 读取 JSON，提取 tool_input.path
PATH_ARG=$(python3 -c "
import json, sys

raw = sys.stdin.read()
if not raw.strip():
    sys.exit(0)

d = json.loads(raw)
tool = d.get('tool_name', '')

# 只处理 Obsidian MCP 读操作
if tool not in ('mcp__obsidian__read_note', 'mcp__obsidian__read_multiple_notes'):
    sys.exit(0)

inp = d.get('tool_input', {})

# read_note: { path: 'wiki/...' }
# read_multiple_notes: { paths: ['wiki/...', ...] }
paths = []
if 'path' in inp and isinstance(inp['path'], str):
    paths.append(inp['path'])
if 'paths' in inp and isinstance(inp['paths'], list):
    paths.extend(inp['paths'])

# 只跟踪 wiki/ 下非系统文件
for p in paths:
    if isinstance(p, str) and p.startswith('wiki/') and not '/.' in p and not p.startswith('wiki/.'):
        print(p)
" 2>/dev/null)

if [ -z "$PATH_ARG" ]; then
    exit 0
fi

# 逐行追加到 .pending_access_updates
START_TS=$(python3 -c "import time; print(int(time.time()*1000))")
echo "$PATH_ARG" | while IFS= read -r p; do
    [ -n "$p" ] && python3 "$DAEMON" pending-add "$p"
done
NOW_MS=$(python3 -c "import time; print(int(time.time()*1000))")
    ELAPSED=$(( NOW_MS - START_TS ))
[ "$ELAPSED" -gt 200 ] && echo "[wiki-track] WARNING: PostToolUse pending-add took ${ELAPSED}ms" >&2

exit 0
