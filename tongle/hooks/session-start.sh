#!/bin/bash
# session-start.sh — 会话启动：instinct + working memory 感知 + wiki sync 检查
# stdout → JSON（Hook 系统注入 CC 上下文）  stderr → 日志
set -euo pipefail

# 路径环境变量默认值兜底（可被环境覆盖；默认值按 $HOME 推导 CC 的 per-project memory 编码）
: "${CC_MEMORY_DIR:=$HOME/.claude/projects/${HOME//\//-}/memory}"

# hooks 目录解析：plugin 调起时用 CLAUDE_PLUGIN_ROOT，否则回退脚本自身目录（测试/手动场景）
HOOKS_DIR="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"
if [ -z "$HOOKS_DIR" ]; then
    HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

START_TS=$(python3 -c "import time; print(int(time.time()*1000))")

INSTINCTS_DIR="$HOME/.claude/instincts"
PATTERNS_FILE="$INSTINCTS_DIR/patterns.yaml"
CONTEXT_FILE="$INSTINCTS_DIR/active-context.md"
SNOOZE_FILE="$INSTINCTS_DIR/.alert-snooze"   # 治标：报警静音规则，二阶段注入管道治本后清空
# ── 0. 消费 Wiki 读取队列（access_count 追踪） ──
bash "$HOOKS_DIR/wiki-track-read.sh" --batch >/dev/null 2>&1 || true

mkdir -p "$INSTINCTS_DIR"
SESSION_ID="${CLAUDE_SESSION_ID:-unknown}"

