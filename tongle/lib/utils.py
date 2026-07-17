"""lib/utils.py — 公共工具函数

薄壳消除（P0）：_run_quiet 原散落 session-start / session-end / health 三处，统一入口。
阈值常量化（P1）：low_adoption 被 health.maintenance_check / health.dashboard 共用，
从 lib/health.py 提取。
"""
import os
import subprocess
from datetime import datetime, timedelta, timezone


def run_quiet(cmd, timeout=15):
    """静默跑外部命令，返回 stdout（失败返回 ''）。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def low_adoption(reuse_log):
    """连接点④路3：读 reuse-log adoption verdict，14天窗口样本≥3 且 avg<0.3 返回 (avg, n)，否则 None。

    从 lib/health.py 提取（P1 模块瘦身）：被 maintenance_check / dashboard 共用，
    提取到 utils 消共享散落。
    """
    from . import state  # 延迟 import 避免循环依赖（state 不 import utils）

    if not os.path.exists(reuse_log):
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    rates = []
    for d in state.read_jsonl(reuse_log):
        if d.get("kind") != "adoption" or d.get("rate", -1) < 0:
            continue
        if d.get("judge_model", "doubao-seed-2.0-pro") != "doubao-seed-2.0-pro":
            continue
        _expected = d.get("expected_project", [])
        if _expected and d.get("project") not in _expected:
            continue
        ts = d.get("ts", "")
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if t >= cutoff:
            rates.append(d["rate"])
    if len(rates) < 3:
        return None
    avg = sum(rates) / len(rates)
    if avg >= 0.3:
        return None
    return (avg, len(rates))
