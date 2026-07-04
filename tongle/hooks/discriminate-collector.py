#!/usr/bin/env python3
"""
discriminate-collector.py — L2 判别候选采集器（采集环前置认知判别器）

架构范式 §四采集环 + §三种子范式闭环命门：
  采集 = ke 主动从信息源（对话历史/项目记忆/本地文件）采集时即判别
  信息 vs 知识、与现有关系（新/演进/互补/冲突）。
  本脚本从 observe.sh 现有原料（observations.jsonl）识别"值得人裁的关系候选模式"，
  不另造采集触发器（observe 已采原料，守原则3"判别复用同库不另造"）。

守红线（architecture-paradigm §九）：
  ② 判别候选先攒 jsonl，不建库 schema（随数据长不强建）
  ⑦ 记"关系候选模式"（读了X后写了Y）非"裁决结论"（X和Y是演进关系=结果，留人裁填）
  ⑧ 只收自己裁决（人裁决入口只收指挥官裁决，不吸收他人/外源裁决结果）
  ⑤ 人确认环不可省（本脚本只产候选，不自动裁决；裁决归慢环人裁）

触发（2026-07-02 接生产调用链，任务#2）：
  - SessionEnd hook 自动增量：每会话结束跑无参 `python3 hooks/discriminate-collector.py`，
    游标推进，扫 observations.jsonl 增量。解"collector 生产从不触发"孤儿（审查 P1-D/E 根因）。
  - 慢环触发全量：慢环 workflow 审计 phase 先跑 `python3 hooks/discriminate-collector.py --full-scan`，
    游标归零全量重扫（含跨源扫描留下一轮三源方案）。指挥官定"慢环触发+SessionEnd 自动"双触发。
  - 手动：`python3 hooks/discriminate-collector.py [--full-scan]`。
  fail-open：扫不到候选也 exit 0。

输入：~/.claude/instincts/observations.jsonl（observe.sh 采集的原料）
游标：~/.claude/instincts/.discriminate-cursor（last processed ts，增量扫描避免重扫）
输出：~/.claude/instincts/pending-queue.jsonl（判别候选，待人裁）
      每条 = {ts, session, pattern, evidence, status:pending}

关系候选模式（最小集，守红线②不强建复杂判别器）：
  - evolve_candidate（演进信号）：Read 某 synthesis 后 N分钟内 Write/Edit
    （读取→产出=潜在深化。evidence: read project + 后续 write 事件 ts）
  - new_candidate（新认知信号）：tavily_search 的 query
    （外部采集=潜在新知识。evidence: query 内容）
  - conflict_candidate（冲突信号）：TODO（需语义比对，留后续——守简单方案优先）
  注：只标候选模式，不标关系类型结论（守红线⑦）。关系类型（新/演进/互补/冲突）
  留人裁决时填（人看对话上下文判，hook/脚本判不了）

关联：architecture-paradigm §四采集环 / §五原则3 / §三种子范式达尔文同构 /
      memory ke-architecture-paradigm / [[knowledge-engine-project]]
"""
import sys
import json
import os
import re
from datetime import datetime, timedelta, timezone

_home = os.path.expanduser("~")
INSTINCTS_DIR = os.path.join(_home, ".claude", "instincts")
OBSERVATIONS = os.path.join(INSTINCTS_DIR, "observations.jsonl")
PENDING_QUEUE = os.path.join(INSTINCTS_DIR, "pending-queue.jsonl")
CURSOR = os.path.join(INSTINCTS_DIR, ".discriminate-cursor")

# 演进信号：Read synthesis 后多少秒内的 Write/Edit 算候选
EVOLVE_WINDOW_SEC = 600  # 10 分钟

# 提取 wiki/projects/<project>/synthesis.md 的 project
SYNTHESIS_RE = re.compile(r"wiki[/\\]projects[/\\]([^/\\]+)[/\\]synthesis\.md", re.IGNORECASE)


def parse_ts(s):
    """observations 用 %Y-%m-%dT%H:%M:%SZ"""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_cursor():
    """上次处理到的 ts（不含）。增量扫描跳过已处理。"""
    if not os.path.exists(CURSOR):
        return None
    try:
        return open(CURSOR, encoding="utf-8").read().strip()
    except OSError:
        return None


def save_cursor(ts):
    try:
        with open(CURSOR, "w", encoding="utf-8") as f:
            f.write(ts)
    except OSError:
        pass  # 游标丢失不影响候选输出（下次全扫，可接受）


