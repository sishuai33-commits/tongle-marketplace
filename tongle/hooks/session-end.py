#!/usr/bin/env python3
"""session-end.py — SessionEnd 合并 hook（Step 4 Part C）

合并2个 SessionEnd：
  §1 wiki-check（原 session-end-wiki-check.sh 59行）：working-memory [synthesized]
     无 wiki 引用 → 写 .pending-wiki-sync marker（下次 SessionStart 消费）
  §2 discriminate（原 session-end-discriminate.sh 97行）：串联 scanner+collector
     采集判别（源1 local_file + 源2 transcript + 源3 ima）+ 独立 marker 刷新

fail-open：任何失败不阻断会话结束（采集失败下次补扫，游标不推进则重扫）。
Part D 改 hooks.json 指向本脚本 + 删旧 .sh。
"""
import json
import os
import sys

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)
from lib import paths, discriminate, compile, scanner

os.environ.setdefault("PYTHONUTF8", "1")


# ── §1 wiki-check ──────────────────────────────────────
def _wiki_check():
    """§1 wiki-check：working-memory [synthesized] 无 wiki ref -> pending-compile.jsonl 持久化队列

    v1.3.0 改：原写一次性 .pending-wiki-sync marker（session-start 读后删），无编译
    执行器接。改持久化 pending-compile.jsonl，/ke-compile 编译后标 compiled。
    逻辑下沉 lib/compile.collect_from_working_memory 可测。
    """
    try:
        compile.collect_from_working_memory(paths.cc_memory_dir(), paths.instincts_dir())
    except Exception as e:
        print(f"[session-end] WARN: wiki-check 采集失败 {e}", file=sys.stderr)


# ── §2 discriminate 串联 ───────────────────────────────
def _discriminate(raw):
    # 解析 stdin（transcript_path + session_id，供源2 transcript 扫描）
    transcript_path = ""
    session_id = ""
    if raw.strip():
        try:
            d = json.loads(raw)
            transcript_path = d.get("transcript_path", "") or ""
            session_id = d.get("session_id", "") or ""
        except Exception:
            pass

    # 源1 local_file + 源2 transcript + 源3 ima（直调 lib/scanner，消薄壳 subprocess）
    try:
        source_root = os.environ.get(
            "SOURCE_ROOT", os.path.join(paths.home(), "Documents", "My_Code_Projects"))
        scanner.scan_local_file(source_root, False)
        if transcript_path:
            scanner.scan_transcript(transcript_path, session_id)
        scanner.scan_ima(False)
    except Exception as e:
        print(f"[session-end] WARN: scanner 采集失败 {e}", file=sys.stderr)

    # collector（直调 lib/discriminate，消薄壳 subprocess）
    try:
        discriminate.run_source_mode(False)
        discriminate.run_observe_mode(False)
    except Exception as e:
        print(f"[session-end] WARN: collector 判别失败 {e}", file=sys.stderr)

    # 独立 marker 刷新（P1-1 真实根因修复：基于 pending 总数，不依赖 collector 是否产出新候选）
    try:
        discriminate.update_trigger_marker()
    except Exception as e:
        print(f"[session-end] WARN: marker 刷新失败 {e}", file=sys.stderr)


def main():
    raw = sys.stdin.read()
    _wiki_check()
    _discriminate(raw)
    sys.exit(0)


if __name__ == "__main__":
    main()
