#!/usr/bin/env python3
"""
cost-guard.py — 基于真实 token 消耗的单轮成本预警

Stop hook 读取 session JSONL 文件中的 assistant 消息，
提取 message.usage 中的 input_tokens / output_tokens /
cache_creation_input_tokens / cache_read_input_tokens，
根据 model 字段匹配定价，计算真实成本。

阈值: 8 元/轮
状态文件: ~/.claude/instincts/.cost-state/{session_id}.json
"""

import sys, os, json
from datetime import datetime
from pathlib import Path
from collections import defaultdict

STATE_DIR = Path(os.path.expanduser("~/.claude/instincts/.cost-state"))
PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))
THRESHOLD_CNY = 8.0

STATE_DIR.mkdir(parents=True, exist_ok=True)

# ── 模型定价 (USD / 1M tokens) ──
# 匹配规则：按 model 字段子串匹配，先匹配到的生效
MODEL_PRICING = [
    # Anthropic Opus 4.5/4.6/4.7
    ("opus-4.7",   {"input": 5,   "cache_write": 6.25, "cache_read": 0.50, "output": 25}),
    ("opus-4.6",   {"input": 5,   "cache_write": 6.25, "cache_read": 0.50, "output": 25}),
    ("opus-4.5",   {"input": 5,   "cache_write": 6.25, "cache_read": 0.50, "output": 25}),
    ("opus-4",     {"input": 15,  "cache_write": 18.75,"cache_read": 1.50, "output": 75}),
    ("opus",       {"input": 15,  "cache_write": 18.75,"cache_read": 1.50, "output": 75}),
    # Anthropic Sonnet 4.x
    ("sonnet-4.6", {"input": 3,   "cache_write": 3.75, "cache_read": 0.30, "output": 15}),
    ("sonnet-4.5", {"input": 3,   "cache_write": 3.75, "cache_read": 0.30, "output": 15}),
    ("sonnet-4",   {"input": 3,   "cache_write": 3.75, "cache_read": 0.30, "output": 15}),
    ("sonnet",     {"input": 3,   "cache_write": 3.75, "cache_read": 0.30, "output": 15}),
    # Anthropic Haiku
    ("haiku",      {"input": 1,   "cache_write": 1.25, "cache_read": 0.10, "output": 5}),
    # DeepSeek (通过代理)
    ("deepseek-r1",    {"input": 0.55, "cache_write": 0.55, "cache_read": 0.14, "output": 2.19}),
    ("deepseek-v3",    {"input": 0.27, "cache_write": 0.27, "cache_read": 0.07, "output": 1.10}),
    ("deepseek-v4",    {"input": 0.27, "cache_write": 0.27, "cache_read": 0.07, "output": 1.10}),
    ("deepseek",       {"input": 0.27, "cache_write": 0.27, "cache_read": 0.07, "output": 1.10}),
    # 默认（保守估计，按 Sonnet 定价）
    ("*",              {"input": 3,   "cache_write": 3.75, "cache_read": 0.30, "output": 15}),
]

def get_pricing(model_id):
    """根据 model 字段返回定价 (USD/1M tokens)"""
    if not model_id:
        model_id = "*"
    model_lower = model_id.lower()
    for pattern, pricing in MODEL_PRICING:
        if pattern == "*" or pattern in model_lower:
            return pricing
    return MODEL_PRICING[-1][1]  # fallback to default


def cost_usd(model_id, input_tok, cache_write, cache_read, output_tok):
    """计算单条消息的 USD 成本"""
    p = get_pricing(model_id)
    return (
        input_tok / 1_000_000 * p["input"] +
        cache_write / 1_000_000 * p["cache_write"] +
        cache_read / 1_000_000 * p["cache_read"] +
        output_tok / 1_000_000 * p["output"]
    )


def find_session_files(session_id):
    """查找 session 的所有 JSONL 文件（主 session + subagent）"""
    files = []
    for projects_dir in PROJECTS_DIR.iterdir():
        if not projects_dir.is_dir():
            continue
        # 主 session 文件
        main_file = projects_dir / f"{session_id}.jsonl"
        if main_file.exists():
            files.append(main_file)
        # Subagent 文件
        sub_dir = projects_dir / session_id
        if sub_dir.is_dir():
            for f in sorted(sub_dir.glob("*.jsonl")):
                files.append(f)
    return files


def load_state(session_id):
    """加载状态文件"""
    p = STATE_DIR / f"{session_id}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "session_id": session_id,
        "seen_message_ids": [],        # 已处理过的 message.id
        "cumulative_cost_usd": 0.0,
        "turn_history": [],            # [{turn_start_cost, turn_end_cost, delta, tool_count, timestamp}]
    }


