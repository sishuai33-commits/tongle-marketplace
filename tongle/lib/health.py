"""质量保障：健康检查三合一（maintenance + memory-health + runtime）

提取自 maintenance-guard.sh + memory-health.sh + runtime-health-check.py（Step 3 内核提取2）。
IO 经 lib/state，路径经 lib/paths。

架构范式 §2.2 模块契约：memory/wiki/instincts目录 → 多维度健康检查 → 健康报告 + maintenance marker。
三个入口职责不同，合于一模块共享路径/IO 抽象：
- maintenance_check()：SessionStart 静默门禁，6 维度超标写 marker（targeted/full），exit 1/0
- memory_health_report()：慢环深度检查，三模式（完整/--json/--quick）+ HEALTH_LOG
- runtime_check()：运行态实体检查，5 项（采集环/判别环/消费环/加工环/四库），exit 0/1/2
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import paths, state, utils, guards
from .health_runtime import runtime_check  # P1 提取到独立模块，保持向后兼容 re-export


# ---------- maintenance_check（原 maintenance-guard.sh bash→py）----------

def _extract_type(path):
    """从 frontmatter 提取 type 字段值（原 bash sed -n '/^---$/,/^---$/p' | grep '  type:' | awk '{print $2}'）"""
    text = state.read_text(path)[:2000]
    m = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
    if not m:
        return None
    for line in m.group(1).splitlines():
        if re.match(r'^\s*type:\s*\S', line):
            # awk '{print $2}' = 第二字段（type: 后的值）
            return line.split("type:")[1].strip().split()[0] if "type:" in line else None
    return None


def maintenance_check():
    """SessionStart 静默门禁：6 维度超标写 marker（targeted/full），exit 1/0。

    原 maintenance-guard.sh bash→py 重写（Step 3）。stderr 格式保留（特征测试钉）。
    """
    memory_dir = Path(paths.cc_memory_dir())
    marker = paths.instincts_file(".maintenance-due")
    issues = []
    triggered = False

    # 1. CLAUDE.md 行数（>预警阈值，文件不存在跳过）
    claude_md = Path(paths.home()) / ".claude" / "CLAUDE.md"
    if claude_md.is_file():
        claude_lines = state.read_lines(claude_md)
        if claude_lines > guards.CLAUDE_WARN_LINES:
            issues.append(f"主配置 {claude_lines}/{guards.CLAUDE_MAX_LINES}行")
            triggered = True

    # 2. MEMORY.md 行数（>预警阈值）
    index_file = memory_dir / "MEMORY.md"
    if index_file.is_file():
        mem_lines = state.read_lines(index_file)
        if mem_lines > guards.INDEX_WARN_LINES:
            issues.append(f"记忆索引 {mem_lines}/{guards.INDEX_MAX_LINES}行")
            triggered = True

    # 3. 慢环过期（>7天）
    wm_file = memory_dir / "working-memory.md"
    if wm_file.is_file():
        wm_text = state.read_text(wm_file)
        m = re.search(r'last_slow_loop:\s*(\S+)', wm_text)
        if m:
            last_sl = m.group(1)
            try:
                last_sec = datetime.strptime(last_sl, "%Y-%m-%d").timestamp()
                days_since = int((time.time() - last_sec) / 86400)
                if days_since >= 7:
                    issues.append(f"慢环{days_since}天未执行")
                    triggered = True
            except Exception:
                pass

    # 4. Wiki 目录卫生（vault 根有散落 .md 或 wiki/__pycache__）
    vault = Path(paths.wiki_vault())
    if vault.is_dir():
        # find "$VAULT" -maxdepth 1 -name '*.md' -not -name '.ai-vocab*'
        orphans = [f for f in vault.glob("*.md") if not f.name.startswith(".ai-vocab")]
        pycache = (vault / "wiki" / "__pycache__").is_dir()
        if orphans or pycache:
            issues.append("Wiki目录违规")
            triggered = True

    # 5. Memory frontmatter 检查（type 在 metadata 块内）
    bad_fm = 0
    for f in os.listdir(memory_dir):
        if not f.endswith(".md") or f in ("MEMORY.md", ".memory-health-log.md", "working-memory.md"):
            continue
        content = state.read_text(memory_dir / f)[:2000]
        fm_match = re.match(r'^---\n(.*?)\n---', content, re.DOTALL)
        if not fm_match:
            bad_fm += 1
            continue
        if not re.search(r'^\s*type:\s*\S', fm_match.group(1), re.MULTILINE):
            bad_fm += 1
    if bad_fm > 0:
        issues.append(f"{bad_fm}记忆文件格式异常")
        triggered = True

    # 6. 注入采纳率（连接点④路3：消费→慢环）
    reuse_log = paths.instincts_file("reuse-log.jsonl")
    if utils.low_adoption(reuse_log):
        issues.append("低采纳率")
        triggered = True

    # === 写 marker ===
    Path(marker).parent.mkdir(parents=True, exist_ok=True)
    issues_str = " ".join(issues)

    if triggered:
        # mode：单项→targeted / 多项→full
        issue_count = sum(1 for kw in ("主配置", "记忆索引", "慢环", "Wiki", "记忆文件", "低采纳率") if kw in issues_str)
        mode = "targeted" if issue_count <= 1 else "full"
        state.write_json(marker, {
            "triggered": True,
            "mode": mode,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "issues": issues_str,
        })
        if mode == "targeted":
            print(f"[maintenance-guard] 🟡 单项超标({issues_str})→ 定向修复", file=sys.stderr)
        else:
            print(f"[maintenance-guard] 🔴 多项超标({issues_str})→ 全量慢环", file=sys.stderr)
        sys.exit(1)
    else:
        if os.path.exists(marker):
            os.remove(marker)
        print("[maintenance-guard] 🟢 全部正常", file=sys.stderr)
        sys.exit(0)


# ---------- memory_health_report（原 memory-health.sh bash→py）----------

def memory_health_report():
    """慢环深度健康检查，三模式（完整/--json/--quick）+ HEALTH_LOG。

    原 memory-health.sh bash→py 重写（Step 3）。stdout 格式保留（特征测试钉）。
    """
    memory_dir = Path(paths.cc_memory_dir())
    health_log = memory_dir / ".memory-health-log.md"
    json_mode = "--json" in sys.argv
    quick_mode = "--quick" in sys.argv
    now = time.time()

    # === 数据采集 ===
    files_info = []  # (size, age_days, name)
    types = []
    prefixes = []
    total_size = 0
    for f in state.md_files(memory_dir):
        name = f.name
        if name == "MEMORY.md":
            continue
        size = os.path.getsize(f)
        mtime = os.path.getmtime(f)
        age_days = int((now - mtime) / 86400)
        files_info.append((size, age_days, name))
        total_size += size
        type_ = _extract_type(f)
        types.append(type_ or "unknown")
        prefix = re.sub(r'\.md$', '', re.sub(r'-[0-9][0-9-]*', '', name))
        prefixes.append(prefix)

    total = len(files_info)
    new_week = sum(1 for _, ag, _ in files_info if ag <= 7)
    new_month = sum(1 for _, ag, _ in files_info if 7 < ag <= 30)
    old_30 = sum(1 for _, ag, _ in files_info if 30 < ag <= 60)
    old_60 = sum(1 for _, ag, _ in files_info if 60 < ag <= 90)
    old_90 = sum(1 for _, ag, _ in files_info if ag > 90)

    # 膨胀预警 (>5KB)
    bloat = "  ".join(f"{nm}:{sz//1024}K" for sz, _, nm in files_info if sz > 5000)
    # 日落候选 (>60天+<300B 或 >90天)
    sunset = "  ".join(f"{nm}:{ag}d:{sz}B" for sz, ag, nm in files_info if (ag > 60 and sz < 300) or ag > 90)
    # 合并候选 (同前缀≥3)
    from collections import Counter
    prefix_counts = Counter(prefixes)
    merge = "  ".join(f"{p}:{c}个" for p, c in prefix_counts.most_common() if c >= 3)
    # 毕业候选 (project/reference类, 稳定>30天, >1KB, 未迁移)
    graduate_parts = []
    for sz, ag, nm in files_info:
        if ag > 30 and sz > 1000:
            type_ = _extract_type(memory_dir / nm)
            if type_ in ("project", "reference"):
                if "完整内容已迁移至 Wiki" not in state.read_text(memory_dir / nm):
                    graduate_parts.append(f"{nm}:{ag}d:{sz//1024}K:{type_}")
    graduate = "  ".join(graduate_parts)
    # 毕业残留 (MEMORY.md 标记 ~~毕业~~ 但文件仍在)
    stale_graduation_parts = []
    index_text = state.read_text(memory_dir / "MEMORY.md")
    for line in index_text.splitlines():
        m = re.search(r'~~([^~]+)~~', line)
        if not m:
            continue
        entry_name = m.group(1)
        entry_norm = re.sub(r'\s', '', entry_name).lower()
        for f in state.md_files(memory_dir):
            fname = f.name
            fname_norm = re.sub(r'-[0-9-]*\.md$', '', fname)
            if len(entry_norm) > 4 and entry_norm in fname_norm.lower():
                if (memory_dir / fname).is_file():
                    stale_graduation_parts.append(fname)
    stale_graduation = "  ".join(stale_graduation_parts)
    # 微小文件 (<300B)
    tiny = "  ".join(f"{nm}:{sz}B" for sz, _, nm in files_info if sz < 300)
    # 类型统计
    type_counts = Counter(types)
    type_summary = "\n".join(f"  {t}: {c}" for t, c in type_counts.most_common())
    # 最大文件
    max_info = ""
    if files_info:
        max_sz, _, max_nm = sorted(files_info, reverse=True)[0]
        max_info = f"{max_nm} ({max_sz//1024}K)"

    # === 输出 ===
    if json_mode:
        print(json.dumps({
            "total": total,
            "age_week": new_week, "age_month": new_month,
            "age_30d": old_30, "age_60d": old_60, "age_90d+": old_90,
            "total_kb": total_size // 1024,
            "largest": max_info,
            "bloat": bloat,
            "sunset": sunset,
            "graduate": graduate,
            "stale_graduation": stale_graduation,
            "merge": merge,
            "tiny": tiny,
        }, ensure_ascii=False, indent=2))
        return

    if quick_mode:
        w = 0
        if bloat:
            print(f"⚠️  膨胀:{bloat}"); w += 1
        if sunset:
            print(f"⚠️  日落:{sunset}"); w += 1
        if merge:
            print(f"💡 合并:{merge}"); w += 1
        if graduate:
            print(f"💡 毕业:{graduate}"); w += 1
        if stale_graduation:
            print(f"⚠️  毕业残留:{stale_graduation}"); w += 1
        if w == 0:
            print("✅ 无预警")
        return

    # 完整报告
    print(f"## Memory 慢环深度检查 — {datetime.now().strftime('%Y-%m-%d')}")
    print()
    print("### 📊 概览")
    print(f"- 文件总数: **{total}** | 总大小: {total_size//1024}KB | 最大: {max_info}")
    print(f"- 本周: **{new_week}** | 月内: {new_month} | >30天: {old_30} | >60天: {old_60} | >90天: {old_90}")
    print()
    print("### 📂 类型分布")
    print(type_summary)
    print()
    print("### 📏 膨胀预警 (>5KB)")
    print(f"  {'✅ 无' if not bloat else bloat}")
    print()
    print("### 🔀 合并候选 (同前缀≥3个文件)")
    print(f"  {'✅ 无' if not merge else merge}")
    print()
    print("### 🌅 日落候选 (>60天+<300B 或 >90天)")
    print(f"  {'✅ 无' if not sunset else sunset}")
    print()
    print("### 🎓 毕业候选 (project/reference类, 稳定>30天, >1KB)")
    print(f"  {'✅ 无' if not graduate else graduate}")
    print()
    print("### ⚠️ 毕业残留 (MEMORY.md已标记~~毕业~~但文件仍在)")
    print(f"  {'✅ 无' if not stale_graduation else stale_graduation}")
    print()
    print("### 🔬 微小文件 (<300B)")
    print(f"  {'✅ 无' if not tiny else tiny}")

    # === 写健康日志 ===
    log_parts = [f"## {datetime.now().strftime('%Y-%m-%d')} | 文件:{total} | 本周:{new_week} | >30d:{old_30}"]
    if bloat:
        log_parts.append(f"- 膨胀:{bloat}")
    if sunset:
        log_parts.append(f"- 日落:{sunset}")
    if graduate:
        log_parts.append(f"- 毕业:{graduate}")
    if merge:
        log_parts.append(f"- 合并:{merge}")
    if not (bloat or sunset or graduate or merge):
        log_parts.append("- ✅ 健康")
    log_parts.append("")
    try:
        with open(health_log, "a", encoding="utf-8") as f:
            f.write("\n".join(log_parts) + "\n")
    except OSError:
        pass


# P1 模块瘦身：runtime_check 已提取到 lib/health_runtime.py，此处 re-export 保持向后兼容。
# 直接 import 仍可用 from lib.health import runtime_check。


# P1 模块瘦身：SessionStart 告警函数已提取到 lib/health_alerts.py，此处 re-export 保持向后兼容。
from .health_alerts import (
    memory_alert, pending_wiki_sync, dashboard, alert_context,
    first_run_check, collection_visibility, wiki_health,
)
