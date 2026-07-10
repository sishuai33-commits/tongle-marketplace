---
name: wiki-management
description: Wiki 知识体系维护。Wiki 是中游知识结构化层——上游采集，下游消费(nuwa蒸馏/Agent/会话注入)。触发场景：Wiki 编译、知识摄入、健康检查、
  日落归档、IMA 同步、时效验证。用户说"编译 Wiki"、"ingest"、"lint"、"日落检查"、"整理 wiki"时触发。
license: MIT License
category: knowledge
source: self-built
metadata:
  version: 3.2.0
  last_modified: '2026-07-08'
  skill-author: 指挥官
---


# Wiki 知识体系维护

> 基于 Karpathy LLM Wiki 三层架构的知识库全生命周期管理
> 页面模板和格式定义见 WIKI_SCHEMA.md，衰减参数见 wiki/.dream-config.md（relevance计算数据源；"归档参考"指不再自动触发Dream，非不再使用）

## 一、触发条件

### 自动触发

| 场景 | 触发动作 |
|------|---------|
| 会话开始 | 通过 MEMORY.md 感知 wiki 存在，遇相关话题主动搜索 |
| 提到项目/实体/概念 | 加载对应 wiki 页面 |
| 新资料加入项目 | 提示执行 ingest |
| AI 查询 wiki 页面 | 触发记忆再巩固（access 追踪自动更新 access_count + last_access_date） |

### 用户指令

| 指令关键词 | 触发动作 |
|-----------|---------|
| "ingest"、"摄入"、"编译" | 执行知识摄入 |
| "lint"、"检查"、"审视" | 执行健康检查 |
| "wiki 状态" | 输出知识库概览 |
| "更新 wiki" | 增量更新 Wiki 层 |
| "日落"、"归档" | 执行知识日落 |
| "整理 wiki" | 手动触发轻量维护（扫描 stale + access=0 重判 + 产出清单） |
| "日落检查" | 执行 Forget 衰减扫描 |
| "清理 wiki" | 季度轻清理（§3.6 检查清单） |
| "同步 IMA"、"数据源状态" | IMA 数据源操作 |
| "全量编译"、"workflow 编译" | 触发 Workflow 全量编译（`/wiki-full-compile`，~30 Agent，~750K token） |

### 编译方式选择

| 场景 | 推荐方式 |
|------|---------|
| 少量新资料摄入（≤5 页） | 手动 ingest（本 skill） |
| 全量编译（按需） | Workflow (`/wiki-full-compile`)，自动并行+对抗验证 |
| 日常健康检查 | `wiki_checks.py --fix`（脚本，0 token） |
| 深度提纯/慢环 | 手动触发（说"慢环/提纯"），Workflow 慢环 (`/wiki-slow-loop`)，4维度并行+工单输出 |

Workflow 编译详情见 [[workflow-wiki-compile-experiment-2026-06-11]]。

---

## 二、架构与目录

### 2.1 Wiki 在全链路中的位置

Wiki 是知识管线的**中游结构化层**，不是终点：

```
上游（信息采集）              中游（Wiki = 知识结构）           下游（消费）              反馈
─────────────────       ──────────────────────────       ───────────────────       ──────────────
crawler/Tavily    →                                  → nuwa蒸馏 → Skill         Phase 0.5源同步
对话transcripts   →   ingest → lint → compile       → Agent适配 → 认知种子      偏差报告 → 慢环
本地文件          →   慢环提纯 → 日落归档             → darwin进化 → 优化工单     指挥官反馈 → working-memory
                       events快照（架构变更记录）      → 会话注入(AGENTS.md)
                       ↑← ← ← ← ← ← wiki-management 的域 → → → → → ↑
                         消费结果通过反馈回路回灌 Wiki
```

**域边界**：
- wiki-management 管理**中游**：ingest/lint/compile/日落/events
- 上游采集由 crawler/Tavily/skills 各自负责
- 下游消费由 [[Agent蒸馏流水线-架构与实践-2026-06-21|Agent蒸馏流水线]]（nuwa/darwin/Agent体系）负责
- 反馈回路由 SessionEnd hook + working-memory + 慢环机制共同完成

### 2.2 目录结构