# ── 1. 解析 patterns.yaml（手工 instinct） ──────────────
MANUAL_INSTINCTS=""
if [ -f "$PATTERNS_FILE" ]; then
    MANUAL_INSTINCTS=$(python3 -c "
import sys, re
with open('$PATTERNS_FILE') as f:
    content = f.read()
blocks = re.split(r'\n---\n?', content)
instincts = []
i = 0
while i < len(blocks) - 1:
    fm_block = blocks[i].strip()
    body_block = blocks[i+1].strip() if i+1 < len(blocks) else ''
    fm_block = re.sub(r'^---\s*\n?', '', fm_block)
    if not fm_block.startswith('id:'):
        i += 1; continue
    fm = {}
    for line in fm_block.split('\n'):
        line = line.strip()
        if ':' in line and not line.startswith('#'):
            key, _, val = line.partition(':')
            fm[key.strip()] = val.strip().strip('\"')
    action_match = re.search(r'## Action\s*\n(.*?)(\n##|\Z)', body_block, re.DOTALL)
    action = action_match.group(1).strip() if action_match else 'N/A'
    action = ' '.join(action.split())
    instincts.append({
        'id': fm.get('id', '?'),
        'trigger': fm.get('trigger', '?'),
        'confidence': float(fm.get('confidence', 0)),
        'domain': fm.get('domain', '?'),
        'action': action
    })
    i += 2
active = [i for i in instincts if i['confidence'] >= 0.5]
active.sort(key=lambda x: x['confidence'], reverse=True)
for i in active:
    print(f\"ID:{i['id']}|DOMAIN:{i['domain']}|CONF:{i['confidence']}|TRIGGER:{i['trigger']}|ACTION:{i['action'][:200]}\")
" 2>/dev/null || echo "")
fi

# ── 2. Working Memory 感知 ────────────────────────────
WM_FILE="$CC_MEMORY_DIR/working-memory.md"
WM_SECTION=""

if [ -f "$WM_FILE" ]; then
    WM_TOPICS=$(python3 -c "
import sys
with open('$WM_FILE') as f:
    wm = f.read()

topics = []
current = None
for line in wm.split('\n'):
    if line.startswith('## Topic:'):
        if current and current['signals']:
            topics.append(current)
        current = {'title': line.replace('## Topic:', '').strip(), 'signals': []}
    elif current and line.startswith('- [') and len(current['signals']) < 3:
        sig = line.strip()
        if len(sig) > 120:
            sig = sig[:117] + '...'
        current['signals'].append(sig)
if current and current['signals']:
    topics.append(current)

active = [t for t in topics if '[active]' in t['title'] or '[活跃]' in t['title']]

if active:
    for i, t in enumerate(active[:5]):
        title = t['title'].replace('[active]','').replace('[活跃]','').strip()
        signals = '<br>'.join(t['signals'])
        print(f'### {title}')
        print(signals)
        if i < min(len(active), 5) - 1:
            print()
" 2>/dev/null)

    if [ -n "$WM_TOPICS" ]; then
        WM_COUNT=$(echo "$WM_TOPICS" | grep -c '^### ' 2>/dev/null || echo 0)
        WM_SECTION="## Working Memory (${WM_COUNT} active topic(s))

${WM_TOPICS}

> 建议新会话启动后读取 working-memory.md 获取完整上下文，交叉引用 MEMORY.md 项目索引对应 Wiki 页面。"
    fi
fi

# ── 3. 生成 active-context.md（文件备份，供调试用） ──
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
MANUAL_COUNT=$(echo "$MANUAL_INSTINCTS" | grep -c . 2>/dev/null || echo 0)

cat > "$CONTEXT_FILE" << CONTEXTEOF
# Active Context — $(date -u +"%Y-%m-%d %H:%M") UTC

> Session: ${SESSION_ID}

## Active Instincts (patterns.yaml)

$(if [ -n "$MANUAL_INSTINCTS" ] && [ "$MANUAL_COUNT" -gt 0 ]; then
    echo "$MANUAL_INSTINCTS" | while IFS='|' read -r line; do
        ID=$(echo "$line" | grep -o 'ID:[^|]*' | cut -d: -f2)
        DOMAIN=$(echo "$line" | grep -o 'DOMAIN:[^|]*' | cut -d: -f2)
        CONF=$(echo "$line" | grep -o 'CONF:[^|]*' | cut -d: -f2)
        TRIGGER=$(echo "$line" | grep -o 'TRIGGER:[^|]*' | cut -d: -f2)
        ACTION=$(echo "$line" | grep -o 'ACTION:.*' | cut -d: -f2-)
        echo "- **[${ID}]** (${DOMAIN}, conf=${CONF}) — ${ACTION}"
    done
else
    echo "(none)"
fi)

$( [ -n "$WM_SECTION" ] && echo "$WM_SECTION" )
CONTEXTEOF

# ── 4. Memory 健康提醒（基于 guard 状态文件 + 分级节流） ──
MEM_GUARD_STATE="$HOME/.claude/instincts/.memory-guard-state.json"
MEM_HEALTH_SECTION=""

if [ -f "$MEM_GUARD_STATE" ]; then
    MEM_HEALTH_SECTION=$(python3 -c "
import json, time

with open('$MEM_GUARD_STATE') as f:
    g = json.load(f)

severity = g.get('severity', 'green')
now = int(time.time())

# 节流规则
should_remind = False
if severity == 'red':
    should_remind = True  # 每次都提醒
elif severity == 'yellow':
    last_reminded = g.get('last_reminded_epoch', 0)
    days_since = (now - last_reminded) / 86400 if last_reminded else 999
    should_remind = days_since >= 3  # 最多每3天1次

if not should_remind:
    print('')
    exit(0)

# 生成提醒（精简格式：单行概要）
lines = []
orphan_count = g.get('orphan_count', 0)
dangling_count = g.get('dangling_count', 0)
fm_bad = g.get('frontmatter_bad', 0)
index_over = g.get('index_over_limit', False)
claude_over = g.get('claude_md_over_limit', False)
total_v = g.get('total_violations', 0)

parts = []
if orphan_count > 0:
    parts.append(f'{orphan_count} 无引用')
if fm_bad > 0:
    parts.append(f'{fm_bad} 格式错')
if dangling_count > 0:
    parts.append(f'{dangling_count} 断链')
if index_over:
    parts.append('索引超长')
if claude_over:
    parts.append('主配置超长')

icon = '🔴' if severity == 'red' else '🟡'
detail = ' '.join(parts) if parts else ''
detail_prefix = f'其中 {detail}，' if detail else ''
if severity == 'red':
    lines.append(f'{icon} 记忆库 {total_v} 项待整理，{detail_prefix}影响跨会话接续，需立即整理')
else:
    lines.append(f'{icon} 记忆库 {total_v} 项待整理，{detail_prefix}有空处理')

# 更新提醒计数和时间
g['reminder_count'] = g.get('reminder_count', 0) + 1
g['last_reminded_epoch'] = now
with open('$MEM_GUARD_STATE', 'w') as f:
    json.dump(g, f, indent=2)

print('\n'.join(lines))
" 2>/dev/null)
fi

# ── 5. 慢环守卫（触发机制收敛 2026-06-25） ───────────
# 慢环过期检查收敛到 maintenance-guard 维度3（唯一检查点）：
# maintenance-guard 写 marker → §6.5 读取注入 MAINT_SECTION。
# slow-loop-guard 已删（输出冗余：慢环提醒由 maintenance marker 覆盖，
# 活跃主题数由 §2 Working Memory 感知覆盖）。原 L205 内部调用是死代码
# （stdout/stderr 全丢注入无效），已清理。

# ── 6. Pending Wiki Sync 检查 ───────────────────────
# SessionEnd hook → marker 文件 → 此处消费
# 治标(2026-06-25)：.alert-snooze 关键词过滤 — 已毕业未清标记/主动暂缓的 topic 不报
# 治本(二阶段注入管道)：改产生端，写入前判 Topic 有无 wiki 引用行/[deferred] 标记
PENDING_FILE="$INSTINCTS_DIR/.pending-wiki-sync"
PENDING_SECTION=""
PENDING_COUNT=0
if [ -f "$PENDING_FILE" ]; then
    PENDING_SECTION=$(python3 -c "
import json, os
snooze = []
sf = '$SNOOZE_FILE'
if os.path.exists(sf):
    with open(sf) as f:
        for line in f:
            k = line.strip()
            if not k or k.startswith('#') or k == 'wiki-health':
                continue
            snooze.append(k.lower())
with open('$PENDING_FILE') as f:
    d = json.load(f)
items = [t for t in d.get('unsynced', [])]
filtered = [t for t in items if not any(k in t.lower() for k in snooze)]
if filtered:
    print(f'🟡 知识待沉淀 {len(filtered)} 条，新会话接不上，本次会话抽空归档：')
    print()
    for t in filtered:
        print(f'- {t.replace("[synthesized]", "").strip()}')
" 2>/dev/null)
    PENDING_COUNT=$(echo "$PENDING_SECTION" | grep -c '^- ⚠️' 2>/dev/null || echo 0)
    rm -f "$PENDING_FILE"
fi

# ── 6.4 Wiki 资产路由（静默注入，CC 知道有哪些知识域） ──
ASSET_MANIFEST=""
ASSET_MANIFEST=$(python3 "$HOOKS_DIR/build-asset-manifest.py" 2>/dev/null || echo "")

# ── 6.5 维护门禁（静默，超标自动标记） ──────────────
bash "$HOOKS_DIR/maintenance-guard.sh" 1>&2 2>/dev/null || true
# marker 文件 → CC 下次会话检测到后主动触发慢环
MAINT_MARKER="$HOME/.claude/instincts/.maintenance-due"
MAINT_SECTION=""
if [ -f "$MAINT_MARKER" ]; then
    MAINT_SECTION=$(python3 -c "
import json
with open('$MAINT_MARKER') as f:
    d = json.load(f)
issues = d.get('issues', '')
print(f'🔴 知识库该整理了，{issues}')
print('本次会话抽空清理，去重和精简')
" 2>/dev/null)
fi

# ── 6.5b 仪表盘（待批阅 + 经验值周，触发式呈现） ──
# 待批阅 = pending-queue 未 resolved；经验值(周) = 近7天 reuse-log ok 正负
# 阈值：待批阅≥5 或 失败≥5 才弹；都没触发不显示（尽量不打扰用户）
# 触发了都一起呈现。处理完归零自动回归安静。
# 注：旧 .discriminate-due marker 机制已并入此仪表盘，collector 端 marker 写入待清理
DASHBOARD_SECTION=""
DASHBOARD_SECTION=$(python3 -c "
import json, os
from datetime import datetime, timezone, timedelta

INSTINCTS = os.path.expanduser('~/.claude/instincts')

pending = 0
pq = os.path.join(INSTINCTS, 'pending-queue.jsonl')
if os.path.exists(pq):
    with open(pq, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                d = json.loads(line)
                if d.get('status') != 'resolved':
                    pending += 1
            except: pass

succ = fail = 0
rl = os.path.join(INSTINCTS, 'reuse-log.jsonl')
if os.path.exists(rl):
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    with open(rl, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                d = json.loads(line)
                ts = d.get('ts', '')
                if ts:
                    t = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                    if t >= week_ago:
                        ok = d.get('ok')
                        if ok is True: succ += 1
                        elif ok is False: fail += 1
            except: pass

parts = []
if pending >= 5:
    parts.append(f'待批阅 {pending} 条')
if fail >= 5:
    parts.append(f'本周经验值 {succ} 成 {fail} 败')
if parts:
    print('🟡 ' + '，'.join(parts) + '，抽空处理')
" 2>/dev/null)
# ── 6.6 Wiki 健康检查 ──────────────────────────────────
WIKI_CHECKS="$HOME/.claude/skills/wiki-management/scripts/wiki_checks.py"
WIKI_HEALTH_SECTION=""

# 治标(2026-06-25)：.alert-snooze 含 wiki-health 行则整段静音（低优先级，自带"有空处理"）
if [ -f "$WIKI_CHECKS" ] && ! grep -qE '^[[:space:]]*wiki-health[[:space:]]*$' "$SNOOZE_FILE" 2>/dev/null; then
    WIKI_HEALTH_SECTION=$(python3 -c "
import json, subprocess, time

result = subprocess.run(['python3', '$WIKI_CHECKS', '--json'],
                       capture_output=True, text=True, timeout=15)
if result.returncode != 0:
    print('')
    exit(0)

d = json.loads(result.stdout)
fm_missing = len(d.get('frontmatter', {}).get('missing', []))
fm_incomplete = len(d.get('frontmatter', {}).get('incomplete', []))
dead = len(d.get('dead_links', {}).get('dead', []))
stale = len(d.get('staleness_mismatches', []))
sunset = len(d.get('sunset_candidates', []))
total = d.get('total_pages', 0)

issues = fm_missing + fm_incomplete + dead + stale + sunset
if issues == 0:
    print('')
    exit(0)

# 严重度分级
is_red = fm_missing >= 10 or dead >= 5 or stale >= 10
icon = '🔴' if is_red else '🟡'

parts = []
if fm_missing > 0: parts.append(f'{fm_missing} 缺元数据')
if fm_incomplete > 0: parts.append(f'{fm_incomplete} 元数据不全')
if dead > 0: parts.append(f'{dead} 断链')
if stale > 0: parts.append(f'{stale} 过期')
if sunset > 0: parts.append(f'{sunset} 待归档')

detail = ' '.join(parts)
detail_prefix = f'其中 {detail}，' if detail else ''
if is_red:
    print(f'{icon} 知识库 {total} 页 {issues} 项待整理，{detail_prefix}影响知识查找，需立即整理')
else:
    print(f'{icon} 知识库 {total} 页 {issues} 项待整理，{detail_prefix}有空处理')
" 2>/dev/null)
fi

# ── 7. 输出（注入 CC 上下文） ──
NOW_MS=$(python3 -c "import time; print(int(time.time()*1000))")
ELAPSED=$(( NOW_MS - START_TS ))
WM_COUNT=${WM_COUNT:-0}

# 构建注入上下文
#   systemMessage = 用户可见提醒（健康告警/维护触发）
#   additionalContext = 静默注入（Wiki资产路由，CC自知，用户不可见）
ADDITIONAL=""     # 用户可见提醒（仪表盘最前，触发式）
if [ -n "$DASHBOARD_SECTION" ]; then
  ADDITIONAL="${DASHBOARD_SECTION}"
fi
SILENT=""         # 静默注入（CC专用，不显示给用户）

# Wiki 资产路由 → 静默注入
if [ -n "$ASSET_MANIFEST" ]; then
  SILENT="${ASSET_MANIFEST}"
fi

# 用户可见提醒
if [ -n "$MEM_HEALTH_SECTION" ]; then
  [ -n "$ADDITIONAL" ] && ADDITIONAL="${ADDITIONAL}"$'\n\n'
  ADDITIONAL="${ADDITIONAL}${MEM_HEALTH_SECTION}"
fi
if [ -n "$PENDING_SECTION" ]; then
  [ -n "$ADDITIONAL" ] && ADDITIONAL="${ADDITIONAL}"$'\n\n'
  ADDITIONAL="${ADDITIONAL}${PENDING_SECTION}"
fi
if [ -n "$WIKI_HEALTH_SECTION" ]; then
  [ -n "$ADDITIONAL" ] && ADDITIONAL="${ADDITIONAL}"$'\n\n'
  ADDITIONAL="${ADDITIONAL}${WIKI_HEALTH_SECTION}"
fi
if [ -n "$MAINT_SECTION" ]; then
  [ -n "$ADDITIONAL" ] && ADDITIONAL="${ADDITIONAL}"$'\n\n'
  ADDITIONAL="${ADDITIONAL}${MAINT_SECTION}"
fi
# DISCRIM_SECTION 已并入 §6.5b 仪表盘（触发式统一呈现）

# 合并：静默内容注入 additionalContext，用户可见的放 systemMessage
COMBINED=""
[ -n "$SILENT" ] && COMBINED="$SILENT"
[ -n "$ADDITIONAL" ] && [ -n "$COMBINED" ] && COMBINED="${COMBINED}"$'\n\n'
[ -n "$ADDITIONAL" ] && COMBINED="${COMBINED}${ADDITIONAL}"

python3 -c "
import json
silent = '''${SILENT}'''
alerts = '''${ADDITIONAL}'''
sys_msg = alerts.strip() if alerts.strip() else ''
combined = silent.strip()
if alerts.strip():
    combined = combined + '\n\n' + alerts.strip() if combined else alerts.strip()
print(json.dumps({
    'systemMessage': sys_msg,
    'hookSpecificOutput': {
        'hookEventName': 'SessionStart',
        'additionalContext': combined.strip()
    }
}))
"

# ── 7. 输出到 stderr（日志） ─────────────────────────
echo "[session-start] session=${SESSION_ID} | instincts=${MANUAL_COUNT} | wm_active=${WM_COUNT} | pending_sync=${PENDING_COUNT} | elapsed=${ELAPSED}ms" >&2

exit 0
