#!/usr/bin/env python3
"""
discriminate-resolve.py — L2 判别候选人裁决入口（棘轮只升）

架构范式 §四采集环 达尔文同构"人裁决→棘轮只升→更新判别经验库" +
§三种子范式闭环连接点1（判别经验库↔采集判别）：
  人（指挥官）逐条裁决判别候选 → 标 pending→resolved + 判别经验 append。
  判别经验回流指导下次采集判别（⑤最小闭环：collector load_discard_keys 读本文件 discard 经验做去重）。
  注：⑤回流最小形态=已 discard 不重复采（2026-06-29 对抗审查收尾落地），
  L3 完善（调权重/泛化模式/主题聚合层）见 STATE.md §Level2。

守红线（architecture-paradigm §九）：
  ⑤ 人确认环不可省——本脚本是人裁决的执行入口，不自动裁决。
     调用本脚本=人已裁决（CLI 即人裁决行为），脚本只记录不判断。
  ⑦ 记判别模式——关系类型/处置是人裁决填的（人看对话上下文判，非脚本判）。
     脚本只忠实记录人填的值，不做推断。
  ⑧ 只收自己裁决——本脚本只收本会话指挥官裁决，不吸收他人/外源裁决结果。
     外源判别经验走采集环判别重新结构化（见 §六外源吸收定律）。
  ② 棘轮只升——discriminate-experience.jsonl 只 append 不删（达尔文棘轮"只增不丢"）。
     pending 条目改 resolved（不改判别经验历史）。

用法：
  python3 hooks/discriminate-resolve.py <index> <relation_type> <disposition> [note]
    index: pending-queue.jsonl 中 pending 候选的行号（1-based，从 pending 列表算）
    relation_type: new|evolve|complement|conflict（关系类型，人判）
    disposition: adopt|discard|isolate（处置：采纳/丢弃/隔离，人判）
    note: 可选，人裁备注

输出：
  - pending-queue.jsonl：对应条目 status 改 resolved（带 resolved_ts/relation/disposition）
  - discriminate-experience.jsonl：append 判别经验（棘轮只升，含原 pattern+evidence+人裁决）
  - 更新 .discriminate-due marker（pending 数变化）

关联：architecture-paradigm §四采集环 / §三种子范式 / §五原则4 /
      memory ke-architecture-paradigm / [[knowledge-engine-project]]
"""
import sys
import json
import os
from datetime import datetime, timezone

_home = os.path.expanduser("~")
INSTINCTS_DIR = os.path.join(_home, ".claude", "instincts")
PENDING_QUEUE = os.path.join(INSTINCTS_DIR, "pending-queue.jsonl")
EXPERIENCE = os.path.join(INSTINCTS_DIR, "discriminate-experience.jsonl")
MARKER = os.path.join(INSTINCTS_DIR, ".discriminate-due")

RELATION_TYPES = {"new", "evolve", "complement", "conflict"}
DISPOSITIONS = {"adopt", "discard", "isolate"}


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_pending():
    """读 pending-queue，返回 (lines_raw, entries_parsed)"""
    if not os.path.exists(PENDING_QUEUE):
        return [], []
    raws, parsed = [], []
    try:
        for ln in open(PENDING_QUEUE, encoding="utf-8"):
            ln = ln.rstrip("\n")
            raws.append(ln)
            try:
                parsed.append(json.loads(ln))
            except Exception:
                parsed.append(None)
    except OSError:
        pass
    return raws, parsed


def write_pending(raws, parsed):
    """回写 pending-queue（resolved 标记更新）"""
    tmp = PENDING_QUEUE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for raw, d in zip(raws, parsed):
                f.write(json.dumps(d, ensure_ascii=False) + "\n" if d else raw + "\n")
        os.replace(tmp, PENDING_QUEUE)
    except OSError as e:
        print(f"[discriminate-resolve] 写 pending 失败: {e}", file=sys.stderr)
        sys.exit(1)


