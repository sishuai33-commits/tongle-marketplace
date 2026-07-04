#!/usr/bin/env python3
"""eval①注入增益 runner — 对比 all vs access 注入策略的增益。

被测口径（指挥官拍板）：清单 + 匹配 synthesis 内容注入。
  A组 = 裸跑（无注入）
  B组 = 注入（build-asset-manifest 资产路由清单 + 按痛点匹配的 synthesis.md 片段）
  judge 盲评 B vs A，gain_score 高 = 注入有效。
  对比 B_all vs B_access 增益 → 哪个策略注入质量最高。

跑法：离线/脚本触发/不干扰。结果落 eval/results/。
"""
import json
import os
import sys
import subprocess
import time
import argparse

_RUNNER = os.path.dirname(os.path.abspath(__file__))      # ke/dev/eval/runners
_EVAL = os.path.dirname(_RUNNER)                            # ke/dev/eval
_DEV = os.path.dirname(_EVAL)                              # ke/dev（dev/重组后多一层）
_KE = os.path.dirname(_DEV)                                # ke 项目根
sys.path.insert(0, _EVAL)
sys.path.insert(0, os.path.join(_EVAL, "judges"))

from llm_client import chat, MODEL_SUBJECT
from llm_judge import judge

BUILD_MANIFEST = os.path.join(_KE, "hooks", "build-asset-manifest.py")
WIKI_PROJECTS = os.path.expanduser("~/Documents/Obsidian Vault/wiki/projects")
DATASETS = os.path.join(_EVAL, "datasets")
RESULTS = os.path.join(_EVAL, "results")

# 对比的注入策略（MVP 跑 all vs access，回答"注入哪些质量最高"）
STRATEGIES = ["all", "access"]
SYN_LIMIT = 2500   # 每个匹配 synthesis 截断字符数（控 prompt 长度）
SCN_LIMIT = 4000   # 场景页截断字符数（场景细节粒度，略宽于 synthesis）
SYN_HEAD_FOR_SCENE = 500  # 分层注入场景页时，synthesis 只取开头定位段作背景

# 痛点问"场景细节"的判别关键词（命中任一=场景细节型，优先注入 scenarios）
SCENE_DETAIL_KEYWORDS = [
    "参数", "数据", "指标", "怎么算", "具体", "场景", "操作", "步骤",
    "技术细节", "做了什么", "排查", "补全", "原始数据", "工作", "流程",
]


def clean_wikilink(text):
    """清洗 Obsidian wikilink/block-id，防被测模型形态诱导（pp-004 铁证）。

    glm 见 [[scenarios/找地选楼]] 会把链接翻译成"读取动作"→输出伪 read_link
    工具调用文本而非回答→judge 判 sb=0。清洗只动注入文本，不改源文件。
      [[x]]         → x              （取链接目标文件名，去路径前缀）
      [[a|b]]       → b              （别名语法取显示名 b）
      [[x#锚点]]    → x              （去锚点）
      ^block-id     → （删除行末块引用）
    """
    import re

    def _repl(m):
        inner = m.group(1)
        if "|" in inner:          # [[a|b]] → b（显示名）
            return inner.split("|", 1)[1].strip()
        inner = inner.split("#", 1)[0]   # [[x#锚点]] → x
        return os.path.basename(inner).strip()

    text = re.sub(r"\[\[([^\]]+)\]\]", _repl, text)
    text = re.sub(r"\s*\^[a-zA-Z0-9_-]+\s*$", "", text, flags=re.MULTILINE)  # ^block-id
    return text


