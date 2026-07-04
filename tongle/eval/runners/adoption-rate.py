#!/usr/bin/env python3
"""eval②采纳率 runner (Level 4 阶段2a 离线 MVP) — 判 B 回答对注入 synthesis 的采纳率。

复用 eval① results JSON（已有 B 回答 + matched project），不新跑被测模型。
按 matched project 重读 synthesis → judge 判 B 回答采纳了 synthesis 的哪些关键概念。
采纳率 = 采纳概念数 / 总概念数。低采纳率 = 信息容器（注入了没用）。

⚠️ 本 runner 是阶段2a 离线判据验证，不读 reuse-log.jsonl——
连接点③真消费方（读 reuse-log 配对真实对话 CC 输出）留阶段2b，需扩 Stop hook
捕获 CC 输出（侵入真实对话，待指挥官定）。

跑法：
  python3 dev/eval/runners/adoption-rate.py                  # 用最新 results 全量
  python3 dev/eval/runners/adoption-rate.py --limit 2         # 快速验证
  python3 dev/eval/runners/adoption-rate.py --results <path>  # 指定 results
  ANTHROPIC_DEFAULT_OPUS_MODEL=doubao-seed-2.0-pro python3 -u dev/eval/runners/adoption-rate.py
  （judge 必须 doubao，同 eval①：kimi-k2.7-code 等 code 模型盲评空输出）
"""
import json
import os
import sys
import glob
import time
import argparse

_RUNNER = os.path.dirname(os.path.abspath(__file__))      # ke/dev/eval/runners
_EVAL = os.path.dirname(_RUNNER)                            # ke/dev/eval
sys.path.insert(0, _EVAL)
sys.path.insert(0, os.path.join(_EVAL, "judges"))

from llm_client import chat, MODEL_JUDGE

WIKI_PROJECTS = os.path.expanduser("~/Documents/Obsidian Vault/wiki/projects")
RESULTS = os.path.join(_EVAL, "results")
SYN_LIMIT = 2500   # 同 eval①，控 prompt 长度

# judge 一步做：从 synthesis 提取关键概念 + 判 B 回答采纳几个
ADOPTION_PROMPT = """你是知识采纳评测员。给你：用户问题、注入的知识摘要（synthesis）、AI 的回答。

任务：判断 AI 回答实际采纳了注入知识的哪些关键概念。

步骤：
1. 从注入的 synthesis 提取关键概念（3-6 个，是该知识的核心要点，非"系统/数据/模型"这类通用词）
2. 逐个判断这些概念是否在回答中被实质性采纳：
   - 采纳 = 回答用了该概念的内容做分析/结论（用自己的话组织但内容源自该概念 也算）
   - 不采纳 = 仅复述概念名但没用其内容 / 完全没提 / 提了但理解错误
3. 采纳率 = 采纳数 / 总概念数

判据要点：
- "注入了但没用" = 信息容器（仅复述名字或完全忽略）→ 不算采纳
- "用概念内容做了分析" = 真知识采纳 → 算采纳
- 回答若声明"细节未加载/需自行检索"则该概念未采纳（被框住没真用）

先简短分析（2-3 句），然后严格按此 JSON 输出（只输出 JSON）：
{"concepts":[{"name":"概念名","adopted":true或false}],"adopted":采纳数,"total":总概念数,"reason":"一句理由"}
"""


def latest_results():
    files = sorted(glob.glob(os.path.join(RESULTS, "gain-*.json")))
    if not files:
        raise SystemExit("无 eval① results，先跑 inject-gain.py")
    return files[-1]


def read_synthesis(project):
    """按项目名读 synthesis 片段（matched=[] 返回 None）。"""
    if not project:
        return None
    syn = os.path.join(WIKI_PROJECTS, project, "synthesis.md")
    if not os.path.exists(syn):
        return None
    return open(syn, encoding="utf-8").read()[:SYN_LIMIT]


