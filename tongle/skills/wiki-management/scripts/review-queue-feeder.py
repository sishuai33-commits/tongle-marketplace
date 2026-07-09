#!/usr/bin/env python3
"""
review-queue-feeder.py — 从 wiki_checks.py 输出提取 needs_ai 项，分类后输出结构化 JSON
供 Agent 用 MCP patch_note 追加到 review-queue.md 对应 section。

用法:
  python3 review-queue-feeder.py [--source /tmp/wiki_checks_result.json]
  输出 JSON 到 stdout: { buckets: {...}, items: [...], summary: "..." }
"""

import json
import sys
import os
from collections import defaultdict

SOURCE = "/tmp/wiki_checks_result.json"
if "--source" in sys.argv:
    idx = sys.argv.index("--source")
    SOURCE = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else SOURCE


def load_checks_result(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def classify_item(item: dict | str) -> str:
    """将 needs_ai 项分类到 bucket: dead_link / outdated / research / structure / other"""
    if isinstance(item, str):
        s = item.lower()
    else:
        s = json.dumps(item, ensure_ascii=False).lower()

    if any(k in s for k in ("dead", "死链", "broken link", "[[", "wikilink")):
        return "dead_link"
    if any(k in s for k in ("outdated", "过时", "stale", "sunset", "日落")):
        return "outdated"
    if any(k in s for k in ("research", "待研究", "待确认", "verify", "核实")):
        return "research"
    if any(k in s for k in ("structure", "归属", "alien", "目录", "frontmatter")):
        return "structure"
    return "other"


def main():
    data = load_checks_result(SOURCE)
    needs_ai = data.get("needs_ai", [])
    fixed = data.get("fixed", {})

    if not needs_ai:
        print(json.dumps({"buckets": {}, "items": [], "summary": "无 needs_ai 项"}, ensure_ascii=False))
        return

    # 按类型分桶 + 去重
    buckets = defaultdict(list)
    seen = set()

    for item in needs_ai:
        bucket = classify_item(item)
        # 去重 key: 取字符串表示的前 120 字符
        key = (bucket, str(item)[:120])
        if key in seen:
            continue
        seen.add(key)
        buckets[bucket].append(item)

    # 构建输出
    items = []
    for bucket, entries in sorted(buckets.items()):
        for entry in entries:
            items.append({
                "bucket": bucket,
                "content": str(entry)[:500],
            })

    summary_parts = []
    bucket_names = {"dead_link": "断链", "outdated": "过时内容", "research": "待研究", "structure": "结构问题", "other": "其他"}
    for bucket in ["dead_link", "outdated", "research", "structure", "other"]:
        count = len(buckets.get(bucket, []))
        if count > 0:
            summary_parts.append(f"{bucket_names.get(bucket, bucket)}: {count}条")

    result = {
        "buckets": {k: len(v) for k, v in sorted(buckets.items())},
        "items": items,
        "summary": "；".join(summary_parts) if summary_parts else "无",
        "total": len(items),
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
