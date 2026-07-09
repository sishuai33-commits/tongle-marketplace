---
description: ke 运行态健康检查 — 11项检查（包装 runtime-health-check.py，检查 ~/.claude/instincts/ 运行态实体）
---
执行 ke 运行态健康检查：

`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/runtime-health-check.py"`

退出码：0=全绿(A5 pass) / 1=有warn / 2=有fail。

展示输出并解读 11 项检查（probe mtime/active-context/pending-queue/experience/reuse-log/四库文件存在/cursor 推进等）哪项异常，给出修复建议。

可选 `--json` 模式输出结构化结果：`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/runtime-health-check.py" --json`

## 建议下一步（检查完 AI 主动建议）
- 全绿 + `pending-queue.jsonl` 有 pending -> `/ke-review` 裁决
- 全绿 + `pending-compile.jsonl` 有待编译 -> `/ke-compile` 编译
- 有 fail -> 按修复建议处理后重跑本命令确认
- 全绿无待办 -> 系统健康，正常使用
