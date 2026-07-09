---
description: ke 编译 - 把待编译候选 Ingest 成 wiki 页面（编排 prep 脚本 + CC Ingest S1/S2 + finalize 脚本，守 SKILL.md §3.0 硬约束 + §3.1 两阶段）
argument-hint: [--candidate <idx>] [--file <path>]
---

ke 编译环执行器：把 pending-compile.jsonl 待编译条目 Ingest 成 wiki 页面。

**参数**：
- 无参：编译全队列（所有 pending 条目）
- `--candidate <idx>`：只编译第 idx 条（idx 见第0步 prep 输出的 pending 清单）
- `--file <path>`：直接编译指定文件（不经队列）

## 编排（prep 脚本 + CC Ingest S1/S2 + finalize 脚本）

### 第0步 前置脚本（机械修复 + 待编译清单）

`python3 "${CLAUDE_PLUGIN_ROOT}/lib/compile.py" --prep`

展示输出：
- `fixed`：wiki_checks.py --fix 机械修复结果（frontmatter/死链/staleness，fail-open 跑失败不阻断）
- `pending`：待编译条目清单（title + added_ts）
- `pending_count`：待编译条目数

若 `pending_count=0` 且无 `--file`：报告"无待编译条目，采集环产出后再来"，结束。

### 第1步 确定编译范围

- `--file <path>`：编译指定文件（单条，不经队列，跳过第4步 finalize）
- `--candidate <idx>`：只编译第 idx 条
- 无参：编译全队列。**按 idx 从大到小顺序**编译+finalize（每条编译完立即 finalize，避免 finalize 后 pending 列表 idx 位移）

### 第2步 CC Ingest S1 分析（按 SKILL.md §3.1，**不写文件**）

对每条待编译条目：
1. 读取原始资料全文（working-memory.md 对应 `## Topic: <title>` 段全文 / `--file` 文件全文，非摘要）
2. 识别：资料类型、主题领域、与已有 Wiki 的关联点
3. 抽取：实体（人物/组织/项目）/ 概念术语 / 事件 -> 是否已有对应页面？
4. 关系判断：补充/修正/矛盾/无关 + 潜在 `[[wikilink]]` 连接点
5. 缺口与冲突标记（与已有 Wiki 矛盾处）
6. 输出分析摘要展示给指挥官审阅（实体列表 + 概念列表 + 关系图 + 冲突标记 + 建议动作），**不写文件**

**守 §3.0.1 红线1**：S1/S2 不可合并（禁止"边读边写"）。

### 第3步 CC Ingest S2 生成（按 SKILL.md §3.1 + WIKI_SCHEMA 模板，写文件）

基于 S1 分析摘要：
1. 按 `WIKI_SCHEMA.md` 模板创建/更新 wiki 页面（wiki 根走 `WIKI_VAULT_PATH` env）
2. 初始化 frontmatter（relevance_score / access_count / last_access_date / aliases）
3. 补充 aliases + 建立 `[[wikilink]]` 双向链接
4. 更新 index.md 索引/关键词映射
5. 冲突标记：若 S1 发现矛盾 -> 在两篇页面追加 `#conflict` 标签

**守 §3.0.1 红线4**：写页面必须用 WIKI_SCHEMA.md 模板，禁止自由格式。

### 第4步 收尾脚本（标 compiled + vocab 时间戳）

对每条已编译的 pending 条目，按其 idx 执行：
`python3 "${CLAUDE_PLUGIN_ROOT}/lib/compile.py" --finalize <idx>`

--file 模式跳过此步（不经队列，无 pending 条目可标）。

## 红线（守 SKILL.md §3.0.1）
- **红线0**：编译前必须 prep（含 wiki_checks.py --fix），不依赖 AI 记忆
- **红线1**：Ingest S1/S2 不可合并（禁止边读边写）
- **红线4**：写页面必须用 WIKI_SCHEMA.md 模板

## 边界
- **基础 Ingest（本命令）= 日常增量**：待编译条目 <5 页用本命令逐条 Ingest
- **全量 Workflow = 按需非日常**：≥5 页或跨域重构走 `wiki-full-compile.js`（30 Agent ~750K token，见 `~/.claude/workflows/`）
- 本命令的 prep/finalize 是脚本机械部分（0 token），S1/S2 是 CC 按 SKILL.md 执行（AI 驱动）

## 关联
- `skills/wiki-management/SKILL.md` §3.0（硬约束）/ §3.1（Ingest 两阶段）
- `lib/compile.py` prep/finalize（脚本机械部分）
- `commands/ke-collect.md`（手动采集，产出 pending-compile 候选）
