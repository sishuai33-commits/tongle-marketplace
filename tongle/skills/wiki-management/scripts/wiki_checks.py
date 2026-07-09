#!/usr/bin/env python3
"""
Wiki 健康检查 + 自动修复脚本
================================
检查模式: python3 wiki_checks.py         → 输出 JSON 报告
修复模式: python3 wiki_checks.py --fix   → 自动修 + 标出待 AI 判断项
JSON输出: python3 wiki_checks.py --json  → 纯 JSON，供 AI 消费

设计原则（ECC）:
- 脚本管"是什么"（事实层），AI 管"怎么办"（判断层）
- --fix 只做机械操作，不做语义判断
- 一个文件，持续生长
"""

import os, sys, json, re, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

# ── 配置 ───────────────────────────────────────────
WIKI_ROOT = os.path.expanduser(
    os.getenv("WIKI_VAULT_PATH", "~/Documents/Obsidian Vault/wiki")
)
TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)

# 衰减参数 fallback（与 .dream-config.md 保持一致，仅作兜底）
_FALLBACK = {
    "decay_lambda_time": 0.0116,
    "grace_days": 7,
    "base_weight": {
        "entity": 1.0, "concept": 0.9, "synthesis": 0.8,
        "event": 0.75, "source": 0.6, "scenario": 0.8,
        "project": 0.8, "procedure": 0.7, "note": 0.6,
        "journal": 0.8, "l2-narrative": 0.75, "tracker": 0.7,
    },
    "link_factor_threshold_5": 1.2,
    "link_factor_threshold_2": 1.0,
    "link_factor_threshold_1": 0.7,
    "link_factor_default": 0.5,
}
_DREAM_CONFIG = None  # 延迟加载

def _load_dream_config():
    """从 .dream-config.md frontmatter 读取衰减参数，失败时返回 fallback"""
    global _DREAM_CONFIG
    if _DREAM_CONFIG is not None:
        return _DREAM_CONFIG
    try:
        dream_fp = os.path.join(WIKI_ROOT, ".dream-config.md")
        fm, _, _ = read_fm(dream_fp)
        cfg = {}
        for key in _FALLBACK:
            if key in fm:
                cfg[key] = fm[key]
        if cfg:
            _DREAM_CONFIG = cfg
            return _DREAM_CONFIG
    except Exception:
        pass
    _DREAM_CONFIG = dict(_FALLBACK)
    return _DREAM_CONFIG

def _dc(key):
    """读取衰减配置项，优先 .dream-config.md，fallback 到硬编码"""
    return _load_dream_config().get(key, _FALLBACK.get(key))

REQUIRED_FIELDS = ["type", "created", "validated", "relevance_score",
                   "access_count", "last_access_date", "staleness"]

# ── 工具函数 ───────────────────────────────────────
def relpath(p):
    return os.path.relpath(p, WIKI_ROOT)

def parse_date(s):
    """解析 YAML 可能的各种日期格式"""
    if not s:
        return None
    s = str(s).strip().replace("T", " ").replace("Z", "")
    for fmt in ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%d %H:%M:%S+00:00", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return datetime.strptime(s[:len(fmt.replace('%f',''))], fmt)
        except:
            continue
    return None

def read_fm(filepath):
    """读取 frontmatter，返回 (dict, raw_yaml_str, body_start_pos)"""
    with open(filepath, "r") as f:
        content = f.read()
    if not content.startswith("---"):
        return {}, "", 0
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, "", 0
    raw = parts[1]
    try:
        import yaml
        d = yaml.safe_load(raw) or {}
    except:
        d = {}
    return d, raw, len(parts[0]) + len(parts[1]) + 6

