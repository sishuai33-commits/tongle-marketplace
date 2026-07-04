#!/usr/bin/env python3
"""LLM-as-judge — 盲评 A/B 回答，给两回答各打绝对质量分。

盲评：随机打乱 A/B 呈现顺序防位置偏差，judge 给"回答一/回答二"各打 0-10 质量分。
judge() 翻回真实 A/B 语义：score_a/score_b，gain=score_b-score_a（B注入 vs A裸跑增益）。
gain 正=注入有效，负=注入有害，0=无差。
"""
import json
import re
import random
import sys
import os

_EVAL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _EVAL_ROOT)
from llm_client import chat, MODEL_JUDGE

JUDGE_PROMPT = """你是知识有效性评测员。给你一个用户问题和两个AI回答（回答一/回答二）。盲评。

评判维度（知识有效性——回答是否让用户"更懂"，不是纯技术对错）：
- 准确性：事实/机制描述是否正确（不能瞎编内部细节）
- 贴切性（关键）：若问题问的是某个特定体系/项目/产品的具体机制，回答"这个体系实际怎么运作"（带体系专属术语、决策记录、落地细节）比回答"通用概念定义/教科书原理"更贴用户需求。通用泛泛即使流畅，也不应高于"切中该体系实际运作"的回答。
- 深度：是否切中要害、有结构化洞察而非泛泛而谈
- 可用性：用户能否据此理解或行动

注意：不要把"通用性强"当成加分项。用户问的是特定体系，通用答案 = 没答到点上。

先简短分析（2-3句），然后严格按此 JSON 输出（只输出 JSON，不要其他文字）：
{"score_one":0到10的整数,"score_two":0到10的整数,"reason":"一句理由"}

给"回答一"和"回答二"各打质量分（0-10）。质量越高分越高。两个都好可都高分，都差都低分。"""


def judge(problem, ans_a, ans_b):
    """盲评 A/B，返回 {winner, gain_score, score_a, score_b, reason, flip}。

    gain_score = score_b - score_a（B注入相对A裸跑增益，-10..10）。
    flip=True 表示呈现时 B 在前（翻回时映射对调）。
    """
    flip = random.choice([True, False])
    first, second = (ans_b, ans_a) if flip else (ans_a, ans_b)
    user = (f"用户问题：{problem}\n\n"
            f"=== 回答一 ===\n{first}\n\n"
            f"=== 回答二 ===\n{second}\n\n请盲评，给两个回答各打质量分。")
    raw = chat(MODEL_JUDGE, JUDGE_PROMPT, user, max_tokens=2048)
    m = re.search(r'\{[^{}]*\}', raw, re.S)
    if not m:
        return {"winner": "unknown", "gain_score": -99, "score_a": -1, "score_b": -1,
                "reason": "judge 解析失败", "raw": raw[:200], "flip": flip}
    try:
        d = json.loads(m.group(0))
        s1 = int(d.get("score_one", -1))
        s2 = int(d.get("score_two", -1))
    except Exception:
        return {"winner": "unknown", "gain_score": -99, "score_a": -1, "score_b": -1,
                "reason": "json 解析失败", "raw": raw[:200], "flip": flip}
    # 翻回真实 A/B：flip=True 时 first=B(=one), second=A(=two)
    if flip:
        score_b, score_a = s1, s2
    else:
        score_a, score_b = s1, s2
    if score_b > score_a:
        winner = "B"
    elif score_a > score_b:
        winner = "A"
    else:
        winner = "tie"
    return {"winner": winner, "gain_score": score_b - score_a,
            "score_a": score_a, "score_b": score_b,
            "reason": d.get("reason", ""), "flip": flip}


if __name__ == "__main__":
    # 自检：B 明显更好 → 期望 score_b 高、gain 正、winner=B
    q = "什么是 Agent 认知种子？"
    a = "不知道。"
    b = "Agent认知种子是≤800tokens的可spawn执行指令，由nuwa蒸馏的Skill压缩适配而来，双轨产出完整版+压缩版。"
    print(json.dumps(judge(q, a, b), ensure_ascii=False, indent=2))
