#!/bin/bash
# ============================================================
# wiki-lint.sh — Wiki 健康检查守卫（L2）
# ============================================================
# 封装 wiki_checks.py，提供与 skills-lint.sh 一致的接口
# 只读操作，不修改任何文件
#
# 用法:
#   bash wiki-lint.sh               # 检查所有 lint 规则
#   bash wiki-lint.sh --json        # JSON 输出
#   bash wiki-lint.sh --fix         # 自动修复（委托给 wiki_checks.py）
# ============================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WIKI_CHECKS="$SCRIPT_DIR/scripts/wiki_checks.py"
WIKI_ROOT="${WIKI_VAULT_PATH:-$HOME/Documents/Obsidian Vault}/wiki"  # v1.5.1: 统一用 WIKI_VAULT_PATH（和 daemon/wiki_checks.py 一致）
JSON_OUTPUT=false
FIX_MODE=false

for arg in "$@"; do
    case "$arg" in
        --json) JSON_OUTPUT=true ;;
        --fix)  FIX_MODE=true ;;
        --help|-h)
            echo "用法: wiki-lint.sh [--json] [--fix]"
            echo "Wiki 健康检查守卫，封装 wiki_checks.py"
            exit 0
            ;;
    esac
done

# ============================================================
# 调用 wiki_checks.py
# ============================================================

PASSES=()
WARNS=()
FAILS=()

if [ ! -f "$WIKI_CHECKS" ]; then
    echo "Error: wiki_checks.py not found at $WIKI_CHECKS"
    exit 1
fi

# 运行 wiki_checks.py 获取 JSON 结果
if $FIX_MODE; then
    raw_output=$(python3 "$WIKI_CHECKS" --fix 2>&1) || true
else
    raw_output=$(python3 "$WIKI_CHECKS" --json 2>&1) || true
fi

# wiki_checks.py --json 输出是多行 JSON，直接解析
json_part=$(echo "$raw_output" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(json.dumps(data))
" 2>/dev/null)

if [ -z "$json_part" ]; then
    # Fallback: 如果 wiki_checks.py 没有 JSON 输出，用基本检查
    # 规则 1: 检查 wiki 目录存在
    if [ ! -d "$WIKI_ROOT" ]; then
        FAILS+=("FAIL|wiki-root|Wiki 根目录不存在: $WIKI_ROOT")
    else
        # 统计页面文件
        total_pages=$(find "$WIKI_ROOT" -name "*.md" | wc -l | tr -d ' ')
        PASSES+=("PASS|wiki-root|${total_pages} 个 .md 页面")

        # 规则 4: 检查过期页面 (validated > 90d)
        outdated=$(python3 -c "
import os, sys, re
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
outdated = 0
for root, dirs, files in os.walk('$WIKI_ROOT'):
    for f in files:
        if not f.endswith('.md'): continue
        path = os.path.join(root, f)
        try:
            with open(path) as fh:
                content = fh.read(10000)
            if not content.startswith('---'): continue
            parts = content.split('---', 2)
            if len(parts) < 3: continue
            fm = parts[1]
            # 找 validated 字段
            m = re.search(r'validated:\s*\"?([^\"]+)\"?', fm)
            if not m: continue
            dt = m.group(1).strip().replace('T', ' ').replace('Z', '')[:10]
            validated = datetime.strptime(dt, '%Y-%m-%d').replace(tzinfo=timezone.utc)
            # 找 pinned
            pinned = 'pinned: true' in fm or 'pinned: True' in fm
            if not pinned and (now - validated).days > 90:
                outdated += 1
        except:
            pass
print(outdated)
" 2>/dev/null)
        if [ -n "$outdated" ] && [ "$outdated" -gt 0 ]; then
            WARNS+=("WARN|expired-content|${outdated} 个页面超过 90 天未验证")
        else
            PASSES+=("PASS|expired-content|所有页面验证时效合格")
        fi
    fi
else
    # wiki_checks.py 正常输出了 JSON
    checks=$(echo "$json_part" | python3 -c "
import sys, json
data = json.load(sys.stdin)
# 尝试提取关键指标
total = data.get('total_pages', '?')
issues = data.get('issues', data.get('violations', 0))
print(f'{total}|{issues}')
" 2>/dev/null)
    total=$(echo "$checks" | cut -d'|' -f1)
    issues=$(echo "$checks" | cut -d'|' -f2)

    if [ "$issues" != "0" ] && [ -n "$issues" ]; then
        WARNS+=("WARN|wiki-checks|${issues} 项问题，运行 wiki_checks.py --fix 可自动修复")
    else
        PASSES+=("PASS|wiki-checks|${total} 页面，${issues} 项问题")
    fi
fi

# ============================================================
# 输出
# ============================================================

n_pass_val=${#PASSES[@]}
n_fail_val=${#FAILS[@]}
n_warn_val=${#WARNS[@]}

if $JSON_OUTPUT; then
    echo "{"
    echo "  \"total_checks\": $((n_pass_val + n_fail_val + n_warn_val)),"
    echo "  \"passed\": $n_pass_val,"
    echo "  \"failed\": $n_fail_val,"
    echo "  \"warned\": $n_warn_val,"
    echo "  \"results\": {"
    echo "    \"pass\": $(printf '%s\n' "${PASSES[@]}" | jq -R . | jq -s . 2>/dev/null || echo '[]'),"
    echo "    \"fail\": $(printf '%s\n' "${FAILS[@]}" | jq -R . | jq -s . 2>/dev/null || echo '[]'),"
    echo "    \"warn\": $(printf '%s\n' "${WARNS[@]}" | jq -R . | jq -s . 2>/dev/null || echo '[]')"
    echo "  }"
    echo "}"
else
    echo "============================================"
    echo " Wiki Lint Report"
    echo "============================================"
    echo ""

    for p in "${PASSES[@]}"; do
        echo "  ✅ ${p#PASS|}"
    done
    for w in "${WARNS[@]}"; do
        echo "  ⚠️  ${w#WARN|}"
    done
    for f in "${FAILS[@]}"; do
        echo "  ❌ ${f#FAIL|}"
    done

    echo ""
    echo "---"
    echo "Total: $((n_pass_val + n_fail_val + n_warn_val)) | ✅ $n_pass_val | ⚠️ $n_warn_val | ❌ $n_fail_val"

    if [ "$n_fail_val" -gt 0 ]; then
        echo ""
        echo "🔴 FAIL detected. 运行 python3 $WIKI_CHECKS --fix 自动修复。"
        exit 1
    elif [ "$n_warn_val" -gt 0 ]; then
        echo ""
        echo "🟡 WARN only. 运行 wiki_checks.py --fix 可自动修复部分问题。"
        exit 2
    else
        echo ""
        echo "🟢 All clear."
        exit 0
    fi
fi
