export const meta = {
  name: 'wiki-slow-loop',
  description: 'Wiki 慢环提纯 — 维度并行扫描→深度交叉比对→工单合成→指挥官确认后执行',
  phases: [
    { title: '1: Signal Detection', detail: '4 维度并行扫描：内容重叠、定义冲突、僵尸内容、结构健康' },
    { title: '2: Synthesize', detail: '去重→优先级排序→合成提纯工单' },
    { title: '3: Output', detail: '输出结构化工单，待指挥官确认' },
  ],
}

// 路径配置（env化，朋友环境通过env覆盖；bash自解析，规避sandbox禁process.env）
const VAULT = '${WIKI_VAULT_PATH:-$HOME/Documents/Obsidian Vault}'
const DAEMON = VAULT + '/wiki/.wiki-daemon.py'


// ══════════════════════════════════════════════════
// Schemas
// ══════════════════════════════════════════════════

const OVERLAP_FINDING = {
  type: 'object',
  additionalProperties: false,
  properties: {
    groupTag: { type: 'string' },
    pages: { type: 'array', items: { type: 'string' } },
    overlapDescription: { type: 'string' },
    suggestedAction: { type: 'string', enum: ['MERGE', 'REFACTOR', 'DELEGATE', 'KEEP'] },
    confidence: { type: 'string', enum: ['HIGH', 'MEDIUM', 'LOW'] },
    mergedTarget: { type: 'string' },
  },
  required: ['groupTag', 'pages', 'overlapDescription', 'suggestedAction', 'confidence'],
}

const CONFLICT_FINDING = {
  type: 'object',
  additionalProperties: false,
  properties: {
    conceptName: { type: 'string' },
    pages: { type: 'array', items: { type: 'string' } },
    conflictType: { type: 'string', enum: ['FACTUAL', 'METHODOLOGICAL', 'INTERPRETIVE', 'OUTDATED'] },
    description: { type: 'string' },
    suggestedResolution: { type: 'string' },
    confidence: { type: 'string', enum: ['HIGH', 'MEDIUM', 'LOW'] },
  },
  required: ['conceptName', 'pages', 'conflictType', 'description', 'confidence'],
}

const ZOMBIE_FINDING = {
  type: 'object',
  additionalProperties: false,
  properties: {
    page: { type: 'string' },
    reason: { type: 'string' },
    daysSinceAccess: { type: 'number' },
    relevanceScore: { type: 'number' },
    action: { type: 'string', enum: ['ARCHIVE', 'UPDATE', 'VERIFY', 'DELETE'] },
    confidence: { type: 'string', enum: ['HIGH', 'MEDIUM', 'LOW'] },
  },
  required: ['page', 'reason', 'action', 'confidence'],
}

const STRUCTURE_FINDING = {
  type: 'object',
  additionalProperties: false,
  properties: {
    issue: { type: 'string' },
    location: { type: 'string' },
    description: { type: 'string' },
    suggestedAction: { type: 'string' },
    priority: { type: 'string', enum: ['P0', 'P1', 'P2'] },
  },
  required: ['issue', 'description', 'priority'],
}

const WORK_ORDER = {
  type: 'object',
  additionalProperties: false,
  properties: {
    items: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          id: { type: 'string' },
          category: { type: 'string', enum: ['MERGE', 'RESOLVE_CONFLICT', 'ARCHIVE', 'RESTRUCTURE', 'UPDATE_STATUS', 'FIX_LINK'] },
          priority: { type: 'string', enum: ['P0', 'P1', 'P2'] },
          title: { type: 'string' },
          finding: { type: 'string' },
          evidence: { type: 'string' },
          suggestedAction: { type: 'string' },
          affectedPages: { type: 'array', items: { type: 'string' } },
          confidence: { type: 'string', enum: ['HIGH', 'MEDIUM', 'LOW'] },
          effort: { type: 'string', enum: ['LOW', 'MEDIUM', 'HIGH'] },
        },
        required: ['id', 'category', 'priority', 'title', 'finding', 'suggestedAction', 'confidence'],
      },
    },
    summary: { type: 'string' },
    totalFindings: { type: 'number' },
    highConfidenceCount: { type: 'number' },
  },
  required: ['items', 'summary', 'totalFindings'],
}

// ══════════════════════════════════════════════════
// Phase 1: Signal Detection (4 维度并行)
// ══════════════════════════════════════════════════
phase('1: Signal Detection')

