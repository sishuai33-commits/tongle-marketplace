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

## 建议下一步（裁决完 AI 主动建议）
- 裁决完 pending 候选后，查 `pending-compile.jsonl`：有待编译条目 -> `/ke-compile` 编译进 wiki
- 无待编译 + 有未采原料 -> `/ke-collect` 补采
- 裁决经验已入 experience 棘轮 -> 积累跨多 session 后可触发跨域提炼识别跨域同构（见下"跨域提炼衔接"）
- 都无 -> 判别环闭环，正常使用

## 跨域提炼衔接（跨域同构识别）

裁决产出的经验入 `discriminate-experience.jsonl` 棘轮，积累跨多 session 后可触发跨域提炼识别跨域同构模式（不同领域相同结构，如"偏差->累积->评估->确认->棘轮"在 Agent 进化与 ke 判别都出现）。

**触发**（`cross-domain-extract.py` 保持独立脚本，本命令只编排触发，职责不混入 review 逻辑）：

`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/cross-domain-extract.py" [--min-sessions 3] [--dry-run] [--no-llm]`

- `--min-sessions 3`：跨≥N session 才算候选（默认3，经验少时无候选）
- `--dry-run`：只看候选不写入（建议先跑看候选再决定）
- `--no-llm`：跳过 LLM 判定降级纯规则（test 用）

**输出**：`cross-domain-patterns.jsonl`（跨域模式库，重新生成但保留已确认状态/消费佐证/verdict，守红线⑤人确认不丢）

**守红线⑤**：跨域模式 `human_confirmed` 默认 false，人确认后才可用于消费环。脚本只产 LLM 依据（两域映射+可溯源证据+理由）供人裁决，不自动判 confirm/reject。

**scope 边界（v1.4.0）**：本衔接只补命令入口 + 流程衔接。跨域提炼深度（认脸主题级去重/语义去重/跨域同构识别准确率）是独立架构级问题（见独立评估风险#1#2），不在 v1.4.0。
