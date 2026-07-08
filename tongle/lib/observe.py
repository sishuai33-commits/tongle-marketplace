"""采集环被动采集（PostToolUse → observations.jsonl）

提取自 observe.sh（Step 2）。逻辑：stdin JSON → observation entry → append + 节流信号。
IO 经 lib/state，路径经 lib/paths。

架构范式 §四采集环：被动采集 CC 工具调用（所有工具泛记，vs reuse-log 专记复用）。
节流：每 SIGNAL_EVERY_N 次 touch .observer-pending（慢环消费触发点）。
文件大小：observations.jsonl 超 5MB 归档到 observations.archive/。
"""
import json
import os
import sys
from datetime import datetime, timezone

from . import state, paths

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5MB
SIGNAL_EVERY_N = 20
INPUT_PREVIEW_MAX = 3000


def build_observation(tool, tool_input, session):
    """构建 observation entry，tool 空/unknown 返回 None（不采集）

    dict tool_input → json 字符串（ensure_ascii=False）；其他 → str()。
    input_preview 截断 INPUT_PREVIEW_MAX。
    """
    if not tool or tool == "unknown":
        return None
    if isinstance(tool_input, dict):
        inp = json.dumps(tool_input, ensure_ascii=False)
    else:
        inp = str(tool_input)
    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": session,
        "tool": tool,
        "input_preview": inp[:INPUT_PREVIEW_MAX],
    }


def archive_if_too_large(obs_file):
    """observations.jsonl 超 MAX_FILE_SIZE_BYTES 归档到 observations.archive/

    对应 observe.sh 的文件大小检查段。不存在的文件不崩。
    """
    try:
        size = os.path.getsize(obs_file)
    except OSError:
        return
    if size <= MAX_FILE_SIZE_BYTES:
        return
    archive_dir = os.path.join(os.path.dirname(obs_file), "observations.archive")
    try:
        os.makedirs(archive_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        os.rename(obs_file, os.path.join(archive_dir, f"observations-{ts}.jsonl"))
    except OSError:
        pass


def bump_signal_counter(instincts_dir):
    """counter 递增返回新值，每 SIGNAL_EVERY_N 次 touch .observer-pending

    对应 observe.sh 的节流信号段。counter 文件不存在从 0 起。
    """
    counter_file = os.path.join(instincts_dir, ".observer-signal-counter")
    counter = 0
    try:
        with open(counter_file, encoding="utf-8", errors="replace") as f:
            counter = int(f.read().strip() or "0")
    except (OSError, ValueError):
        pass
    counter += 1
    try:
        with open(counter_file, "w", encoding="utf-8") as f:
            f.write(str(counter))
    except OSError:
        return counter
    if counter % SIGNAL_EVERY_N == 0:
        pending = os.path.join(instincts_dir, ".observer-pending")
        try:
            open(pending, "a").close()  # touch（不存在则建，存在则不动内容）
            os.utime(pending)  # 更新 mtime
        except OSError:
            pass
    return counter


def main(raw=None):
    """PostToolUse 入口：stdin JSON → observation → append + 节流

    raw 参数：合并 hook(post-tool-use.py)读 stdin 后传入；None 时自读（薄壳兼容）。
    """
    if raw is None:
        raw = sys.stdin.read()
    if not raw.strip():
        sys.exit(0)
    try:
        d = json.loads(raw)
    except Exception:
        sys.exit(0)

    session = d.get("session_id", os.environ.get("CLAUDE_SESSION_ID", "unknown"))
    entry = build_observation(d.get("tool_name", ""), d.get("tool_input", ""), session)
    if entry is None:
        sys.exit(0)

    instincts = paths.instincts_dir()
    obs_file = paths.instincts_file("observations.jsonl")
    state.append_jsonl(obs_file, entry)  # 父目录自动建（lib/state._ensure_parent）
    archive_if_too_large(obs_file)
    bump_signal_counter(instincts)
    sys.exit(0)