def judge_adoption(question, synthesis, ans_b):
    """判 B 回答对 synthesis 的采纳率。返回 {adopted, total, rate, reason, concepts}。"""
    user = (f"用户问题：{question}\n\n"
            f"=== 注入的知识摘要 (synthesis) ===\n{synthesis}\n\n"
            f"=== AI 的回答 ===\n{ans_b}\n\n"
            f"请判断回答采纳了 synthesis 的哪些关键概念。")
    raw = chat(MODEL_JUDGE, ADOPTION_PROMPT, user, max_tokens=2048)
    import re
    m = re.search(r'\{.*\}', raw, re.S)
    if not m:
        return {"adopted": -1, "total": -1, "rate": -1, "reason": "judge 解析失败",
                "concepts": [], "raw": raw[:200]}
    try:
        d = json.loads(m.group(0))
        adopted = int(d.get("adopted", -1))
        total = int(d.get("total", -1))
        rate = round(adopted / total, 2) if (total and total > 0) else 0
        return {"adopted": adopted, "total": total, "rate": rate,
                "reason": d.get("reason", ""), "concepts": d.get("concepts", [])}
    except Exception:
        return {"adopted": -1, "total": -1, "rate": -1, "reason": "json 解析失败",
                "concepts": [], "raw": raw[:200]}


