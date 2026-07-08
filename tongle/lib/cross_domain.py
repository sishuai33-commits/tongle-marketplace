"""加工环·跨域同构识别（M1，综合路四层架构）

提取自 cross-domain-extract.py（Step 3 内核提取2）。
IO 经 lib/state，路径经 lib/paths。

从判别经验库识别跨域同构模式，产出跨域模式库 cross-domain-patterns.jsonl。

四层架构（防 proxy substitution，指挥官 2026-07-05 定）：
1. 规则粗筛层：按 pattern×disposition 分组，跨≥3session=候选池（提候选，不识别）
2. LLM 判定层：对候选输出"两域映射+可溯源证据+理由"（给依据非给结论），
   confirmed 案例库 few-shot 反哺（棘轮只升，darwin 结构同构）
3. 人裁决层：审 LLM 依据 confirm/reject（守红线5，human_confirmed）
4. 消费佐证层：confirmed 模式被消费环真复用=增智判据（事后，consumption_evidence）

LLM fail-open：env 缺/调用失败 → 降级纯规则产出（llm_judged=false），不阻断。
--no-llm 跳过 LLM 判定（test 用，mock 态）。

用法：python -m lib.cross_domain [--min-sessions 3] [--dry-run] [--no-llm]
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import state, paths

MODEL_JUDGE = "doubao-seed-2.0-pro"  # 固定 doubao，与 adoption-judge-worker 同源防 env 漂移

CROSS_DOMAIN_PROMPT = """你是跨域同构识别员。给你：一组判别经验记录（按 pattern×disposition 分组，跨 N session/M project）。

任务：判断这组记录是否反映"不同领域间的同构模式"——即不同项目/领域出现相同结构（非仅按 disposition 分组的统计聚合）。

要求：
1. is_cross_domain: 是否真同构（不同领域相同结构）
2. domain_a / domain_b: 涉及的两个领域
3. structural_mapping: 两域在什么结构上同构（一句话）
4. evidence_refs: 可溯源证据（引用具体记录的 session/note 片段）
5. rationale: 判定理由
6. confidence: 0-1

判据：
- 同构 = 不同领域出现相同结构模式（如"偏差→累积→评估→确认→棘轮"在 Agent 进化和 ke 判别都出现）
- 非同构 = 仅统计聚合（如"多个项目都有 discard"=按结果分组，非结构同构）

{few_shot_section}

严格按 JSON 输出（只输出 JSON）：
{{"is_cross_domain": true/false, "domain_a": "...", "domain_b": "...", "structural_mapping": "...", "evidence_refs": ["..."], "rationale": "...", "confidence": 0.0}}
"""


def llm_judge(candidate, llm_chat, confirmed_examples):
    """对候选调 LLM 判定，返回可溯源依据。fail-open：失败返回 llm_judged=false。

    纯逻辑（接受 llm_chat + confirmed_examples 参数，无模块级依赖）。
    """
    if llm_chat is None:
        return {"llm_judged": False, "llm_reason": "llm_client 不可用(env缺/--no-llm)"}
    few_shot = ""
    if confirmed_examples:
        few_shot = "已确认案例(参考结构,非照抄):\n"
        for ex in confirmed_examples[:3]:
            few_shot += f"- {ex.get('domain_a','?')} ↔ {ex.get('domain_b','?')}: {ex.get('structural_mapping','?')[:100]}\n"
    recs = candidate["_records"]
    rec_summary = "\n".join(
        f"  [session={r.get('session','?')[:8]} project={(r.get('evidence',{}) or {}).get('read_project','?')}] {r.get('note','')[:100]}"
        for r in recs[:5]
    )
    user = f"""候选分组: pattern={candidate['pattern']} disposition={candidate['disposition']}
跨 {candidate['session_count']} session, {candidate['cross_project_count']} project, {candidate['record_count']} 条记录
样本记录:
{rec_summary}

