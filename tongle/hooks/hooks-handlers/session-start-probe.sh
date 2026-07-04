#!/bin/bash
# session-start-probe.sh — 验证 plugin hook 是否被加载的探针
# 若此 hook 生效, 会在 ~/.claude/instincts/.ke-plugin-probe 写入时间戳
# 用途: 验证 symlink plugin 的 SessionStart hook 是否被 CC 发现并执行
MARKER="$HOME/.claude/instincts/.ke-plugin-probe"
mkdir -p "$(dirname "$MARKER")"
date -u +"%Y-%m-%dT%H:%M:%SZ plugin-sessionstart-fired" > "$MARKER"
exit 0