def save_state(session_id, state):
    p = STATE_DIR / f"{session_id}.json"
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def cmd_check(session_id):
    """Stop hook: 扫描 JSONL，计算本轮增量成本"""
    session_files = find_session_files(session_id)
    state = load_state(session_id)
    seen = set(state.get("seen_message_ids", []))
    prev_cumulative = state.get("cumulative_cost_usd", 0.0)

    new_messages = []
    total_delta = 0.0

    for jsonl_file in sorted(session_files):
        try:
            with open(jsonl_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if entry.get("type") != "assistant":
                        continue

                    msg = entry.get("message", {})
                    msg_id = msg.get("id")
                    if not msg_id or msg_id in seen:
                        continue

                    usage = msg.get("usage", {})
                    model = msg.get("model", "")
                    input_tok = usage.get("input_tokens", 0) or 0
                    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
                    cache_read = usage.get("cache_read_input_tokens", 0) or 0
                    output_tok = usage.get("output_tokens", 0) or 0

                    msg_cost = cost_usd(model, input_tok, cache_write, cache_read, output_tok)
                    seen.add(msg_id)
                    total_delta += msg_cost
                    new_messages.append({
                        "id": msg_id,
                        "model": model,
                        "input_tokens": input_tok,
                        "output_tokens": output_tok,
                        "cost_usd": round(msg_cost, 6),
                    })
        except (IOError, PermissionError):
            continue

    new_cumulative = prev_cumulative + total_delta

    # 更新状态
    state["seen_message_ids"] = list(seen)
    if len(state["seen_message_ids"]) > 10000:
        # 防止无限增长：保留最近 5000 条
        state["seen_message_ids"] = state["seen_message_ids"][-5000:]
    state["cumulative_cost_usd"] = new_cumulative

    # 记录本轮
    turn_entry = {
        "delta_usd": round(total_delta, 4),
        "delta_cny": round(total_delta * 7.2, 2),
        "messages": len(new_messages),
        "timestamp": datetime.now().isoformat(),
    }
    state.setdefault("turn_history", []).append(turn_entry)
    if len(state["turn_history"]) > 50:
        state["turn_history"] = state["turn_history"][-50:]

    save_state(session_id, state)

    # 输出结果
    delta_cny = total_delta * 7.2
    if delta_cny > THRESHOLD_CNY:
        # 按模型分别统计
        model_stats = defaultdict(lambda: {"count": 0, "tokens": 0, "cost": 0.0})
        for m in new_messages:
            mdl = m["model"] or "unknown"
            model_stats[mdl]["count"] += 1
            model_stats[mdl]["tokens"] += m["input_tokens"] + m["output_tokens"]
            model_stats[mdl]["cost"] += m["cost_usd"]

        stats_lines = []
        for mdl, s in sorted(model_stats.items(), key=lambda x: -x[1]["cost"]):
            tok_k = s["tokens"] / 1000
            cost_cny = s["cost"] * 7.2
            stats_lines.append(f"  {mdl}: {s['count']}条消息, {tok_k:.1f}K tokens, ¥{cost_cny:.2f}")

        result = {
            "decision": "approve",
            "systemMessage": (
                f"⚠️⚠️⚠️ 成本预警 ⚠️⚠️⚠️\n"
                f"本轮消耗: ${total_delta:.4f} (约 ¥{delta_cny:.2f}) — 超过阈值 (¥{THRESHOLD_CNY})\n"
                f"本轮消息数: {len(new_messages)}\n"
                + "\n".join(stats_lines) + "\n"
                f"累计会话成本: ${new_cumulative:.4f} (约 ¥{new_cumulative * 7.2:.2f})\n"
                f"请确认是否继续。"
            ),
        }
        print(json.dumps(result, ensure_ascii=False))
    else:
        # 静默
        result = {"decision": "approve", "systemMessage": ""}
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: cost-guard.py <hook|check> [session_id]", file=sys.stderr)
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "hook":
        # 从 stdin 读取 hook 上下文
        raw = sys.stdin.read()
        if not raw.strip():
            sys.exit(0)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            sys.exit(0)
        event = data.get("hook_event_name", "")
        session_id = data.get("session_id", "unknown")
        if event == "Stop":
            cmd_check(session_id)
        # PostToolUse 和 UserPromptSubmit 不再需要
    elif cmd == "check":
        sid = sys.argv[2] if len(sys.argv) > 2 else "unknown"
        cmd_check(sid)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
