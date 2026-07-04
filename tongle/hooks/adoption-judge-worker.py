#!/usr/bin/env python3
"""adoption-judge-worker.py — 阶段2b 连接点③异步 worker（Stop hook 启动）

读 .adoption-pending.jsonl 待判队列，对每条 (syn_file + 本轮CC输出) 调 doubao judge
判采纳率，记 kind=adoption verdict 到 reuse-log.jsonl，删 pending 已处理条。

不记 CC 输出到持久层（pending 是临时队列判完即清，reuse-log 只记 verdict）。
异步：Stop hook 用 Popen 启动本 worker 后立即 exit 0，不阻塞 CC。

judge 逻辑内联（与 runners/adoption-rate.py 同 prompt 同判据，避免改已验 runner）。
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

_home = os.path.expanduser("~")
INSTINCTS = os.path.join(_home, ".claude", "instincts")
PENDING = os.path.join(INSTINCTS, ".adoption-pending.jsonl")
REUSE_LOG = os.path.join(INSTINCTS, "reuse-log.jsonl")

_EVAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dev", "eval")
sys.path.insert(0, _EVAL)
sys.path.insert(0, os.path.join(_EVAL, "judges"))
try:
    from llm_client import chat
    # judge 固定 doubao 不读 env：env 漂 kimi-k2.7-code 致空输出/解析失败
    # （STATE 缺陷#5 验证铁证：5/5 verdict kimi，1 条 rate=-1 解析失败）
    # eval 离线 runner 保留 env 可控跑对比，生产 worker 必须固定
    MODEL_JUDGE = "doubao-seed-2.0-pro"
except Exception as e:
    # llm_client 不可用（env 缺）时静默退出，pending 保留待下次
    sys.exit(0)

SYN_LIMIT = 2500

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


def read_synthesis(syn_file):
    if not syn_file or not os.path.exists(syn_file):
        return None
    return open(syn_file, encoding="utf-8").read()[:SYN_LIMIT]


def judge_adoption(question, synthesis, ans_b):
    user = (f"用户问题：{question}\n\n"
            f"=== 注入的知识摘要 (synthesis) ===\n{synthesis}\n\n"
            f"=== AI 的回答 ===\n{ans_b}\n\n"
            f"请判断回答采纳了 synthesis 的哪些关键概念。")
    raw = chat(MODEL_JUDGE, ADOPTION_PROMPT, user, max_tokens=2048)
    m = re.search(r'\{.*\}', raw, re.S)
    if not m:
        return {"adopted": -1, "total": -1, "rate": -1, "reason": "judge 解析失败"}
    try:
        d = json.loads(m.group(0))
        adopted = int(d.get("adopted", -1))
        total = int(d.get("total", -1))
        rate = round(adopted / total, 2) if (total and total > 0) else 0
        return {"adopted": adopted, "total": total, "rate": rate, "reason": d.get("reason", "")}
    except Exception:
        return {"adopted": -1, "total": -1, "rate": -1, "reason": "json 解析失败"}


def detect_expected_project():
    """从 working-memory 焦点推断本该注入的项目（错配标注用，B 防御层）。
    复用 build-asset-manifest 的 detect_wm_project_focus，importlib 动态加载
    （文件名带连字符不能常规 import）。失败返回 [] 不阻断。"""
    try:
        import importlib.util
        _hook_dir = os.path.dirname(os.path.abspath(__file__))
        _spec = importlib.util.spec_from_file_location(
            "build_asset_manifest", os.path.join(_hook_dir, "build-asset-manifest.py"))
        _bam = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_bam)
        domains, *_ = _bam.parse_vocab()
        if not domains:
            return []
        pkm = {d['name']: d['keywords'] for d in domains}
        pnames = [d['name'] for d in domains]
        return _bam.detect_wm_project_focus(pnames, pkm)
    except Exception:
        return []


def main():
    if not os.path.exists(PENDING):
        sys.exit(0)
    # env 漂移检测（防御性）：env 非 doubao 时记 log 供排查，worker 已硬编 doubao 覆盖不受影响
    _env_judge = os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "")
    if _env_judge and _env_judge != MODEL_JUDGE:
        print(f"[adoption-judge-worker] env ANTHROPIC_DEFAULT_OPUS_MODEL={_env_judge} 漂移，worker 已硬编 {MODEL_JUDGE} 覆盖", file=sys.stderr)
    # 算 expected_project（从 working-memory 焦点推断，错配标注用，B 防御层）
    expected_project = detect_expected_project()
    # 读全部 pending
    with open(PENDING, encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]
    if not lines:
        sys.exit(0)

    remaining = []
    processed = 0
    for line in lines:
        try:
            task = json.loads(line)
        except Exception:
            continue  # 坏行跳过
        syn_file = task.get("syn_file")
        output = task.get("output_text", "")
        question = task.get("question", "")
        session = task.get("session", "unknown")
        project = task.get("syn_project", "")

        syn = read_synthesis(syn_file)
        if not syn or not output:
            # syn 读不了或无输出，跳过不重试
            processed += 1
            continue

        try:
            v = judge_adoption(question, syn, output)
        except Exception:
            # judge 调用失败，保留待下次重试
            remaining.append(line)
            continue

        # 记 verdict 到 reuse-log（kind=adoption，只记 verdict 不记输出）
        # ⚠️ verdict 是 LLM-judge 判的非真值：单 judge（doubao）有偏差风险（pp-009 模型随机性
        # 致 0% 采纳即实例）。下游消费（collector adoption_hint/maintenance-guard 触发/adoption-rate
        # insight_judge）需留余量+人工抽检，judge_model 字段供追溯可信度（诚实性审查 C1）
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session": session, "kind": "adoption",
            "project": project, "syn_file": syn_file,
            "adopted": v["adopted"], "total": v["total"], "rate": v["rate"],
            "reason": v["reason"],
            "judge_model": MODEL_JUDGE,
            "expected_project": expected_project,  # 从 working-memory 焦点推断，错配标注（B 防御层）
        }
        try:
            with open(REUSE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        processed += 1

    # 重写 pending（只留 judge 失败待重试的）
    try:
        with open(PENDING, "w", encoding="utf-8") as f:
            for l in remaining:
                f.write(l)
    except OSError:
        pass


if __name__ == "__main__":
    main()
