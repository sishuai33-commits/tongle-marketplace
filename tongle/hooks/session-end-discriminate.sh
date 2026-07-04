#!/bin/bash
# session-end-discriminate.sh — 会话结束自动跑判别采集器（增量，双源）
#
# 任务#2 接判别环生产调用链（2026-07-02，整体对抗审查 P1-D/E 根因修复）：
# collector 此前纯批处理无 hook 契约，cursor 冻结 6-29，observations 涨到 1320 行
# 4 天未处理。指挥官定"SessionEnd 自动 + 慢环触发全量"双触发。
#
# 任务#3① 采集环三源（2026-07-02）：加 source-scanner 源1 local_file 主动扫描，
# 补 observe 单源缺口。SessionEnd 串联：source-scanner 增量（产变更信号）→
# collector --source 增量（判 file_change_candidate）→ collector 增量（observe 模式判 evolve/new）。
#
# 任务#3② 源2 transcript（2026-07-02）：scanner 加 transcript 源，从 SessionEnd stdin 解析
# transcript_path+session_id，同步扫提取决策动作信号（实测 1.3MB transcript 0.007s，无需异步）。
# 串联：source-scanner 源1+源2 → collector --source（判 file_change+transcript_candidate）→
# collector 增量（observe 模式判 evolve/new）。
#
# 守原则3"判别复用同库不另造"：不另造采集器，调既有 collector.py + source-scanner.py。
# fail-open：任何失败都不阻断会话结束（采集失败下次会话补扫，游标不推进则重扫）。
# 性能：source-scanner 增量 os.walk 4670 文件 ~0.14s + transcript 0.007s + collector ~0.1s，
#   合计 < 0.5s，SessionEnd 默认 1.5s timeout 内（首次部署建基线也 <0.5s）。
#   源3 ima 待实现（crawler 接入设计）。
set -uo pipefail

# hooks 目录解析：plugin 调起时用 CLAUDE_PLUGIN_ROOT，否则回退脚本自身目录
HOOKS_DIR="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"
if [ -z "$HOOKS_DIR" ]; then
    HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

COLLECTOR="$HOOKS_DIR/discriminate-collector.py"
SCANNER="$HOOKS_DIR/source-scanner.py"

# 解析 SessionEnd stdin（含 transcript_path + session_id + reason），供源2 transcript 扫描用
# 用 python3 解析 JSON（shell 原生不可靠），失败 fail-open（源2 跳过，不阻断源1+collector）
KE_TRANSCRIPT_PATH=""
KE_SESSION_ID=""
if [ -f "$SCANNER" ]; then
    INPUT=$(cat 2>/dev/null || true)
    if [ -n "$INPUT" ]; then
        read -r KE_TRANSCRIPT_PATH KE_SESSION_ID < <(
            printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('transcript_path', '') or '', d.get('session_id', '') or '')
except Exception:
    print('', '')
" 2>/dev/null
        ) || true
    fi
    export KE_TRANSCRIPT_PATH KE_SESSION_ID
fi

# 源1 local_file 主动扫描（增量：cursor 后变更产信号；首次部署建基线 0 信号）
# 扫描根默认 ~/Documents/My_Code_Projects（自用），朋友按 SOURCE_ROOT env 覆盖
if [ -f "$SCANNER" ]; then
    python3 "$SCANNER" --source local_file --root "${SOURCE_ROOT:-$HOME/Documents/My_Code_Projects}" >/dev/null 2>&1 || true
    # 源2 transcript（仅当 stdin 解析到 transcript_path）：扫决策动作信号，补 observe 不记语义缺口
    if [ -n "$KE_TRANSCRIPT_PATH" ]; then
        python3 "$SCANNER" --source transcript --transcript "$KE_TRANSCRIPT_PATH" --session "$KE_SESSION_ID" >/dev/null 2>&1 || true
    fi
    # 源3 ima：IMA 笔记增量同步（实测 0.429s < 1.5s，同步可行；fail-open 凭证缺失/网络错跳过）
    python3 "$SCANNER" --source ima >/dev/null 2>&1 || true
fi

# collector --source 增量：判 source-observations 产 file_change_candidate
if [ -f "$COLLECTOR" ]; then
    python3 "$COLLECTOR" --source >/dev/null 2>&1 || true
    # collector 增量（observe 模式）：判 observations 产 evolve/new_candidate（任务#2 既有）
    python3 "$COLLECTOR" >/dev/null 2>&1 || true
fi

# 独立 marker 刷新（阶段5发版前审查 P1-1 真实根因修复，2026-07-02）：
# update_trigger_marker() 原只挂在 collector "产出新候选" 路径末尾（observe line 557 / source line 376），
# 但两模式都有提前 sys.exit(0)（无新增原料时），生产 SessionEnd 真跑时游标已推进无新事件→提前exit→
# marker 永不写。但 marker 语义是「pending 总数≥阈值就该提醒人裁」与「本次是否采到新原料」无关。
# 修复：SessionEnd 串联末尾独立调一次 update_trigger_marker()，基于当前 pending 总数判写/删，
# 不依赖 collector 是否产出新候选。每次 SessionEnd 保证 marker 状态正确（有积压写/无积压删）。
if [ -f "$COLLECTOR" ]; then
    python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('c', '$COLLECTOR')
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
    m.update_trigger_marker()
except Exception as e:
    print(f'[session-end-discriminate] WARN: marker 刷新失败 {e}', file=sys.stderr)
" 2>/dev/null || true
fi

exit 0