const [overlaps, conflicts, zombies, structure] = await parallel([
  // Agent A: 内容重叠检测
  () => agent(`
    你是内容重叠检测专家。扫描 Wiki 中内容高度重叠的页面组。

    方法:
    1. bash 获取所有页面的 tags:
       find "${VAULT}/"wiki/ -name "*.md" -not -path "*/_archived/*" -not -name ".*" | xargs grep -l "^tags:" | head -50
    2. 对 tags 分组，找出 ≥3 页共享同一 tag 的组
    3. 对每个 tag 组，用 mcp__obsidian__read_multiple_notes 批量读取页面
    4. 判断: 这些页面说的是一件事吗？内容重叠度？

    重点关注（高价值发现）:
    - 同一项目内，events/ 下有多个事件描述同一决策的不同阶段 → 建议合并为 _narrative
    - concepts/ 或 entities/ 下多个页面定义同一概念 → 建议合并为权威页面
    - 项目专属概念出现在全局 concepts/ 下 → 建议迁移

    过滤规则:
    - 纯 tracker 数据页（pinned=true, type=tracker）之间的 tag 重叠不算
    - timeline 文件之间的日期重叠是正常的
    - 只在 ≥3 页确实"说同一件事"时报告

    返回: 高置信度 ≥ MEDIUM 的发现
  `, { label: 'detect:overlaps' }),

  // Agent B: 定义不一致检测
  () => agent(`
    你是定义一致性检测专家。扫描 Wiki 中同一概念在不同页面的定义是否一致。

    方法:
    1. 读取 wiki/.ai-vocab.md 获取所有概念列表和它们的定义位置
    2. 对每个出现在 ≥2 个页面的概念（如"网格引擎"出现在量子项目 concepts/ 和 research/ 中）:
       - 读取两个页面的定义段落
       - 比对: 定义一致？有矛盾？版本更新导致一个过时？
    3. 特别关注:
       - 项目专属概念被多处定义（应在 concepts/ 下只有一个权威定义）
       - 实体页面和项目页面中对同一实体的描述不一致
       - 投资跟踪 concepts/ 中的规则定义是否与实际执行一致

    过滤规则:
    - INTERPRETIVE 类型矛盾（不同视角的合理差异）标 LOW，不报
    - 只报告 FACTUAL（事实矛盾）和 OUTDATED（一个过时）类型
    - 置信度 ≥ MEDIUM 才报告

    返回: 高价值定义冲突
  `, { label: 'detect:conflicts' }),

  // Agent C: 僵尸内容检测
  () => agent(`
    你是僵尸内容检测专家。找出 Wiki 中的"僵尸页面"——存在但已失去价值的页面。

    方法:
    1. bash 运行 python3 "${DAEMON}" status 获取全貌
    2. bash 运行 python3 "${DAEMON}" orphan-list 获取孤儿列表
    3. 对以下页面重点审查:
       - access_count = 0 且 validated > 60 天前
       - staleness = "outdated" 且未标记 sunset_candidate
       - 孤儿页面（link_count = 0）且非事件/timeline 类型
       - 项目目录 ≥20 文件（目录膨胀信号）
       - 项目目录只有 synthesis.md 无子页面（空壳项目）

    4. 逐页读取判断:
       - 内容是否仍有时效性？
       - 是否已被其他页面覆盖？
       - 是否属于"历史参考"价值（压为 _narrative 而非删除）？

    只报告确实失去价值的页面。不确定的不要报。
    对每个发现给出具体行动建议: ARCHIVE / UPDATE / VERIFY / DELETE
  `, { label: 'detect:zombies' }),

  // Agent D: 结构健康
  () => agent(`
    你是 Wiki 结构健康检测专家。检查 Wiki 的目录和归属结构是否合理。

    方法:
    1. bash: python3 "${DAEMON}" counts
       获取每个项目的文件分布
    2. bash: python3 ~/.claude/skills/wiki-management/scripts/wiki_checks.py --alien --json
       获取非标字段分布（结构问题的代理指标）
    3. 检查:
       A. 归属混乱: concepts/ 下是否有项目专属概念（应迁移到 projects/{项目}/concepts/）？
          entities/ 下是否有只被一个项目引用的实体（可以留在全局，但要标记）？
       B. 空壳项目: 哪些项目只有 synthesis.md 没有 events/concepts 子目录？
          它们是否还应保持为项目目录？还是应降级为概念页？
       C. 索引一致性: .ai-vocab.md 的项目状态 vs index.md 的项目状态是否一致？
          上次慢环发现的"AI创业探索 [轻量]"标记错误是否已修复？
       D. 目录膨胀: 哪些项目目录 ≥20 文件？是否需要拆分子目录？
       E. 孤零零的页面: 全站有 60 个孤儿页面（45%），排除 events/timeline 类型后的"有效孤儿率"是多少？

    返回: P0/P1/P2 优先级的结构问题清单
  `, { label: 'detect:structure' }),
])