```
wiki/
├── index.md / .ai-vocab.md / log.md
├── review-queue.md   # 已冻结（不再写入），历史工单保留
├── .lint-rules.md    # Lint 硬规则（5条）
├── entities/         # 跨项目实体
├── concepts/         # 跨项目概念
├── projects/{项目}/synthesis.md + events/
│   └── events/       # 架构变更快照（轻量版本管理：改了什么/为什么/回滚条件）
├── sources/          # 来源摘要
├── queries/          # 高价值查询结果沉淀
├── procedures/       # 操作流程（系统健康周检/季度审计/摘要漂移监控）
└── _archived/        # 日落归档
```

**events/ 模式**：每次架构级变更（synthesis 重构/目录调整/跨页关联变更）写入 events 条目，文件名 `YYYY-MM-DD-<简述>.md`。这是 Wiki 的轻量版本管理——不做全量 git，只在关键节点留快照。

---

## 三、核心操作

### 3.0 编译前置：硬约束检查（必须优先执行）

> **ECC 公理1（反馈闭环）+ 公理3（不可靠元件→可靠系统）**：AI 是"不可靠元件"（会忘、会漏、会主观），编译的第一步必须是脚本强制执行硬约束检查，而非依赖 AI 记忆。

**任何编译/ingest 操作前，必须先运行**：

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/skills/knowledge-engine}/skills/wiki-management/scripts/wiki_checks.py" --fix
```

**脚本职责（机械操作，AI 不可跳过）**：
- frontmatter 完整性检查 + 自动注入/补齐
- 全量 wikilink 校验 → 死链分类（路径错误自动修 / 目标不存在标出）
- relevance_score 按 .dream-config 公式重算
- staleness 与实际日期自动对齐
- 日落候选标记 + 缓冲到期自动归档

**AI 职责（判断层，脚本不做）**：
- 读脚本输出的 `needs_ai` 清单 → 逐条判断 → 执行修复
- 写 .ai-vocab / index 叙事条目
- 对话信号解读 + Wiki 页面内容更新

**演进规则**：当发现新的"AI 总是漏"的检查项时，向 `wiki_checks.py` 追加检查函数，而非另写新脚本。

### 3.0.1 执行纪律（v2.5.0 强化，v3.0.0 修订）

类比 invest skill 的 Phase 0 哨兵机制。以下规则不可跳过：

| # | 规则 |
|---|------|
| 0 | **编译前必须运行 `wiki_checks.py --fix`** — §3.0 硬约束，不依赖 AI 记忆 |
| 1 | **Ingest 两阶段不可合并** — Stage 1(分析)和 Stage 2(生成)必须分开执行，禁止"边读边写" |
| 2 | **日落缓冲期 7 天不可缩短** — 防止冲动归档 |
| 3 | **全量编译前先增量** — 新增 <5 页用增量，≥5 页或跨域重构才走 Workflow 全量 |
| 4 | **写页面必须用 WIKI_SCHEMA.md 模板** — 禁止自由格式 |

### 3.0.2 禁止绕过的检查点（v3.0.0 精简）

| Agent 可能的绕路借口 | 反驳 |
|---------------------|------|
| "内容很少，不需要跑 wiki_checks" | → **禁止**。wiki_checks 除了内容检查，还负责 frontmatter 补齐、死链修复、staleness 对齐——这些与内容量无关 |
| "Ingest 只有一篇文章，S1/S2 合并更快" | → **禁止**。S1 的目的是暴露冲突和缺口，单篇文章也可能与已有知识矛盾 |
| "这个页面应该立即归档，不等 7 天" | → **禁止冲动归档**。缓冲期是为了让指挥官的隐式知识有机会浮现（"等等，这个还有用"） |

### 3.1 Ingest（知识摄入）— 两阶段编译

> **设计原则**：借鉴 nashsu/llm_wiki 的两阶段编译模式。Stage 1 做结构判断（"这份资料应如何进入知识体系"），Stage 2 做页面生成（"写出具体的 Wiki 页面"）。分离的目的是在正式写入文件前暴露关系、冲突和缺口，减少"边写边偏离"的风险。

**触发**：新资料加入 / 用户指令

**Stage 1: Analysis（分析 — 不写文件）**

```
1. 读取原始资料全文（非摘要）
2. 识别：资料类型、主题领域、与已有 Wiki 的关联点
3. 抽取：
   - 实体（人物/组织/项目）→ 是否已有对应页面？
   - 概念/术语 → 是否已有对应页面？是否需要新建？
   - 事件 → 时间线定位