def write_fm(filepath, updates):
    """向文件注入/更新 frontmatter 字段"""
    with open(filepath, "r") as f:
        content = f.read()

    if content.startswith("---"):
        parts = content.split("---", 2)
        body = parts[2].lstrip("\n") if len(parts) > 2 else ""
        try:
            import yaml
            existing = yaml.safe_load(parts[1]) or {}
        except:
            existing = {}
        existing.update(updates)
    else:
        existing = updates
        body = content.lstrip("\n")

    # 紧凑格式写入
    import yaml
    fm_str = yaml.dump(existing, default_flow_style=False,
                       allow_unicode=True, sort_keys=False, width=120)
    new_content = f"---\n{fm_str}---\n{body}"

    with open(filepath, "w") as f:
        f.write(new_content)

def extract_links(filepath):
    """提取文件中所有 [[wikilink]]，过滤目录引用和模板示例"""
    with open(filepath, "r") as f:
        content = f.read()
    # 预处理：移除转义的管道符 \|
    content = content.replace("\\|", "|")
    links = []
    for m in re.finditer(r'\[\[([^\]|#]+)(?:[|#][^\]]+)?\]\]', content):
        target = m.group(1).strip()
        # 跳过空链接、URL、目录引用（以/结尾）、模板示例
        if not target or target.startswith("http"):
            continue
        if target.endswith("/"):
            continue
        if target in ("A", "B", "对方页面路径", "页面路径"):
            continue
        links.append(target)
    return links

def link_factor(link_count):
    if link_count >= 5: return _dc("link_factor_threshold_5")
    if link_count >= 2: return _dc("link_factor_threshold_2")
    if link_count >= 1: return _dc("link_factor_threshold_1")
    return _dc("link_factor_default")

