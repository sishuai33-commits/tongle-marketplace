---
description: ke 判别人裁决 - 查看 pending 候选并裁决（包装 discriminate-resolve.py，守人确认环红线⑤⑦⑧②）
argument-hint: <index> <relation_type> <disposition> [note]
---
判别人裁决闭环。

**参数**：
- `index`：pending 候选序号（1-based，只在 pending 条目里计数，每次裁决后重排）
- `relation_type`（关系类型，人判）：`new`(新增) / `evolve`(演进) / `complement`(互补) / `conflict`(冲突)
- `disposition`（处置，人判）：`adopt`(采纳) / `discard`(丢弃) / `isolate`(隔离)
- `note`（可选）：裁决说明

## 带参数（$ARGUMENTS 非空）
执行：`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/discriminate-resolve.py" $ARGUMENTS`
展示结果（pending->resolved + experience 棘轮 append + marker 刷新）。
> discard 时脚本自动提取 keyword 写入 discard-patterns.yaml（human_confirmed=false 待人确认）；
> adopt/isolate 不提取。批量裁决建议走 lib/review 纯函数脚本，避免 keyword 公共前缀误杀。

## 无参数
1. 执行：`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/discriminate-resolve.py"`（无参自带 `list_pending_by_confidence`，按 confidence 排序 high 优先，输出 index+pattern+摘要）
2. 展示 pending 列表给指挥官
3. 请指挥官给 `<index> <relation_type> <disposition> [note]`
4. 拿到参数后执行裁决命令

## 红线（守人确认环）
- 红线⑤：relation/disposition 必须人填，脚本不自动判
- 红线⑦：experience 棘轮只升 append 不覆盖
- 红线⑧：只收自己裁决不收他人裁决结果
