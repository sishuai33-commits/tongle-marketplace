#!/usr/bin/env python3
"""lib/adoption.py — 消费环采纳判定（连接点③真消费方）

从 hooks/reuse-adoption-judge.py + hooks/adoption-judge-worker.py 提取（Step 3.5）。
逻辑在本文，IO 在 lib/state.py，路径在 lib/paths.py，manifest 在 lib/manifest.py。
reuse-adoption-judge.py / adoption-judge-worker.py 改薄壳调本模块。

架构范式 §四消费环 L1 + §四加工环增智判据 L3：
  注入≠复用（公理1反模式②）—— CC 真读了 synthesis 才算消费证据。

两个入口：
  hook_main() — Stop hook：检测本轮 syn 读取 + 跨域消费回写 → 写 pending → nohup worker
  main()      — 异步 worker：读 pending → LLM judge → 写 verdict 到 reuse-log

守"不干扰"硬约束：hook 立即 exit 0，worker 异步 nohup 不阻塞 CC。
judge 固定 doubao 不读 env（env 漂 kimi 致空输出，STATE 缺陷#5 铁证）。

## cross-domain 消费证据回写（A8重审修复）
范式§四L86"跨域同构被消费环真实复用过才算真洞察"——consumption_evidence 是增智判据。
hook_main 在 Stop 时检测：本会话 assistant 输出是否命中 cross-domain-patterns.jsonl 中
human_verdict=confirm 且 consumption_evidence=null 模式的 common_keywords，命中则回写
（让判据真卡死，守红线⑥）。判据层级：keyword match < LLM 判定，运行态验证后若太弱再升级。

关联：architecture-paradigm §四消费环复用日志 + §四加工环增智判据（连接点3）
"""
import sys
import os
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from lib import state, paths, manifest

# === 路径常量（与原脚本一致保测试兼容）===
INSTINCTS = paths.instincts_dir()
PENDING = os.path.join(INSTINCTS, ".adoption-pending.jsonl")
REUSE_LOG = paths.instincts_file("reuse-log.jsonl")
CROSS_DOMAIN_LIB = os.path.join(INSTINCTS, "cross-domain-patterns.jsonl")
# worker 入口 = lib/adoption.main()，Step 4 删 adoption-judge-worker.py 壳后改 -m 启动
LOG = os.path.join(INSTINCTS, ".adoption-judge.log")

OUTPUT_LIMIT = 4000  # 本轮输出截断（控 pending 体积 + judge prompt 长度）

# === LLM judge（worker 用，hook 不用）===
_EVAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dev", "eval")
sys.path.insert(0, _EVAL)
sys.path.insert(0, os.path.join(_EVAL, "judges"))
try:
    from llm_client import chat
    # judge 固定 doubao 不读 env：env 漂 kimi-k2.7-code 致空输出/解析失败
    # （STATE 缺陷#5 验证铁证：5/5 verdict kimi，1 条 rate=-1 解析失败）
    # eval 离线 runner 保留 env 可控跑对比，生产 worker 必须固定
    MODEL_JUDGE = "doubao-seed-2.0-pro"
except Exception:
    # llm_client 不可用（env 缺）时静默，worker_main 检查 chat is None 早退
    chat = None
    MODEL_JUDGE = None

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


# === hook 函数（reuse-adoption-judge 入口）===