def resolve_link(source_path, target):
    """解析 wikilink 目标为绝对路径，或返回 None"""
    src_dir = os.path.dirname(source_path)
    # 去掉 .md 后缀如果没带
    if not target.endswith(".md"):
        t = target + ".md"
    else:
        t = target

    # 尝试相对路径解析
    candidates = [
        os.path.normpath(os.path.join(src_dir, t)),
        os.path.normpath(os.path.join(WIKI_ROOT, t)),
    ]

    # 也尝试在 projects/ 下搜索
    for root, dirs, files in os.walk(WIKI_ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        basename = os.path.basename(t)
        if basename in files:
            found = os.path.join(root, basename)
            if found not in candidates:
                candidates.append(found)
            break

    for c in candidates:
        if os.path.exists(c):
            return c
    return None

# ── 检查函数 ───────────────────────────────────────
def check_frontmatter():
    """检查所有页面 frontmatter 完整性"""
    results = {"missing": [], "incomplete": [], "ok": 0}
    for fp in scan_pages():
        fm, _, _ = read_fm(fp)
        rp = relpath(fp)
        if not fm or len(fm) <= 1:  # 只有 link_factor/link_count
            results["missing"].append(rp)
            continue
        missing = [f for f in REQUIRED_FIELDS if f not in fm or fm[f] is None]
        if missing:
            results["incomplete"].append({"page": rp, "missing": missing})
        else:
            results["ok"] += 1
    return results

def check_dead_links():
    """全量 wikilink 校验，仅检查内容页发的链接"""
    dead, valid = [], 0
    # 建全量页面索引（含配置文件，因为它们可能是链接目标）
    all_files = {}
    for root, dirs, files in os.walk(WIKI_ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                fp = os.path.join(root, f)
                rp = relpath(fp)
                # 多个别名
                all_files[rp] = fp
                all_files[rp.replace(".md", "")] = fp
                all_files[os.path.basename(rp)] = fp
                all_files[os.path.basename(rp).replace(".md", "")] = fp

    for fp in scan_pages(include_dotfiles=False):
        # 跳过配置页面发的链接（它们是给人看的说明，不是真实引用）
        if not is_content_page(fp):
            continue
        for link in extract_links(fp):
            if resolve_link(fp, link) is None:
                # 再尝试用全量索引
                target_md = link if link.endswith(".md") else link + ".md"
                basename = os.path.basename(target_md)
                if basename in all_files:
                    # 目标存在但路径不对 → 记录为可修复死链
                    dead.append({"from": relpath(fp), "to": link,
                                 "fixable": True,
                                 "found_at": relpath(all_files[basename])})
                else:
                    dead.append({"from": relpath(fp), "to": link,
                                 "fixable": False})
            else:
                valid += 1
    return {"dead": dead, "valid": valid}

def check_relevance():
    """按公式计算 relevance，对比当前值"""
    deviations = []
    for fp in scan_pages():
        fm, _, _ = read_fm(fp)
        if "type" not in fm or fm.get("pinned") == True:
            continue
        calculated = calc_relevance(fm, fp)
        current = fm.get("relevance_score", 0)
        if abs(calculated - current) > 0.1:
            deviations.append({
                "page": relpath(fp),
                "current": round(current, 2),
                "calculated": round(calculated, 2),
                "diff": round(calculated - current, 2)
            })
    return deviations

def check_staleness():
    """检查 staleness 标记是否与实际日期匹配"""
    mismatches = []
    for fp in scan_pages():
        fm, _, _ = read_fm(fp)
        validated = parse_date(fm.get("validated"))
        staleness = fm.get("staleness", "")
        if not validated:
            continue
        days = (NOW.replace(tzinfo=None) - validated.replace(tzinfo=None)).days
        if days <= 30 and staleness != "fresh":
            mismatches.append({"page": relpath(fp), "days_since": days,
                              "current": staleness, "should_be": "fresh"})
        elif 30 < days <= 60 and staleness not in ("stale", "stale"):
            mismatches.append({"page": relpath(fp), "days_since": days,
                              "current": staleness, "should_be": "stale"})
        elif days > 60 and staleness not in ("outdated", "sunset"):
            mismatches.append({"page": relpath(fp), "days_since": days,
                              "current": staleness, "should_be": "outdated"})
    return mismatches

def check_sunset():
    """日落候选扫描"""
    candidates = []
    for fp in scan_pages():
        fm, _, _ = read_fm(fp)
        if fm.get("pinned") == True or fm.get("archived") == True:
            continue
        ac = fm.get("access_count", 0)
        validated = parse_date(fm.get("validated"))
        lc = fm.get("link_count", 0)
        if not validated:
            continue
        days = (NOW.replace(tzinfo=None) - validated.replace(tzinfo=None)).days
        if ac == 0 and days > 60 and lc == 0:
            candidates.append({
                "page": relpath(fp),
                "access_count": ac,
                "days_since_validated": days,
                "link_count": lc
            })
    return candidates

# 非内容页（配置文件、索引等），不需要完整 frontmatter
NON_CONTENT_GLOBS = [".dream-config.md", ".dream-log.md", ".ai-vocab.md",
                      "index.md", "log.md"]
NON_CONTENT_DIRS = ["procedures", "_archived"]

def is_content_page(filepath):
    """判断是否为需要完整 frontmatter 的内容页"""
    rp = relpath(filepath)
    if os.path.basename(rp) in NON_CONTENT_GLOBS:
        return False
    for d in NON_CONTENT_DIRS:
        if rp.startswith(d + "/") or rp == d:
            return False
    return True

def scan_pages(include_dotfiles=False):
    """扫描所有 .md 文件。include_dotfiles=False 时排除配置文件。"""
    pages = []
    for root, dirs, files in os.walk(WIKI_ROOT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                pages.append(os.path.join(root, f))
    if not include_dotfiles:
        pages = [p for p in pages if is_content_page(p)]
    return pages

def calc_relevance(fm, filepath=None):
    """根据 dream-config 公式计算 relevance_score"""
    if fm.get("pinned") == True:
        return 1.0

    page_type = fm.get("type", "note")
    base = _dc("base_weight").get(page_type, 0.7)

    validated = parse_date(fm.get("validated"))
    if not validated and filepath:
        validated = datetime.fromtimestamp(os.path.getmtime(filepath), tz=TZ)
    elif not validated:
        validated = NOW - timedelta(days=90)

    created = parse_date(fm.get("created"))
    if created and (NOW.replace(tzinfo=None) - created.replace(tzinfo=None)).days <= _dc("grace_days"):
        return 1.0

    days = max(0, (NOW.replace(tzinfo=None) - validated.replace(tzinfo=None)).days)
    lc = fm.get("link_count", 0)
    lf = link_factor(lc)

    relevance = base * pow(2.71828, -_dc("decay_lambda_time") * days) * lf
    return round(min(1.0, max(0.05, relevance)), 2)

# ── 修复函数 ───────────────────────────────────────
def fix_frontmatter(results):
    """自动注入/补齐 frontmatter"""
    fixed = []
    for rp in results["missing"]:
        fp = os.path.join(WIKI_ROOT, rp)
        mtime = datetime.fromtimestamp(os.path.getmtime(fp), tz=TZ)
        updates = {
            "type": "note",
            "created": mtime.strftime("%Y-%m-%d"),
            "validated": mtime.strftime("%Y-%m-%d"),
            "relevance_score": 0.5,
            "access_count": 0,
            "last_access_date": NOW.strftime("%Y-%m-%d"),
            "staleness": "stale",
        }
        write_fm(fp, updates)
        fixed.append({"page": rp, "action": "injected_all"})

    for item in results["incomplete"]:
        fp = os.path.join(WIKI_ROOT, item["page"])
        fm, _, _ = read_fm(fp)
        updates = {}
        for field in item["missing"]:
            if field == "relevance_score":
                updates["relevance_score"] = calc_relevance(fm, fp)
            elif field == "access_count":
                updates["access_count"] = fm.get("access_count", 0)
            elif field == "last_access_date":
                updates["last_access_date"] = fm.get("last_access_date", NOW.strftime("%Y-%m-%d"))
            elif field == "staleness":
                updates["staleness"] = "stale"
            elif field == "validated":
                mtime = datetime.fromtimestamp(os.path.getmtime(fp), tz=TZ)
                updates["validated"] = mtime.strftime("%Y-%m-%d")
            elif field == "created":
                mtime = datetime.fromtimestamp(os.path.getmtime(fp), tz=TZ)
                updates["created"] = mtime.strftime("%Y-%m-%d")
            elif field == "type":
                updates["type"] = "note"
        if updates:
            write_fm(fp, updates)
            fixed.append({"page": item["page"], "action": "filled", "fields": list(updates.keys())})

    return fixed

def fix_dead_links(dead_links):
    """自动修复路径层级错误类死链"""
    fixed, needs_ai = [], []
    for item in dead_links["dead"]:
        src_fp = os.path.join(WIKI_ROOT, item["from"])
        target = item["to"]
        resolved = resolve_link(src_fp, target)
        if resolved:
            continue  # 实际上是有效的（可能是别名）

        # 尝试路径修正
        src_dir = os.path.dirname(os.path.join(WIKI_ROOT, item["from"]))
        # 常见错误: ../../entities/X → ../../../entities/X
        target_md = target if target.endswith(".md") else target + ".md"
        basename = os.path.basename(target_md)

        # 全量搜索目标文件
        found = None
        for root, dirs, files in os.walk(WIKI_ROOT):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if basename in files:
                found = os.path.join(root, basename)
                break

        if found:
            new_target = os.path.relpath(found, src_dir)
            new_target = new_target.replace(".md", "")
            # 修正文件中的链接
            with open(src_fp, "r") as f:
                content = f.read()
            old_link = f"[[{target}"
            new_link = f"[[{new_target}"
            if old_link in content:
                content = content.replace(old_link, new_link)
                with open(src_fp, "w") as f:
                    f.write(content)
                fixed.append({"from": item["from"], "to": target, "fixed_to": new_target})
            else:
                needs_ai.append({"from": item["from"], "to": target,
                                "found_at": relpath(found), "suggested": new_target,
                                "reason": "链接格式不完全匹配，需手动检查"})
        else:
            needs_ai.append({"from": item["from"], "to": target,
                            "reason": "目标页面不存在，需创建或移除引用"})

    return fixed, needs_ai

def fix_staleness(mismatches):
    """自动修正 staleness 标记"""
    fixed = []
    for item in mismatches:
        fp = os.path.join(WIKI_ROOT, item["page"])
        write_fm(fp, {"staleness": item["should_be"]})
        fixed.append(item)
    return fixed

def fix_relevance(deviations):
    """自动修正 relevance_score"""
    fixed = []
    for item in deviations:
        fp = os.path.join(WIKI_ROOT, item["page"])
        write_fm(fp, {"relevance_score": item["calculated"]})
        fixed.append(item)
    return fixed

def fix_sunset(candidates):
    """标记日落候选"""
    tagged, needs_ai = [], []
    for item in candidates:
        fp = os.path.join(WIKI_ROOT, item["page"])
        fm, _, _ = read_fm(fp)
        if fm.get("sunset_candidate") == True:
            # 检查缓冲是否到期
            buffer_until = fm.get("sunset_buffer_until", "")
            if buffer_until:
                buf_date = parse_date(buffer_until)
                if buf_date and buf_date.replace(tzinfo=None) <= NOW.replace(tzinfo=None):
                    write_fm(fp, {"archived": True, "staleness": "sunset",
                                  "relevance_score": 0.05})
                    tagged.append({"page": item["page"], "action": "archived"})
                else:
                    continue  # 仍在缓冲期
        else:
            # 新候选
            buffer = (NOW + timedelta(days=7)).strftime("%Y-%m-%d")
            write_fm(fp, {"sunset_candidate": True, "sunset_buffer_until": buffer,
                          "staleness": "sunset"})
            needs_ai.append({"page": item["page"], "action": "new_sunset_candidate",
                            "buffer_until": buffer})
    return tagged, needs_ai

# ── Alien Field 检测 ─────────────────────────────────

# 合法字段全集（按页面类型），用于检测非标字段
KNOWN_FIELDS_ALL = {
    "type", "created", "updated", "validated", "staleness", "confidence",
    "relevance_score", "access_count", "last_access_date", "pinned",
    "tags", "aliases", "category", "status", "project", "link_count",
    "link_factor",
    # 事件专属
    "event_date", "event_type",
    # 日落/归档
    "sunset_candidate", "sunset_buffer_until", "archived",
    # 已知的历史遗留（逐步消除）
    "date",  # 旧事件日期字段，已迁移到 event_date
    # 项目状态
    "status_note", "phase",
    # 元数据
    "source", "source_url",
}

def check_alien_fields():
    """检测所有内容页中不在 KNOWN_FIELDS_ALL 中的字段"""
    alien = []
    for fp in scan_pages():
        fm, _, _ = read_fm(fp)
        if not fm:
            continue
        unknowns = set(fm.keys()) - KNOWN_FIELDS_ALL
        if unknowns:
            alien.append({
                "page": relpath(fp),
                "alien_fields": sorted(unknowns),
                "page_type": fm.get("type", "unknown"),
            })
    return alien


def counts_selfcheck():
    """验证 .ai-vocab.md / index.md / daemon counts 三方计数自洽"""
    result = {"consistent": True, "discrepancies": []}

    # 运行 daemon counts
    daemon_path = os.path.join(WIKI_ROOT, ".wiki-daemon.py")
    try:
        output = subprocess.check_output(
            ["python3", daemon_path, "counts"], text=True, timeout=10)
        daemon_counts = json.loads(output)
    except Exception as e:
        return {"consistent": False, "discrepancies": [f"daemon counts 失败: {e}"], "daemon": None}

    # 读取 .ai-vocab.md 中的计数
    vocab_path = os.path.join(WIKI_ROOT, ".ai-vocab.md")
    vocab_counts = {}
    if os.path.exists(vocab_path):
        content = open(vocab_path).read()
        # 提取 "实体(X)" "概念(Y)" "项目(Z)" 等模式
        for pattern, key in [
            (r'实体[（(](\d+)[）)]', 'entities'),
            (r'全局概念[（(](\d+)[）)]', 'global_concepts'),
            (r'项目[（(](\d+)[）)]', 'projects'),
            (r'事件[（(](\d+)[）)]', 'events'),
            (r'场景[（(](\d+)[）)]', 'scenes'),
            (r'数据源[（(](\d+)[）)]', 'sources'),
        ]:
            m = re.search(pattern, content)
            if m:
                vocab_counts[key] = int(m.group(1))

    # 读取 index.md 中的计数
    index_path = os.path.join(WIKI_ROOT, "index.md")
    index_counts = {}
    if os.path.exists(index_path):
        content = open(index_path).read()
        for pattern, key in [
            (r'活跃项目[（(](\d+)[）)]', 'active_projects'),
            (r'全局概念[（(](\d+)[）)]', 'concepts'),
        ]:
            m = re.search(pattern, content)
            if m:
                index_counts[key] = int(m.group(1))

    # 交叉比对
    checks = [
        ("实体", "entities", daemon_counts.get("entities"), vocab_counts.get("entities")),
        ("全局概念", "global_concepts", daemon_counts.get("global_concepts"), vocab_counts.get("global_concepts")),
        ("项目数", "projects", len(daemon_counts.get("projects", {})), vocab_counts.get("projects")),
        ("事件文件", "events", daemon_counts.get("total_event_files"), vocab_counts.get("events")),
        ("场景", "scenes", daemon_counts.get("total_scene_files"), vocab_counts.get("scenes")),
        ("数据源", "sources", daemon_counts.get("sources"), vocab_counts.get("sources")),
        ("总md", "total_md", daemon_counts.get("total_md"), None),
    ]

    for name, key, daemon_val, vocab_val in checks:
        if vocab_val is not None and daemon_val is not None and daemon_val != vocab_val:
            result["discrepancies"].append({
                "item": name,
                "daemon": daemon_val,
                "vocab": vocab_val,
            })
            result["consistent"] = False

    result["daemon"] = daemon_counts
    result["vocab_counts"] = vocab_counts
    result["index_counts"] = index_counts
    return result


# ── 主流程 ─────────────────────────────────────────
def run_checks():
    """执行所有检查，返回结构化结果"""
    return {
        "timestamp": NOW.strftime("%Y-%m-%d %H:%M:%S"),
        "total_pages": len(scan_pages()),
        "frontmatter": check_frontmatter(),
        "dead_links": check_dead_links(),
        "relevance_deviations": check_relevance(),
        "staleness_mismatches": check_staleness(),
        "sunset_candidates": check_sunset(),
        "alien_fields": check_alien_fields(),
    }

def run_fix():
    """执行修复，返回修复结果 + 标出项"""
    report = run_checks()
    result = {
        "fixed": {},
        "needs_ai": [],
        "summary": ""
    }

    # Frontmatter
    if report["frontmatter"]["missing"] or report["frontmatter"]["incomplete"]:
        result["fixed"]["frontmatter"] = fix_frontmatter(report["frontmatter"])

    # Dead links
    if report["dead_links"]["dead"]:
        f, na = fix_dead_links(report["dead_links"])
        if f:
            result["fixed"]["dead_links"] = f
        result["needs_ai"].extend(na)

    # Staleness
    if report["staleness_mismatches"]:
        result["fixed"]["staleness"] = fix_staleness(report["staleness_mismatches"])

    # Relevance
    if report["relevance_deviations"]:
        result["fixed"]["relevance"] = fix_relevance(report["relevance_deviations"])

    # Sunset
    if report["sunset_candidates"]:
        tagged, na = fix_sunset(report["sunset_candidates"])
        if tagged:
            result["fixed"]["sunset"] = tagged
        result["needs_ai"].extend(na)

    # Alien fields — 全部标为 needs_ai（语义判断）
    if report.get("alien_fields"):
        result["alien_fields"] = report["alien_fields"]
        result["needs_ai"].extend([
            {"page": a["page"], "alien_fields": a["alien_fields"], "action": "review_alien_fields"}
            for a in report["alien_fields"]
        ])

    # Build summary
    total_fixed = sum(len(v) for v in result["fixed"].values())
    result["summary"] = (
        f"自动修复: {total_fixed} 项 | "
        f"待AI判断: {len(result['needs_ai'])} 项 | "
        f"总页面: {report['total_pages']}"
    )

    return result

# ── CLI ────────────────────────────────────────────
if __name__ == "__main__":
    # 独立模式: --alien 只检测非标字段
    if "--alien" in sys.argv:
        aliens = check_alien_fields()
        if "--json" in sys.argv:
            print(json.dumps({"alien_fields": aliens, "count": len(aliens)}, ensure_ascii=False, indent=2))
        else:
            print(f"\n  非标字段检测: {len(aliens)} 页有未知字段")
            for a in aliens[:20]:
                print(f"    {a['page']}: {', '.join(a['alien_fields'])}")
            if len(aliens) > 20:
                print(f"    ... 其余 {len(aliens)-20} 页")
        sys.exit(0)

    # 独立模式: --counts-selfcheck 只做计数自洽
    if "--counts-selfcheck" in sys.argv:
        result = counts_selfcheck()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    if "--fix" in sys.argv:
        result = run_fix()
        # 重新检查以验证修复
        verify = run_checks()
        result["post_fix_check"] = {
            "frontmatter_missing": len(verify["frontmatter"]["missing"]),
            "dead_links": len(verify["dead_links"]["dead"]),
            "staleness_mismatches": len(verify["staleness_mismatches"]),
            "relevance_deviations": len(verify["relevance_deviations"]),
        }

        if "--json" in sys.argv:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"\n{'='*60}")
            print(f"  Wiki 健康检查 + 自动修复")
            print(f"{'='*60}")
            print(f"\n  {result['summary']}")
            print(f"\n  修复详情:")
            for category, items in result["fixed"].items():
                print(f"    [{category}] {len(items)} 项")
                if category == "dead_links":
                    for item in items[:5]:
                        print(f"      ✓ {item['from']}")
                        print(f"        {item['to']} → {item['fixed_to']}")
                    if len(items) > 5:
                        print(f"      ... 其余 {len(items)-5} 项")
                elif category == "frontmatter":
                    for item in items[:5]:
                        print(f"      ✓ {item['page']} ({item['action']})")
                    if len(items) > 5:
                        print(f"      ... 其余 {len(items)-5} 项")
                elif category == "staleness":
                    for item in items[:5]:
                        print(f"      ✓ {item['page']} ({item['current']} → {item['should_be']})")
                    if len(items) > 5:
                        print(f"      ... 其余 {len(items)-5} 项")
                elif category == "relevance":
                    for item in items[:5]:
                        print(f"      ✓ {item['page']} ({item['current']} → {item['calculated']})")
                    if len(items) > 5:
                        print(f"      ... 其余 {len(items)-5} 项")

            if result["needs_ai"]:
                print(f"\n  ⚠️  待 AI 判断 ({len(result['needs_ai'])} 项):")
                for item in result["needs_ai"][:10]:
                    print(f"      ? {item.get('from', item.get('page', '?'))} — {item.get('reason', item.get('action', ''))}")
                if len(result["needs_ai"]) > 10:
                    print(f"      ... 其余 {len(result['needs_ai'])-10} 项")

            if result.get("post_fix_check"):
                print(f"\n  修复后验证:")
                for k, v in result["post_fix_check"].items():
                    icon = "✅" if v == 0 else "⚠️"
                    print(f"    {icon} {k}: {v}")

            print()
    else:
        report = run_checks()
        if "--json" in sys.argv:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"\n{'='*60}")
            print(f"  Wiki 健康检查")
            print(f"{'='*60}")
            print(f"\n  总页面: {report['total_pages']}")
            print(f"  Frontmatter: {report['frontmatter']['ok']} 完整, "
                  f"{len(report['frontmatter']['missing'])} 缺失, "
                  f"{len(report['frontmatter']['incomplete'])} 缺字段")
            print(f"  死链: {len(report['dead_links']['dead'])} 处")
            print(f"  Relevance 偏离: {len(report['relevance_deviations'])} 页")
            print(f"  Staleness 不匹配: {len(report['staleness_mismatches'])} 页")
            print(f"  日落候选: {len(report['sunset_candidates'])} 页")
            print()
