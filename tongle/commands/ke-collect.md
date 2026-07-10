---
description: ke 采集 - 手动触发采集环（串 scanner + collector，复用 SessionEnd 逻辑，参数化）
argument-hint: [--source file|dialogue|note] [--full-scan] [--file <path>]
---

ke 采集环手动触发器：串 scanner（三源扫描）+ collector（判别 evolve/new 候选）。

**参数**：
- 无参：增量采集（等同 SessionEnd 自动触发：scanner 三源 + collector 两模式）
- `--source file`：只扫本地文件变更（scanner --source local_file）
- `--source dialogue`：只扫会话 transcript（scanner --source transcript，需 --transcript 或 KE_TRANSCRIPT_PATH env）
- `--source note`：只扫知识星球 ima（scanner --source ima）
- `--full-scan`：全量重扫（归零游标，ke慢环触发用）
- `--file <path>`：覆盖扫描根（传给 scanner --root，目录路径）

## 编排（scanner + collector）

### 第1步 scanner 扫描（产 file_change/transcript/ima 信号）

无参或 --source 未指定：
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/source-scanner.py" --source all [--full-scan]`

--source file：
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/source-scanner.py" --source local_file --root <path 或默认> [--full-scan]`

--source dialogue：
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/source-scanner.py" --source transcript --transcript <path> --session <id>`

--source note：
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/source-scanner.py" --source ima [--full-scan]`

展示 scanner stderr 输出（各源信号条数）。fail-open：某源 skip 不阻断其他源。

### 第2步 collector 判别（产 pending 候选）

先跑 --source 模式（判 file_change/transcript_candidate）：
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/discriminate-collector.py" --source`

再跑 observe 模式（判 evolve/new 候选）：
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/discriminate-collector.py"`

展示产出：pending-queue.jsonl 新增候选数。

### 第3步 报告
- scanner 各源信号条数
- collector 新增 pending 候选数（pending-queue.jsonl）
- 若 pending 候选 ≥3 条：提示"判别候选待裁决，可用 /ke-review 裁决"
- 若 working-memory [synthesized] 有无 wiki ref 条目：提示"有待编译条目，可用 /ke-compile 编译"

## 边界
- 本命令 = 手动触发采集（日常自动由 SessionEnd 触发，本命令用于补采/全量重扫/指定源）
- collector 产出的 pending 候选走 `/ke-review` 人裁决（守红线⑤⑦⑧人确认环）
- `[synthesized]` 无 wiki ref 条目走 `/ke-compile` 编译（经 pending-compile.jsonl）

## 关联
- `hooks/source-scanner.py`（薄壳调 lib/scanner，三源扫描）
- `hooks/discriminate-collector.py`（薄壳调 lib/discriminate，判别候选）
- `commands/ke-review.md`（pending 候选人裁决）
- `commands/ke-compile.md`（待编译条目 Ingest）
