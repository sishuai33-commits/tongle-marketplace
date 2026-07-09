#!/usr/bin/env python3
"""lib/compile.py - 编译环（v1.3.0 新增）

接通采集->编译断层：working-memory [synthesized] 无 wiki ref 的条目
-> pending-compile.jsonl 持久化队列 -> /ke-compile Ingest 编译 -> wiki 页面。

架构范式 §四编译环（v1.3.0 补齐，原采集->消费间断层）。
守红线②⑤⑦⑧（人裁非自动判/棘轮只升/只收自己裁决）。

历史：原 session-end._wiki_check 写一次性 .pending-wiki-sync marker（session-start
读后 os.remove），无编译执行器接，导致采集的候选无处沉淀。本模块改为持久化队列，
/ke-compile 编译后标 status=compiled。
"""
import json
import os
import subprocess
import sys
import time


def collect_from_working_memory(cc_memory_dir, instincts_dir):
    """从 working-memory.md 采 [synthesized] 无 wiki ref 的条目，
    append 到 pending-compile.jsonl（按 title 去重，status=pending）。

    替代原 session-end._wiki_check 的一次性 marker：持久化队列，
    /ke-compile 编译后标 status=compiled，不再 os.remove。

    Args:
        cc_memory_dir: working-memory.md 所在目录
        instincts_dir: pending-compile.jsonl 写入目录
    Returns:
        int: 新增条目数
    """
    wm_file = os.path.join(cc_memory_dir, "working-memory.md")
    if not os.path.isfile(wm_file):
        return 0
    try:
        with open(wm_file, encoding="utf-8", errors="replace") as f:
            wm = f.read()
    except OSError:
        return 0

    # 解析 ## Topic: 段（逻辑迁自 session-end._wiki_check，保行为一致）
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

    # 去 [synthesized] 标记存（比原 _wiki_check 存带标记+显示时去更干净）
    unsynced = [t['title'].replace('[synthesized]', '').strip()
                for t in topics
                if '[synthesized]' in t['title'] and not t['has_wiki_ref']]
    if not unsynced:
        return 0

    # 读已有 pending-compile.jsonl 按 title 去重（不分 status：compiled 的也不重加）
    pc_file = os.path.join(instincts_dir, "pending-compile.jsonl")
    existing = set()
    if os.path.isfile(pc_file):
        try:
            with open(pc_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                        if d.get("title"):
                            existing.add(d["title"])
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass

    new = [t for t in unsynced if t not in existing]
    if not new:
        return 0

    os.makedirs(instincts_dir, exist_ok=True)
    ts = int(time.time() * 1000)
    try:
        with open(pc_file, "a", encoding="utf-8") as f:
            for title in new:
                f.write(json.dumps({
                    "title": title, "added_ts": ts,
                    "status": "pending", "source": "working-memory"
                }, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return len(new)


def prep(instincts_dir, wiki_checks_path, wiki_root):
    """编译前置：跑 wiki_checks.py --fix（机械修复，fail-open）+ 读 pending-compile.jsonl 输出待编译清单。

    /ke-compile 命令的前置脚本部分（plan §决策3.1）。
    wiki_checks.py --fix 机械修复 frontmatter/死链/staleness（0 token）。
    fail-open：wiki_checks 跑失败不阻断 CC 后续 Ingest。

    Args:
        instincts_dir: pending-compile.jsonl 所在目录
        wiki_checks_path: wiki_checks.py 路径
        wiki_root: wiki 根目录（WIKI_VAULT_PATH）
    Returns:
        dict: {"fixed": wiki_checks --fix 的 JSON 结果或 None, "pending": [待编译条目], "pending_count": N}
    """
    # 1. wiki_checks.py --fix --json（机械修复，fail-open）
    fixed = None
    try:
        env = {**os.environ, "WIKI_VAULT_PATH": wiki_root}
        proc = subprocess.run(
            ["python3", wiki_checks_path, "--fix", "--json"],
            capture_output=True, text=True, env=env, timeout=120
        )
        if proc.returncode == 0 and proc.stdout:
            fixed = json.loads(proc.stdout)
    except Exception:
        pass  # fail-open，不阻断

    # 2. 读 pending-compile.jsonl pending 条目
    pc_file = os.path.join(instincts_dir, "pending-compile.jsonl")
    pending = []
    if os.path.isfile(pc_file):
        try:
            with open(pc_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                        if d.get("status") == "pending":
                            pending.append(d)
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass
    return {"fixed": fixed, "pending": pending, "pending_count": len(pending)}


def finalize(instincts_dir, idx, wiki_root=None):
    """编译收尾：标 pending-compile.jsonl 第 idx 条 status=compiled + 更新 .ai-vocab.md 时间戳。

    /ke-compile 命令的收尾脚本部分（plan §决策3.3）。
    idx 是 pending 条目中的索引（0-based，按文件顺序只数 pending，跳 compiled）。

    Args:
        instincts_dir: pending-compile.jsonl 所在目录
        idx: pending 条目索引（0-based）
        wiki_root: wiki 根目录（更新 .ai-vocab.md 时间戳，None 跳过）
    Returns:
        bool: 是否成功标记
    """
    pc_file = os.path.join(instincts_dir, "pending-compile.jsonl")
    if not os.path.isfile(pc_file):
        return False
    lines = []
    pending_seen = -1
    marked = False
    try:
        with open(pc_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    lines.append(line)
                    continue
                try:
                    d = json.loads(line)
                    if d.get("status") == "pending":
                        pending_seen += 1
                        if pending_seen == idx:
                            d["status"] = "compiled"
                            d["compiled_ts"] = int(time.time() * 1000)
                            marked = True
                            lines.append(json.dumps(d, ensure_ascii=False) + "\n")
                        else:
                            lines.append(line)
                    else:
                        lines.append(line)
                except json.JSONDecodeError:
                    lines.append(line)
    except OSError:
        return False
    if not marked:
        return False
    try:
        with open(pc_file, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except OSError:
        return False
    if wiki_root:
        _update_vocab_timestamp(wiki_root)
    return True


def _update_vocab_timestamp(wiki_root):
    """更新 .ai-vocab.md 生成时间戳（治本逻辑迁自 daemon cmd_compile_finalize L424-437）。

    根因：wiki-full-compile.js "生成时间: 今天" 是 prompt 指令靠 LLM 自觉，
    LLM 可能忘写/写错 -> 时间戳过时。这里强制覆盖。
    """
    vocab_file = os.path.join(wiki_root, ".ai-vocab.md")
    if not os.path.isfile(vocab_file):
        return
    today = time.strftime("%Y-%m-%d")
    try:
        with open(vocab_file, encoding="utf-8", errors="replace") as f:
            content = f.read()
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if line.startswith('> 生成时间：'):
                lines[i] = f'> 生成时间：{today}（编译自动生成）'
                break
        new_vocab = '\n'.join(lines)
        if new_vocab != content:
            with open(vocab_file, "w", encoding="utf-8") as f:
                f.write(new_vocab)
    except OSError:
        pass


def main():
    """CLI 入口：--prep / --finalize <idx>。/ke-compile 命令的脚本机械部分。"""
    import argparse
    from lib import paths
    parser = argparse.ArgumentParser(description="ke 编译环脚本机械部分（/ke-compile 前置+收尾）")
    parser.add_argument("--prep", action="store_true", help="前置：wiki_checks.py --fix + 输出待编译清单")
    parser.add_argument("--finalize", type=int, metavar="IDX", help="收尾：标第 IDX 条 pending 为 compiled")
    args = parser.parse_args()

    instincts_dir = os.path.join(paths.home(), ".claude", "instincts")
    wiki_checks = os.path.join(paths.plugin_root(), "skills", "wiki-management", "scripts", "wiki_checks.py")
    wiki_root = os.environ.get("WIKI_VAULT_PATH", os.path.expanduser("~/Documents/Obsidian Vault/wiki"))

    if args.prep:
        result = prep(instincts_dir, wiki_checks, wiki_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.finalize is not None:
        ok = finalize(instincts_dir, args.finalize, wiki_root)
        if ok:
            print(f"✅ pending#{args.finalize} 标记 compiled + .ai-vocab 时间戳更新")
        else:
            print(f"❌ pending#{args.finalize} 未找到（idx 超范围或 pending-compile.jsonl 不存在）")
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
