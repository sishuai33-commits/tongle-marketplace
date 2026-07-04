#!/bin/bash
# observe.sh — PostToolUse 观察采集
# CC hook 通过 stdin 传入 JSON 事件数据。

set -euo pipefail

INSTINCTS_DIR="$HOME/.claude/instincts"
OBSERVATIONS_FILE="$INSTINCTS_DIR/observations.jsonl"
SIGNAL_COUNTER="$INSTINCTS_DIR/.observer-signal-counter"
MAX_FILE_SIZE_MB=5
SIGNAL_EVERY_N=20

mkdir -p "$INSTINCTS_DIR"

# ── 读取 stdin JSON，一步构造合法 JSON ────────────────
OBS=$(python3 -c "
import json, sys, os
from datetime import datetime, timezone

raw = sys.stdin.read()
if not raw.strip():
    sys.exit(0)

d = json.loads(raw)
tool = d.get('tool_name', '')
if not tool or tool == 'unknown':
    sys.exit(0)

inp = d.get('tool_input', '')
if isinstance(inp, dict):
    inp = json.dumps(inp, ensure_ascii=False)

out = {
    'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'session': d.get('session_id', os.environ.get('CLAUDE_SESSION_ID', 'unknown')),
    'tool': tool,
    'input_preview': str(inp)[:3000]
}
print(json.dumps(out, ensure_ascii=False))
" 2>/dev/null)

if [ -z "$OBS" ]; then
    exit 0
fi

# ── 写入 observations.jsonl ───────────────────────────
echo "$OBS" >> "$OBSERVATIONS_FILE"

# ── 文件大小检查 ───────────────────────────────────────
SIZE=$(wc -c < "$OBSERVATIONS_FILE" 2>/dev/null || echo 0)
if [ "$SIZE" -gt $((MAX_FILE_SIZE_MB * 1024 * 1024)) ]; then
    ARCHIVE="$INSTINCTS_DIR/observations.archive"
    mkdir -p "$ARCHIVE"
    mv "$OBSERVATIONS_FILE" "$ARCHIVE/observations-$(date +%Y%m%d-%H%M%S).jsonl"
fi

# ── 节流信号 ──────────────────────────────────────────
COUNTER=0
[ -f "$SIGNAL_COUNTER" ] && COUNTER=$(cat "$SIGNAL_COUNTER" 2>/dev/null || echo 0)
COUNTER=$((COUNTER + 1))
echo "$COUNTER" > "$SIGNAL_COUNTER"

# 每 SIGNAL_EVERY_N 次写入标记文件（observer-loop 已废弃移除，仅留 pending 标记供慢环消费）
if [ $((COUNTER % SIGNAL_EVERY_N)) -eq 0 ]; then
    touch "$INSTINCTS_DIR/.observer-pending"
fi

exit 0
