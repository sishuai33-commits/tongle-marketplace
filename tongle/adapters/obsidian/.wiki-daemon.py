#!/usr/bin/env python3
"""wiki-daemon: Wiki 确定性操作引擎

所有不需要 LLM 判断的 Wiki 维护操作收在这里。
Hooks 调用子命令，LLM 通过 `counts` / `status` 消费结构化数据。

Usage:
  python3 .wiki-daemon.py status          # 状态快照 JSON → stdout
  python3 .wiki-daemon.py counts           # 自动统计 → JSON
  python3 .wiki-daemon.py session-inc      # .dream-count += 1
  python3 .wiki-daemon.py access-update    # 批量更新 access_count（消费 .pending）
  python3 .wiki-daemon.py pending-add PATH # 追加 PATH 到 .pending_access_updates
  python3 .wiki-daemon.py verify           # 编译 checklist 自动验证
  python3 .wiki-daemon.py orphan-list      # 孤儿页面 + 90天无访问候选
  python3 .wiki-daemon.py lock-acquire     # 获取编译互斥锁
  python3 .wiki-daemon.py lock-release     # 释放编译互斥锁
  python3 .wiki-daemon.py preflight [--fix-stray] [--fix-empty]  # 编译前置修复
  python3 .wiki-daemon.py compile-finalize [--dream]  # 编译收尾
"""

import os, sys, json
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

# VAULT 路径 env 化（P1.2）：解耦对 Obsidian Vault 路径的硬编，可被 WIKI_VAULT_PATH 环境变量驱动。
# daemon 代码位置已迁入项目 adapters/obsidian/（机制层），但操作的数据仍在 Vault（资产层），
# 通过 env 指向数据，实现"代码与数据位置解耦"。默认值=当前绝对路径（自用场景无害，对外发布待远期推导）。
VAULT = Path(os.environ.get("WIKI_VAULT_PATH", os.path.expanduser("~/Documents/Obsidian Vault")))
WIKI = VAULT / "wiki"
NOW = datetime.now()
TODAY = NOW.strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════
# Frontmatter helpers
# ══════════════════════════════════════════════════

def read_fm(path):
    """读取 .md 文件 YAML frontmatter, 返回 dict; 读取失败返回 {}"""
    if not path.exists():
        return {}
    content = path.read_text()
    if not content.startswith("---"):
        return {}
    end = content.find("---", 4)
    if end == -1:
        return {}
    try:
        if yaml:
            return yaml.safe_load(content[4:end]) or {}
        return {}
    except:
        return {}

def write_fm(path, updates, merge=True):
    """更新 frontmatter 字段。merge=True 合并, merge=False 覆盖"""
    if not path.exists() or yaml is None:
        return False
    content = path.read_text()
    if not content.startswith("---"):
        return False
    end = content.find("---", 4)
    if end == -1:
        return False
    fm = yaml.safe_load(content[4:end]) or {}
    if merge:
        fm.update(updates)
    else:
        fm = updates
    new_fm = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False).strip()
    path.write_text(f"---\n{new_fm}\n---{content[end+3:]}")
    return True


# ══════════════════════════════════════════════════
# Subcommands
# ══════════════════════════════════════════════════

def cmd_status():
    """完整状态快照 → JSON stdout（Hook 打印 + LLM 消费）"""
    report = {
        "timestamp": NOW.isoformat(),
        "compilation": {},
        "dream": {},
        "counts": {},
        "pending_access_updates": [],
        "alerts": [],
    }

    # ── 编译状态 ──
    comp_file = WIKI / ".last_compilation"
    if comp_file.exists():
        try:
            last = datetime.fromisoformat(comp_file.read_text().strip())
            days = (NOW - last).days
            report["compilation"] = {"last": last.isoformat(), "days_ago": days}
            if days >= 7:
                report["alerts"].append(f"Wiki {days}天未编译，建议本次会话执行编译")
        except:
            pass

    # ── Dream 闸门 ──
    dream_file = WIKI / ".last_dream"
    count_file = WIKI / ".dream-count"
    if dream_file.exists():
        try:
            last = datetime.fromisoformat(dream_file.read_text().strip())
            hours = (NOW - last).total_seconds() / 3600
            count = int(count_file.read_text().strip()) if count_file.exists() else 0
            gates_ok = hours >= 24 and count >= 3
            report["dream"] = {
                "last": last.isoformat(), "hours_ago": round(hours, 1),
                "session_count": count, "gates_ok": gates_ok
            }
            if gates_ok:
                report["alerts"].append("Dream 闸门已通过，可执行")
            elif hours >= 24:
                report["alerts"].append(f"Dream 时间闸门通过, 会话闸门 {count}/3")
        except:
            pass

    # ── access_count 待更新 ──
    pending = WIKI / ".pending_access_updates"
    if pending.exists():
        lines = [l.strip() for l in pending.read_text().splitlines() if l.strip()]
        report["pending_access_updates"] = lines
        if lines:
            report["alerts"].append(f"{len(lines)}个页面 access_count 待更新 (上次会话读取)")

    # ── 自动计数 ──
    report["counts"] = _do_counts()
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_counts():
    """自动统计 → JSON stdout"""
    print(json.dumps(_do_counts(), ensure_ascii=False, indent=2))