def append_candidate(entry):
    """棘轮只升：append 不删（达尔文棘轮）。pending 状态待人裁改 resolved。"""
    try:
        with open(PENDING_QUEUE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 写失败不阻断（fail-open，候选丢失可接受）


EXPERIENCE = os.path.join(INSTINCTS_DIR, "discriminate-experience.jsonl")
REUSE_LOG = os.path.join(INSTINCTS_DIR, "reuse-log.jsonl")

# 连接点④路1（消费→采集）阈值：低于此采纳率的 project，采集候选时标 adoption_hint
# 人裁参考信号（"该知识注入过但未被真用=信息容器候选"），不自动判关系（守红线⑤⑦）
LOW_ADOPTION_THRESHOLD = 0.3


def load_discard_keys():
    """⑤回流（最小形态）：读 discriminate-experience.jsonl 的 discard 经验，
    返回已被丢弃的候选去重键（已 discard 的不再重复采 → 经验回流指导下次采集）。

    ⑤闭环连接点1（范式 §三）：判别经验库→采集判别。experience.jsonl 是棘轮只升的
    完整历史（pending-queue 清理后仍在），是⑤真正数据源（非 pending-queue 的 resolved 标记——
    那是队列状态非经验库，双视角审查 C1 坐实）。
    守最小：只读 discard 做去重，不调权重/不泛化模式/不主题聚合（L3 完善）。
    守红线⑦：只读 disposition 判丢弃，不读 relation_type 推断关系（关系仍人裁）。
    fail-open：experience 不存在/读失败返回空集（0 真裁时无回流，正常）。
    """
    keys = set()
    if not os.path.exists(EXPERIENCE):
        return keys
    try:
        for ln in open(EXPERIENCE, encoding="utf-8"):
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("disposition") != "discard":
                continue  # 只对 discard 做回流去重（adopt/isolate 的候选可能演进后值得重采）
            ev = d.get("evidence", {})
            # 去重键优先级：signal_key(源2 transcript) > read_project(evolve) > query(new) > path(源1 file_change)
            # 注：file_change_candidate evidence 字段是 path 非 read_project，早期 key 构造漏 path
            # 致源1 ⑤回流+M5 幂等失效（key 全 None），源2 transcript_candidate 需 signal_key，
            # 一并修（2026-07-02 源2 落地时发现并修正）
            k = (d.get("pattern"), ev.get("signal_key") or ev.get("read_project")
                 or ev.get("query") or ev.get("path"))
            keys.add(k)
    except OSError:
        pass
    return keys


def load_adoption_signal():
    """连接点④路1（消费→采集）：读 reuse-log.jsonl 的 kind=adoption verdict，
    返回 {project: avg_rate} 近期采纳率档案。

    采集器产 evolve_candidate 时，若 read_project 历史采纳率低（< LOW_ADOPTION_THRESHOLD），
    evidence 标 adoption_hint 提示人裁关注"该知识注入过但未被真用=信息容器候选"。

    同构 load_discard_keys（⑤回流）：读消费环证据指导采集判别，不自动判关系。
    守红线⑤⑦：只标注证据供人裁参考，不调权重/不丢弃候选（采纳率低≠知识无价值，
    可能是注入错配如 pp-002，人裁判关系类型时多一个维度）。
    守最小：project 级聚合非 knowledge 级（verdict 字段限制），不强建知识级档案（留远期）。
    fail-open：reuse-log 不存在/无 verdict 返回空 dict（0 采纳数据时无回流，正常）。
    ⚠️ verdict 是 LLM-judge 判的非真值（单 judge 有偏差风险，见 adoption-judge-worker.py entry 注释），
    hint 是人裁参考信号非裁决，下游留余量+人工抽检。
    """
    signal = {}
    if not os.path.exists(REUSE_LOG):
        return signal
    try:
        for ln in open(REUSE_LOG, encoding="utf-8"):
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("kind") != "adoption":
                continue
            proj = d.get("project")
            rate = d.get("rate")
            if proj is None or not isinstance(rate, (int, float)) or rate < 0:
                continue  # 字段缺失/类型异常（verdict 被篡改为字符串等）跳过，fail-open
            signal.setdefault(proj, []).append(rate)
    except OSError:
        return signal
    return {p: sum(rs) / len(rs) for p, rs in signal.items()}


def load_pending_keys():
    """M5 幂等去重：读 pending-queue 中已 pending 的候选去重键，
    append 前查重避免同 (pattern, evidence-key) 重复累积（审查 M5：投资跟踪 x5/量子 x3 同项目重复）。
    fail-open：pending-queue 不存在/读失败返回空集。
    """
    keys = set()
    if not os.path.exists(PENDING_QUEUE):
        return keys
    try:
        for ln in open(PENDING_QUEUE, encoding="utf-8"):
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("status") != "pending":
                continue
            ev = d.get("evidence", {})
            # 去重键优先级同 load_discard_keys（signal_key > read_project > query > path）
            k = (d.get("pattern"), ev.get("signal_key") or ev.get("read_project")
                 or ev.get("query") or ev.get("path"))
            keys.add(k)
    except OSError:
        pass
    return keys


# 源1 local_file：从 path 推导项目名（My_Code_Projects/<project>/）
PROJECT_RE = re.compile(r"My_Code_Projects[/\\]([^/\\]+)", re.IGNORECASE)

# source 模式独立游标（与 observe 模式 .discriminate-cursor 区分，原料源不同）
SOURCE_CURSOR = os.path.join(INSTINCTS_DIR, ".source-collector-cursor")
SOURCE_OBS_FILE = os.path.join(INSTINCTS_DIR, "source-observations.jsonl")


def derive_project_from_path(path):
    """从本地文件路径推导项目名（My_Code_Projects/<project>/ 的下一段）。
    推不出返回 None（如路径不在项目根下）。留人裁确认项目归属，不强行归一个。"""
    m = PROJECT_RE.search(path.replace("\\", "/"))
    return m.group(1) if m else None


def run_source_mode(full_scan):
    """--source 模式：扫 source-observations.jsonl（source-scanner 产的三源原料），
    产 file_change_candidate（本地文件变更=潜在新知识/演进信号）。

    与默认 observe 模式独立：
    - 原料源不同（source-observations vs observations.jsonl）
    - 游标不同（.source-collector-cursor vs .discriminate-cursor）
    - 候选模式不同（file_change_candidate vs evolve/new_candidate）

    守原则3：复用 pending-queue/experience/resolve 链路，不另造判别器。
    守红线②⑦：只标"有变更"候选，关系类型（新/演进/互补/冲突）留人裁。
    守红线⑥：不总结文件内容，evidence 只记 path/mtime/size（人裁看原文件）。
    去重键=(file_change_candidate, path)：同 path 已 pending 或已 discard 不重复采。
    """
    if full_scan and os.path.exists(SOURCE_CURSOR):
        try:
            os.remove(SOURCE_CURSOR)
        except OSError:
            pass

    if not os.path.exists(SOURCE_OBS_FILE):
        sys.exit(0)  # 无源原料，fail-open

    cursor_ts_str = None if full_scan else load_cursor_file(SOURCE_CURSOR)
    cursor_ts = parse_ts(cursor_ts_str) if cursor_ts_str else None

    events = []
    try:
        for ln in open(SOURCE_OBS_FILE, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            src = d.get("source", "")
            if src not in ("local_file", "transcript", "ima"):
                continue
            ts = parse_ts(d.get("ts", ""))
            if not ts:
                continue
            if cursor_ts and ts <= cursor_ts:
                continue  # 增量跳过已处理
            events.append((ts, d))
    except OSError:
        sys.exit(0)

    if not events:
        sys.exit(0)

    events.sort(key=lambda x: x[0])

    discard_keys = load_discard_keys()   # ⑤回流：已 discard 不重复采
    pending_keys = load_pending_keys()   # M5 幂等：已 pending 不重复累积
    seen = set(pending_keys) | set(discard_keys)
    adoption_signal = load_adoption_signal()  # 连接点④路1：消费环采纳率回灌采集判别（标注非裁决）
    new_candidates = 0

    for ts, d in events:
        src = d.get("source", "")
        if src == "local_file":
            path = d.get("path", "")
            if not path:
                continue
            key = ("file_change_candidate", path)
            if key in seen:
                continue  # 已 pending / 已 discard，不重复采
            seen.add(key)
            project = derive_project_from_path(path)
            evidence = {
                "path": path,
                "project": project,  # 推导的项目名（None=不在项目根，人裁确认归属）
                "mtime": d.get("mtime", ""),
                "size": d.get("size", 0),
                "note": "本地文件变更（source-scanner 主动扫描），潜在新知识/演进，留人裁是否值得 wiki 摄入+关系类型",
            }
            # 连接点④路1（消费→采集）：该项目历史注入采纳率低 → 标注 hint 供人裁参考
            # （与 observe 模式 evolve_candidate 同构，file_change_candidate 有 project 字段可接）
            # 阶段5发版前审查修复（C 方案前置三修③）：原只回流 evolve_candidate，source 模式
            # 三源候选有 project 字段却未接，采集环方案前置三修之一。transcript/ima 候选无
            # project 字段不接（与 evolve/file_change 同构性不同，守最小不硬造 project 归属）。
            if project and project in adoption_signal:
                rate = adoption_signal[project]
                if rate < LOW_ADOPTION_THRESHOLD:
                    evidence["adoption_hint"] = (
                        f"该项目近期注入采纳率 {rate:.0%}（< {LOW_ADOPTION_THRESHOLD:.0%}，"
                        f"注入未真用=信息容器候选，人裁关注是知识无价值还是注入错配）"
                    )
            append_candidate({
                "ts": now_utc_iso(),
                "session": "source-scan",  # source 模式非会话事件，标 source-scan 区分
                "pattern": "file_change_candidate",
                "evidence": evidence,
                "status": "pending",
            })
            new_candidates += 1
        elif src == "transcript":
            # 源2 transcript：决策动作信号 → transcript_candidate
            # signal_key = session|action_type|context前40字（与 scanner 去重键同构，load_*_keys 用此查重）
            session = d.get("session", "")
            action_type = d.get("action_type", "")
            context = d.get("context", "")
            signal_key = f"{session}|{action_type}|{context[:40]}"
            key = ("transcript_candidate", signal_key)
            if key in seen:
                continue  # 已 pending / 已 discard，不重复采（⑤回流 + M5 幂等）
            seen.add(key)
            evidence = {
                "signal_key": signal_key,
                "session": session,
                "action_type": action_type,  # decision / revision
                "context": context,  # 匹配处前后60字片段（人裁定位，不总结语义守红线⑥）
                "transcript_path": d.get("transcript_path", ""),
                "note": "对话含决策动作(决策/修正)，潜在认知演进/判别经验原料，留人裁关系类型+是否入wiki/experience",
            }
            append_candidate({
                "ts": now_utc_iso(),
                "session": session or "source-scan",
                "pattern": "transcript_candidate",
                "evidence": evidence,
                "status": "pending",
            })
            new_candidates += 1
        elif src == "ima":
            # 源3 IMA：笔记变更信号 → ima_candidate
            # signal_key = docid（笔记唯一 id，load_*_keys 用此查重，⑤回流+M5 幂等）
            docid = d.get("docid", "")
            if not docid:
                continue
            key = ("ima_candidate", docid)
            if key in seen:
                continue  # 已 pending / 已 discard，不重复采（⑤回流 + M5 幂等）
            seen.add(key)
            evidence = {
                "signal_key": docid,
                "docid": docid,
                "title": d.get("title", ""),
                "modify_time": d.get("modify_time", ""),
                "folder_id": d.get("folder_id", ""),
                "folder_name": d.get("folder_name", ""),
                "note": "IMA 笔记变更，潜在新知识/演进，留人裁是否值得 wiki 摄入+关系类型（人裁看 IMA 原文）",
            }
            append_candidate({
                "ts": now_utc_iso(),
                "session": "ima-scan",  # ima 模式非会话事件，标 ima-scan 区分
                "pattern": "ima_candidate",
                "evidence": evidence,
                "status": "pending",
            })
            new_candidates += 1

    # 游标推进到最后一条源事件 ts
    save_cursor_file(SOURCE_CURSOR, events[-1][0].strftime("%Y-%m-%dT%H:%M:%SZ"))

    # 累积触发 marker（复用 observe 模式机制，pending≥3 写 .discriminate-due）
    update_trigger_marker()

    print(f"[discriminate-collector --source] 扫描 {len(events)} 条源原料，"
          f"产出 {new_candidates} 条判别候选(file_change/transcript_candidate) → {PENDING_QUEUE}", file=sys.stderr)
    sys.exit(0)


def load_cursor_file(path):
    """读指定游标文件（source 模式用 SOURCE_CURSOR）。复用 load_cursor 形态。"""
    if not os.path.exists(path):
        return None
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return None


def save_cursor_file(path, ts):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(ts)
    except OSError:
        pass


def main():
    # --source 模式：扫 source-observations.jsonl（source-scanner 产的三源原料），
    #   产 file_change_candidate。与默认 observe 模式独立（游标/原料/候选模式都不同）。
    # --full-scan：游标归零全量重扫（慢环触发用）。无参=增量（SessionEnd 用）。
    if "--source" in sys.argv[1:]:
        run_source_mode("--full-scan" in sys.argv[1:])
        return

    full_scan = "--full-scan" in sys.argv[1:]
    if full_scan and os.path.exists(CURSOR):
        try:
            os.remove(CURSOR)
        except OSError:
            pass  # 游标删失败按增量跑，fail-open

    if not os.path.exists(OBSERVATIONS):
        sys.exit(0)  # 无原料，fail-open

    cursor_ts_str = None if full_scan else load_cursor()
    cursor_ts = parse_ts(cursor_ts_str) if cursor_ts_str else None

    # 读全部原料，按时间排序（跨事件关联需要全局视图）
    events = []
    try:
        for ln in open(OBSERVATIONS, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            ts = parse_ts(d.get("timestamp", ""))
            if not ts:
                continue
            # 增量：跳过 cursor 之前的（已处理）
            if cursor_ts and ts <= cursor_ts:
                continue
            events.append((ts, d))
    except OSError:
        sys.exit(0)

    if not events:
        sys.exit(0)  # 无新增原料

    events.sort(key=lambda x: x[0])
    discard_keys = load_discard_keys()   # ⑤回流：已 discard 经验指导下次采集（不重复采）
    pending_keys = load_pending_keys()  # M5 幂等：已 pending 的不重复累积
    adoption_signal = load_adoption_signal()  # 连接点④路1：消费环采纳率回灌采集判别（标注非裁决）
    seen = set(pending_keys) | set(discard_keys)  # 合并查重集（已存在即 skip）
    new_candidates = 0

    # === 演进信号：Read synthesis → 后 EVOLVE_WINDOW_SEC 内 Write/Edit ===
    synthesis_reads = []  # [(ts, project, session)]
    for ts, d in events:
        if d.get("tool") != "Read":
            continue
        pv = d.get("input_preview", "")
        # input_preview 是 JSON 字符串，含 file_path
        try:
            inp = json.loads(pv) if pv.startswith("{") else {}
        except Exception:
            inp = {}
        fp = inp.get("file_path", "") if isinstance(inp, dict) else ""
        m = SYNTHESIS_RE.search(fp.replace("\\", "/"))
        if m:
            synthesis_reads.append((ts, m.group(1), d.get("session", "unknown")))

    for r_ts, r_project, r_session in synthesis_reads:
        # 找后续窗口内的 Write/Edit（含事件内容，供人裁判关系——光 file_path 不够）
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
        # 提取 follow_write 的目标路径+内容片段（人裁判关系类型的关键证据）
        # 缺此字段人只看 read_project+时间，根本判不了演进/噪声（2026-06-29 真裁试水实证）
        fw_ev = {"ts": follow_write_ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "tool": follow_write_event.get("tool", "")}
        pv = follow_write_event.get("input_preview", "")
        try:
            fw_inp = json.loads(pv) if pv.startswith("{") else {}
        except Exception:
            fw_inp = {}
        if isinstance(fw_inp, dict):
            fw_ev["file_path"] = fw_inp.get("file_path", "")
            # Edit 有 old_string/new_string（改了什么最直观）；Write 有 content
            if fw_inp.get("new_string"):
                fw_ev["change"] = (fw_inp.get("old_string", "") + " → " + fw_inp.get("new_string", ""))[:200]
            elif fw_inp.get("content"):
                fw_ev["content_preview"] = str(fw_inp.get("content", ""))[:200]
        # 机制进化第2圈防御（阶段4 D3）：fw_ev 残缺（input_preview 解析失败/file_path 空
        # 且无 change/content_preview）→ 人裁判不动关系类型 → 不采集
        # 根因：11条evolve缓做债务即旧版"只记ts不记事件"产物，修复补字段后仍需防"字段存在但内容残缺"
        # 守红线②：不产判不动的候选堆 pending（守原则4可验性：消费方=人裁，残缺evidence人裁无法判）
        if not fw_ev.get("file_path") and not fw_ev.get("change") and not fw_ev.get("content_preview"):
            continue
        key = ("evolve_candidate", r_project)
        if key in seen:
            continue  # 已 pending 或已 discard，不重复采（⑤回流 + M5 幂等）
        seen.add(key)
        evidence = {
            "read_project": r_project,
            "read_ts": r_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "followed_by_write": fw_ev,
            "note": "Read synthesis 后 10min 内 Write/Edit，潜在演进/深化，留人裁关系类型",
        }
        # 连接点④路1（消费→采集）：该项目历史注入采纳率低 → 标注 hint 供人裁参考
        # （信息容器候选：知识注入过但未被真用。采纳率低≠无价值，可能是注入错配，人裁多一个维度）
        if r_project in adoption_signal:
            rate = adoption_signal[r_project]
            if rate < LOW_ADOPTION_THRESHOLD:
                evidence["adoption_hint"] = (
                    f"该项目近期注入采纳率 {rate:.0%}（< {LOW_ADOPTION_THRESHOLD:.0%}，"
                    f"注入未真用=信息容器候选，人裁关注是知识无价值还是注入错配）"
                )
        append_candidate({
            "ts": now_utc_iso(),
            "session": r_session,
            "pattern": "evolve_candidate",
            "evidence": evidence,
            "status": "pending",
        })
        new_candidates += 1

    # === 新认知信号：tavily_search 的 query（去重：单次运行内 + pending/已discard 持久化查重） ===
    for ts, d in events:
        tool = d.get("tool", "")
        if "tavily_search" not in tool and "websearch" not in tool.lower():
            continue
        pv = d.get("input_preview", "")
        try:
            inp = json.loads(pv) if pv.startswith("{") else {}
        except Exception:
            inp = {}
        query = inp.get("query", "") if isinstance(inp, dict) else ""
        if not query:
            continue
        key = ("new_candidate", query)
        if key in seen:
            continue  # 已 pending / 已 discard / 本次已采，不重复（M5 幂等 + ⑤回流）
        seen.add(key)
        append_candidate({
            "ts": now_utc_iso(),
            "session": d.get("session", "unknown"),
            "pattern": "new_candidate",
            "evidence": {
                "query": query,
                "search_ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "note": "外部搜索，潜在新认知，留人裁是否 wiki 未收录+是否采纳",
            },
            "status": "pending",
        })
        new_candidates += 1

    # 游标推进到最后一条事件 ts（已处理标记）
    if events:
        save_cursor(events[-1][0].strftime("%Y-%m-%dT%H:%M:%SZ"))

    # === 累积触发（达尔文同构"≥3触发"）：pending 候选数 ≥ 阈值写 marker，
    # 复用 maintenance-guard marker 形态 + session-start §6.5b 读取注入"判别候选待裁决"，
    # 提醒 CC 触发慢环人裁。不另造触发器（守原则3）。人裁决入口=慢环手动触发（守红线⑤）。===
    update_trigger_marker()

    # 输出到 stderr（不污染 stdout，供慢环/人看）
    print(f"[discriminate-collector] 扫描 {len(events)} 条新增原料，产出 {new_candidates} 条判别候选 → {PENDING_QUEUE}", file=sys.stderr)
    sys.exit(0)


def update_trigger_marker():
    """pending 候选 ≥ 阈值写 .discriminate-due marker，< 阈值删 marker。
    累积触发阈值=3（达尔文同构"≥3次累积触发"）。复用 maintenance-guard marker 形态。"""
    MARKER = os.path.join(INSTINCTS_DIR, ".discriminate-due")
    TRIGGER_THRESHOLD = 3
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
        if pending_count >= TRIGGER_THRESHOLD:
            with open(MARKER, "w", encoding="utf-8") as f:
                json.dump({
                    "triggered": True,
                    "pending_count": pending_count,
                    "threshold": TRIGGER_THRESHOLD,
                    "timestamp": now_utc_iso(),
                    "hint": f"判别候选 {pending_count} 条待裁决，触发慢环人裁（关系类型标注+处置）",
                }, f, ensure_ascii=False)
        else:
            # 候选不足阈值，删 marker（已裁决清空后不再提醒）
            if os.path.exists(MARKER):
                os.remove(MARKER)
    except OSError as e:
        # fail-open：marker 写失败不阻断主流程，但留 stderr 痕迹防静默吞错
        # （阶段5发版前审查 P1-1：原 `pass` 静默吞错，marker 未落盘时无任何线索可查）
        print(f"[discriminate-collector] WARN: marker 写入失败 {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
