"""判别环：observations→候选 + 读experience去重（采集环前置认知判别器）

提取自 discriminate-collector.py + discard-pattern-check.py（Step 2 内核提取1）。
合并 81 行 discard-pattern-check（架构方案 §3.1：81行不应独立存在）。
IO 经 lib/state，路径经 lib/paths。

架构范式 §四采集环 + §三种子范式闭环命门 + §2.2 模块契约：
  observations + source-observations + experience → 模式匹配→候选 + 读experience去重
  → pending-queue.jsonl
  反馈：消费 experience 改进候选质量（⑤回流：已 discard 不重复采）

守红线（architecture-paradigm §九）：
  ② 候选先攒 jsonl 不建库schema / ⑤ 只产候选不自动裁决 / ⑦ 只标候选模式不标关系结论
  ⑥ 不总结文件内容只记路径mtime / ⑧ 只收自己裁决
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

from . import state, paths

# === 常量 ===
EVOLVE_WINDOW_SEC = 600  # 演进信号：Read synthesis 后多少秒内的 Write/Edit 算候选
LOW_ADOPTION_THRESHOLD = 0.3  # 连接点④路1：采纳率低于此标 adoption_hint
TRIGGER_THRESHOLD = 3  # 累积触发：pending≥3 写 .discriminate-due marker

# 提取 wiki/projects/<project>/synthesis.md 的 project
SYNTHESIS_RE = re.compile(r"wiki[/\\]projects[/\\]([^/\\]+)[/\\]synthesis\.md", re.IGNORECASE)
# 源1 local_file：从 path 推导项目名（My_Code_Projects/<project>/）
PROJECT_RE = re.compile(r"My_Code_Projects[/\\]([^/\\]+)", re.IGNORECASE)


# ---------- 纯逻辑 ----------

def parse_ts(s):
    """observations 用 %Y-%m-%dT%H:%M:%SZ，解析失败返回 None"""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_project_from_path(path):
    """从本地文件路径推导项目名（My_Code_Projects/<project>/ 的下一段）。
    推不出返回 None（如路径不在项目根下）。留人裁确认项目归属，不强行归一个。"""
    m = PROJECT_RE.search(path.replace("\\", "/"))
    return m.group(1) if m else None


def _dedup_key(pattern, evidence):
    """统一去重键构造：(pattern, signal_key|read_project|query|path)

    优先级：signal_key(源2 transcript) > read_project(evolve) > query(new) > path(源1 file_change)。
    注：file_change_candidate evidence 字段是 path 非 read_project，早期 key 构造漏 path
    致源1 ⑤回流+M5 幂等失效（key 全 None），源2 transcript_candidate 需 signal_key，
    一并修（2026-07-02 源2 落地时发现并修正）。
    """
    k = evidence.get("signal_key") or evidence.get("read_project") \
        or evidence.get("query") or evidence.get("path")
    return (pattern, k)


def extract_discard_keys(experience_entries):
    """⑤回流（最小形态）：从 experience 条目提取已 discard 的候选去重键。

    守最小：只读 discard 做去重，不调权重/不泛化模式/不主题聚合（L3 完善）。
    守红线⑦：只读 disposition 判丢弃，不读 relation_type 推断关系。
    adopt/isolate 的候选可能演进后值得重采，只对 discard 做回流去重。
    """
    keys = set()
    for d in experience_entries:
        if d.get("disposition") != "discard":
            continue
        ev = d.get("evidence", {}) or {}
        keys.add(_dedup_key(d.get("pattern"), ev))
    return keys


def extract_adoption_signal(reuse_entries):
    """连接点④路1（消费→采集）：从 reuse-log adoption verdict 提取 {project: avg_rate}。

    采集器产候选时，若 project 历史采纳率低（< LOW_ADOPTION_THRESHOLD），
    evidence 标 adoption_hint 提示人裁关注"该知识注入过但未被真用=信息容器候选"。
    守红线⑤⑦：只标注证据供人裁参考，不调权重/不丢弃候选。
    ⚠️ verdict 是 LLM-judge 判的非真值，hint 是人裁参考信号非裁决。
    """
    signal = {}
    for d in reuse_entries:
        if d.get("kind") != "adoption":
            continue
        proj = d.get("project")
        rate = d.get("rate")
        if proj is None or not isinstance(rate, (int, float)) or rate < 0:
            continue  # 字段缺失/类型异常跳过，fail-open
        signal.setdefault(proj, []).append(rate)
    return {p: sum(rs) / len(rs) for p, rs in signal.items()}


def extract_pending_keys(pending_entries):
    """M5 幂等去重：从 pending-queue 提取已 pending 的候选去重键。

    append 前查重避免同 (pattern, evidence-key) 重复累积
    （审查 M5：投资跟踪 x5/量子 x3 同项目重复）。
    """
    keys = set()
    for d in pending_entries:
        if d.get("status") != "pending":
            continue
        ev = d.get("evidence", {}) or {}
        keys.add(_dedup_key(d.get("pattern"), ev))
    return keys


def _parse_input_preview(pv):
    """解析 input_preview JSON 字符串为 dict，失败/非对象返回 {}"""
    if not pv or not isinstance(pv, str) or not pv.startswith("{"):
        return {}
    try:
        inp = json.loads(pv)
        return inp if isinstance(inp, dict) else {}
    except Exception:
        return {}


def build_evolve_evidence(r_ts, r_project, follow_write_ts, follow_write_event):
    """构造 evolve_candidate 的 evidence，fw_ev 残缺返回 None（不采集）

    机制进化第2圈防御（阶段4 D3）：fw_ev 残缺（input_preview 解析失败/file_path 空
    且无 change/content_preview）→ 人裁判不动关系类型 → 不采集。
    守红线②：不产判不动的候选堆 pending。
    """
    fw_ev = {"ts": follow_write_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "tool": follow_write_event.get("tool", "")}
    fw_inp = _parse_input_preview(follow_write_event.get("input_preview", ""))
    fw_ev["file_path"] = fw_inp.get("file_path", "")
    # Edit 有 old_string/new_string（改了什么最直观）；Write 有 content
    if fw_inp.get("new_string"):
        fw_ev["change"] = (fw_inp.get("old_string", "") + " → " + fw_inp.get("new_string", ""))[:200]
    elif fw_inp.get("content"):
        fw_ev["content_preview"] = str(fw_inp.get("content", ""))[:200]
    # 残缺（file_path 空 且 无 change/content_preview）→ 不采集
    if not fw_ev.get("file_path") and not fw_ev.get("change") and not fw_ev.get("content_preview"):
        return None
    return {
        "read_project": r_project,
        "read_ts": r_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "followed_by_write": fw_ev,
        "note": "Read synthesis 后 10min 内 Write/Edit，潜在演进/深化，留人裁关系类型",
    }


def build_new_candidate_evidence(ts, query, session):
    """new_candidate：外部搜索 query → 候选 evidence"""
    return {
        "ts": now_utc_iso(),
        "session": session,
        "pattern": "new_candidate",
        "evidence": {
            "query": query,
            "search_ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "note": "外部搜索，潜在新认知，留人裁是否 wiki 未收录+是否采纳",
        },
        "status": "pending",
    }


def build_file_change_evidence(d):
    """file_change_candidate：本地文件变更 evidence（含 adoption_hint 拼接）"""
    path = d.get("path", "")
    project = derive_project_from_path(path)
    evidence = {
        "path": path,
        "project": project,  # 推导的项目名（None=不在项目根，人裁确认归属）
        "mtime": d.get("mtime", ""),
        "size": d.get("size", 0),
        "note": "本地文件变更（source-scanner 主动扫描），潜在新知识/演进，留人裁是否值得 wiki 摄入+关系类型",
    }
    return evidence, project  # project 返回供调用方接 adoption_hint


def build_transcript_evidence(d):
    """transcript_candidate：对话决策动作信号 evidence"""
    session = d.get("session", "")
    action_type = d.get("action_type", "")
    context = d.get("context", "")
    signal_key = f"{session}|{action_type}|{context[:40]}"
    return {
        "signal_key": signal_key,
        "session": session,
        "action_type": action_type,  # decision / revision
        "context": context,  # 匹配处前后60字片段（人裁定位，不总结语义守红线⑥）
        "transcript_path": d.get("transcript_path", ""),
        "note": "对话含决策动作(决策/修正)，潜在认知演进/判别经验原料，留人裁关系类型+是否入wiki/experience",
    }, signal_key


def build_ima_evidence(d):
    """ima_candidate：IMA 笔记变更信号 evidence"""
    docid = d.get("docid", "")
    return {
        "signal_key": docid,
        "docid": docid,
        "title": d.get("title", ""),
        "modify_time": d.get("modify_time", ""),
        "folder_id": d.get("folder_id", ""),
        "folder_name": d.get("folder_name", ""),
        "note": "IMA 笔记变更，潜在新知识/演进，留人裁是否值得 wiki 摄入+关系类型（人裁看 IMA 原文）",
    }, docid


def check_candidate(candidate, patterns):
    """检查候选是否匹配 confirmed discard 模式，返回命中列表（纯逻辑，原 discard-pattern-check）

    候选文本：query/note/path/action_type/read_project 拼接 + 顶层 note/reason。
    keyword 子串匹配即命中。
    """
    hits = []
    ev = candidate.get("evidence", {}) if isinstance(candidate.get("evidence"), dict) else {}
    text = " ".join(str(v) for v in [
        ev.get("query", ""), ev.get("note", ""), ev.get("path", ""),
        ev.get("action_type", ""), ev.get("read_project", ""),
        candidate.get("note", ""), candidate.get("reason", "")
    ] if v)
    for p in patterns:
        if p.get("keyword", "") in text:
            hits.append({"pattern_id": p.get("id"), "keyword": p.get("keyword"),
                         "note": p.get("note", ""), "source_pattern": p.get("source_pattern")})
    return hits


# ---------- IO ----------

def load_confirmed_patterns():
    """加载 discard-patterns.yaml 中 human_confirmed=true 的模式（原 discard-pattern-check）"""
    yaml_path = os.path.join(paths.hooks_dir(), "discard-patterns.yaml")
    if not os.path.exists(yaml_path):
        return []
    try:
        import yaml
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
        return [p for p in (data.get("patterns") or []) if p.get("human_confirmed")]
    except Exception:
        return []


def mark_discard_pattern(entry):
    """M2 认脸：检查候选是否匹配 confirmed discard 模式，命中则标 discard_pattern_hit
    （不自动discard，守红线5）。fail-open：标记失败不影响采集。"""
    try:
        patterns = load_confirmed_patterns()
        if patterns:
            hits = check_candidate(entry, patterns)
            if hits:
                entry["discard_pattern_hit"] = hits
    except Exception:
        pass
    return entry


def append_candidate(entry):
    """棘轮只升：append 不删（达尔文棘轮）。pending 状态待人裁改 resolved。
    先标 discard_pattern_hit（M2 认脸），再经 state 写 pending-queue。"""
    entry = mark_discard_pattern(entry)
    state.append_instincts_jsonl("pending-queue.jsonl", entry)


def load_discard_keys():
    """⑤回流：读 discriminate-experience.jsonl 的 discard 经验去重键"""
    return extract_discard_keys(state.read_instincts_jsonl("discriminate-experience.jsonl"))


def load_adoption_signal():
    """连接点④路1：读 reuse-log.jsonl 的 adoption verdict 采纳率档案"""
    return extract_adoption_signal(state.read_instincts_jsonl("reuse-log.jsonl"))


def load_pending_keys():
    """M5 幂等：读 pending-queue 中已 pending 的候选去重键"""
    return extract_pending_keys(state.read_instincts_jsonl("pending-queue.jsonl"))


def _count_pending():
    """数 pending-queue 中 status=pending 的条目数"""
    n = 0
    for d in state.read_instincts_jsonl("pending-queue.jsonl"):
        if d.get("status") == "pending":
            n += 1
    return n


def update_trigger_marker():
    """pending 候选 ≥ 阈值写 .discriminate-due marker，< 阈值删 marker。
    累积触发阈值=3（达尔文同构"≥3次累积触发"）。复用 maintenance-guard marker 形态。
    resolve 裁决后也调此函数刷新 marker（消除 collector/resolve 重复逻辑）。
    """
    marker = paths.instincts_file(".discriminate-due")
    pending_count = _count_pending()
    try:
        if pending_count >= TRIGGER_THRESHOLD:
            state.write_json(marker, {
                "triggered": True,
                "pending_count": pending_count,
                "threshold": TRIGGER_THRESHOLD,
                "timestamp": now_utc_iso(),
                "hint": f"判别候选 {pending_count} 条待裁决，触发慢环人裁（关系类型标注+处置）",
            })
        else:
            if os.path.exists(marker):
                os.remove(marker)
    except OSError as e:
        # fail-open：marker 写失败不阻断主流程，但留 stderr 痕迹防静默吞错
        print(f"[discriminate] WARN: marker 写入失败 {e}", file=sys.stderr)


# ---------- 编排 ----------

def _adoption_hint_str(rate):
    """构造 adoption_hint 文本（evolve/file_change 共用）"""
    return (f"该项目近期注入采纳率 {rate:.0%}（< {LOW_ADOPTION_THRESHOLD:.0%}，"
            f"注入未真用=信息容器候选，人裁关注是知识无价值还是注入错配）")


def _load_collector_context(cursor_file, obs_file, ts_field, full_scan, filter_fn=None):
    """公共编排：cursor 处理 + 原料读取 + events 构建 + 去重键加载。

    observe/source 两模式共用。ts_field 参数化（observations 用 "timestamp"，
    source-observations 用 "ts"）；filter_fn 参数化（source 模式过滤 source 类型）。
    无原料/无新增早退 sys.exit(0)（fail-open）。返回 (events, seen, adoption_signal)。
    """
    if full_scan and os.path.exists(cursor_file):
        try:
            os.remove(cursor_file)
        except OSError:
            pass

    if not os.path.exists(obs_file):
        sys.exit(0)

    cursor_ts_str = None if full_scan else state.read_cursor(cursor_file)
    cursor_ts = parse_ts(cursor_ts_str) if cursor_ts_str else None

    events = []
    for d in state.read_jsonl(obs_file):
        if filter_fn and not filter_fn(d):
            continue
        ts = parse_ts(d.get(ts_field, ""))
        if not ts:
            continue
        if cursor_ts and ts <= cursor_ts:
            continue
        events.append((ts, d))

    if not events:
        sys.exit(0)

    events.sort(key=lambda x: x[0])
    discard_keys = load_discard_keys()
    pending_keys = load_pending_keys()
    adoption_signal = load_adoption_signal()
    seen = set(pending_keys) | set(discard_keys)
    return events, seen, adoption_signal


def _finalize_collector(cursor_file, events, log_msg):
    """公共收尾：游标推进 + marker 刷新 + 输出 + exit。"""
    state.write_cursor(cursor_file, events[-1][0].strftime("%Y-%m-%dT%H:%M:%SZ"))
    update_trigger_marker()
    print(log_msg, file=sys.stderr)
    sys.exit(0)


def run_observe_mode(full_scan):
    """默认模式：扫 observations.jsonl 产 evolve_candidate + new_candidate。

    输入：observations.jsonl（observe.sh 采集的原料）
    游标：.discriminate-cursor（增量扫描避免重扫）
    输出：pending-queue.jsonl（判别候选，待人裁）
    fail-open：无原料/扫不到候选也 exit 0。
    """
    cursor_file = paths.instincts_file(".discriminate-cursor")
    obs_file = paths.instincts_file("observations.jsonl")
    events, seen, adoption_signal = _load_collector_context(
        cursor_file, obs_file, "timestamp", full_scan)
    new_candidates = 0

    # === 演进信号：Read synthesis → 后 EVOLVE_WINDOW_SEC 内 Write/Edit ===
    synthesis_reads = []  # [(ts, project, session)]
    for ts, d in events:
        if d.get("tool") != "Read":
            continue
        inp = _parse_input_preview(d.get("input_preview", ""))
        fp = inp.get("file_path", "")
        m = SYNTHESIS_RE.search(fp.replace("\\", "/"))
        if m:
            synthesis_reads.append((ts, m.group(1), d.get("session", "unknown")))

    for r_ts, r_project, r_session in synthesis_reads:
        # 找后续窗口内的 Write/Edit（含事件内容，供人裁判关系）
        follow_write_ts = None
        follow_write_event = None
        for e_ts, e_d in events:
            if e_ts <= r_ts:
                continue
            if (e_ts - r_ts).total_seconds() > EVOLVE_WINDOW_SEC:
                break  # 已排序，超出窗口后不再找
            if e_d.get("tool") in ("Write", "Edit"):
                follow_write_ts = e_ts
                follow_write_event = e_d
                break
        if not follow_write_ts:
            continue
        evidence = build_evolve_evidence(r_ts, r_project, follow_write_ts, follow_write_event)
        if evidence is None:
            continue  # fw_ev 残缺不采集
        key = ("evolve_candidate", r_project)
        if key in seen:
            continue  # 已 pending 或已 discard，不重复采（⑤回流 + M5 幂等）
        seen.add(key)
        # 连接点④路1：该项目历史注入采纳率低 → 标注 hint
        if r_project in adoption_signal and adoption_signal[r_project] < LOW_ADOPTION_THRESHOLD:
            evidence["adoption_hint"] = _adoption_hint_str(adoption_signal[r_project])
        append_candidate({
            "ts": now_utc_iso(),
            "session": r_session,
            "pattern": "evolve_candidate",
            "evidence": evidence,
            "status": "pending",
        })
        new_candidates += 1

    # === 新认知信号：tavily_search/websearch 的 query ===
    for ts, d in events:
        tool = d.get("tool", "")
        if "tavily_search" not in tool and "websearch" not in tool.lower():
            continue
        inp = _parse_input_preview(d.get("input_preview", ""))
        query = inp.get("query", "")
        if not query:
            continue
        key = ("new_candidate", query)
        if key in seen:
            continue  # 已 pending / 已 discard / 本次已采，不重复
        seen.add(key)
        append_candidate(build_new_candidate_evidence(ts, query, d.get("session", "unknown")))
        new_candidates += 1

    _finalize_collector(cursor_file, events,
        f"[discriminate-collector] 扫描 {len(events)} 条新增原料，产出 {new_candidates} 条判别候选 → "
        f"{paths.instincts_file('pending-queue.jsonl')}")


def run_source_mode(full_scan):
    """--source 模式：扫 source-observations.jsonl（source-scanner 产的三源原料），
    产 file_change_candidate/transcript_candidate/ima_candidate。

    与默认 observe 模式独立：原料源/游标/候选模式都不同。
    守原则3：复用 pending-queue/experience/resolve 链路，不另造判别器。
    """
    cursor_file = paths.instincts_file(".source-collector-cursor")
    source_obs = paths.instincts_file("source-observations.jsonl")
    events, seen, adoption_signal = _load_collector_context(
        cursor_file, source_obs, "ts", full_scan,
        filter_fn=lambda d: d.get("source", "") in ("local_file", "transcript", "ima"))
    new_candidates = 0

    for ts, d in events:
        src = d.get("source", "")
        if src == "local_file":
            path = d.get("path", "")
            if not path:
                continue
            key = ("file_change_candidate", path)
            if key in seen:
                continue
            seen.add(key)
            evidence, project = build_file_change_evidence(d)
            # 连接点④路1：该项目历史注入采纳率低 → 标注 hint
            if project and project in adoption_signal \
                    and adoption_signal[project] < LOW_ADOPTION_THRESHOLD:
                evidence["adoption_hint"] = _adoption_hint_str(adoption_signal[project])
            append_candidate({
                "ts": now_utc_iso(),
                "session": "source-scan",  # source 模式非会话事件，标 source-scan 区分
                "pattern": "file_change_candidate",
                "evidence": evidence,
                "status": "pending",
            })
            new_candidates += 1
        elif src == "transcript":
            evidence, signal_key = build_transcript_evidence(d)
            if not signal_key:
                continue
            key = ("transcript_candidate", signal_key)
            if key in seen:
                continue
            seen.add(key)
            append_candidate({
                "ts": now_utc_iso(),
                "session": d.get("session", "") or "source-scan",
                "pattern": "transcript_candidate",
                "evidence": evidence,
                "status": "pending",
            })
            new_candidates += 1
        elif src == "ima":
            evidence, docid = build_ima_evidence(d)
            if not docid:
                continue
            key = ("ima_candidate", docid)
            if key in seen:
                continue
            seen.add(key)
            append_candidate({
                "ts": now_utc_iso(),
                "session": "ima-scan",  # ima 模式非会话事件，标 ima-scan 区分
                "pattern": "ima_candidate",
                "evidence": evidence,
                "status": "pending",
            })
            new_candidates += 1

    _finalize_collector(cursor_file, events,
        f"[discriminate-collector --source] 扫描 {len(events)} 条源原料，"
        f"产出 {new_candidates} 条判别候选(file_change/transcript_candidate) → "
        f"{paths.instincts_file('pending-queue.jsonl')}")


def discard_check_main():
    """原 discard-pattern-check.py 的 main：M2 认脸批量扫描 / --candidate 单候选检查。

    Step 2 并入 lib/discriminate（架构方案 §3.1：81行不应独立存在）。
    Step 4 将删 hook 壳，此函数留 lib 供 /ke-health 或慢环调用。
    """
    patterns = load_confirmed_patterns()
    print(f"# M2 认脸：confirmed discard 模式 {len(patterns)}条")

    # 单候选模式（供 resolve 调用）
    if "--candidate" in sys.argv:
        idx = sys.argv.index("--candidate") + 1
        if idx < len(sys.argv):
            cand = json.loads(sys.argv[idx])
            hits = check_candidate(cand, patterns)
            if hits:
                print(json.dumps({"discard_pattern_hit": hits}, ensure_ascii=False))
            else:
                print("{}")
            return

    # 批量扫描 pending-queue
    pending = state.read_instincts_jsonl("pending-queue.jsonl")
    if not pending:
        print(f"# pending-queue 不存在")
        return
    pending_count = 0
    matched = 0
    for r in pending:
        if r.get("status") != "pending":
            continue
        pending_count += 1
        hits = check_candidate(r, patterns)
        if hits:
            matched += 1
            print(f"  ⚠ {r.get('pattern', '?')} 命中: {hits}")
    print(f"# 扫描完成: pending={pending_count} 匹配discard模式={matched}")
    print(f"# 注：当前confirmed={len(patterns)}条(全未确认则0条生效)。人确认discard-patterns.yaml后生效")


def main():
    """CLI 入口：--source 扫源原料 / --full-scan 全量重扫 / 无参=增量（SessionEnd 用）"""
    if "--source" in sys.argv[1:]:
        run_source_mode("--full-scan" in sys.argv[1:])
        return
    run_observe_mode("--full-scan" in sys.argv[1:])