def main():
    ap = argparse.ArgumentParser(description="eval②采纳率 runner")
    ap.add_argument("--limit", type=int, default=0, help="只跑前N个痛点(0=全量)")
    ap.add_argument("--results", default=None, help="指定 eval① results JSON 路径")
    ap.add_argument("--mode", default="offline", choices=["offline", "reuse-log"],
                    help="offline=阶段2a复用eval①results判采纳率; "
                         "reuse-log=阶段2b读reuse-log kind=adoption verdict做真实对话回灌分析")
    args = ap.parse_args()

    if args.mode == "reuse-log":
        return run_reuse_log_mode()

    rpath = args.results or latest_results()
    data = json.load(open(rpath))
    details = data["details"]
    if args.limit:
        details = details[:args.limit]
    print(f"复用 results: {os.path.basename(rpath)} | {len(details)} 痛点 | judge={MODEL_JUDGE}")
    if "kimi" in MODEL_JUDGE.lower() or "code" in MODEL_JUDGE.lower():
        print("⚠️ 警告：judge 疑似 code 模型，可能空输出。建议 ANTHROPIC_DEFAULT_OPUS_MODEL=doubao-seed-2.0-pro")

    results = []
    for r in details:
        pid, proj, q = r["id"], r.get("project"), r["question"]
        print(f"\n[{pid}] {proj}")
        entry = {"id": pid, "project": proj, "question": q}
        for strat in ["all", "access"]:
            matched = r.get(f"matched_{strat}", [])
            ans_b = r[f"ans_b_{strat}"]
            syn = read_synthesis(matched[0]) if matched else None
            if syn is None:
                # access 策略对非热门项目 matched=[]（只注入清单无 synthesis），②采纳率无 synthesis 可判
                entry[f"adoption_{strat}"] = {"rate": None, "reason": "无 synthesis 注入(matched=[]只清单)",
                                              "matched": matched}
                print(f"  [{strat}] matched={matched} → 无 synthesis, 跳过")
                continue
            v = judge_adoption(q, syn, ans_b)
            v["matched"] = matched
            entry[f"adoption_{strat}"] = v
            print(f"  [{strat}] matched={matched} 采纳 {v['adopted']}/{v['total']} = {v['rate']}  ({v['reason'][:40]})")
        results.append(entry)

    # 聚合：只算有 synthesis 的（rate 非 None）
    summary = {}
    for s in ["all", "access"]:
        rates = [r[f"adoption_{s}"]["rate"] for r in results
                 if r[f"adoption_{s}"].get("rate") is not None and r[f"adoption_{s}"]["rate"] >= 0]
        n = len(rates)
        summary[s] = {
            "avg_adoption": round(sum(rates) / n, 2) if n else 0,
            "n_with_synthesis": n,
            "n_total": len(results),
        }

    print("\n" + "=" * 60)
    print("采纳率（B 回答对注入 synthesis 的采纳，低=信息容器废）")
    print("=" * 60)
    for s, v in summary.items():
        print(f"  策略 {s:8s}: 平均采纳率 {v['avg_adoption']:>5}  "
              f"({v['n_with_synthesis']}/{v['n_total']} 痛点有 synthesis 注入)")
    print(f"\n  → 低采纳率痛点 = synthesis 注入了但没用 = 信息容器候选")

    out = os.path.join(RESULTS, f"adoption-{time.strftime('%Y%m%d-%H%M%S')}.json")
    json.dump({"summary": summary, "source_results": os.path.basename(rpath),
               "details": results}, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n结果已存 {out}")


def run_reuse_log_mode():
    """阶段2b：读 reuse-log.jsonl 的 kind=adoption verdict（Stop hook 在线判的真实对话采纳率），
    做真实对话回灌分析。连接点③真消费方 + 连接点④路2（消费→加工）判据接口。

    连接点④路2（跨域模式库回灌）：范式"增智判据——跨域同构被消费环真实复用过才算真洞察，
    否则伪洞察，判据转嫁消费环"。本函数输出 insight_judge 字段（按采纳率高/低分组）作为
    增智判据接口。⚠️ 消费方跨域模式库当前不存在（L3 守红线②缓做，范式表标"新概念不存在"），
    此为判据接口预留——等跨域模式库建立时消费（高采纳=真洞察/低采纳=伪洞察），不强建库。
    verdict 现为 project 级非模式级，模式级判据留远期（同 collector adoption_signal 守最小）。
    ⚠️ verdict 是 LLM-judge 判的非真值（单 judge 偏差风险），insight_judge 是人审参考非自动裁决。
    """
    rlog = os.path.expanduser("~/.claude/instincts/reuse-log.jsonl")
    if not os.path.exists(rlog):
        print("reuse-log.jsonl 不存在，先有真实对话读 synthesis 触发 Stop hook")
        return
    verdicts = []
    with open(rlog, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("kind") == "adoption":
                verdicts.append(e)
    if not verdicts:
        print(f"reuse-log 无 kind=adoption verdict（{len(open(rlog).readlines())} 条总记录）")
        print("阶段2b Stop hook 真执行需新会话触发（hook 会话级加载）")
        return

    rates = [v["rate"] for v in verdicts if v.get("rate", -1) >= 0]
    n = len(rates)
    print(f"reuse-log kind=adoption verdict: {len(verdicts)} 条 | 有效 {n} 条")
    print("=" * 60)
    print("真实对话采纳率（Stop hook 在线判，连接点③真消费方）")
    print("=" * 60)
    for v in verdicts:
        print(f"  [{v.get('ts','')[:16]}] {v.get('project',''):12} "
              f"采纳 {v.get('adopted')}/{v.get('total')}={v.get('rate')}  {v.get('reason','')[:40]}")
    if n:
        print(f"\n  平均采纳率: {round(sum(rates)/n, 2)} ({n} 条真实对话)")
    print(f"\n  → 低采纳率 = synthesis 注入了但 CC 没真用 = 信息容器候选（喂慢环降级信号）")

    # 连接点④路2：增智判据接口（高/低采纳分组）。消费方跨域模式库待建，此为接口预留
    # 阈值 0.5 = 真洞察/伪洞察中性分界（区别于 collector 路1 的 0.3 低采纳hint阈值）
    INSIGHT_THRESHOLD = 0.5
    proj_rate = {}
    for v in verdicts:
        p, r = v.get("project"), v.get("rate", -1)
        if p and r >= 0:
            proj_rate.setdefault(p, []).append(r)
    proj_avg = {p: round(sum(rs)/len(rs), 2) for p, rs in proj_rate.items()}
    insight_judge = {
        "high_adoption": [p for p, r in proj_avg.items() if r >= INSIGHT_THRESHOLD],
        "low_adoption": [p for p, r in proj_avg.items() if r < INSIGHT_THRESHOLD],
        "project_avg": proj_avg,
        "note": "增智判据接口（范式：高采纳=真洞察/低采纳=伪洞察）。消费方跨域模式库待建（L3守红线②缓做），此为接口预留",
    }
    print(f"\n  [连接点④路2 增智判据接口] 高采纳(真洞察候选): {insight_judge['high_adoption']} | "
          f"低采纳(伪洞察候选): {insight_judge['low_adoption']}")
    print(f"  ⚠️ 消费方跨域模式库待建，接口预留（守红线②不强建库）")

    out = os.path.join(RESULTS, f"adoption-reuse-{time.strftime('%Y%m%d-%H%M%S')}.json")
    json.dump({"verdicts": verdicts, "avg": round(sum(rates)/n, 2) if n else 0,
               "insight_judge": insight_judge},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n结果已存 {out}")


if __name__ == "__main__":
    main()
