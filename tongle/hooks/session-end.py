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
import subprocess
import sys

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)
from lib import paths, discriminate

os.environ.setdefault("PYTHONUTF8", "1")


def _run_quiet(cmd, timeout=15):
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:
        pass


# ── §1 wiki-check ──────────────────────────────────────
def _wiki_check():
    wm_file = os.path.join(paths.cc_memory_dir(), "working-memory.md")
    if not os.path.isfile(wm_file):
        return
    try:
        with open(wm_file, encoding="utf-8", errors="replace") as f:
            wm = f.read()
    except OSError:
        return
    topics = []
    current = None
    for line in wm.split('\n'):
        if line.startswith('## Topic:'):
            if current:
                topics.append(current)
            current = {'title': line.replace('## Topic:', '').strip(), 'has_wiki_ref': False}
        elif current:
            s = line.strip()
            if s.startswith('wiki:') or s.startswith('- wiki:'):
                current['has_wiki_ref'] = True
    if current:
        topics.append(current)
    unsynced = [t['title'] for t in topics
                if '[synthesized]' in t['title'] and not t['has_wiki_ref']]
    if unsynced:
        marker = paths.instincts_file(".pending-wiki-sync")
        try:
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            with open(marker, "w", encoding="utf-8") as f:
                json.dump({"unsynced": unsynced, "count": len(unsynced)}, f)
        except OSError:
            pass


# ── §2 discriminate 串联 ───────────────────────────────
def _discriminate(raw):
    hooks_dir = paths.hooks_dir()
    scanner = os.path.join(hooks_dir, "source-scanner.py")
    collector = os.path.join(hooks_dir, "discriminate-collector.py")

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

    # 源1 local_file + 源2 transcript + 源3 ima（scanner 薄壳调 lib/scanner）
    if os.path.isfile(scanner):
        source_root = os.environ.get(
            "SOURCE_ROOT", os.path.join(paths.home(), "Documents", "My_Code_Projects"))
        _run_quiet(["python3", scanner, "--source", "local_file", "--root", source_root])
        if transcript_path:
            _run_quiet(["python3", scanner, "--source", "transcript",
                        "--transcript", transcript_path, "--session", session_id])
        _run_quiet(["python3", scanner, "--source", "ima"])

    # collector --source（判 file_change/transcript_candidate）+ collector（observe 模式判 evolve/new）
    if os.path.isfile(collector):
        _run_quiet(["python3", collector, "--source"])
        _run_quiet(["python3", collector])

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
