#!/bin/bash
# maintenance-guard.sh — 静默门禁
# SessionStart 调用。超标不提醒，直接标记 → CC 下次会话自动触发慢环。
set -euo pipefail

: "${CC_MEMORY_DIR:=$HOME/.claude/projects/${HOME//\//-}/memory}"
: "${WIKI_VAULT_PATH:=$HOME/Documents/Obsidian Vault}"

MEMORY_DIR="$CC_MEMORY_DIR"
MARKER="$HOME/.claude/instincts/.maintenance-due"
TRIGGERED=0
ISSUES=""

# ── 1. CLAUDE.md 行数（空环境 graceful：文件不存在跳过，不崩）──
if [ -f "$HOME/.claude/CLAUDE.md" ]; then
  CLAUDE_LINES=$(wc -l < "$HOME/.claude/CLAUDE.md" | tr -d ' ')
  if [ "$CLAUDE_LINES" -gt 180 ]; then
    ISSUES="${ISSUES}CLAUDE.md ${CLAUDE_LINES}/200行 "
    TRIGGERED=1
  fi
fi

# ── 2. MEMORY.md 行数（文件不存在跳过）──
if [ -f "$MEMORY_DIR/MEMORY.md" ]; then
  MEM_LINES=$(wc -l < "$MEMORY_DIR/MEMORY.md" | tr -d ' ')
  if [ "$MEM_LINES" -gt 140 ]; then
    ISSUES="${ISSUES}MEMORY.md ${MEM_LINES}/150行 "
    TRIGGERED=1
  fi
fi

# ── 3. 慢环过期 (>7天) ──
WM_FILE="$MEMORY_DIR/working-memory.md"
if [ -f "$WM_FILE" ]; then
    last_sl=$(grep 'last_slow_loop:' "$WM_FILE" 2>/dev/null | head -1 | sed 's/.*last_slow_loop: *//' | sed 's/ .*//' | tr -d '\r\n')
    if [ -n "$last_sl" ]; then
        now_sec=$(date +%s)
        last_sec=$(python3 -c "from datetime import datetime; print(int(datetime.strptime('$last_sl','%Y-%m-%d').timestamp()))" 2>/dev/null || echo 0)
        if [ "$last_sec" -gt 0 ]; then
            days_since=$(( (now_sec - last_sec) / 86400 ))
            if [ "$days_since" -ge 7 ]; then
                ISSUES="${ISSUES}慢环${days_since}天未执行 "
                TRIGGERED=1
            fi
        fi
    fi
fi

# ── 4. Wiki 目录卫生（vault 不存在跳过）──
VAULT="$WIKI_VAULT_PATH"
if [ -d "$VAULT" ]; then
  # 检查 vault 根是否有 wiki/ 之外的 .md 文件
  ORPHANS=$(find "$VAULT" -maxdepth 1 -name '*.md' -not -name '.ai-vocab*' 2>/dev/null | wc -l | tr -d ' ')
  # 检查 wiki 根有没有工具文件混入
  PYCACHE=$(test -d "$VAULT/wiki/__pycache__" && echo 1 || echo 0)
  if [ "$ORPHANS" -gt 0 ] || [ "$PYCACHE" -eq 1 ]; then
    ISSUES="${ISSUES}Wiki目录违规 "
    TRIGGERED=1
  fi
fi

