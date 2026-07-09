# WIKI_SCHEMA — 页面模板与字段规范

> wiki-management 管理的页面格式标准。Ingest Stage 2 按此模板创建页面，wiki_checks.py 按此规范校验。

---

## 一、Frontmatter 字段定义

### 必填字段（所有内容页）

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `type` | string | 页面类型（见§二） | `concept` |
| `created` | date | 创建日期 | `2026-06-14` |
| `validated` | date | 最后验证日期 | `2026-06-14` |
| `relevance_score` | float(0-1) | 衰减计算后的相关性 | `0.85` |
| `access_count` | int | 被查询次数 | `3` |
| `last_access_date` | date | 最后被查询日期 | `2026-06-14` |
| `staleness` | enum | 时效状态（fresh/stale/outdated/sunset） | `fresh` |

### 可选字段

| 字段 | 类型 | 说明 | 适用类型 |
|------|------|------|---------|
| `confidence` | enum | 置信度（VERIFIED/INFERRED/AMBIGUOUS） | concept, synthesis |
| `link_factor` | float | 入链加权因子 | 所有内容页 |
| `link_count` | int | 被引用次数 | 所有内容页 |
| `pinned` | bool | 永久豁免衰减 | 核心页面 |
| `sources` | list | 来源引用列表 | concept, synthesis, query |
| `aliases` | list | 别名（用于 wikilink 匹配） | 任意 |
| `tags` | list | 标签 | 任意 |
| `conflict_with` | string | 矛盾对方的页面路径 | 有冲突标记的页面 |
| `conflict_type` | enum | factual / methodological / interpretive | 有冲突标记的页面 |

### 非内容页豁免

以下文件不需要完整 frontmatter（`wiki_checks.py` 自动跳过）：
- `.dream-config.md` `.dream-log.md` `.ai-vocab.md`
- `index.md` `log.md` `review-queue.md`
- `procedures/` 目录下所有文件
- `_archived/` 目录下所有文件

---

## 二、页面类型

### entity（实体）
持久存在的对象：人物、组织、项目、产品、工具。

```yaml
type: entity
confidence: VERIFIED          # 通常为 VERIFIED
pinned: false
# 必含 section: 基本定义、相关项目、相关概念
```

### concept（概念）
跨项目复用的抽象概念、方法论、框架。

```yaml
type: concept
confidence: VERIFIED | INFERRED | AMBIGUOUS
# 必含 section: 定义、应用场景、相关概念、来源
# 特别注意：宁可标注 INFERRED，不要滥用 VERIFIED
```

### synthesis（综述）
项目级综述，一个项目一个 `synthesis.md`。

```yaml
type: synthesis
pinned: true                  # 活跃项目建议 pinned
# 必含 section: 项目概述、当前状态、关键实体、关键概念、事件时间线
```

### event（事件）
时间点上的具体事件记录。

```yaml
type: event
# 必含 section: 时间、参与者、决策/结论、影响
# 豁免：缺失来源不报警（事件页本身就是记录）
```

### source（来源摘要）
外部资料的摘要和元数据。

```yaml
type: source
# 必含 section: 来源信息、关键摘要、关联概念
# base_weight 最低(0.6)，衰减最快
```

### procedure（流程）
操作流程、SOP、检查清单。

```yaml
type: procedure
# 必含 section: 触发条件、执行步骤、验收标准
# 位于 procedures/ 目录，豁免 frontmatter 完整性检查
```

### query（查询沉淀）
高价值查询及其结果。

```yaml
type: query
sources: [必填]               # 查询的数据来源
# 必含 section: 查询问题、查询结果、关键发现、数据来源
# 位于 queries/ 目录
```

### tracker（跟踪器）
持续更新的数据跟踪页面（市场指标、项目进度等）。

```yaml
type: tracker
pinned: true                  # 建议 pinned，永久豁免衰减
# 数据更新频繁，不参与日落归档
```

---

## 三、时效四级

| 等级 | relevance_score | staleness 值 | 行为 |
|------|----------------|-------------|------|
| 🟢 fresh | > 0.7 | `fresh` | 直接信任 |
| 🟡 stale | 0.3 - 0.7 | `stale` | 提醒验证 |
| 🔴 outdated | 0.1 - 0.3 | `outdated` | 必须先验证 |
| ⚫ sunset | < 0.1 | `sunset` | 7天缓冲后归档 |

`pinned: true`：`staleness = fresh`，`relevance_score = 1.0`，永久豁免。

---

## 四、置信度三级

| 等级 | 含义 | 使用原则 |
|------|------|---------|
| 🟢 VERIFIED | 原文逐字提及或指挥官确认 | 必须可追溯到具体来源 |
| 🟡 INFERRED | 合理推论、跨文档关联 | 标注推理链 |
| 🔴 AMBIGUOUS | 信息不足、冲突、待确认 | 标记 #conflict 或写入 review-queue |

**核心原则：宁可标注 INFERRED，不要滥用 VERIFIED。**

---

## 五、Wikilink 规范

### 格式
```markdown
[[projects/量子项目/synthesis|量子项目综述]]   # 带别名
[[concepts/LLM Wiki方法论]]                      # 不带别名
```

### 链接要求

| 页面类型 | 应链接到 |
|---------|---------|
| entity | 参与的项目、相关概念 |
| concept | 相关实体、应用场景、来源 |
| event | 参与者、涉及概念、所属项目 |
| synthesis | 关键实体、关键概念 |

每页必须包含 `## 相关页面` section，至少 1 个 `[[link]]`。

---

## 六、知识冲突优先级

```
当前文件状态 > Wiki VERIFIED 内容 > Wiki INFERRED 内容
```

发现冲突时：
1. 以最新文件为准
2. 在 Wiki 页面追加 `conflict_with` + `conflict_type`
3. 写入 `review-queue` 矛盾 section
4. 追加 `#conflict` 标签

---

## 七、日期格式

统一使用 `YYYY-MM-DD`：
```yaml
created: 2026-06-14
validated: 2026-06-14
last_access_date: 2026-06-14
```

---

## 相关页面

- [[.dream-config]] — 衰减公式与参数
- [[.lint-rules]] — 5 条硬 lint 规则
- [[review-queue]] — 工单队列
- [[../SKILL.md|wiki-management SKILL.md]] — 操作流程
