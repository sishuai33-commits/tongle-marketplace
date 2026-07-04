#!/usr/bin/env python3
"""LLM 调用封装 — 火山方舟 anthropic 兼容接口。

被测模型(glm-5.2[1m]，复现 CC 真实配置) 与 judge 模型(doubao-seed-2.0-pro，异模型)
分离，降低"被测与评判同源"偏差。人工抽检高价值案例兜底。

接口返回纯文本（跳过 thinking block——glm/doubao 默认带思考，取 text block 拼接）。
"""
import os
import anthropic

_BASE = os.environ.get("ANTHROPIC_BASE_URL")
_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN")
_client = anthropic.Anthropic(base_url=_BASE, api_key=_TOKEN) if (_BASE and _TOKEN) else None

# 被测系统：复现 CC 当前真实模型配置
MODEL_SUBJECT = os.environ.get("ANTHROPIC_MODEL", "glm-5.2[1m]")
# judge：用异模型降同源偏差（opus 档，doubao-pro）
MODEL_JUDGE = os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "doubao-seed-2.0-pro")


def chat(model, system, user, max_tokens=4096):
    """调一次 LLM，返回纯文本（跳过 thinking block）。"""
    if _client is None:
        raise RuntimeError("ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN 未设置")
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user}],
    }
    if system:
        kwargs["system"] = system
    resp = _client.messages.create(**kwargs)
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "".join(parts).strip() or "(空输出)"


if __name__ == "__main__":
    # 自检：两模型各回一句
    print("被测", MODEL_SUBJECT, "→", chat(MODEL_SUBJECT, None, "只回复两个字：通了", max_tokens=200))
    print("judge", MODEL_JUDGE, "→", chat(MODEL_JUDGE, None, "只回复两个字：通了", max_tokens=200))