def select_scenarios(scenarios_dir, question):
    """按文件名与 question 的子串相关度选 top1-2 场景页（离线 runner 用规则，非 LLM 判断）。

    中文场景页文件名是有意义词组（如"找地选楼"），用子串包含判相关度最直接，
    不依赖分词（中文无词边界，token 切分会把整句切成大块丢掉子串信号）。
    无相关页时返回空（让调用方回退 synthesis，避免过注入全部场景页）。
    返回 [(filename_no_ext, content)] 列表，content 已截断+清洗。
    """
    if not os.path.isdir(scenarios_dir):
        return []
    files = sorted(f for f in os.listdir(scenarios_dir) if f.endswith(".md"))
    if not files:
        return []
    scored = []
    for f in files:
        fname = f[:-3]  # 去 .md
        # 双向子串判：文件名整体出现在 question，或 question 较长片段出现在文件名
        score = 2 if fname in question else 0
        if score == 0:
            # 退化：question 里任一>=3字片段出现在文件名（抓"防汛"命中"苏州河防汛"）
            for i in range(len(question) - 2):
                frag = question[i:i + 3]
                if frag in fname:
                    score = 1
                    break
        scored.append((score, f))
    scored.sort(key=lambda x: x[0], reverse=True)
    chosen = [f for s, f in scored[:2] if s > 0]
    out = []
    for f in chosen:
        path = os.path.join(scenarios_dir, f)
        txt = open(path, encoding="utf-8").read()[:SCN_LIMIT]
        out.append((f[:-3], clean_wikilink(txt)))
    return out


def load_painpoints():
    path = os.path.join(DATASETS, "painpoints.jsonl")
    return [json.loads(l) for l in open(path) if l.strip()]


def get_manifest(strategy):
    """调 build-asset-manifest 拿注入清单（前置参数化的消费方调用）。"""
    cmd = ["python3", BUILD_MANIFEST, "--strategy", strategy]
    if strategy == "access":
        cmd += ["--limit", "3"]
    return subprocess.check_output(cmd, text=True).strip()


def match_knowledge(question, manifest_text, expected_project=None):
    """从清单匹配痛点 → 按内容类型分层注入 synthesis 或 scenarios（解 pp-002/pp-004）。

    三级匹配：① expected_project 字段直接命中（最准，痛点设计时指定）
              ② 项目名整体出现在 question
              ③ build-asset-manifest 的项目关键词出现在 question（模拟CC见关键词→读）

    分层注入（task1.3，"清单常驻+synthesis按需"分层假设的工程化）：
      痛点问场景细节（命中 SCENE_DETAIL_KEYWORDS）且该项目有 scenarios/ →
          注入最相关场景页 + synthesis 开头定位段作背景（粒度对齐，防过注入）
      否则 → 注入 synthesis.md 现状（架构层/方向型痛点 + 无 scenarios 项目回退）

    所有注入文本经 clean_wikilink 清洗（防 pp-004 形态诱导）。
    返回 (knowledge_text, [matched_projects])。
    注：access 策略只注入热度前3，若痛点对应项目不在前3则匹配不到——
    这正是 access 策略对非热门项目痛点的真实劣势，属设计要测的。
    """
    import re
    proj_kw = {}
    for line in manifest_text.split('\n'):
        # 适配 L0 精简后格式（去status/去"—"分隔符，任务1.2连带修复）：
        #   P0 · 名称 [轻量] (更新:..) [#关联:..] #关键词:..
        pm = re.match(r'^\s+P[012]\s+·\s+(.+?)\s+(?:\[轻量\]\s+)?\(更新:', line)
        if not pm or '#关键词:' not in line:
            continue
        pname = pm.group(1).strip()
        km = re.search(r'#关键词:(.+?)\s*$', line)
        kws = km.group(1).strip().split() if km else []
        proj_kw[pname] = kws

    is_scene_detail = any(kw in question for kw in SCENE_DETAIL_KEYWORDS)
    matched, blocks = [], []
    for pname, kws in proj_kw.items():
        hit = (expected_project and pname == expected_project) or \
              pname in question or \
              any(k in question for k in kws if len(k) >= 2)
        if not hit:
            continue
        syn = os.path.join(WIKI_PROJECTS, pname, "synthesis.md")
        if not os.path.exists(syn):
            continue
        matched.append(pname)
        syn_full = open(syn, encoding="utf-8").read()
        syn_clean = clean_wikilink(syn_full)
        scn_dir = os.path.join(WIKI_PROJECTS, pname, "scenarios")
        # 分层注入：场景细节型 + 有 scenarios/ → 场景页为主 + synthesis 定位段背景
        if is_scene_detail and os.path.isdir(scn_dir) and os.listdir(scn_dir):
            scns = select_scenarios(scn_dir, question)
            if scns:
                syn_head = syn_clean[:SYN_HEAD_FOR_SCENE]
                scn_block = "\n\n".join(f"## {pname}/scenarios/{name}.md\n{txt}"
                                        for name, txt in scns)
                blocks.append(f"## {pname}/synthesis.md（项目定位背景）\n{syn_head}\n\n{scn_block}")
                continue
        # 默认：注入 synthesis 现状（截断+清洗）
        blocks.append(f"## {pname}/synthesis.md\n{syn_clean[:SYN_LIMIT]}")
    return "\n\n".join(blocks), matched


