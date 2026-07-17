#!/usr/bin/env python3
"""session-start.py — 会话启动 hook（bash→python 重写，Step 4 Part A）

stdout → JSON（Hook 系统注入 CC 上下文）  stderr → 日志

12 section 编排（原 session-start.sh 435 行 bash→py）：
  §0 wiki 队列 / §1 patterns / §2 working-memory / §3 active-context /
  §4 memory 健康 / §6 pending-wiki-sync / §6.4 资产路由 / §6.5 维护门禁 /
  §6.5b 仪表盘 / §6.6 wiki 健康 / §7 输出

编排+section 留壳（选项2务实分层），资产解析下沉 lib/manifest、健康提醒下沉
lib/health（ponytail-audit 批3）。调 lib.paths/platform 消 bash heredoc/变量
展开陷阱。§0/§6.4/§6.5 保留 subprocess 调外部薄壳（子进程隔离 sys.exit + 特征
测试 fake 不变），Part D 再优化。
"""
import json
import os
import sys
import time

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)
from lib import paths, health, manifest, state, utils, session_context

os.environ.setdefault("PYTHONUTF8", "1")


def main():
    start_ts = int(time.time() * 1000)
    hooks_dir = paths.hooks_dir()
    instincts_dir = paths.instincts_dir()
    cc_memory_dir = paths.cc_memory_dir()
    snooze_file = os.path.join(instincts_dir, ".alert-snooze")
    session_id = os.environ.get("CLAUDE_SESSION_ID", "unknown")

    # §0 消费 Wiki 读取队列（daemon access-update，原 wiki-track-read.sh --batch 内联）
    daemon = os.path.join(_PLUGIN_ROOT, "adapters", "obsidian", ".wiki-daemon.py")
    utils.run_quiet(["python3", daemon, "access-update"])

    try:
        os.makedirs(instincts_dir, exist_ok=True)
    except OSError:
        pass

    # §1 patterns.yaml
    active = session_context.parse_patterns(instincts_dir)
    manual_instincts = '\n'.join(session_context.instinct_line(x) for x in active)
    manual_count = len(active)

    # §2 Working Memory
    wm_section, wm_count = session_context.parse_working_memory(cc_memory_dir)

    # §3 active-context.md
    session_context.gen_active_context(instincts_dir, session_id, manual_instincts, manual_count, wm_section)

    # §4 Memory 健康提醒
    mem_health = health.memory_alert(instincts_dir)

    # §6 Pending Wiki Sync
    pending_section, pending_count = health.pending_wiki_sync(instincts_dir, snooze_file)

    # §6.4 Wiki 资产路由（静默注入，直调 lib/manifest.build，消薄壳 subprocess）
    asset_manifest = manifest.build(strategy="all").strip()

    # §6.5 维护门禁（直调 lib/health.maintenance_check，消薄壳 subprocess；读 marker）
    try:
        health.maintenance_check()
    except SystemExit:
        pass  # maintenance_check exit(1)=触发 / exit(0)=正常，不阻断会话启动
    maint_section = ""
    marker = os.path.join(instincts_dir, ".maintenance-due")
    if os.path.isfile(marker):
        try:
            with open(marker, encoding="utf-8", errors="replace") as f:
                issues = json.load(f).get('issues', '')
            maint_section = f'🔴 知识库该整理了，{issues}\n本次会话抽空清理，去重和精简'
        except (OSError, json.JSONDecodeError):
            pass

    # §6.5b 仪表盘
    dashboard = health.dashboard(instincts_dir)

    # §6.6 Wiki 健康
    wiki_health = health.wiki_health(snooze_file)

    # §6.7 首次运行/版本变更检测（仪式感，v1.3.0 阶段5）
    first_run = health.first_run_check(instincts_dir)

    # §6.8 采集可见性已移除（指挥官7/9反馈：📊上次采集N条打扰无价值，collection_visibility 函数保留待慢环定夺）

    # §7 输出（注入 CC 上下文）
    elapsed = int(time.time() * 1000) - start_ts

    # additional = 用户可见提醒（首次/仪表盘最前，触发式）
    additional = ""
    for section in (first_run, dashboard, mem_health, pending_section, wiki_health, maint_section):
        if section:
            additional = additional + "\n\n" + section if additional else section

    # silent = 静默注入（Wiki 资产路由 + 报警自解释，CC 自知，不进人可见对话）
    alert_ctx = health.alert_context(instincts_dir)
    silent_parts = [p for p in (asset_manifest, alert_ctx) if p]
    silent = "\n\n".join(silent_parts)
    combined = silent + "\n\n" + additional if silent and additional else (silent or additional)

    sys_msg = additional.strip()
    print(json.dumps({
        "systemMessage": sys_msg,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": combined.strip(),
        },
    }))

    # stderr 日志
    print(f"[session-start] session={session_id} | instincts={manual_count} "
          f"| wm_active={wm_count} | pending_sync={pending_count} | elapsed={elapsed}ms",
          file=sys.stderr)


if __name__ == "__main__":
    main()
