"""加工环·规整（M1 减熵，快频）

提取自 refine-raw.py（Step 3 内核提取2）。
IO 经 lib/state，路径经 lib/paths。

对原料库做减熵规整：去重检测 + 冲突裁决标记 + 死链扫描
- 输入：pending-queue.jsonl + discriminate-experience.jsonl + wiki markdown 链接
- 输出：.refine-last-run（运行时间）+ stdout 报告（不删数据，只检测报告）
- 原则：规整不删原始数据（守可回溯），只标记冲突/重复/死链供人确认

注：WIKI_VAULT_PATH 语义=wiki 根（含 /wiki 后缀，与 build-asset-manifest 不同），
env 优先，否则 Vault/wiki（保留原 refine-raw 语义，非 paths.wiki_vault 抽象）。
"""
import glob
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

from . import state, paths


def main():
    """规整入口：去重+冲突+死链检测 → .refine-last-run + stdout 报告"""
    pending = state.read_instincts_jsonl("pending-queue.jsonl")
    experience = state.read_instincts_jsonl("discriminate-experience.jsonl")
    now = datetime.now(timezone.utc).isoformat()
    print(f"# 规整输入: pending={len(pending)} experience={len(experience)}")

    # ============================================
    # 1. 重复检测：pending-queue 同 session+pattern+evidence 重复候选
    # ============================================
    seen = {}
    duplicates = []
    for i, r in enumerate(pending):
        ev = r.get("evidence", {}) if isinstance(r.get("evidence"), dict) else {}
        key = (r.get("session", ""), r.get("pattern", ""),
               ev.get("read_project", "") or ev.get("query", "")[:40] or ev.get("path", "")[:40] or ev.get("action_type", ""))
        if key in seen:
            duplicates.append((seen[key], i, str(key)[:80]))
        else:
            seen[key] = i
    print(f"\n# 1.重复检测: {len(duplicates)}条疑似重复(同session+pattern+evidence)")
    for a, b, k in duplicates[:5]:
        print(f"    行{a} ≈ 行{b}: {k}")

    # ============================================
    # 2. 冲突检测：experience 同主题(同read_project+pattern) 不同 disposition
    # ============================================
    topic_groups = defaultdict(list)
    for r in experience:
        ev = r.get("evidence", {}) if isinstance(r.get("evidence"), dict) else {}
        topic = (ev.get("read_project", "") or ev.get("query", "")[:30] or "?", r.get("pattern", ""))
        topic_groups[topic].append(r)

    conflicts = []
    for topic, recs in topic_groups.items():
        dispositions = set(r.get("disposition", "") for r in recs)
        if len(dispositions) > 1 and len(recs) >= 2:
            conflicts.append((topic, dispositions, len(recs), recs))
    print(f"\n# 2.冲突检测: {len(conflicts)}组同主题不同disposition(判别不一致)")
    for topic, disps, n, recs in conflicts[:5]:
        print(f"    {topic[0]}/{topic[1]} ({n}条) dispositions={disps}")
        for r in recs[:2]:
            print(f"      - {r.get('disposition')}: {(r.get('note', '') or r.get('reason', ''))[:60]}")

    # ============================================
    # 3. 死链扫描：扫 wiki markdown 链接存在性（B2 修复，最小实现）
    #    跳过外链(http/https/mailto)与锚点；wikilink [[x]] 归 wiki_checks.py 分工
    # ============================================
    # WIKI_VAULT_PATH 语义=wiki 根（含 /wiki 后缀），env 优先，否则 Vault/wiki
    wiki_root = os.environ.get("WIKI_VAULT_PATH",
                               os.path.join(paths.wiki_vault(), "wiki"))
    deadlinks = []
    link_re = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
    if os.path.isdir(wiki_root):
        for md in glob.glob(os.path.join(wiki_root, "**/*.md"), recursive=True):
            try:
                with open(md, encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            for m in link_re.finditer(content):
                link = m.group(2).split("#")[0].strip()
                if not link or link.startswith(("http://", "https://", "mailto:")):
                    continue
                target = os.path.normpath(os.path.join(os.path.dirname(md), link))
                if not os.path.exists(target):
                    deadlinks.append((os.path.relpath(md, wiki_root), link))
        print(f"\n# 3.死链扫描: {len(deadlinks)}条死链(wiki markdown链接)")
        for src, link in deadlinks[:10]:
            print(f"    {src} → {link}")
    else:
        print(f"\n# 3.死链扫描: 跳过(wiki目录不存在: {wiki_root})")

    # ============================================
    # 4. 写 .refine-last-run + 报告
    # ============================================
    report = {
        "ts": now,
        "pending_total": len(pending),
        "experience_total": len(experience),
        "duplicates_found": len(duplicates),
        "conflicts_found": len(conflicts),
        "deadlinks_found": len(deadlinks),
    }
    state.write_json(paths.instincts_file(".refine-last-run"), report)

    print(f"\n# ✅ 规整完成 @ {now[:19]}")
    print(f"# 报告: 重复={len(duplicates)} 冲突={len(conflicts)} 死链={len(deadlinks)} (规整不删数据只报告)")
    print(f"# .refine-last-run 已写入(供runtime-health-check检测加工环运行态)")
    print(f"# 注：规整不删数据只报告，守可回溯；冲突/重复/死链供人确认后处置")
    sys.exit(0)