判断这组是否反映跨域同构(不同领域相同结构),非仅按disposition分组。"""
    try:
        raw = llm_chat(MODEL_JUDGE, CROSS_DOMAIN_PROMPT.format(few_shot_section=few_shot),
                       user, max_tokens=1024)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            return {"llm_judged": False, "llm_reason": "LLM输出无JSON", "llm_raw": raw[:200]}
        result = json.loads(m.group(0))
        return {
            "llm_judged": True,
            "is_cross_domain": result.get("is_cross_domain", False),
            "domain_a": result.get("domain_a", ""),
            "domain_b": result.get("domain_b", ""),
            "structural_mapping": result.get("structural_mapping", ""),
            "llm_evidence_refs": result.get("evidence_refs", []),
            "llm_rationale": result.get("rationale", ""),
            "llm_confidence": result.get("confidence", 0.0),
        }
    except Exception as e:
        return {"llm_judged": False, "llm_reason": f"LLM调用失败: {e}"}


def main():
    """跨域同构识别入口"""
    min_sessions = int(sys.argv[sys.argv.index("--min-sessions") + 1]) \
        if "--min-sessions" in sys.argv else 3
    dry_run = "--dry-run" in sys.argv
    no_llm = "--no-llm" in sys.argv
    now = datetime.now(timezone.utc).isoformat()

    experience_file = paths.instincts_file("discriminate-experience.jsonl")
    cross_domain_lib = paths.instincts_file("cross-domain-patterns.jsonl")
    confirmed_examples_file = paths.instincts_file("cross-domain-confirmed-examples.jsonl")

    # LLM client（可选，fail-open：env 缺/--no-llm 时降级纯规则）
    llm_chat = None
    if not no_llm:
        _eval = os.path.join(paths.plugin_root(), "dev", "eval")
        sys.path.insert(0, _eval)
        try:
            from llm_client import chat as _chat
            llm_chat = _chat
        except Exception:
            pass

    if not os.path.exists(experience_file):
        print(f"✗ 判别经验库不存在: {experience_file}", file=sys.stderr)
        sys.exit(2)

    # ============================================
    # 1. 加载判别经验
    # ============================================
    records = state.read_jsonl(experience_file)
    print(f"# 加载判别经验: {len(records)}条")

    # 读现有 cross-domain-patterns.jsonl 的 human_confirmed + consumption_evidence + human_verdict 状态
    # 守：人确认状态+消费佐证+verdict不被自动重新生成覆盖（否则阶段二人确认白做，违红线5）
    existing_confirmed = {}
    existing_consumption = {}
    existing_verdicts = {}
    if os.path.exists(cross_domain_lib):
        for _p in state.read_jsonl(cross_domain_lib):
            _key = (_p.get("pattern", ""), _p.get("disposition", ""))
            if _p.get("human_confirmed"):
                existing_confirmed[_key] = True
            if _p.get("consumption_evidence"):
                existing_consumption[_key] = _p.get("consumption_evidence")
            if _p.get("human_verdict"):
                existing_verdicts[_key] = {
                    "human_verdict": _p.get("human_verdict"),
                    "human_confirmed_at": _p.get("human_confirmed_at"),
                }
    if existing_confirmed:
        print(f"# 保留已确认模式: {len(existing_confirmed)}条(不被重新生成覆盖)")

    # ============================================
    # 2. 读 confirmed 案例库（LLM few-shot 反哺，棘轮只升）
    # ============================================
    confirmed_examples = state.read_jsonl(confirmed_examples_file) \
        if os.path.exists(confirmed_examples_file) else []
    if confirmed_examples:
        print(f"# confirmed 案例库: {len(confirmed_examples)}条(LLM few-shot 反哺)")

    # ============================================
    # 3. 规则粗筛层：按 (pattern, disposition) 分组 → 候选池
    # ============================================
    groups = defaultdict(list)
    for r in records:
        key = (r.get("pattern", "?"), r.get("disposition", "?"))
        groups[key].append(r)

    candidates = []
    pid = 0
    for (pattern, disposition), recs in sorted(groups.items()):
        sessions = set(r.get("session", "") for r in recs if r.get("session"))
        if len(sessions) < min_sessions:
            continue
        pid += 1
        projects = set()
        for r in recs:
            ev = r.get("evidence", {}) if isinstance(r.get("evidence"), dict) else {}
            proj = ev.get("read_project") or ev.get("project")
            if proj:
                projects.add(proj)
        tss = sorted(r.get("ts", "") for r in recs if r.get("ts"))
        time_span = f"{tss[0][:10]} ~ {tss[-1][:10]}" if tss else ""
        samples = []
        for r in recs[:3]:
            ev = r.get("evidence", {}) if isinstance(r.get("evidence"), dict) else {}
            samples.append({
                "session": r.get("session", "")[:8],
                "note": r.get("note", "")[:120],
                "evidence_brief": {k: str(v)[:80] for k, v in list(ev.items())[:3]},
            })
        notes_text = " ".join(r.get("note", "") + r.get("reason", "") for r in recs)
        words = re.findall(r'[一-龥]{2,6}', notes_text)
        word_counts = Counter(w for w in words if len(w) >= 2)
        common_keywords = [w for w, c in word_counts.most_common(5) if c >= 2]

        candidates.append({
            "pattern_id": f"cd-{pid:03d}",
            "pattern": pattern,
            "disposition": disposition,
            "domain_source": sorted(projects) if projects else ["(无project字段)"],
            "cross_project_count": len(projects),
            "session_count": len(sessions),
            "record_count": len(recs),
            "time_span": time_span,
            "common_keywords": common_keywords,
            "sample_evidence": samples,
            "_records": recs,  # LLM 判定用，输出前删除
        })

    # ============================================
    # 4. LLM 判定层：对每个候选输出可溯源依据（fail-open）
    # ============================================
    print(f"\n# LLM 判定层（{'启用 doubao' if llm_chat else '降级(env缺/--no-llm)'}）...")
    for c in candidates:
        c.update(llm_judge(c, llm_chat, confirmed_examples))

    # 保留人确认状态 + 消费佐证 + verdict（不被重新生成覆盖，守红线5）
    for c in candidates:
        _key = (c.get("pattern", ""), c.get("disposition", ""))
        c["human_confirmed"] = existing_confirmed.get(_key, False)
        c["consumption_evidence"] = existing_consumption.get(_key, None)
        _verdict = existing_verdicts.get(_key)
        if _verdict:
            c["human_verdict"] = _verdict["human_verdict"]
            c["human_confirmed_at"] = _verdict["human_confirmed_at"]
        c["provenance"] = f"auto-extracted from {len(records)} experience records @ {now[:10]}"
        c["ts"] = now

    # ============================================
    # 5. M2 联动：提取 discard_pattern 种子
    # ============================================
    discard_notes = [r.get("note", "") + r.get("reason", "") for r in records if r.get("disposition") == "discard"]
    discard_text = " ".join(discard_notes)
    discard_words = re.findall(r'[一-龥]{2,6}', discard_text)
    discard_kw = Counter(w for w in discard_words if len(w) >= 2)
    print(f"\n# M2 种子：discard 理由关键词(top10，供认脸主题级去重)")
    for w, c in discard_kw.most_common(10):
        if c >= 3:
            print(f"    {w}: {c}次")

    # ============================================
    # 6. 输出
    # ============================================
    print(f"\n# 跨域同构模式识别结果：{len(candidates)}条(跨≥{min_sessions}session)")
    for p in candidates:
        confirmed = "✓" if p.get("human_confirmed") else "⚠未确认"
        cross = f"跨{p['cross_project_count']}project" if p['cross_project_count'] > 0 else ""
        if p.get("llm_judged"):
            llm_tag = f" LLM={'同构' if p.get('is_cross_domain') else '非同构'}(conf={p.get('llm_confidence', 0):.2f})"
        else:
            llm_tag = f" LLM降级({p.get('llm_reason', '')[:20]})"
        print(f"  [{confirmed}] {p['pattern_id']} {p['pattern']}/{p['disposition']} "
              f"session={p['session_count']} records={p['record_count']} {cross} "
              f"kw={p['common_keywords'][:3]}{llm_tag}")

    if dry_run:
        print(f"\n# --dry-run，未写入 {cross_domain_lib}")
    else:
        with open(cross_domain_lib, "w", encoding="utf-8") as f:
            for c in candidates:
                c.pop("_records", None)
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"\n# ✅ 跨域模式库已写入: {cross_domain_lib} ({len(candidates)}条)")
        n_judged = sum(1 for c in candidates if c.get("llm_judged"))
        n_cross = sum(1 for c in candidates if c.get("is_cross_domain"))
        print(f"# LLM 判定: {n_judged}/{len(candidates)} 成功, {n_cross} 条判为同构")
        print(f"# ⚠ human_confirmed 默认 false，人确认后才可用于消费环(守红线5)")

    sys.exit(0)
