#!/usr/bin/env python3
"""post-tool-use.py — PostToolUse 合并 hook（Step 4 Part B）

合并5个 PostToolUse（原 hooks.json 5 handler → 1）：
  observe（全匹配）+ memory-guard（Write|Edit）+ ref-integrity（Write|Edit）
  + wiki-track（mcp__obsidian__read_*）+ reuse-log（Read|Skill）

读 stdin 一次，按 tool_name 分流调 lib（observe.main/guards.memory_check/
guards.ref_main 接受 raw 参数；wiki-track + reuse-log 内联）。

ref-integrity exit(2) 传播阻断 CC（悬空引用）；其他 fail-open 捕获继续。
Step 4 Part D 删旧壳 + 改 hooks.json 指向本脚本。
"""
import json
import os
import subprocess
import sys

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)
from lib import paths, observe, guards, reuse, state

os.environ.setdefault("PYTHONUTF8", "1")


def _wiki_track(d):
    """mcp__obsidian__read_* → daemon pending-add（原 wiki-track-read.sh 77行逻辑）"""
    inp = d.get("tool_input", {})
    if not isinstance(inp, dict):
        return
    paths_list = []
    if isinstance(inp.get("path"), str):
        paths_list.append(inp["path"])
    if isinstance(inp.get("paths"), list):
        paths_list.extend(inp["paths"])
    daemon = os.path.join(paths.plugin_root(), "adapters", "obsidian", ".wiki-daemon.py")
    for p in paths_list:
        if (isinstance(p, str) and p.startswith("wiki/")
                and "/." not in p and not p.startswith("wiki/.")):
            try:
                subprocess.run(["python3", daemon, "pending-add", p],
                               capture_output=True, timeout=10)
            except Exception:
                pass


def _reuse_log(d):
    """Read|Skill → reuse-log.jsonl（原 reuse-log.py 57行逻辑）"""
    entry = reuse.classify(
        d.get("tool_name", ""),
        d.get("tool_input", {}),
        d.get("tool_response", {}),
        session=d.get("session_id", os.environ.get("CLAUDE_SESSION_ID", "unknown")),
    )
    if entry:
        state.append_jsonl(paths.instincts_file("reuse-log.jsonl"), entry)


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        sys.exit(0)
    try:
        d = json.loads(raw)
    except Exception:
        sys.exit(0)

    tool = d.get("tool_name", "")

    # observe（原 observe.sh 无 matcher，全匹配，每次都调）
    try:
        observe.main(raw)
    except SystemExit:
        pass
    except Exception:
        pass  # fail-open

    # Write|Edit|MultiEdit → memory-guard + ref-integrity
    if tool in ("Write", "Edit", "MultiEdit"):
        try:
            guards.memory_check()
        except SystemExit:
            pass  # memory-guard exit(1)=违规记state，不阻断写
        except Exception:
            pass
        try:
            guards.ref_main(raw)
        except SystemExit as e:
            if e.code == 2:
                sys.exit(2)  # ref-integrity 悬空引用 → 阻断 CC
        except Exception:
            pass  # fail-open

    # mcp__obsidian__read_* → wiki-track
    if tool in ("mcp__obsidian__read_note", "mcp__obsidian__read_multiple_notes"):
        _wiki_track(d)

    # Read|Skill → reuse-log
    if tool in ("Read", "Skill"):
        _reuse_log(d)

    sys.exit(0)


if __name__ == "__main__":
    main()
