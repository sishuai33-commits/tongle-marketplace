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
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import paths, state


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


def _low_adoption(reuse_log):
    """连接点④路3：读 reuse-log adoption verdict，14天窗口样本≥3 且 avg<0.3 返回 (avg, n)，否则 None（truthy 兼容 maintenance_check）"""
    if not os.path.exists(reuse_log):
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    rates = []
    for d in state.read_jsonl(reuse_log):
        if d.get("kind") != "adoption" or d.get("rate", -1) < 0:
            continue
        # 非 doubao judge 不可靠，不计降级信号
        if d.get("judge_model", "doubao-seed-2.0-pro") != "doubao-seed-2.0-pro":
            continue
        # 错配过滤：expected_project 存在且 actual 不在其中 = 注入错配，不计
        _expected = d.get("expected_project", [])
        if _expected and d.get("project") not in _expected:
            continue
        ts = d.get("ts", "")
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if t >= cutoff:
            rates.append(d["rate"])
    if len(rates) < 3:
        return None  # 样本不足不判
    avg = sum(rates) / len(rates)
    if avg >= 0.3:
        return None  # 采纳率正常
    return (avg, len(rates))


def maintenance_check():
    """SessionStart 静默门禁：6 维度超标写 marker（targeted/full），exit 1/0。

    原 maintenance-guard.sh bash→py 重写（Step 3）。stderr 格式保留（特征测试钉）。
    """
    memory_dir = Path(paths.cc_memory_dir())
    marker = paths.instincts_file(".maintenance-due")
    issues = []
    triggered = False

    # 1. CLAUDE.md 行数（>180 预警，文件不存在跳过）
    claude_md = Path(paths.home()) / ".claude" / "CLAUDE.md"
    if claude_md.is_file():
        claude_lines = state.read_lines(claude_md)
        if claude_lines > 180:
            issues.append(f"CLAUDE.md {claude_lines}/200行")
            triggered = True

    # 2. MEMORY.md 行数（>140 预警）
    index_file = memory_dir / "MEMORY.md"
    if index_file.is_file():
        mem_lines = state.read_lines(index_file)
        if mem_lines > 140:
            issues.append(f"MEMORY.md {mem_lines}/150行")
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
    if _low_adoption(reuse_log):
        issues.append("低采纳率")
        triggered = True

    # === 写 marker ===
    Path(marker).parent.mkdir(parents=True, exist_ok=True)
    issues_str = " ".join(issues)

    if triggered:
        # mode：单项→targeted / 多项→full
        issue_count = sum(1 for kw in ("CLAUDE", "MEMORY", "慢环", "Wiki", "记忆文件", "低采纳率") if kw in issues_str)
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


# ---------- runtime_check（原 runtime-health-check.py python 搬）----------