log(`Phase 1 信号检测完成（4 维度）`)
log(`  重叠: ${overlaps?.slice(0,100) || '(无)'}...`)
log(`  冲突: ${conflicts?.slice(0,100) || '(无)'}...`)
log(`  僵尸: ${zombies?.slice(0,100) || '(无)'}...`)
log(`  结构: ${structure?.slice(0,100) || '(无)'}...`)

// ══════════════════════════════════════════════════
// Phase 2: Synthesize Work Order（跳过 Phase 2 pipeline，直接合成）
// ══════════════════════════════════════════════════
phase('2: Synthesize')

const workOrder = await agent(`
  你是 Wiki 提纯工单合成专家。基于 Phase 1 五个维度检测 Agent 的全部发现，合成一份结构化提纯工单。

  === 维度 A: 内容重叠 ===
  ${overlaps || '(无发现)'}

  === 维度 B: 定义冲突 ===
  ${conflicts || '(无发现)'}

  === 维度 C: 僵尸内容 ===
  ${zombies || '(无发现)'}

  === 维度 D: 结构健康 ===
  ${structure || '(无发现)'}

  合成规则:
  1. 去重: 如果多个 Agent 报告了同一页面组的问题 → 合并为一条工单
  2. 优先级:
     P0 — 事实性错误/矛盾，影响 AI 判断准确性
     P1 — 内容冗余/过时/归属混乱，影响查询效率
     P2 — 结构优化建议，不影响当前使用
  3. 只保留置信度 ≥ MEDIUM 的发现。LOW 的全部丢弃。
  4. 每条工单必须包含:
     - 具体页面路径（不是笼统的项目名）
     - 操作所需的具体步骤（"把 A 合并到 B"而非"优化 A 和 B"）
     - 预估执行难度（LOW=改一个文件, MEDIUM=改2-5个文件, HIGH=改>5个文件或需跨项目协调）
  5. 质量 > 数量: 宁可有 5 条高价值工单，不要 20 条"也许可以优化"的模糊建议。

  分类标记:
  - MERGE: 内容重叠，多个页面合并为一个
  - RESOLVE_CONFLICT: 定义矛盾，需要确认正确版本
  - ARCHIVE: 僵尸页面归档
  - RESTRUCTURE: 目录/归属调整
  - UPDATE_STATUS: 状态标记与实际不符
  - FIX_LINK: 链接问题

  最后，写一段 summary（100 字以内），概括本次慢环的核心发现。
`, { schema: WORK_ORDER, label: 'synthesize' })

// ══════════════════════════════════════════════════
// Phase 3: Output
// ══════════════════════════════════════════════════
phase('3: Output')

// 写入工单到文件，供指挥官审阅
await agent(`
  将以下提纯工单写入 "${VAULT}/"wiki/随笔/慢环工单-${args?.today || 'TODAY'}.md

  ${JSON.stringify(workOrder, null, 2)}

  文件格式:
  ---
  type: work-order
  created: ${args?.today || 'TODAY'}
  status: pending_approval
  ---

  # Wiki 慢环提纯工单 — ${args?.today || 'TODAY'}

  ## 摘要
  {summary}

  ## 统计
  - 总发现: {totalFindings}
  - 高置信度: {highConfidenceCount}
  - P0/P1/P2: {按优先级计数}

  ## 工单明细
  {每条工单: ID | 优先级 | 分类 | 标题 | 发现 | 证据 | 建议操作 | 影响页面 | 置信度 | 难度}

  ## 待指挥官确认
  - [ ] 逐条审核并标注 approve/reject/modify
  - [ ] 确认后回复"执行慢环工单"，AI 将逐条执行

  用 mcp__obsidian__write_note 创建文件。
`, { label: 'output' })

// ══════════════════════════════════════════════════
// Summary
// ══════════════════════════════════════════════════

log(`
🔍 ====== 慢环提纯完成 ======
  工单条目: ${workOrder?.totalFindings || '?'} 条（高置信度 ${workOrder?.highConfidenceCount || '?'}）
  工单位置: wiki/随笔/慢环工单-${args?.today || 'TODAY'}.md
============================

👆 请审阅工单，逐条确认后回复"执行慢环工单"。
`)

return {
  findings: { overlaps: overlaps.length, conflicts: conflicts.length, zombies: zombies.length, structure: structure.length },
  workOrder: { total: workOrder.totalFindings, highConfidence: workOrder.highConfidenceCount },
  outputFile: `wiki/随笔/慢环工单-${args?.today || 'TODAY'}.md`,
}
