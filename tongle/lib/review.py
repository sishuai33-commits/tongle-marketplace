"""人裁决入口：resolve候选 + 记录经验（棘轮只升）

提取自 discriminate-resolve.py（Step 2 内核提取1）。
IO 经 lib/state，路径经 lib/paths，marker 复用 lib/discriminate.update_trigger_marker。

架构范式 §2.2 模块契约：
  pending-queue + 人裁决结果 → experience.jsonl
  反馈：裁决结果回流→discriminate下次读（⑤闭环连接点1）

守红线（architecture-paradigm §九）：
  ⑤ 人确认环不可省——本模块是人裁决的执行入口，不自动裁决。
     调用=人已裁决（CLI 即人裁决行为），脚本只记录不判断。
  ⑦ 记判别模式——关系类型/处置是人裁决填的（人看对话上下文判，非脚本判）。
  ⑧ 只收自己裁决——只收本会话指挥官裁决，不吸收他人/外源裁决结果。
  ② 棘轮只升——discriminate-experience.jsonl 只 append 不删（达尔文棘轮）。
"""
import json
import os
import sys

from . import state, paths
from . import discriminate


RELATION_TYPES = {"new", "evolve", "complement", "conflict"}
DISPOSITIONS = {"adopt", "discard", "isolate"}


# ---------- 纯逻辑 ----------

def extract_discard_keyword(entry):
    """M2 认脸：从 discard 候选提取关键词作为 pattern 候选（简单提取，待人确认）

    优先级：query > path > signal_key（与 discard-pattern-check check_candidate text 拼接对齐）。
    截断到 30 字（keyword 是短词，子串匹配用）。
    """
    ev = entry.get("evidence", {}) if isinstance(entry.get("evidence"), dict) else {}
    for key in ("query", "path", "signal_key"):
        val = str(ev.get(key, "")).strip()
        if val:
            return val[:30]
    return None


def build_resolved_entry(entry, relation, disposition, note):
    """标 pending → resolved（带人裁决信息），返回新 entry（不改原 dict 顺序外字段）"""
    entry["status"] = "resolved"
    entry["resolved_ts"] = discriminate.now_utc_iso()
    entry["resolved_relation"] = relation
    entry["resolved_disposition"] = disposition
    if note:
        entry["resolved_note"] = note
    return entry


def build_experience_entry(entry, relation, disposition, note):
    """构造判别经验 entry（棘轮只升 append，含原候选 pattern+evidence + 人裁决）"""
    return {
        "ts": discriminate.now_utc_iso(),
        "session": entry.get("session", "unknown"),
        "pattern": entry.get("pattern"),
        "evidence": entry.get("evidence"),
        "relation_type": relation,        # 人判关系类型（守红线⑦：人填非脚本判）
        "disposition": disposition,       # 人判处置
        "note": note,
    }


# ---------- IO ----------

def load_pending():
    """读 pending-queue，返回 (lines_raw, entries_parsed)

    raws 保留原始行（rstrip \\n），parsed 为 dict 或 None（坏行）。
    回写时坏行 raw 原样保留（write_pending zip(raws, parsed)）。
    """
    path = paths.instincts_file("pending-queue.jsonl")
    if not os.path.exists(path):
        return [], []
    raws, parsed = [], []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
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
    """回写 pending-queue（resolved 标记更新，原子替换 tmp+os.replace）

    坏行（parsed=None）原样写回 raw；好行序列化 dict 覆写。
    写失败 exit(1)（pending 状态丢失会致重复裁决，不可 fail-open）。
    """
    path = paths.instincts_file("pending-queue.jsonl")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for raw, d in zip(raws, parsed):
                f.write(json.dumps(d, ensure_ascii=False) + "\n" if d else raw + "\n")
        os.replace(tmp, path)
    except OSError as e:
        print(f"[discriminate-resolve] 写 pending 失败: {e}", file=sys.stderr)
        sys.exit(1)


