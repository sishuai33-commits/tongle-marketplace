"""lib/health_runtime.py — 运行态健康检查

提取自 lib/health.py（P1 模块瘦身）。runtime_check() 独立于 maintenance_check
和 SessionStart 告警函数，职责单一：5 项运行态实体检查。

依赖：lib/paths, lib/state
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

from . import paths, state


def runtime_check():
    """ke 运行态健康检查：5 项（采集环/判别环/消费环/加工环/四库），exit 0/1/2。

    原 runtime-health-check.py 搬（Step 3），后从 health.py 提取（P1）。
    stdout 格式保留（特征测试钉 pass=11）。
    """
    instincts = paths.instincts_dir()
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    def parse_ts(ts_str):
        if not ts_str:
            return None
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            return None

    def file_info(name):
        path = os.path.join(instincts, name)
        if not os.path.exists(path):
            return {"exists": False, "path": path}
        size = os.path.getsize(path)
        mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        lines = state.read_lines(path)
        return {
            "exists": True, "path": path, "size": size, "lines": lines,
            "mtime": mtime.isoformat(),
            "recent_7d": mtime > seven_days_ago,
        }

    checks = []  # (level, name, status, detail)

    def check(level, name, status, detail=""):
        checks.append((level, name, status, detail))

    # --- 1. 采集环 ---
    obs = file_info("observations.jsonl")
    src_obs = file_info("source-observations.jsonl")
    check("pass" if obs.get("recent_7d") else "warn",
          "采集环.observations 活跃度",
          obs.get("recent_7d", False),
          f"mtime={obs.get('mtime')} lines={obs.get('lines')} (7天内有更新=采集环真转)")
    check("pass" if src_obs.get("exists") else "fail",
          "采集环.source-observations 存在",
          src_obs.get("exists", False),
          f"三源扫描器产出文件")
    cursor = file_info(".discriminate-cursor")
    check("pass" if cursor.get("recent_7d") else "warn",
          "采集环.cursor 推进",
          cursor.get("recent_7d", False),
          f"mtime={cursor.get('mtime')} (cursor 停滞=采集没跑)")

    # --- 2. 判别环 ---
    pq = file_info("pending-queue.jsonl")
    exp = file_info("discriminate-experience.jsonl")
    marker = file_info(".discriminate-due")

    pending_count = 0
    resolved_count = 0
    if pq.get("exists"):
        for r in state.read_jsonl(pq["path"]):
            if r.get("status") == "pending":
                pending_count += 1
            elif r.get("status") == "resolved":
                resolved_count += 1

    threshold = 3
    marker_exists = marker.get("exists", False)
    if pending_count >= threshold:
        consistent = marker_exists
        check("fail" if not consistent else "pass",
              "判别环.marker-pending 一致性",
              consistent,
              f"pending={pending_count}>=阈值{threshold} → marker 应在(实际{'在' if marker_exists else '不在'})")
    else:
        consistent = not marker_exists
        check("pass" if consistent else "warn",
              "判别环.marker-pending 一致性",
              consistent,
              f"pending={pending_count}<阈值{threshold} → marker 应不在(实际{'在' if marker_exists else '不在'})")

    check("pass" if exp.get("exists") and exp.get("lines", 0) > 0 else "fail",
          "判别环.判别经验库 有数据",
          exp.get("exists") and exp.get("lines", 0) > 0,
          f"lines={exp.get('lines')} (人裁判别经验回流)")

    # --- 3. 消费环 ---
    rl = file_info("reuse-log.jsonl")
    synthesis_reads = 0
    adoption_verdicts = 0
    recent_synthesis = 0
    if rl.get("exists"):
        for r in state.read_jsonl(rl["path"]):
            k = r.get("kind", "")
            ts = parse_ts(r.get("ts"))
            is_syn = "synthesis" in (r.get("file", "") or "").lower() or k == "synthesis"
            if is_syn:
                synthesis_reads += 1
                if ts and ts > seven_days_ago:
                    recent_synthesis += 1
            if k == "adoption":
                adoption_verdicts += 1

    check("pass" if synthesis_reads > 0 else "fail",
          "消费环.真Read synthesis 有记录",
          synthesis_reads > 0,
          f"total={synthesis_reads}条 (A1验收基础：CC真读synthesis触发PostToolUse写入)")
    check("pass" if recent_synthesis > 0 else "warn",
          "消费环.最近7天有真Read",
          recent_synthesis > 0,
          f"近7天{recent_synthesis}条 (消费环持续在转，非一次性)")
    check("pass" if adoption_verdicts > 0 else "warn",
          "消费环.adoption verdict 有数据",
          adoption_verdicts > 0,
          f"total={adoption_verdicts}条 (采纳率判定数据源，注入有效性证据)")

    # --- 4. 加工环 ---
    cross_domain_lib = file_info("cross-domain-patterns.jsonl")
    refine_log = file_info(".refine-last-run")
    check("fail" if not cross_domain_lib.get("exists") else "pass",
          "加工环.跨域模式库 存在",
          cross_domain_lib.get("exists", False),
          f"{'存在' if cross_domain_lib.get('exists') else '不存在(M1未做，当前唯一真零生产环)'}")
    check("fail" if not refine_log.get("exists") else "pass",
          "加工环.规整脚本运行过",
          refine_log.get("exists", False),
          f"{'运行过' if refine_log.get('exists') else '从未运行(M1未做)'}")

    # --- 5. 四库存在性汇总 ---
    libs = {
        "原料库(observations)": obs.get("exists", False),
        "判别经验库(experience)": exp.get("exists", False) and exp.get("lines", 0) > 0,
        "复用日志(reuse-log)": rl.get("exists", False) and rl.get("lines", 0) > 0,
        "跨域模式库(cross-domain)": cross_domain_lib.get("exists", False),
    }
    exist_count = sum(1 for v in libs.values() if v)
    check("pass" if exist_count == 4 else ("warn" if exist_count >= 3 else "fail"),
          "四库存在性",
          exist_count,
          f"{exist_count}/4: " + " ".join(f"{'✓' if v else '✗'}{k}" for k, v in libs.items()))

    # === 输出 ===
    json_mode = "--json" in sys.argv
    fails = [c for c in checks if c[0] == "fail"]
    warns = [c for c in checks if c[0] == "warn"]

    if json_mode:
        print(json.dumps({
            "ts": now.isoformat(),
            "total": len(checks),
            "pass": len(checks) - len(fails) - len(warns),
            "warn": len(warns),
            "fail": len(fails),
            "checks": [{"level": c[0], "name": c[1], "status": c[2], "detail": c[3]} for c in checks],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"# ke 运行态健康检查 @ {now.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"# 实体目录: {instincts}")
        print()
        for level, name, status, detail in checks:
            icon = {"pass": "✓", "warn": "⚠", "fail": "✗"}[level]
            print(f"  {icon} [{level.upper()}] {name}")
            if detail:
                print(f"      {detail}")
        print()
        print(f"# 汇总: {len(checks)}项 / pass={len(checks)-len(fails)-len(warns)} warn={len(warns)} fail={len(fails)}")
        if fails:
            print(f"# 🔴 FAIL项(硬阻断):")
            for _, n, _, d in fails:
                print(f"#   - {n}: {d}")
        print(f"# 退出码: {0 if not fails else (1 if warns else 2)}")

    sys.exit(0 if not fails and not warns else (2 if fails else 1))