def _do_counts():
    """遍历 wiki 目录，精确统计各类页面"""
    c = {
        "entities": _count_dir(WIKI / "entities"),
        "global_concepts": _count_dir(WIKI / "concepts"),
        "sources": _count_dir(WIKI / "sources"),
        "procedures": _count_dir(WIKI / "procedures"),
        "total_event_files": 0,
        "total_scene_files": 0,
        "project_concepts": 0,
        "projects": {},
        "total_md": 0,
        "orphans": 0,
        "lightweight": [],
    }

    proj_root = WIKI / "projects"
    if proj_root.exists():
        for d in sorted(proj_root.iterdir()):
            if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
                continue
            name = d.name
            md_files = list(d.glob("*.md"))
            event_files = list((d / "events").glob("*.md")) if (d / "events").exists() else []
            concept_files = list((d / "concepts").glob("*.md")) if (d / "concepts").exists() else []
            scene_files = list((d / "scenarios").glob("*.md")) if (d / "scenarios").exists() else []
            tl_files = list((d / "timeline").glob("*.md")) if (d / "timeline").exists() else []
            total = len(md_files) + len(event_files) + len(concept_files) + len(scene_files) + len(tl_files)
            c["projects"][name] = {
                "synthesis": len(md_files),
                "events": len(event_files),
                "concepts": len(concept_files),
                "scenes": len(scene_files),
                "timeline": len(tl_files),
                "total": total,
            }
            c["total_event_files"] += len(event_files) + len(tl_files)
            c["total_scene_files"] += len(scene_files)
            c["project_concepts"] += len(concept_files)
            if total <= 3:
                c["lightweight"].append(name)

    # 全站 .md 总数（排除 dotfile 和 _archived）
    all_md = [f for f in WIKI.rglob("*.md")
              if not any(p.startswith(".") or p.startswith("_") for p in f.parts)
              and "_archived" not in str(f)]
    c["total_md"] = len(all_md)

    # orphan 检测
    for f in all_md:
        fm = read_fm(f)
        if fm.get("link_count", 1) == 0:
            c["orphans"] += 1

    return c


def cmd_session_inc():
    """.dream-count += 1"""
    count_file = WIKI / ".dream-count"
    count = int(count_file.read_text().strip()) if count_file.exists() else 0
    count_file.write_text(str(count + 1))
    return count + 1


def cmd_access_update():
    """批量更新 access_count（消费 .pending_access_updates）"""
    pending = WIKI / ".pending_access_updates"
    if not pending.exists():
        print(json.dumps({"updated": 0, "message": "无待更新页面"}))
        return

    paths = set(l.strip() for l in pending.read_text().splitlines() if l.strip())
    updated, skipped = 0, 0

    for rel in paths:
        full = WIKI / rel
        if not full.exists():
            continue
        fm = read_fm(full)
        if fm.get("pinned") == True:
            skipped += 1
            continue
        count = fm.get("access_count", 0)
        # 文本级更新：只改 access_count/last_access_date 两行，保留原 frontmatter 格式(空行/字段顺序)
        content = full.read_text()
        if content.startswith("---"):
            end = content.find("---", 4)
            if end != -1:
                lines = content[4:end].split('\n')
                out, seen_ac, seen_la = [], False, False
                for ln in lines:
                    if ln.startswith('access_count:'):
                        out.append(f'access_count: {count + 1}'); seen_ac = True
                    elif ln.startswith('last_access_date:'):
                        out.append(f"last_access_date: '{TODAY}'"); seen_la = True
                    else:
                        out.append(ln)
                if not seen_ac: out.insert(0, f'access_count: {count + 1}')
                if not seen_la: out.insert(0, f"last_access_date: '{TODAY}'")
                full.write_text('---\n' + '\n'.join(out) + '---' + content[end+3:])
        updated += 1

    pending.unlink()
    result = {"updated": updated, "skipped": skipped, "total_pending": len(paths)}
    print(json.dumps(result, ensure_ascii=False))
    return result


