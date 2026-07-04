#!/usr/bin/env python3
"""reuse-adoption-judge.py — Stop hook 阶段2b 连接点③真消费方

本轮 CC 若读了 synthesis（reuse-log 有 kind=synthesis 记录），Stop 时：
1. 解析 transcript 拿本轮最后一条 assistant 文本输出
2. 查 reuse-log 本 session 最近一条 kind=synthesis（本轮/最近读的 syn）
3. 把 (syn_file + question + 本轮输出截断) 写入 .adoption-pending.jsonl 临时队列
4. nohup 异步启动 adoption-judge-worker.py 消费队列（调 judge 记 verdict 删 pending）
5. hook 立即 exit 0 不阻塞 CC

设计（指挥官定方案 E）：
- 不记 CC 输出到持久层（pending 是临时队列 worker 判完即清）
- reuse-log 持久层只记 kind=adoption verdict（adopted/total/rate/reason）
- 异步不阻塞 CC（守"不干扰"硬约束）

vs adoption-rate.py（阶段2a 离线）：离线复用 eval① results 判采纳率（判据验证）；
本 hook 是在线真实对话判采纳率（连接点③真消费方）。

⚠️ hook 真执行验证留新会话（hook 会话级加载，本会话注册不生效，同 L1/L2/L3 规律）。
"""
import sys
import os
import json
import subprocess
from pathlib import Path

_home = os.path.expanduser("~")
INSTINCTS = Path(_home) / ".claude" / "instincts"
REUSE_LOG = INSTINCTS / "reuse-log.jsonl"
PENDING = INSTINCTS / ".adoption-pending.jsonl"
HOOKS_DIR = Path(__file__).parent
WORKER = HOOKS_DIR / "adoption-judge-worker.py"
LOG = INSTINCTS / ".adoption-judge.log"

OUTPUT_LIMIT = 4000  # 本轮输出截断（控 pending 体积 + judge prompt 长度）


def read_stdin():
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def get_last_assistant_text(transcript_path):
    """读 transcript 最后一条 assistant 消息的文本输出（拼 text blocks）。
    同时返回最后一条 user 消息文本（作 question 上下文）。
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None, None
    last_text = None
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
                        texts = [b.get("text", "") for b in content
                                 if isinstance(b, dict) and b.get("type") == "text"]
                        t = "\n".join(x for x in texts if x)
                        if t.strip():
                            last_user = t
                elif etype == "assistant" and isinstance(content, list):
                    texts = [b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text"]
                    text = "\n".join(t for t in texts if t)
                    if text.strip():
                        last_text = text
    except OSError:
        return None, None
    return last_text, last_user


def get_recent_syn(session_id):
    """查 reuse-log 本 session 最近一条 ok 的 kind=synthesis。"""
    if not REUSE_LOG.exists():
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


def main():
    d = read_stdin()
    transcript = d.get("transcript_path")
    session = d.get("session_id", os.environ.get("CLAUDE_SESSION_ID", "unknown"))

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
        INSTINCTS.mkdir(parents=True, exist_ok=True)
        with open(PENDING, "a", encoding="utf-8") as f:
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
    except OSError:
        sys.exit(0)

    # 异步启动 worker（start_new_session=True 脱离 hook 进程组，hook 退出后存活）
    try:
        with open(LOG, "a") as logf:
            subprocess.Popen(
                ["python3", str(WORKER)],
                stdout=logf, stderr=logf,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass  # worker 启动失败不阻塞 CC，pending 保留待下次

    sys.exit(0)


if __name__ == "__main__":
    main()