def append_experience(entry):
    """棘轮只升：append 到 discriminate-experience.jsonl，不删历史"""
    state.append_instincts_jsonl("discriminate-experience.jsonl", entry)


def append_discard_pattern(keyword, source_pattern, note=""):
    """M2 认脸：人裁 discard 时提取模式追加到 discard-patterns.yaml
    （文本追加保留格式，human_confirmed=false 待人确认）"""
    if os.environ.get("KE_TEST_MODE"):
        return None  # test 模式不写 yaml，避免污染
    yaml_path = os.path.join(paths.hooks_dir(), "discard-patterns.yaml")
    if not os.path.exists(yaml_path):
        return None
    try:
        # 读现有 id 生成新 id
        existing_nums = []
        with open(yaml_path) as f:
            for line in f:
                if line.strip().startswith("- id: dp-"):
                    pid = line.strip().split("id: ")[1].strip()
                    try:
                        existing_nums.append(int(pid.split("-")[1]))
                    except Exception:
                        pass
        new_num = max(existing_nums + [0]) + 1
        new_id = f"dp-{new_num:03d}"
        # 文本追加（保留 yaml 格式 + 注释）
        safe_note = (note or "人裁discard提取待确认")[:60]
        with open(yaml_path, "a") as f:
            f.write(f'  - id: {new_id}\n')
            f.write(f'    keyword: "{keyword}"\n')
            f.write(f'    source_pattern: "{source_pattern}"\n')
            f.write(f'    note: "{safe_note}"\n')
            f.write(f'    human_confirmed: false\n')
        return new_id
    except Exception as e:
        print(f"[discriminate-resolve] M2 提取模式失败: {e}", file=sys.stderr)
        return None


# ---------- 编排 ----------

def main():
    """CLI 入口：python -m lib.review <index> <relation_type> <disposition> [note]

    index: pending-queue.jsonl 中 pending 候选的行号（1-based，从 pending 列表算）
    relation_type: new|evolve|complement|conflict（关系类型，人判）
    disposition: adopt|discard|isolate（处置：采纳/丢弃/隔离，人判）
    """
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
    build_resolved_entry(entry, relation, disposition, note)
    parsed[target_row] = entry
    write_pending(raws, parsed)

    # 棘轮只升：判别经验 append（含原候选 pattern+evidence + 人裁决，供下次判别参考）
    append_experience(build_experience_entry(entry, relation, disposition, note))

    # M2 认脸：人裁 discard 时提取模式写入 discard-patterns.yaml（human_confirmed=false 待人确认）
    if disposition == "discard":
        keyword = extract_discard_keyword(entry)
        if keyword:
            new_pid = append_discard_pattern(keyword, entry.get("pattern", ""), note)
            if new_pid:
                print(f"  📝 M2 提取 discard 模式 → {new_pid} (keyword={keyword}) 待人确认", file=sys.stderr)

    # 刷新 marker（复用 discriminate 消除重复）
    discriminate.update_trigger_marker()

    print(f"[discriminate-resolve] ✓ 候选#{idx} 已裁决: {relation}/{disposition}", file=sys.stderr)
    print(f"  pattern={entry.get('pattern')}", file=sys.stderr)
    ev = entry.get("evidence", {})
    if entry.get("pattern") == "evolve_candidate":
        print(f"  project={ev.get('read_project')}", file=sys.stderr)
        # 连接点④路1 回灌证据：adoption_hint 提示该 project 历史注入采纳率低
        if ev.get("adoption_hint"):
            print(f"  ⚠️ {ev['adoption_hint']}", file=sys.stderr)
    elif entry.get("pattern") == "new_candidate":
        print(f"  query={str(ev.get('query', ''))[:50]}", file=sys.stderr)
    print(f"  判别经验 → {paths.instincts_file('discriminate-experience.jsonl')}（棘轮只升）", file=sys.stderr)
    sys.exit(0)