def cmd_pending_add(path):
    """追加一个被读取的 wiki 页面路径到 .pending"""
    if not path or path.startswith("wiki/."):
        return
    # 去掉 MCP 传入的 wiki/ 前缀，统一为相对路径
    if path.startswith("wiki/"):
        path = path[5:]
    pending = WIKI / ".pending_access_updates"
    with open(pending, "a") as f:
        f.write(f"{path}\n")


def cmd_verify():
    """编译 checklist 自动验证 → 逐行输出"""
    today = TODAY
    all_pass = True

    def check(item, passed, detail=""):
        nonlocal all_pass
        mark = "✓" if passed else "✗"
        if not passed:
            all_pass = False
        print(f"  [{mark}] {item}")
        if detail and not passed:
            print(f"      {detail}")

    # 1. log.md 今天有更新
    log_content = (WIKI / "log.md").read_text()
    check("log.md 已追加本次条目", f"[{today}]" in log_content)

    # 2. .ai-vocab 生成时间
    vocab = (WIKI / ".ai-vocab.md").read_text()
    check(".ai-vocab.md 生成时间已更新", f"生成时间：{today}" in vocab,
          "日期不匹配，需手动更新 .ai-vocab.md 生成时间")

    # 3. index.md 最近更新
    idx = (WIKI / "index.md").read_text()
    check("index.md 最近更新已追加", f"| {today}" in idx)

    # 4. .last_compilation
    comp = WIKI / ".last_compilation"
    if comp.exists():
        last = comp.read_text().strip()
        check(".last_compilation 已更新", today in last)
    else:
        check(".last_compilation 已更新", False, "文件不存在")

    # 5. 无散落增量文件
    stray = []
    for pattern in ["vocab-increment-*", "log-2*"]:
        for f in VAULT.rglob(pattern):
            if f != WIKI / "log.md" and "_archived" not in str(f):
                stray.append(str(f.relative_to(VAULT)))
    check("无散落增量文件", len(stray) == 0,
          f"发现: {stray}" if stray else "")

    # 摘要
    print(f"\n{'✅ 全部通过' if all_pass else '❌ 存在未完成项，请检查'}")
    return all_pass


def cmd_orphan_list():
    """列出 link_count=0 + access_count=0 的页面"""
    candidates = []
    for f in sorted(WIKI.rglob("*.md")):
        if f.name.startswith(".") or "_archived" in str(f):
            continue
        fm = read_fm(f)
        lc = fm.get("link_count", -1)
        ac = fm.get("access_count", -1)
        if lc == 0:
            rel = str(f.relative_to(WIKI))
            candidates.append({
                "path": rel,
                "access_count": ac,
                "staleness": fm.get("staleness", "?"),
            })
    print(json.dumps(candidates, ensure_ascii=False, indent=2))


def cmd_lock_acquire():
    """获取编译互斥锁。成功返回 ok，已被锁返回 blocked"""
    lock_file = WIKI / ".dream-lock"
    if lock_file.exists():
        try:
            ts = lock_file.read_text().strip()
            print(json.dumps({"ok": False, "reason": f"已被锁 (since {ts})"}))
            return False
        except:
            pass
    lock_file.write_text(NOW.isoformat())
    print(json.dumps({"ok": True, "acquired_at": NOW.isoformat()}))
    return True


def cmd_lock_release():
    """释放编译互斥锁"""
    lock_file = WIKI / ".dream-lock"
    if lock_file.exists():
        lock_file.unlink()
        print(json.dumps({"ok": True, "released_at": NOW.isoformat()}))
    else:
        print(json.dumps({"ok": True, "message": "锁不存在，无需释放"}))