def _read_transcript_texts(transcript_path):
    """一次遍历 transcript，返回 (assistant_texts 列表, last_user_text)。

    复用于 get_last_assistant_text（取最后一条 assistant）+
    check_cross_domain_consumption（取全部 assistant 拼接），避免两处重复遍历。
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return [], None
    assistant_texts = []
    last_user = None
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                etype = entry.get("type")
                msg = entry.get("message", {})
                content = msg.get("content", [])
                if etype == "user":
                    # user content 可能是 str 或 list
                    if isinstance(content, str):
                        if content.strip():
                            last_user = content
                    elif isinstance(content, list):
                        parts = [b.get("text", "") for b in content
                                 if isinstance(b, dict) and b.get("type") == "text"]
                        t = "\n".join(x for x in parts if x)
                        if t.strip():
                            last_user = t
                elif etype == "assistant" and isinstance(content, list):
                    parts = [b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text"]
                    t = "\n".join(x for x in parts if x)
                    if t.strip():
                        assistant_texts.append(t)
    except OSError:
        return [], None
    return assistant_texts, last_user


def get_last_assistant_text(transcript_path):
    """读 transcript 最后一条 assistant 消息的文本输出 + 最后一条 user 消息。
    委托 _read_transcript_texts 一次遍历（与 check_cross_domain_consumption 共用）。"""
    assistant_texts, last_user = _read_transcript_texts(transcript_path)
    last_text = assistant_texts[-1] if assistant_texts else None
    return last_text, last_user


def get_recent_syn(session_id):
    """查 reuse-log 本 session 最近一条 ok 的 kind=synthesis。"""
    if not os.path.exists(REUSE_LOG):
        return None
    recent = None
    try:
        with open(REUSE_LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if (e.get("kind") == "synthesis" and e.get("ok")
                        and e.get("session") == session_id):
                    recent = e
    except OSError:
        return None
    return recent


def check_cross_domain_consumption(session, transcript_path):
    """检测本会话是否消费了 confirmed 跨域模式（keyword match 级，A8重审修复）。

    扫描 transcript assistant 输出，若命中 cross-domain-patterns.jsonl 中
    human_verdict=confirm 且 consumption_evidence=null 模式的 common_keywords，
    回写 consumption_evidence（让增智判据真卡死，守红线⑥）。

    判据层级：keyword match（CC 输出出现模式关键词=引用层消费证据），非 LLM judge。
    独立于 syn 检测，有 confirmed+null 模式才扫描，避免无谓 transcript 遍历。
    """
    if not os.path.exists(CROSS_DOMAIN_LIB):
        return
    # 读 confirmed 且 consumption_evidence=null 的模式
    patterns_to_check = []
    try:
        with open(CROSS_DOMAIN_LIB, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                    if (p.get("human_verdict") == "confirm"
                            and p.get("consumption_evidence") is None):
                        patterns_to_check.append(p)
                except Exception:
                    continue
    except OSError:
        return
    if not patterns_to_check:
        return  # 无待检测模式，早退避免扫描 transcript

    # 扫描 transcript 拿全部 assistant 输出（委托 _read_transcript_texts 避免重复遍历）
    assistant_texts, _ = _read_transcript_texts(transcript_path)
    if not assistant_texts:
        return
    full_output = "\n".join(assistant_texts)

    # 检查每个模式是否被命中（keyword substring match，过滤 len<2 避免误判）
    hit_map = {}  # pattern_id -> matched_keywords
    for p in patterns_to_check:
        keywords = p.get("common_keywords", [])
        matched = [kw for kw in keywords
                   if isinstance(kw, str) and len(kw) >= 2 and kw in full_output]
        if matched:
            hit_map[p.get("pattern_id")] = matched
    if not hit_map:
        return  # 无命中

    # 回写 consumption_evidence（原子写：读全部→更新命中行→临时文件→rename）
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with open(CROSS_DOMAIN_LIB, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    updated = []
    for line in lines:
        raw = line.strip()
        if not raw:
            updated.append("")
            continue
        try:
            p = json.loads(raw)
        except Exception:
            updated.append(raw)
            continue
        pid = p.get("pattern_id")
        if pid in hit_map:
            p["consumption_evidence"] = {
                "consumption_ts": ts,
                "consumption_session": session,
                "matched_keywords": hit_map[pid],
                "access_type": "keyword_match",
            }
        updated.append(json.dumps(p, ensure_ascii=False))
    tmp = str(CROSS_DOMAIN_LIB) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for line in updated:
                f.write(line + "\n")
        os.replace(tmp, CROSS_DOMAIN_LIB)
    except OSError:
        pass


def hook_main():
    """Stop hook 入口：cross-domain 消费检测 + syn 检测 + 写 pending + nohup worker。"""
    raw = sys.stdin.read()
    d = json.loads(raw) if raw.strip() else {}
    transcript = d.get("transcript_path")
    session = d.get("session_id", os.environ.get("CLAUDE_SESSION_ID", "unknown"))

    # cross-domain 消费证据检测（独立于 syn，有 confirmed+null 模式才扫描 transcript）
    # A8重审修复：让 consumption_evidence 有写入方，守红线⑥增智判据卡死
    try:
        check_cross_domain_consumption(session, transcript)
    except Exception:
        pass  # 消费证据回写失败不阻塞 adoption 判定

    # 先快查 syn（reuse-log 小，遍历快）；本轮未读 syn 则早退，不解析大 transcript
    syn = get_recent_syn(session)
    if not syn:
        sys.exit(0)

    last_text, last_user = get_last_assistant_text(transcript)
    if not last_text:
        sys.exit(0)  # 本轮无输出，不判

    # 写 pending 临时队列（不记输出到持久层，worker 判完即清）
    task = {
        "syn_file": syn.get("file"),
        "syn_project": syn.get("project"),
        "session": session,
        "question": (last_user or "")[:500],  # 最后一条 user 消息作 question 上下文
        "output_text": last_text[:OUTPUT_LIMIT],
    }
    try:
        os.makedirs(INSTINCTS, exist_ok=True)
        with open(PENDING, "a", encoding="utf-8") as f:
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
    except OSError:
        sys.exit(0)

    # 异步启动 worker（start_new_session=True 脱离 hook 进程组，hook 退出后存活）
    # worker 入口 = lib/adoption.main()（Step 4 删 adoption-judge-worker.py 壳后改 -m 启动）
    try:
        with open(LOG, "a") as logf:
            subprocess.Popen(
                ["python3", "-m", "lib.adoption"],
                cwd=paths.plugin_root(),
                stdout=logf, stderr=logf,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass  # worker 启动失败不阻塞 CC，pending 保留待下次

    sys.exit(0)


# === worker 函数（adoption-judge-worker 入口）===

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

    直接 import lib/manifest（原 adoption-judge-worker 用 importlib 加载 build-asset-manifest.py，
    提取后改直接 import 更干净）。失败返回 [] 不阻断。
    """
    try:
        domains, *_ = manifest.parse_vocab()
        if not domains:
            return []
        pkm = {d['name']: d['keywords'] for d in domains}
        pnames = [d['name'] for d in domains]
        return manifest.detect_wm_project_focus(pnames, pkm)
    except Exception:
        return []


def main():
    """worker 入口：读 pending → LLM judge → 写 verdict 到 reuse-log → 重写 pending。"""
    if chat is None:
        sys.exit(0)  # llm_client 不可用，pending 保留待下次
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
        # 致 0% 采纳即实例）。下游消费需留余量+人工抽检，judge_model 字段供追溯可信度（诚实性审查 C1）
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
