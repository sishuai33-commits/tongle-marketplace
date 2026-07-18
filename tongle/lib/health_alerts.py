"""lib/health_alerts.py — SessionStart 健康告警

提取自 lib/health.py（P1 模块瘦身）。原 health.py 混杂三类职责：维护门禁 +
深度体检 + SessionStart 告警。告警函数只被 session-start.py 使用，独立成模块。

依赖：lib/paths, lib/state, lib/utils
"""
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone, timedelta

from . import paths, state, utils

_PENDING_LABELS = {
    "file_change_candidate": "文件变更",
    "new_candidate": "外部搜索",
    "evolve_candidate": "演进",
    "transcript_candidate": "对话",
    "ima_candidate": "IMA",
}


WIKI_HEALTH_CACHE_SECS = 300  # wiki_health 缓存 TTL（5 分钟）：subprocess 扫描耗 ~850ms，短时重复无价值


def _wiki_health_cache_path():
    return os.path.join(paths.instincts_dir(), ".wiki-health-cache.json")


def _wiki_health_scan():
    """实际调 wiki_checks.py --json 扫描，返回结果字符串（空串=无问题/失败）"""
    wiki_checks = os.path.join(paths.plugin_root(), "skills",
                               "wiki-management", "scripts", "wiki_checks.py")
    if not os.path.isfile(wiki_checks):
        wiki_checks = os.path.join(paths.home(), ".claude", "skills",
                                   "wiki-management", "scripts", "wiki_checks.py")
    if not os.path.isfile(wiki_checks):
        return ""
    out = utils.run_quiet(["python3", wiki_checks, "--json"])
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


def _pending_breakdown(instincts_dir):
    """pending 候选按 pattern 分类统计，返回中文摘要（如 '文件变更40/外部搜索8'）。

    报警价值要求：光报计数无法判断值不值得看，带分类才知道构成
    （40 文件变更=噪音可忽略，8 外部搜索=值得裁决）。
    """
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


def dashboard(instincts_dir):
    """触发式仪表盘（待批阅≥50 + 经验值周 fail≥5 + 低采纳率，原 §6.5b）

    阈值 50（指挥官7/9反馈：≥5 太频繁打扰，拉到50且带分类才有价值）。
    低采纳率沿用 utils.low_adoption 自带阈值（14天窗口 avg<0.3），不另设阈值（守7/9不增打扰决策）。
    都没触发返回空串（安静，守 concise-output）。
    """
    pending, succ, fail = _dashboard_stats(instincts_dir)
    parts = []
    if pending >= 50:
        breakdown = _pending_breakdown(instincts_dir)
        parts.append(f'待批阅 {pending} 条（{breakdown}）' if breakdown else f'待批阅 {pending} 条')
    if fail >= 5:
        parts.append(f'本周经验值 {succ} 成 {fail} 败')
    low = utils.low_adoption(os.path.join(instincts_dir, "reuse-log.jsonl"))
    if low:
        parts.append(f'低采纳率（14天均值{low[0]:.2f}）')
    return '🟡 ' + '，'.join(parts) + '，抽空处理' if parts else ''


def alert_context(instincts_dir):
    """报警自解释（silent 注入，CC 后台自知，不进人可见对话）

    指挥官7/8反馈：仪表盘弹"待批阅 N 条"只有计数，新会话AI要查4轮才搞清对象。
    本函数返回报警的 silent 自解释，让 CC 知道"是什么/在哪/怎么处理"，不用查资料。
    人可见文案（dashboard()）不变，守 feedback-concise-output。
    覆盖：dashboard 3 类（待批阅/经验值/低采纳率）+ 维护门禁 marker（maintenance_check 6 维度超标）。
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
    low = utils.low_adoption(os.path.join(instincts_dir, "reuse-log.jsonl"))
    if low:
        parts.append(
            f"低采纳率（14天均值{low[0]:.2f}，{low[1]}样本）= reuse-log.jsonl 中 kind=adoption 记录的 rate 均值<0.3，"
            f"即注入的知识AI采纳率低（注入了没用）。"
            f"处理：检查低采纳注入是否该降级（走慢环或对话内请示调整注入策略）。"
            f"数据源 ~/.claude/instincts/reuse-log.jsonl"
        )

    # 维护门禁 marker 自解释（maintenance_check 6 维度超标，session-start 渲染🔴人可见文案）
    marker = os.path.join(instincts_dir, ".maintenance-due")
    if os.path.isfile(marker):
        try:
            with open(marker, encoding="utf-8", errors="replace") as f:
                m = json.load(f)
            issues = m.get("issues", "")
            parts.append(
                f"维护门禁🔴（{issues}）= maintenance_check 6 维度有超标项。"
                f"消除：修复超标项后下次会话启动自动清 marker（无需手动删）。"
                f"诊断 /ke-health，深度报告 /ke-health --json。"
                f"6 维度：主配置>180 行 / 记忆索引>140 行 / 慢环>7 天 / Wiki 目录散落 .md / 记忆文件缺 type / 低采纳率。"
                f"marker 在 ~/.claude/instincts/.maintenance-due"
            )
        except (OSError, json.JSONDecodeError):
            pass

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
    """Wiki 健康检查（原 §6.6，调 wiki_checks.py --json，带 5 分钟缓存）

    snooze 检查不缓存（每次都查，用户随时可能静音）；
    wiki_checks.py subprocess 扫描带缓存（耗 ~850ms，5 分钟内重复跳过）。
    """
    # .alert-snooze 含 wiki-health 行 -> 整段静音（不缓存，实时查）
    if os.path.isfile(snooze_file):
        with open(snooze_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip() == 'wiki-health':
                    return ""
    # 缓存检查：TTL 内直接返回缓存结果
    cache_path = _wiki_health_cache_path()
    try:
        with open(cache_path, encoding="utf-8", errors="replace") as f:
            c = json.load(f)
        if (time.time() - c.get("ts", 0)) < WIKI_HEALTH_CACHE_SECS:
            return c.get("result", "")
    except (OSError, json.JSONDecodeError):
        pass
    # 实际扫描
    result = _wiki_health_scan()
    # 写缓存（fail-open，写失败不影响结果）
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "result": result}, f)
    except OSError:
        pass
    return result