def runtime_check():
    """ke 运行态健康检查：5 项（采集环/判别环/消费环/加工环/四库），exit 0/1/2。

    原 runtime-health-check.py 搬（Step 3）。stdout 格式保留（特征测试钉 pass=11）。
    """
    instincts = paths.instincts_dir()
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    def parse_ts(ts_str):
        if not ts_str:
            return None
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            return None

    def file_info(name):
        path = os.path.join(instincts, name)
        if not os.path.exists(path):
            return {"exists": False, "path": path}
        size = os.path.getsize(path)
        mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        lines = state.read_lines(path)
        return {
            "exists": True, "path": path, "size": size, "lines": lines,
            "mtime": mtime.isoformat(),
            "recent_7d": mtime > seven_days_ago,
        }

    checks = []  # (level, name, status, detail)

    def check(level, name, status, detail=""):
        checks.append((level, name, status, detail))

    # --- 1. 采集环 ---
    obs = file_info("observations.jsonl")
    src_obs = file_info("source-observations.jsonl")
    check("pass" if obs.get("recent_7d") else "warn",
          "采集环.observations 活跃度",
          obs.get("recent_7d", False),
          f"mtime={obs.get('mtime')} lines={obs.get('lines')} (7天内有更新=采集环真转)")
    check("pass" if src_obs.get("exists") else "fail",
          "采集环.source-observations 存在",
          src_obs.get("exists", False),
          f"三源扫描器产出文件")
    cursor = file_info(".discriminate-cursor")
    check("pass" if cursor.get("recent_7d") else "warn",
          "采集环.cursor 推进",
          cursor.get("recent_7d", False),
          f"mtime={cursor.get('mtime')} (cursor 停滞=采集没跑)")

    # --- 2. 判别环 ---
    pq = file_info("pending-queue.jsonl")
    exp = file_info("discriminate-experience.jsonl")
    marker = file_info(".discriminate-due")

    pending_count = 0
    resolved_count = 0
    if pq.get("exists"):
        for r in state.read_jsonl(pq["path"]):
            if r.get("status") == "pending":
                pending_count += 1
            elif r.get("status") == "resolved":
                resolved_count += 1

    threshold = 3
    marker_exists = marker.get("exists", False)
    if pending_count >= threshold:
        consistent = marker_exists
        check("fail" if not consistent else "pass",
              "判别环.marker-pending 一致性",
              consistent,
              f"pending={pending_count}>=阈值{threshold} → marker 应在(实际{'在' if marker_exists else '不在'})")
    else:
        consistent = not marker_exists
        check("pass" if consistent else "warn",
              "判别环.marker-pending 一致性",
              consistent,
              f"pending={pending_count}<阈值{threshold} → marker 应不在(实际{'在' if marker_exists else '不在'})")

    check("pass" if exp.get("exists") and exp.get("lines", 0) > 0 else "fail",
          "判别环.判别经验库 有数据",
          exp.get("exists") and exp.get("lines", 0) > 0,
          f"lines={exp.get('lines')} (人裁判别经验回流)")

    # --- 3. 消费环 ---
    rl = file_info("reuse-log.jsonl")
    synthesis_reads = 0
    adoption_verdicts = 0
    recent_synthesis = 0
    if rl.get("exists"):
        for r in state.read_jsonl(rl["path"]):
            k = r.get("kind", "")
            ts = parse_ts(r.get("ts"))
            is_syn = "synthesis" in (r.get("file", "") or "").lower() or k == "synthesis"
            if is_syn:
                synthesis_reads += 1
                if ts and ts > seven_days_ago:
                    recent_synthesis += 1
            if k == "adoption":
                adoption_verdicts += 1

    check("pass" if synthesis_reads > 0 else "fail",
          "消费环.真Read synthesis 有记录",
          synthesis_reads > 0,
          f"total={synthesis_reads}条 (A1验收基础：CC真读synthesis触发PostToolUse写入)")
    check("pass" if recent_synthesis > 0 else "warn",
          "消费环.最近7天有真Read",
          recent_synthesis > 0,
          f"近7天{recent_synthesis}条 (消费环持续在转，非一次性)")
    check("pass" if adoption_verdicts > 0 else "warn",
          "消费环.adoption verdict 有数据",
          adoption_verdicts > 0,
          f"total={adoption_verdicts}条 (采纳率判定数据源，注入有效性证据)")

    # --- 4. 加工环 ---
    cross_domain_lib = file_info("cross-domain-patterns.jsonl")
    refine_log = file_info(".refine-last-run")
    check("fail" if not cross_domain_lib.get("exists") else "pass",
          "加工环.跨域模式库 存在",
          cross_domain_lib.get("exists", False),
          f"{'存在' if cross_domain_lib.get('exists') else '不存在(M1未做，当前唯一真零生产环)'}")
    check("fail" if not refine_log.get("exists") else "pass",
          "加工环.规整脚本运行过",
          refine_log.get("exists", False),
          f"{'运行过' if refine_log.get('exists') else '从未运行(M1未做)'}")

    # --- 5. 四库存在性汇总 ---
    libs = {
        "原料库(observations)": obs.get("exists", False),
        "判别经验库(experience)": exp.get("exists", False) and exp.get("lines", 0) > 0,
        "复用日志(reuse-log)": rl.get("exists", False) and rl.get("lines", 0) > 0,
        "跨域模式库(cross-domain)": cross_domain_lib.get("exists", False),
    }
    exist_count = sum(1 for v in libs.values() if v)
    check("pass" if exist_count == 4 else ("warn" if exist_count >= 3 else "fail"),
          "四库存在性",
          exist_count,
          f"{exist_count}/4: " + " ".join(f"{'✓' if v else '✗'}{k}" for k, v in libs.items()))

    # === 输出 ===
    json_mode = "--json" in sys.argv
    fails = [c for c in checks if c[0] == "fail"]
    warns = [c for c in checks if c[0] == "warn"]

    if json_mode:
        print(json.dumps({
            "ts": now.isoformat(),
            "total": len(checks),
            "pass": len(checks) - len(fails) - len(warns),
            "warn": len(warns),
            "fail": len(fails),
            "checks": [{"level": c[0], "name": c[1], "status": c[2], "detail": c[3]} for c in checks],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"# ke 运行态健康检查 @ {now.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"# 实体目录: {instincts}")
        print()
        for level, name, status, detail in checks:
            icon = {"pass": "✓", "warn": "⚠", "fail": "✗"}[level]
            print(f"  {icon} [{level.upper()}] {name}")
            if detail:
                print(f"      {detail}")
        print()
        print(f"# 汇总: {len(checks)}项 / pass={len(checks)-len(fails)-len(warns)} warn={len(warns)} fail={len(fails)}")
        if fails:
            print(f"# 🔴 FAIL项(硬阻断):")
            for _, n, _, d in fails:
                print(f"#   - {n}: {d}")
        print(f"# 退出码: {0 if not fails else (1 if warns else 2)}")

    sys.exit(0 if not fails and not warns else (2 if fails else 1))


# ---------- SessionStart 健康提醒（原 session-start.py §4/§6/§6.5b/§6.6 搬入，ponytail-audit 批3）----------

def _run_quiet(cmd, timeout=15):
    """静默跑外部命令，返回 stdout（失败返回 ''）"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def memory_alert(instincts_dir):
    """Memory 健康提醒（基于 .memory-guard-state.json + 分级节流，原 §4）

    与 memory_health_report() 不同：本函数读 guard 状态做轻量节流提醒
    （red 即报 / yellow 3天报一次），供 SessionStart 用；
    memory_health_report() 是慢环深度扫描报告，供 CLI 用。
    """
    state_file = os.path.join(instincts_dir, ".memory-guard-state.json")
    if not os.path.isfile(state_file):
        return ""
    try:
        with open(state_file, encoding="utf-8", errors="replace") as f:
            g = json.load(f)
    except (OSError, json.JSONDecodeError):
        return ""
    severity = g.get('severity', 'green')
    now = int(time.time())
    should_remind = False
    if severity == 'red':
        should_remind = True
    elif severity == 'yellow':
        last = g.get('last_reminded_epoch', 0)
        days = (now - last) / 86400 if last else 999
        should_remind = days >= 3
    if not should_remind:
        return ""
    parts = []
    if g.get('orphan_count', 0) > 0: parts.append(f"{g['orphan_count']} 无引用")
    if g.get('frontmatter_bad', 0) > 0: parts.append(f"{g['frontmatter_bad']} 格式错")
    if g.get('dangling_count', 0) > 0: parts.append(f"{g['dangling_count']} 断链")
    if g.get('index_over_limit', False): parts.append('索引超长')
    if g.get('claude_md_over_limit', False): parts.append('主配置超长')
    icon = '🔴' if severity == 'red' else '🟡'
    detail = ' '.join(parts) if parts else ''
    total = g.get('total_violations', 0)
    detail_prefix = f"其中 {detail}，" if detail else ""
    g['reminder_count'] = g.get('reminder_count', 0) + 1
    g['last_reminded_epoch'] = now
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(g, f, indent=2)
    except OSError:
        pass
    if severity == 'red':
        return f"{icon} 记忆库 {total} 项待整理，{detail_prefix}影响跨会话接续，需立即整理"
    return f"{icon} 记忆库 {total} 项待整理，{detail_prefix}有空处理"


def pending_wiki_sync(instincts_dir, snooze_file):
    """Pending Wiki Sync 检查（原 §6），返回 (section_str, count)

    v1.3.0 改：读 pending-compile.jsonl 持久化队列（非一次性 .pending-wiki-sync marker），
    只数 status=pending 的，不删（/ke-compile 编译后标 compiled）。
    """
    pc_file = os.path.join(instincts_dir, "pending-compile.jsonl")
    if not os.path.isfile(pc_file):
        return "", 0
    snooze = []
    if os.path.isfile(snooze_file):
        with open(snooze_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                k = line.strip()
                if not k or k.startswith('#') or k == 'wiki-health':
                    continue
                snooze.append(k.lower())
    titles = []
    try:
        with open(pc_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    if d.get("status") == "pending" and d.get("title"):
                        titles.append(d["title"])
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    filtered = [t for t in titles if not any(k in t.lower() for k in snooze)]
    if not filtered:
        return "", 0
    lines = [f'🟡 知识待沉淀 {len(filtered)} 条，新会话接不上，本次会话抽空归档：', '']
    for t in filtered:
        lines.append(f'- {t}')
    return '\n'.join(lines), len(filtered)


def _dashboard_stats(instincts_dir):
    """dashboard 统计：返回 (pending, succ, fail)"""
    pending = 0
    pq = os.path.join(instincts_dir, "pending-queue.jsonl")
    if os.path.isfile(pq):
        with open(pq, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get('status') != 'resolved':
                        pending += 1
                except Exception:
                    pass
    succ = fail = 0
    rl = os.path.join(instincts_dir, "reuse-log.jsonl")
    if os.path.isfile(rl):
        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        with open(rl, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    ts = d.get('ts', '')
                    if ts:
                        t = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                        if t >= week_ago:
                            ok = d.get('ok')
                            if ok is True:
                                succ += 1
                            elif ok is False:
                                fail += 1
                except Exception:
                    pass
    return pending, succ, fail


_PENDING_LABELS = {
    "file_change_candidate": "文件变更",
    "new_candidate": "外部搜索",
    "evolve_candidate": "演进",
    "transcript_candidate": "对话",
    "ima_candidate": "IMA",
}


def _pending_breakdown(instincts_dir):
    """pending 候选按 pattern 分类统计，返回中文摘要（如 '文件变更40/外部搜索8'）。

    报警价值要求：光报计数无法判断值不值得看，带分类才知道构成
    （40 文件变更=噪音可忽略，8 外部搜索=值得裁决）。
    """
    from collections import Counter
    pq = os.path.join(instincts_dir, "pending-queue.jsonl")
    c = Counter()
    if os.path.isfile(pq):
        try:
            with open(pq, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if d.get('status') != 'resolved':
                            c[d.get('pattern', '未知')] += 1
                    except Exception:
                        pass
        except OSError:
            pass
    items = []
    for pat, n in c.most_common():
        label = _PENDING_LABELS.get(pat, pat)
        items.append(f'{label}{n}')
    return '/'.join(items)


def dashboard(instincts_dir):
    """触发式仪表盘（待批阅≥50 + 经验值周 fail≥5 + 低采纳率，原 §6.5b）

    阈值 50（指挥官7/9反馈：≥5 太频繁打扰，拉到50且带分类才有价值）。
    低采纳率沿用 _low_adoption 自带阈值（14天窗口 avg<0.3），不另设阈值（守7/9不增打扰决策）。
    都没触发返回空串（安静，守 concise-output）。
    """
    pending, succ, fail = _dashboard_stats(instincts_dir)
    parts = []
    if pending >= 50:
        breakdown = _pending_breakdown(instincts_dir)
        parts.append(f'待批阅 {pending} 条（{breakdown}）' if breakdown else f'待批阅 {pending} 条')
    if fail >= 5:
        parts.append(f'本周经验值 {succ} 成 {fail} 败')
    low = _low_adoption(os.path.join(instincts_dir, "reuse-log.jsonl"))
    if low:
        parts.append(f'低采纳率（14天均值{low[0]:.2f}）')
    return '🟡 ' + '，'.join(parts) + '，抽空处理' if parts else ''


def alert_context(instincts_dir):
    """报警自解释（silent 注入，CC 后台自知，不进人可见对话）

    指挥官7/8反馈：仪表盘弹"待批阅 N 条"只有计数，新会话AI要查4轮才搞清对象。
    本函数返回报警的 silent 自解释，让 CC 知道"是什么/在哪/怎么处理"，不用查资料。
    人可见文案（dashboard()）不变，守 feedback-concise-output。
    """
    pending, succ, fail = _dashboard_stats(instincts_dir)
    parts = []
    if pending >= 50:
        breakdown = _pending_breakdown(instincts_dir)
        parts.append(
            f"待批阅 {pending} 条（{breakdown}）= 判别候选队列 pending-queue.jsonl 中 status=pending 的条目，"
            f"待人裁决。处理命令 /ke-review，参数：<序号> <关系类型> <处置> [说明]，"
            f"关系类型=新增/演进/互补/冲突，处置=采纳/丢弃/隔离。"
            f"序号是 pending 条目里的 1-based 计数，每次裁决后重排。"
            f"数据源 ~/.claude/instincts/pending-queue.jsonl"
        )
    if fail >= 5:
        parts.append(
            f"本周经验值 {succ} 成 {fail} 败 = reuse-log.jsonl 近7天 ok=False 计数。"
            f"失败多=判别经验采纳后复用效果差，需复盘经验质量。"
            f"数据源 ~/.claude/instincts/reuse-log.jsonl"
        )
    low = _low_adoption(os.path.join(instincts_dir, "reuse-log.jsonl"))
    if low:
        parts.append(
            f"低采纳率（14天均值{low[0]:.2f}，{low[1]}样本）= reuse-log.jsonl 中 kind=adoption 记录的 rate 均值<0.3，"
            f"即注入的知识AI采纳率低（注入了没用）。"
            f"处理：检查低采纳注入是否该降级（走慢环或对话内请示调整注入策略）。"
            f"数据源 ~/.claude/instincts/reuse-log.jsonl"
        )
    if not parts:
        return ""
    return "# 报警自解释（CC 后台自知，不用查资料）\n" + "\n".join(f"- {p}" for p in parts)


def first_run_check(instincts_dir):
    """首次运行/版本变更检测（v1.3.0 阶段5 仪式感，plan 决策5）

    读 plugin.json version 与 .installed-version marker 对比：
    - 无 .installed-version -> 首次：写 marker=version，返回欢迎 section
    - != version -> 更新：更新 marker=version，返回变更 section
    - == version -> 正常：返回 ""（不打扰，守 concise-output）

    fail-open：读不到 version/写 marker 失败不阻塞会话启动。
    """
    plugin_json = os.path.join(paths.plugin_root(), ".claude-plugin", "plugin.json")
    try:
        with open(plugin_json, encoding="utf-8") as f:
            version = json.load(f).get("version", "")
    except (OSError, json.JSONDecodeError):
        return ""
    if not version:
        return ""

    iv_path = os.path.join(instincts_dir, ".installed-version")
    installed = None
    try:
        with open(iv_path, encoding="utf-8", errors="replace") as f:
            installed = f.read().strip()
    except OSError:
        pass  # 首次（文件不存在）

    if installed == version:
        return ""  # 正常态不打扰

    # 首次或更新：写 marker
    try:
        with open(iv_path, "w", encoding="utf-8") as f:
            f.write(version)
    except OSError:
        pass  # 写失败 fail-open

    if installed is None:
        return (
            f"👋 欢迎使用 tongle 知识工程（v{version}）。\n"
            f"4 命令：/ke-health /ke-review /ke-collect /ke-compile\n"
            f"3 步验证：/ke-health 全绿 -> 聊两句看 AI 带入知识 -> /ke-review /ke-collect 看候选\n"
            f"采集自动（会话结束扫变更），编译手动（/ke-compile）"
        )
    # 5.4 更新：版本号 + 变更要点（CHANGELOG）+ 重初始化提示（major 变化）
    note = ""
    changelog = os.path.join(paths.plugin_root(), "dev", "releases", "CHANGELOG.md")
    try:
        with open(changelog, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip().startswith(f"## v{version}"):
                    s = line.strip()
                    if "（" in s and "）" in s:
                        note = s[s.index("（") + 1:s.rindex("）")]
                    else:
                        note = s.replace(f"## v{version}", "").strip(" -")
                    break
    except OSError:
        pass
    reinit = ""
    try:
        if installed.split(".")[0] != version.split(".")[0]:
            reinit = "major 版本变化，建议跑 /ke-health 确认"
    except Exception:
        pass
    parts = [f"🔔 tongle 已更新到 v{version}（原 {installed}）。"]
    if note:
        parts.append(f"变更要点：{note}")
    if reinit:
        parts.append(reinit)
    return "\n".join(parts)


def collection_visibility(instincts_dir):
    """采集可见性（v1.3.0 阶段5 仪式感，plan 5.5）

    每次会话显示"上次采集 N 条候选 / M 条待编译"（读 pending-queue + pending-compile）。
    都为 0 返回 ""（安静，守 concise-output）。
    """
    pending_candidates, _, _ = _dashboard_stats(instincts_dir)  # N: 待批阅候选（未resolved）
    pending_compile = 0
    pc = os.path.join(instincts_dir, "pending-compile.jsonl")
    if os.path.isfile(pc):
        try:
            with open(pc, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        if json.loads(line).get('status') == 'pending':
                            pending_compile += 1
                    except Exception:
                        pass
        except OSError:
            pass
    parts = []
    if pending_candidates > 0:
        parts.append(f"上次采集 {pending_candidates} 条候选")
    if pending_compile > 0:
        parts.append(f"{pending_compile} 条待编译")
    return '📊 ' + '，'.join(parts) if parts else ''


def wiki_health(snooze_file):
    """Wiki 健康检查（原 §6.6，调 wiki_checks.py --json）"""
    # 优先 plugin 自带 skill（v1.3.0 内建），fallback ~/.claude/skills/（旧装法/独立装）
    wiki_checks = os.path.join(paths.plugin_root(), "skills",
                               "wiki-management", "scripts", "wiki_checks.py")
    if not os.path.isfile(wiki_checks):
        wiki_checks = os.path.join(paths.home(), ".claude", "skills",
                                   "wiki-management", "scripts", "wiki_checks.py")
    if not os.path.isfile(wiki_checks):
        return ""
    # .alert-snooze 含 wiki-health 行 → 整段静音
    if os.path.isfile(snooze_file):
        with open(snooze_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip() == 'wiki-health':
                    return ""
    out = _run_quiet(["python3", wiki_checks, "--json"])
    if not out:
        return ""
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return ""
    fm_missing = len(d.get('frontmatter', {}).get('missing', []))
    fm_incomplete = len(d.get('frontmatter', {}).get('incomplete', []))
    dead = len(d.get('dead_links', {}).get('dead', []))
    stale = len(d.get('staleness_mismatches', []))
    sunset = len(d.get('sunset_candidates', []))
    total = d.get('total_pages', 0)
    issues = fm_missing + fm_incomplete + dead + stale + sunset
    if issues == 0:
        return ""
    is_red = fm_missing >= 10 or dead >= 5 or stale >= 10
    icon = '🔴' if is_red else '🟡'
    parts = []
    if fm_missing > 0: parts.append(f'{fm_missing} 缺元数据')
    if fm_incomplete > 0: parts.append(f'{fm_incomplete} 元数据不全')
    if dead > 0: parts.append(f'{dead} 断链')
    if stale > 0: parts.append(f'{stale} 过期')
    if sunset > 0: parts.append(f'{sunset} 待归档')
    detail = ' '.join(parts)
    detail_prefix = f"其中 {detail}，" if detail else ""
    if is_red:
        return f"{icon} 知识库 {total} 页 {issues} 项待整理，{detail_prefix}影响知识查找，需立即整理"
    return f"{icon} 知识库 {total} 页 {issues} 项待整理，{detail_prefix}有空处理"