4. 关系判断：
   - 新资料与已有 Wiki 页面的关系（补充/修正/矛盾/无关）
   - 潜在连接点（应建立 [[wikilink]] 的已有页面列表）
5. 缺口与冲突：
   - 新资料揭示了哪些 Wiki 当前未覆盖的领域？
   - 新资料与已有 Wiki 内容是否存在矛盾？
6. 输出：分析摘要（实体列表 + 概念列表 + 关系图 + 冲突标记 + 建议动作）
   → 此摘要作为 Stage 2 的输入，不直接写入文件
```

**Stage 2: Generation（生成 — 写入文件）**

```
0. 读 wiki_checks.py --json 输出 → 断链/frontmatter缺口/过期页面(>90天) → 直接修复（review-queue 已冻结，不再写入工单）
   （--fix 已在 §3.0 执行过，此处是对剩余 needs_ai 项的 AI 判断修复）
1. 基于 S1 分析摘要 → 按 WIKI_SCHEMA 模板创建/更新页面
2. 初始化/更新 relevance_score、access_count、last_access_date
3. 补充 aliases → 建立 [[wikilink]] 双向链接
4. 更新 index.md 索引/关键词映射
5. 重新生成 .ai-vocab.md → 追加 log.md
6. 冲突标记：若 S1 发现矛盾 → 在两篇页面追加 #conflict 标签
```

**两阶段分离的价值**：
- S1 暴露的问题（缺口/矛盾/重复）在写入前被识别 → 避免"写进去再改"
- S1 分析摘要可被人工快速审阅（不读完整页面也能判断 ingest 方向是否正确）
- 非确定性风险降低：结构判断和页面写作由不同 LLM 调用完成（可交叉验证）

### 3.2 Query（知识查询）

**流程**：遇到项目/概念/实体/事件话题 → search_notes 搜索 → 按需加载 1-3 页 → 融入回答 → 自动更新 access_count/validated（access 追踪，见 §九）

详细查询规则见 MEMORY.md（始终加载），不在本 Skill 重复。

### 3.3 Lint（健康检查）

**触发**：用户指令

**核心检查项**：

| 类别 | 检查项 |
|------|--------|
| 结构 | 孤儿页面、断链、frontmatter 缺失 |
| 内容 | 矛盾信息、过时信息(>60天)、置信度异常、relevance 异常 |
| 链接网络 | link_factor异常（孤儿页面占比>50%需关注）、入链统计偏差 |
| 数据源 | 源文件消失、反向孤儿(文件在但未提取) |
| vocab | 条目数不一致、别名缺失、时效标记不一致 |

### 3.4 轻量维护（原 Dream，已简化）

> **原 Dream 三闸门/四阶段自动维护体系已停**（2026-06-19 转向）。详见 [[wiki-direction-pivot-2026-06-19]]。

**触发**：仅手动（指挥官说"整理 wiki"）

**执行内容**：
1. 运行 `wiki_checks.py --fix`（脚本机械修复）
2. 扫描 frontmatter staleness:stale 页面
3. stale 重判：架构过时(更新) / 动态没追(fresh+validated) / 纯历史(historical)
4. access=0 重判：有架构价值(保留待激活) / 纯事件(保留溯源) / 无价值(归档)。pinned 页豁免
5. 产出清单 → 指挥官确认 → 执行
6. 自检

**衰减计算保留**：relevance_score 计算公式定义在 `wiki/.dream-config.md`，wiki_checks.py 用它算 relevance（数据源）；不再当 Dream 自动触发器。

### 3.5 Forget（智能遗忘）

**触发**：手动（指挥官说"日落检查"），不再由 Dream 自动触发

**原则**：遗忘 ≠ 删除，是权重调整。低 relevance 页面日落归档到 `wiki/_archived/`（可恢复）。

**衰减公式与阈值**：见 `wiki/.dream-config.md`（relevance计算数据源）

**流程**：扫描页面计算 relevance → 四级分类(🟢>0.7/🟡0.3-0.7/🔴0.1-0.3/⚫<0.1) → ⚫进入候选 → 7天缓冲 → 无异议归档 → 90天后物理删除

### 3.6 季度轻清理（指挥官说"清理 wiki"时执行）

```
1. stale 扫描：python 精确统计 frontmatter staleness:stale（re.search ^staleness:\s*stale，排除正文误匹配）
2. stale 重判：架构过时(更新)/动态没追(fresh+validated)/纯历史(historical) 三类
3. access=0 重判：有架构价值(保留待激活)/纯事件(保留溯源)/无价值(归档) 三类。pinned 页豁免
4. 多版本认知抽检：随机抽 3 个概念，wiki 与本地/其他页无矛盾（参考量子三码体系裁决模式）
5. 产出清单 → 指挥官确认 → 执行 frontmatter 修正/归档
6. 自检：抽查 5 页分类准确
```

---

## 四、知识全生命周期

Wiki 中的知识经历五个阶段，中游（Wiki 内）+ 下游（消费）+ 反馈构成闭环：

| 阶段 | 位置 | 触发 | 操作 |
|------|------|------|------|
| **采集** | 上游 | 爬虫/Tavily/对话/本地文件 | 原始资料进入 Raw Layer |
| **加工** | 中游(Wiki) | Ingest 提取 → 创建页面 + 标注来源 + 建链接 | §3.1 两阶段编译 |
| **消费** | 下游 | 对话查询 / nuwa蒸馏 → Agent / 会话注入 | Wiki 知识被读取、引用、转化为认知种子 |
| **复用** | 下游 | Agent spawn / Skill 调用 / 新对话注入 | 同一知识在多场景被反复消费 |
| **反馈** | 回灌上游 | Phase 0.5 源同步 / 偏差报告 / 指挥官纠正 | 消费结果 → working-memory → Wiki 增补/修正 |

**Wiki 内的子循环（中游）**：

| 阶段 | 触发 | 操作 |
|------|------|------|
| **诞生** | Ingest 提取到新知识 | 创建页面 + 标注来源/置信度 + 建立链接 |
| **演进** | 新资料 / 修正 | 更新内容 + 更新 validated/relevance + 记 log |
| **合并** | 发现重复页面 | 评估合并 → 保留完整版 → 删冗余页 |
| **日落** | 过时/源文件消失/relevance<0.1 | 标记 sunset → 移 _archived/ → 更新索引 → 记 log |
| **删除** | 归档>90天 + 无引用 + 用户确认 | 物理删除 → 记 log |

**关键区分**：消费和复用不是 wiki-management 的职责范围，但 wiki-management 需要知道知识流向了哪里——这样才知道为什么某些页面 access_count 高、哪些知识值得优先保鲜。

---

## 五、时效与置信度

### 时效双标准

| 等级 | validated 距今 | relevance_score | 行为 |
|------|---------------|-----------------|------|
| 🟢 fresh | ≤ 30 天 | > 0.7 | 直接信任 |
| 🟡 stale | 31-60 天 | 0.3-0.7 | 提醒验证 |
| 🔴 outdated | > 60 天 | 0.1-0.3 | 必须先验证 |
| ⚫ sunset | — | < 0.1 | 日落候选(7天缓冲) |

两标准不一致时以 relevance_score 为准。

### 置信度（详见 WIKI_SCHEMA.md）

**核心原则**：宁可标注 INFERRED，不要滥用 EXTRACTED。

| 等级 | 含义 |
|------|------|
| 🟢 EXTRACTED | 原文逐字提及，无需推断 |
| 🟡 INFERRED | 合理推论（补充定义/跨文档关联） |
| 🔴 AMBIGUOUS | 信息不足/冲突/待确认 |

### 知识冲突优先级

当前文件状态 > Wiki validated 内容 > Wiki 未 validated 内容。发现冲突以文件为准，标记 Wiki 需更新。

---

## 六、卡片盒方法与链接

**保留分类目录 + 强化链接网络**。每个页面必须包含"相关页面"section 和 `[[链接]]`。
- 实体页 → 参与的项目 + 相关概念
- 概念页 → 相关实体 + 应用场景
- 事件页 → 参与者 + 涉及概念
- 综述页 → 关键实体 + 关键概念

---

## 七、定期审视

| 频率 | 触发方式 | 操作 |
|------|----------|------|
| **周**（手动） | 指挥官说"整理 wiki" | 轻量维护：stale 扫描 + access=0 重判 + 产出清单 |
| **月**（手动） | 指挥官触发 | 提示指挥官：回顾新增知识 + 评估覆盖度 + 识别盲区 + 审计 pinned 占比 + 检查 relevance 分布健康度 |
| **季**（手动触发） | 指挥官说"清理 wiki" | §3.6 季度轻清理检查清单：stale 重判 + access=0 重判 + 多版本抽检 + 归档执行 |

> 原 cron 定时任务（周一 8:57 Dream 巡检、周一 9:17 light-weekly、每月首个周一 9:07 提醒）已全部停用。

---

## 八、LDR 语义增强（v2.2.0 新增）

> LDR (Local Deep Research) 为 Wiki 提供语义搜索 + 深度分析能力，弥补 Obsidian MCP 关键词搜索的局限。

### 核心模式：Wiki 上下文注入

> **仅限 Wiki 知识域相关的 Skill**（如 invest、wiki-management），通用 Skill 不注入。

调用 LDR 前，先通过 Obsidian MCP 实时读取相关 Wiki 页面，拼入 LDR prompt：

```
1. 识别调用涉及的知识域
2. search_notes 找相关页面 3-5 篇
3. read_note 读取最新内容（禁止用缓存值）
4. 拼入 LDR prompt："以下是 Commander 知识库中已有的相关内容：{wiki_content}"
5. LDR 在已有知识基础上做增强分析，而非从零搜索
```

**红线**：Wiki 内容不写死到 Skill 文件，每次运行时动态读取。

### 各操作增强

| 操作 | 原方式 | LDR 增强 |
|------|--------|---------|
| **Query (3.2)** | Obsidian keyword search | 语义搜索：搜"通胀科技"也能命中"CPI上行→成长股承压" |
| **Ingest (3.1)** | 手工提取实体/概念 | LDR 自动识别实体、概念、事件，建议链接 |
| **Lint (3.3)** | 规则检查（断链/孤儿/时效） | **语义矛盾检测**："A 页说 X，B 页三个月后说 Y，是否矛盾？" |
| **碎片归类** | 手工整理 | LDR 扫描本周碎片 → 建议各自归属的 tracker 页面 |

### LDR 策略选择

| Wiki 场景 | LDR 工具 | 策略 |
|-----------|---------|------|
| 语义搜索 Wiki | `analyze_documents` (需 collection，待建) | — |
| 深度知识分析 | `detailed_research` | `evidence` |
| 多页面矛盾检测 | `detailed_research` | `dual-confidence` |
| 知识缺口扫描 | `quick_research` | `focused-iteration` |

### ~~待建：Wiki Collection~~（第二层，研究中）

> 当前 LDR `analyze_documents` 需要预建 collection。Wiki vault 的 collection 建法：
> - LDR web API: `POST /library/create_collection` + `POST /library/collections/<id>/upload`
> - Python API: `LibraryRAGService.index_local_file(file_path)` 直接索引 .md 文件
> - 建好后，全 Wiki 语义搜索立即可用（无需每次读页面再注入）
>
> 详细研究结果见 `memory/wiki-ldr-collection-plan.md`

---

## 九、运转保障：Access 追踪与双轨分工

### 9.1 Access 追踪（全自动）

AI 读 wiki 页面时，`access_count` + `last_access_date` 自动更新（由 ke plugin 的 PostToolUse hook + daemon 实现，详见 ke 项目）。

- **无痕**：frontmatter 仅两行值变，其余原样保留
- **豁免**：pinned 页面跳过 access 追踪

### 9.2 双轨分工

| 轨道 | 角色 | 内容 |
|------|------|------|
| **本地**（项目目录） | 最新细节源 | 代码、数据、会议纪要、二进制文件（乱但最新） |
| **Wiki**（Obsidian vault） | 全局架构+动态摘要源 | 结构化知识，懂全局用 |

**互补规则**：
- 新需求：先看 wiki 懂全局 → 有疑问看本地溯源
- 接续工作：先看本地最新 → wiki 核实架构
- 有本地的项目：wiki=架构+动态摘要源 / 本地=最新细节源
- 纯 wiki 项目：wiki 唯一源，保鲜标准更高

### 9.3 三层维护标准

| 层 | 内容 | 粒度 | 更新 |
|---|------|------|------|
| **架构层** | 核心架构、概念体系、设计原则 | 稳定 | 架构变才更 |
| **动态摘要层** | 当前阶段、进度、阻塞项状态 | 周或里程碑级 | 里程碑更 |
| **细节层** | 会议纪要、数据、方案产出 | 每次会议 | 本地自动 |

**时点纪律**：wiki 动态层周级，精确时点留本地，易过时点用粗粒度+"见本地"兜底。

---

## 十、使用时机

- **对话中**：AI 主动判断话题 → 三层检索 → 按需加载（每次查询触发 access 追踪）
- **新知识产生**：识别 → ingest → 记 log
- **维护**：手动触发（指挥官说"整理 wiki"/"清理 wiki"/"lint"/"日落检查"），不再自动执行

---

## 十一、数据源管理

| ID | 名称 | 类型 | 同步指令 |
|----|------|------|---------|
| `local-projects` | 本地项目目录 | filesystem | ingest |
| `obsidian-vault` | Obsidian 笔记库 | filesystem | ingest |
| `ima-notes` | IMA 笔记 | api | "同步 IMA" |

**IMA 认证**：OpenAPI（凭证：`~/.config/ima/`）。API 端点：`list_note_folder_by_cursor` → `list_note_by_folder_id` → `get_doc_content`。基于 `modify_time` 增量同步。

---

## 版本记录

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v3.2.0 | 2026-07-08 | 精简（ke v1.3.0 阶段1.9）：删与 ke 重叠的 SessionStart/注入段（§9.4 整段 + §一/§七/§十 SessionStart 行），§9.1 access 链路删 ke hook 实现细节保留事实，L113 硬编改双路径 fallback（PLUGIN_ROOT 优先 + ke symlink 兜底） |
| v3.1.0 | 2026-06-22 | 架构升级：补全链路图(上游采集→中游Wiki→下游消费→反馈)，Wiki定位从中游→明确非终点；知识生命周期扩到五阶段(采集/加工/消费/复用/反馈)；events/模式文档化；SessionStart hook 增 Wiki 健康摘要注入；域边界澄清(wiki-management中游 ↔ nuwa/darwin/Agent体系下游) |
| v3.0.0 | 2026-06-20 | 方向转向落地：砍Dream三闸门/四阶段/cron/飞轮/跨域识别/feeder/review-queue自动写入(~23处移除/标注)；补access追踪链路+双轨分工+三层维护标准+SessionStart hook(§九)；新增加季度轻清理检查清单(§3.6)；轻量维护替代Dream(§3.4)；定期审视去自动化(§七)；LDR移除Dream Phase 2-3引用 |
| v2.5.0 | 2026-06-14 | 执行纪律强化：新增 §3.0.1 不可跳过规则表(5条) + §3.0.2 禁止绕过检查点(5条)，对齐 invest v1.5.0 硬约束模式 |
| v2.4.0 | 2026-06-14 | 标准化+解耦：路径解耦(WIKI_VAULT_PATH)、衰减参数单一化(.dream-config.md frontmatter)、WIKI_SCHEMA.md创建、review-queue接线(Stage 2 step 0)、章节编号修复(九→十)、README+references/共享包完善 |
| v2.3.0 | 2026-06-13 | 六项治理升级：Ingest 两阶段分离(S1分析+S2生成)、queries/目录、review-queue、.lint-rules(5条)、摘要漂移监控、Phase 2.6 四风险检查；目录结构更新 |
| v2.2.0 | 2026-05-18 | 集成 LDR 语义增强：Wiki 上下文注入模式、Query/Ingest/Lint/Dream 增强、策略选择表；第二层待建 |
| v2.1.0 | 2026-05-14 | 接线完成：记忆再巩固接入 CLAUDE.md+/invest skill；Dream 双通道触发（会话闸门+cron）；定期审视分四级（天/周/月/季）
| v2.0.0 | 2026-05-08 | 大幅精简（758→~180行），消除与 WIKI_SCHEMA/.dream-config 的重复；新增 Dream/Forget/记忆再巩固 |
| v1.2.0 | 2026-04-10 | 新增时效标记、冲突优先级规则、关键词映射自动化 |
| v1.0.0 | 2026-04-10 | 初始版本 |