def cmd_preflight():
    """编译前置修复: --fix-stray 清理散落增量文件, --fix-empty 补齐空 frontmatter"""
    fix_stray = "--fix-stray" in sys.argv
    fix_empty = "--fix-empty" in sys.argv
    results = {"stray_cleaned": [], "empty_fm_fixed": [], "errors": []}

    # 清理散落增量文件
    if fix_stray:
        for pattern in ["vocab-increment-*", "log-2*"]:
            for f in WIKI.rglob(pattern):
                if "_archived" not in str(f):
                    rel = str(f.relative_to(WIKI))
                    try:
                        f.unlink()
                        results["stray_cleaned"].append(rel)
                    except Exception as e:
                        results["errors"].append(str(e))

    # 补齐空 frontmatter
    if fix_empty:
        for f in sorted(WIKI.rglob("*.md")):
            if f.name.startswith(".") or "_archived" in str(f):
                continue
            content = f.read_text()
            if content.startswith("---"):
                continue  # 已有 frontmatter
            # 推断 type
            rel = str(f.relative_to(WIKI))
            if "/entities/" in rel:
                inferred_type = "entity"
            elif "/concepts/" in rel:
                inferred_type = "concept"
            elif "/events/" in rel:
                inferred_type = "event"
            elif "/scenarios/" in rel:
                inferred_type = "scenario"
            elif "/sources/" in rel:
                inferred_type = "source"
            elif "/procedures/" in rel:
                inferred_type = "procedure"
            else:
                inferred_type = "note"
            fm_block = (
                f"---\n"
                f"type: {inferred_type}\n"
                f"created: {TODAY}\n"
                f"validated: {TODAY}\n"
                f"relevance_score: 1.0\n"
                f"staleness: fresh\n"
                f"access_count: 0\n"
                f"last_access_date: {TODAY}\n"
                f"---\n\n"
            )
            f.write_text(fm_block + content)
            results["empty_fm_fixed"].append(rel)

    print(json.dumps(results, ensure_ascii=False, indent=2))


def cmd_compile_finalize():
    """编译收尾: 写 .last_compilation + .ai-vocab.md 时间戳自动更新 + 可选 .last_dream"""
    comp_file = WIKI / ".last_compilation"
    comp_file.write_text(NOW.isoformat())
    result = {"last_compilation": NOW.isoformat()}

    # 治本(2026-07-04): 自动更新 .ai-vocab.md 生成时间，不依赖 LLM 手动写
    # 根因: wiki-full-compile.js L480 "生成时间: 今天" 是 prompt 指令靠 LLM 自觉，
    # LLM 可能忘写/写错/复制旧值 → 时间戳过时。这里强制覆盖。
    vocab_file = WIKI / ".ai-vocab.md"
    if vocab_file.exists():
        lines = vocab_file.read_text().split('\n')
        for i, line in enumerate(lines):
            if line.startswith('> 生成时间：'):
                lines[i] = f'> 生成时间：{TODAY}（编译自动生成）'
                break
        new_vocab = '\n'.join(lines)
        if new_vocab != vocab_file.read_text():
            vocab_file.write_text(new_vocab)
            result["ai_vocab_timestamp"] = TODAY

    dream_flag = "--dream" in sys.argv
    if dream_flag:
        dream_file = WIKI / ".last_dream"
        dream_file.write_text(NOW.isoformat())
        # 重置 dream-count
        count_file = WIKI / ".dream-count"
        count_file.write_text("0")
        result["last_dream"] = NOW.isoformat()
        result["dream_count_reset"] = True

    print(json.dumps(result, ensure_ascii=False, indent=2))


def _count_dir(path):
    """统计目录下 .md 文件数"""
    return len(list(path.glob("*.md"))) if path.exists() else 0


# ══════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════

COMMANDS = {
    "status": cmd_status,
    "counts": cmd_counts,
    "session-inc": cmd_session_inc,
    "access-update": cmd_access_update,
    "pending-add": lambda: cmd_pending_add(sys.argv[2]) if len(sys.argv) > 2 else None,
    "verify": cmd_verify,
    "orphan-list": cmd_orphan_list,
    "lock-acquire": cmd_lock_acquire,
    "lock-release": cmd_lock_release,
    "preflight": cmd_preflight,
    "compile-finalize": cmd_compile_finalize,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd in COMMANDS:
        COMMANDS[cmd]()
    else:
        print(f"Unknown: {cmd}. Available: {list(COMMANDS.keys())}")
        sys.exit(1)