def inject_prompt(manifest, synthesis):
    s = "以下是你的知识库注入（资产路由清单 + 相关知识摘要），回答时优先据此：\n\n"
    s += "=== 资产路由清单 ===\n" + clean_wikilink(manifest) + "\n\n"
    s += "=== 匹配知识 ===\n" + (synthesis or "(本痛点未匹配到项目 synthesis)")
    return s


def run_one(pp, manifest_cache):
    q = pp["question"]
    res = {"id": pp["id"], "project": pp.get("project"), "question": q}
    res["ans_a"] = chat(MODEL_SUBJECT, None, q)   # A组裸跑
    print(f"  A组(裸跑) 完成, {len(res['ans_a'])}字")
    for strat in STRATEGIES:
        manifest = manifest_cache[strat]
        syn, matched = match_knowledge(q, manifest, pp.get("project"))
        res[f"matched_{strat}"] = matched
        ans_b = chat(MODEL_SUBJECT, inject_prompt(manifest, syn), q)
        verdict = judge(q, res["ans_a"], ans_b)
        res[f"ans_b_{strat}"] = ans_b
        res[f"judge_{strat}"] = verdict
        print(f"  B[{strat}] 匹配={matched} winner={verdict.get('winner')} gain={verdict.get('gain_score')}")
    return res


def main():
    ap = argparse.ArgumentParser(description="eval①注入增益 runner")
    ap.add_argument("--limit", type=int, default=0, help="只跑前N个痛点(0=全量)，快速验证用")
    ap.add_argument("--only", default=None, help="只跑指定id的痛点(如 pp-004)，复用旧A回答省时")
    args = ap.parse_args()
    pps = load_painpoints()
    if args.only:
        pps = [p for p in pps if p["id"] == args.only]
    elif args.limit:
        pps = pps[:args.limit]
    print(f"加载 {len(pps)} 痛点 | 被测={MODEL_SUBJECT}")
    manifest_cache = {s: get_manifest(s) for s in STRATEGIES}
    print(f"清单：all={len(manifest_cache['all'])}字 access={len(manifest_cache['access'])}字")

    results = []
    for pp in pps:
        print(f"\n[{pp['id']}] {pp['project']}")
        results.append(run_one(pp, manifest_cache))

    # 聚合：每策略平均增益（gain=score_b-score_a，正=注入有效，负=有害）
    summary = {}
    for s in STRATEGIES:
        valid = [r[f"judge_{s}"] for r in results if r[f"judge_{s}"]["gain_score"] > -99]
        n = len(valid)
        gains = [v["gain_score"] for v in valid]
        sb = [v["score_b"] for v in valid]
        sa = [v["score_a"] for v in valid]
        wins = sum(1 for v in valid if v["winner"] == "B")
        summary[s] = {
            "avg_gain": round(sum(gains) / n, 2) if n else 0,
            "avg_score_b": round(sum(sb) / n, 2) if n else 0,   # 注入组均分
            "avg_score_a": round(sum(sa) / n, 2) if n else 0,   # 裸跑组均分
            "b_wins": wins,
            "n": n,
        }

    print("\n" + "=" * 60)
    print("增益对比（B注入 vs A裸跑，gain=score_b-score_a，正=注入有效）")
    print("=" * 60)
    for s, v in summary.items():
        print(f"  策略 {s:8s}: 增益 {v['avg_gain']:>+6}  "
              f"注入分 {v['avg_score_b']:>4}/10  裸跑分 {v['avg_score_a']:>4}/10  "
              f"B胜 {v['b_wins']}/{v['n']}")
    best = max(summary, key=lambda s: summary[s]["avg_gain"])
    print(f"\n  → 质量最高注入策略: {best}")

    os.makedirs(RESULTS, exist_ok=True)
    out = os.path.join(RESULTS, f"gain-{time.strftime('%Y%m%d-%H%M%S')}.json")
    json.dump({"summary": summary, "best": best, "details": results},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n结果已存 {out}")


if __name__ == "__main__":
    main()