# ── 5. Memory frontmatter 检查 ──
# 用 python 做准确检查（type 在 metadata 块内，可能缩进）
BAD_FM=$(python3 -c "
import os, re
memdir = '$MEMORY_DIR'
bad = 0
for f in os.listdir(memdir):
    if not f.endswith('.md') or f in ('MEMORY.md', '.memory-health-log.md', 'working-memory.md'):
        continue
    with open(os.path.join(memdir, f)) as fh:
        content = fh.read(2000)
    fm_match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
    if not fm_match:
        bad += 1
        continue
    if not re.search(r'^\s*type:\s*\S', fm_match.group(1), re.MULTILINE):
        bad += 1
print(bad)
" 2>/dev/null || echo 0)
if [ "$BAD_FM" -gt 0 ]; then
    ISSUES="${ISSUES}${BAD_FM}记忆文件格式异常 "
    TRIGGERED=1
fi

# ── 6. 注入采纳率（连接点④路3：消费→慢环）──
# 读 reuse-log kind=adoption verdict，近期平均采纳率低 = synthesis 注入了但 CC 没真用
# （信息容器候选）→ 触发慢环审视注入质量（范式：采纳率趋势喂慢环，增益下降触发报警）。
# 复用 maintenance-guard marker 机制（守原则3不另造触发器），session-start §6.5 已读 marker 注入。
# 阈值：样本≥3（避免单条噪声）且平均<0.3。趋势判断需多时间点数据，当前数据单一留诚实标注。
# ⚠️ verdict 是 LLM-judge 判的非真值（单 judge 偏差风险），触发慢环=审视注入质量非自动降级
# （0% 采纳可能是注入错配如 pp-002，非知识无价值）。人工抽检高价值案例。
REUSE_LOG="$HOME/.claude/instincts/reuse-log.jsonl"
LOW_ADOPTION=$(python3 -c "
import json, os
from datetime import datetime, timezone, timedelta
rlog = '$REUSE_LOG'
if not os.path.exists(rlog):
    print(0); raise SystemExit
# 14 天窗口：只算近期 adoption，旧数据自然过期（修复 L91 '趋势判断需多时间点' 与全量算平均的矛盾，
# 亦防历史污染永久拉低均值——如修复前注入错配产生的 0% 样本）
cutoff = datetime.now(timezone.utc) - timedelta(days=14)
rates = []
for ln in open(rlog, encoding='utf-8'):
    try:
        d = json.loads(ln)
    except Exception:
        continue
    if d.get('kind') != 'adoption' or d.get('rate', -1) < 0:
        continue
    # 非 doubao judge 不可靠（kimi 漂移致空输出/解析失败），不计降级信号（B 防御层）
    if d.get('judge_model', 'doubao-seed-2.0-pro') != 'doubao-seed-2.0-pro':
        continue
    # 错配过滤：expected_project 存在且 actual 不在其中 = 注入错配（假性低采纳），不计降级
    _expected = d.get('expected_project', [])
    if _expected and d.get('project') not in _expected:
        continue
    ts = d.get('ts', '')
    try:
        t = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        continue  # ts 缺失/格式错跳过（不纳入也不报错）
    if t >= cutoff:
        rates.append(d['rate'])
if len(rates) < 3:
    print(0); raise SystemExit  # 样本不足不判（避免单条噪声）
avg = sum(rates) / len(rates)
print(1 if avg < 0.3 else 0)
" 2>/dev/null || echo 0)
if [ "$LOW_ADOPTION" -eq 1 ]; then
    ISSUES="${ISSUES}低采纳率 "
    TRIGGERED=1
fi

# ── 写入标记文件 ──
mkdir -p "$(dirname "$MARKER")"

# 计数超标项（每个检测维度追加时以空格分隔，空 ISSUES 不算）
ISSUE_COUNT=0
# 注：原 L120 `[ -n "$ISSUES" ] && ISSUE_COUNT=$(echo|grep -o [A-Z]|head|wc)` 已删——
# ISSUES 含中文时 grep -o '[A-Z]' 无匹配 exit1 + pipefail → 命令替换退出码1 → set -e 触发，
# 致 maintenance-guard 在写 marker 前退出（触发态才暴露，健康态 ISSUES 空短路不崩）。
# 且该行是死代码（紧接的 ISSUE_COUNT=$TRIGGERED 又被下方 ISSUE_COUNT=0 覆盖）。L4阶段3路3首次测触发态暴露。
# 更准确的方式：统计触发了几次检测
ISSUE_COUNT=$TRIGGERED  # 简化：直接用每个维度的触发次数
# 实际用更细粒度的计数
ISSUE_COUNT=0
echo "$ISSUES" | grep -q "CLAUDE" && ISSUE_COUNT=$((ISSUE_COUNT + 1))
echo "$ISSUES" | grep -q "MEMORY" && ISSUE_COUNT=$((ISSUE_COUNT + 1))
echo "$ISSUES" | grep -q "慢环" && ISSUE_COUNT=$((ISSUE_COUNT + 1))
echo "$ISSUES" | grep -q "Wiki" && ISSUE_COUNT=$((ISSUE_COUNT + 1))
echo "$ISSUES" | grep -q "记忆文件" && ISSUE_COUNT=$((ISSUE_COUNT + 1))
echo "$ISSUES" | grep -q "低采纳率" && ISSUE_COUNT=$((ISSUE_COUNT + 1))

if [ "$TRIGGERED" -eq 1 ]; then
    # 判断严重度：单项超标→定向修复 / 多项超标→全量慢环
    if [ "$ISSUE_COUNT" -le 1 ]; then
        MODE="targeted"
    else
        MODE="full"
    fi

    cat > "$MARKER" << EOF
{
  "triggered": true,
  "mode": "$MODE",
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "issues": "$(echo "$ISSUES" | tr -d '\n')"
}
EOF
    if [ "$MODE" = "targeted" ]; then
        echo "[maintenance-guard] 🟡 单项超标($ISSUES)→ 定向修复" >&2
    else
        echo "[maintenance-guard] 🔴 多项超标($ISSUES)→ 全量慢环" >&2
    fi
    exit 1
else
    rm -f "$MARKER"
    echo "[maintenance-guard] 🟢 全部正常" >&2
    exit 0
fi