def append_experience(entry):
    """棘轮只升：append 到 discriminate-experience.jsonl，不删历史。"""
    try:
        with open(EXPERIENCE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[discriminate-resolve] 写判别经验失败: {e}", file=sys.stderr)
        sys.exit(1)


def update_marker():
    """pending 数变化后更新 .discriminate-due marker（复用采集器逻辑）"""
    pending_count = 0
    if os.path.exists(PENDING_QUEUE):
        try:
            for ln in open(PENDING_QUEUE, encoding="utf-8"):
                try:
                    if json.loads(ln).get("status") == "pending":
                        pending_count += 1
                except Exception:
                    continue
        except OSError:
            pass
    try:
        if pending_count >= 3:
            with open(MARKER, "w", encoding="utf-8") as f:
                json.dump({
                    "triggered": True,
                    "pending_count": pending_count,
                    "threshold": 3,
                    "timestamp": now_utc_iso(),
                    "hint": f"判别候选 {pending_count} 条待裁决",
                }, f, ensure_ascii=False)
        else:
            if os.path.exists(MARKER):
                os.remove(MARKER)
    except OSError:
        pass


def main():
    if len(sys.argv) < 4:
        print("用法: discriminate-resolve.py <index> <relation_type> <disposition> [note]", file=sys.stderr)
        print("  relation_type: new|evolve|complement|conflict", file=sys.stderr)
        print("  disposition: adopt|discard|isolate", file=sys.stderr)
        sys.exit(2)

    try:
        idx = int(sys.argv[1])
    except ValueError:
        print(f"[discriminate-resolve] index 非数字: {sys.argv[1]}", file=sys.stderr)
        sys.exit(2)
    relation = sys.argv[2].strip().lower()
    disposition = sys.argv[3].strip().lower()
    note = sys.argv[4] if len(sys.argv) > 4 else ""

    if relation not in RELATION_TYPES:
        print(f"[discriminate-resolve] relation_type 非法: {relation}（应为 {RELATION_TYPES}）", file=sys.stderr)
        sys.exit(2)
    if disposition not in DISPOSITIONS:
        print(f"[discriminate-resolve] disposition 非法: {disposition}（应为 {DISPOSITIONS}）", file=sys.stderr)
        sys.exit(2)

    raws, parsed = load_pending()
    # 只在 pending 候选里数 index（1-based）
    pending_indices = [i for i, d in enumerate(parsed) if d and d.get("status") == "pending"]
    if idx < 1 or idx > len(pending_indices):
        print(f"[discriminate-resolve] index {idx} 超出 pending 候选范围（1-{len(pending_indices)}）", file=sys.stderr)
        sys.exit(2)

    target_row = pending_indices[idx - 1]
    entry = parsed[target_row]

    # 标 pending → resolved（带人裁决信息）
    entry["status"] = "resolved"
    entry["resolved_ts"] = now_utc_iso()
    entry["resolved_relation"] = relation
    entry["resolved_disposition"] = disposition
    if note:
        entry["resolved_note"] = note
    parsed[target_row] = entry

    write_pending(raws, parsed)

    # 棘轮只升：判别经验 append（含原候选 pattern+evidence + 人裁决，供下次判别参考）
    append_experience({
        "ts": now_utc_iso(),
        "session": entry.get("session", "unknown"),
        "pattern": entry.get("pattern"),
        "evidence": entry.get("evidence"),
        "relation_type": relation,        # 人判关系类型（守红线⑦：人填非脚本判）
        "disposition": disposition,       # 人判处置
        "note": note,
    })

    update_marker()

    print(f"[discriminate-resolve] ✓ 候选#{idx} 已裁决: {relation}/{disposition}", file=sys.stderr)
    print(f"  pattern={entry.get('pattern')}", file=sys.stderr)
    ev = entry.get("evidence", {})
    if entry.get("pattern") == "evolve_candidate":
        print(f"  project={ev.get('read_project')}", file=sys.stderr)
        # 连接点④路1 回灌证据：adoption_hint 提示该 project 历史注入采纳率低（信息容器候选），
        # 裁决时再次展示供人裁参考（采纳率低≠知识无价值，可能是注入错配，人判关系类型多一个维度）
        if ev.get("adoption_hint"):
            print(f"  ⚠️ {ev['adoption_hint']}", file=sys.stderr)
    elif entry.get("pattern") == "new_candidate":
        print(f"  query={str(ev.get('query',''))[:50]}", file=sys.stderr)
    print(f"  判别经验 → {EXPERIENCE}（棘轮只升）", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
