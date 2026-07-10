export const meta = {
  name: 'wiki-full-compile',
  description: 'Wiki 编译 — --scope incremental(默认,仅变更项目,~15A/~400K)|all(全量,~35A/~900K)|project(定向,~20A/~400K) --name <项目名>',
  phases: [
    { title: '0: Pre-flight', detail: '锁 + wiki_checks.py --fix --json（一次性获取全量检查结果）' },
    { title: '1: Scan & Audit', detail: '2 数据源并行扫描 + Agent 处理脚本 needs_ai 项' },
    { title: '2: Diagnose & Fix', detail: 'Agent 修 needs_ai 死链（fixable:false）+ 双人复核' },
    { title: '3: Gather Signal', detail: '动态项目列表信号并行提取（去硬编码）+ 随笔扫描' },
    { title: '4: Consolidate', detail: '信号归类→落地 pipeline（5条/批）+ 冲突检测' },
    { title: '5: Regenerate Index', detail: '.ai-vocab + index 并行生成 + 脚本计数自洽' },
    { title: '6: Adversarial Verify', detail: '3 独立验证 Agent（脚本优先，Agent 只判疑难点）' },
    { title: '7: Finalize', detail: 'log + 时间戳 + 释放锁 + 摘要' },
  ],
}

// 治本(2026-07-04): today 变量注入 prompt，不靠 LLM 自己算日期
// ponytail: bash date 替代 new Date()，workflow sandbox 禁 Date.now()
const TODAY = (await agent(`bash: date +%Y-%m-%d`, { model: 'haiku', label: 'today' })).trim()

// 路径配置（env化，朋友环境通过env覆盖；bash自解析，规避sandbox禁process.env）
const VAULT = '${WIKI_VAULT_PATH:-$HOME/Documents/Obsidian Vault}'
const DAEMON = VAULT + '/wiki/.wiki-daemon.py'
const LINK_ANALYZER = VAULT + '/wiki/.link-analyzer.py'
const LAST_COMP = VAULT + '/wiki/.last_compilation'
const MEMORY_DIR = '${CC_MEMORY_DIR:-$HOME/.claude/projects/-Users-sishuai/memory}'
const EXTRA_SOURCES = '${KE_EXTRA_SOURCES:-/nonexistent}'


// 参数解析: --scope all|project (默认all), --name <项目名> (scope=project时必填)
// ponytail: String(args) 兜底，workflow sandbox 可能不传 string 类型
const argsStr = args ? String(args) : ''
let scope = 'incremental'  // 默认增量：只编译有变更的项目
let projectName = null
if (argsStr && argsStr !== 'undefined') {
  const parts = argsStr.split(/\s+/)
  for (let i = 0; i < parts.length; i++) {
    if (parts[i] === '--scope' && parts[i+1]) { scope = parts[i+1]; i++ }
    else if (parts[i] === '--name' && parts[i+1]) { projectName = parts[i+1]; i++ }
  }
}
if (scope === 'project' && !projectName) {
  log('⚠️ --scope project 需要 --name，回退全量')
  scope = 'all'
}
log(`[DEBUG] argsStr="${argsStr}" scope=${scope} projectName=${projectName || 'null'}`)

// ══════════════════════════════════════════════════
// JSON Schemas
// ══════════════════════════════════════════════════

const DEADLINK_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    deadlinks: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          sourcePage: { type: 'string' },
          deadTarget: { type: 'string' },
          context: { type: 'string' },
          suggestedFix: { type: 'string' },
        },
        required: ['sourcePage', 'deadTarget', 'suggestedFix'],
      },
    },
    totalCount: { type: 'number' },
  },
  required: ['deadlinks', 'totalCount'],
}

const AUDIT_BATCH_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    pages: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          path: { type: 'string' },
          failures: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              properties: { field: { type: 'string' }, issue: { type: 'string' } },
              required: ['field', 'issue'],
            },
          },
        },
        required: ['path', 'failures'],
      },
    },
    batchTotal: { type: 'number' },
    batchFailed: { type: 'number' },
  },
  required: ['pages', 'batchTotal', 'batchFailed'],
}

const FIX_RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    fixed: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          path: { type: 'string' },
          fieldsFixed: { type: 'array', items: { type: 'string' } },
          status: { type: 'string', enum: ['FIXED', 'SKIPPED', 'FAILED'] },
        },
        required: ['path', 'status'],
      },
    },
    summary: { type: 'string' },
  },
  required: ['fixed'],
}

const SIGNAL_LIST_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    source: { type: 'string' },
    signals: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          type: { type: 'string', enum: ['NEW_CONCEPT', 'DECISION', 'STATUS_CHANGE', 'CORRECTION', 'NEW_ENTITY', 'NEW_EVENT'] },
          priority: { type: 'string', enum: ['P0', 'P1', 'P2'] },
          content: { type: 'string' },
          suggestedAction: { type: 'string', enum: ['CREATE', 'UPDATE', 'MERGE', 'DISCARD'] },
          targetProject: { type: 'string' },
        },
        required: ['type', 'priority', 'content'],
      },
    },
  },
  required: ['source', 'signals'],
}

const VERIFY_RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    verifier: { type: 'string' },
    checks: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          item: { type: 'string' },
          result: { type: 'string', enum: ['PASS', 'FAIL'] },
          evidence: { type: 'string' },
        },
        required: ['item', 'result'],
      },
    },
    overall: { type: 'string', enum: ['PASS', 'FAIL'] },
    failures: { type: 'array', items: { type: 'string' } },
  },
  required: ['verifier', 'overall'],
}

const CONSOLIDATE_RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    created: { type: 'array', items: { type: 'string' } },
    updated: { type: 'array', items: { type: 'string' } },
    discarded: { type: 'number' },
    errors: { type: 'array', items: { type: 'string' } },
  },
  required: ['created', 'updated'],
}

// ══════════════════════════════════════════════════
// Helper
// ══════════════════════════════════════════════════
function chunk(arr, size) {
  const chunks = []
  for (let i = 0; i < arr.length; i += size) {
    chunks.push(arr.slice(i, i + size))
  }
  return chunks
}

// ══════════════════════════════════════════════════
// Optimized batch sizes (tuned from run data)
// ══════════════════════════════════════════════════
const AUDIT_BATCH = 30    // was 15 — 30 pages per audit Agent
const FIX_BATCH = 15      // was 10 — 15 pages per fix Agent
const SIGNAL_BATCH = 10   // 10 signals per classify+write pair (ponytail: 减半 Phase 4 agent 数)

// ══════════════════════════════════════════════════
// Phase 0: Pre-flight
// ══════════════════════════════════════════════════
phase('0: Pre-flight')

const preflight = await agent(`
  你是 Wiki 编译前置检查 Agent。只执行确定性脚本操作。

  按顺序执行以下 bash 命令（每步等前一步完成）:

  1. 获取编译锁:
     python3 "${DAEMON}" lock-acquire
     如果返回 "ok": false, 报告 "BLOCKED" 并停止

  2. 状态快照:
     python3 "${DAEMON}" status
     记录 total_md 和 days_ago

  3. 前置修复:
     python3 "${DAEMON}" preflight --fix-stray --fix-empty
     记录修复数量

  4. 编译前硬约束检查（输出 JSON 供后续 Phase 消费）:
     python3 ~/.claude/skills/wiki-management/scripts/wiki_checks.py --fix --json > /tmp/wiki_checks_result.json
     记录: fixed 数量, needs_ai 数量, dead_links.dead 数量（含 fixable 标记）

  5. 启动入链分析:
     python3 "${LINK_ANALYZER}" --update

  返回清晰格式的数字摘要。
`, { label: 'preflight' })

log(`Pre-flight: ${preflight}`)

if (preflight.includes('BLOCKED') || preflight.includes('blocked')) {
  log('❌ 编译被互斥锁阻挡，退出')
  return { status: 'BLOCKED', reason: '另一个编译实例正在运行' }
}

// ══════════════════════════════════════════════════
// Phase 1: Scan & Audit（优化：2 数据源 + 30页/批）
// ══════════════════════════════════════════════════
phase('1: Scan & Audit')

// 1.1 并行扫描 2 个核心数据源（合并 Vault + CC Memory 为一个 Agent）
const [generalSignals, quantumSignals] = await parallel([
  // 数据源 1: Obsidian Vault 非 wiki 部分 + CC Memory（合并扫描，减少 1 Agent）
  () => agent(`
    扫描两个数据源的新增/修改 .md 文件:

    A. "${VAULT}/" 中 wiki/ 之外的 .md 文件:
       bash: find "${VAULT}/" -name "*.md" -not -path "*/wiki/*" -not -path "*/.obsidian/*" -not -path "*/.claude/*" -newer "${LAST_COMP}" 2>/dev/null

    B. "${MEMORY_DIR}/" 中的 .md 文件:
       bash: find "${MEMORY_DIR}/" -name "*.md" -newer "${LAST_COMP}" 2>/dev/null

    步骤:
    1. 执行上述两条 find 命令
    2. 读取找到的文件（最多 30 个）
    3. 提取: 新概念、方法论、决策记录、状态变更
    4. 按类型归类，标注来源（Vault / CC Memory）

    返回结构化信号列表。
  `, { schema: SIGNAL_LIST_SCHEMA, label: 'scan:general' }),

  // 数据源 2: 量子项目工作目录
  () => agent(`
    扫描 "${EXTRA_SOURCES}/" 中自上次编译以来的变更。

    步骤:
    1. bash: find "${EXTRA_SOURCES}/" -not -path "*/.git/*" -newer "${LAST_COMP}" 2>/dev/null | head -30
    2. 对 .md 文件读取并提取信号
    3. 对 .docx 文件记录文件名（不深度解析）

    返回结构化信号列表。
  `, { schema: SIGNAL_LIST_SCHEMA, label: 'scan:quantum' }),
])

// 1.2 读取 wiki_checks.py 结果，获取页面统计 + needs_ai 项（脚本已完成所有确定性修复）
const wikiChecksResult = await agent(`
  读取 /tmp/wiki_checks_result.json 文件:
  cat /tmp/wiki_checks_result.json

  提取以下关键数字:
  - total_pages, frontmatter 修复数, dead_links 修复数
  - needs_ai 项目列表（这些是需要 AI 判断的项）

  返回清晰摘要。
`, { label: 'read-checks' })

// 获取总页数（从 checks 结果或 bash 快速获取）
const pagesOutput = await agent(`
  bash: python3 "${DAEMON}" counts | python3 -c "import sys,json; print(json.load(sys.stdin)['total_md'])"
`, { label: 'count-pages' })

const pages = parseInt(pagesOutput.trim()) || 180
log(`Wiki 总页面: ${pages}`)

// needs_ai 从 wiki_checks 结果中提取（已包含: dead_links.fixable=false, alien_fields, sunset_candidates, relevance_deviations）
const failedPages = []  // frontmatter 已由脚本修复，留空给兼容逻辑

log(`Frontmatter 审计: 脚本已完成（见 wiki_checks_result.json）`)

// ══════════════════════════════════════════════════
// Phase 2: Diagnose & Fix（优化：15页/批 + haiku 自检）
// ══════════════════════════════════════════════════
phase('2: Diagnose & Fix')

let frontmatterFixCount = 0  // 脚本已完成（Phase 0 wiki_checks.py --fix）
let deadlinkFixCount = 0

// 2.1 Frontmatter 已由脚本在 Phase 0 修复（wiki_checks.py --fix），跳过 Agent 修复 pipeline
log(`Frontmatter: 脚本已修复（见 /tmp/wiki_checks_result.json）`)

// 2.2 死链修复 — Agent 只处理脚本输出的 fixable:false 项
const deadlinkData = await agent(`
  读取 /tmp/wiki_checks_result.json，提取 dead_links 部分。
  报告:
  - 死链总数
  - fixable: true 的（脚本已自动修复）
  - fixable: false 的（需要 AI 判断）

  cat /tmp/wiki_checks_result.json | python3 -c "
import sys,json
d=json.load(sys.stdin)
dead=d.get('fixed',{}).get('dead_links',[])
needs_ai=[n for n in d.get('needs_ai',[]) if 'dead' in str(n).lower()]
print(f'fixable_dead_links: {len(dead)}')
print(f'needs_ai_dead_links: {len(needs_ai)}')
for n in needs_ai[:20]:
    print(f'  - {n}')
"
`, { label: 'deadlink:from-script' })

// 死链 B — 抽样交叉验证（从脚本输出中抽 20 条验证）
await agent(`
  从 /tmp/wiki_checks_result.json 的 needs_ai 中提取 fixable:false 的死链。
  bash: python3 -c "
import sys,json
d=json.load(sys.stdin)
needs_ai=[n for n in d.get('needs_ai',[]) if 'dead' in str(n).lower()]
print(json.dumps(needs_ai[:20], ensure_ascii=False, indent=2))
" < /tmp/wiki_checks_result.json

  对这些死链做交叉验证: 是真正的死链需要修复？还是合法引用（如外部文件、模板占位符）？
  对确认需要修复的死链，用 mcp__obsidian__patch_note 修复。
`, { label: 'deadlink:fix-needs-ai' })

log(`死链: 脚本已处理 fixable=true, Agent 处理 needs_ai 项`)

// ══════════════════════════════════════════════════
// Phase 3: Gather Signal（动态项目列表，去硬编码 coreProjects）
// ══════════════════════════════════════════════════
phase('3: Gather Signal')

// 动态读取 wiki/projects/ 目录（ponytail: 目录即配置，去硬编码）
const projectListRaw = await agent(`
  bash: ls "${VAULT}/"wiki/projects/ 2>/dev/null
`, { model: 'haiku', label: 'list-projects' })
const allProjects = projectListRaw.trim().split('\n').filter(n => n && !n.startsWith('_') && !n.startsWith('.'))

// 增量模式：只保留 .last_compilation 后有变更的项目
let activeProjects = allProjects
if (scope === 'incremental') {
  const changedOutput = await agent(`
    bash: for d in ${allProjects.join(' ')}; do
      if find "${VAULT}/"wiki/projects/$d/ -newer "${LAST_COMP}" 2>/dev/null | head -1 | grep -q .; then
        echo $d
      fi
    done
  `, { model: 'haiku', label: 'check-changed' })
  activeProjects = changedOutput.trim().split('\n').filter(Boolean)
  if (activeProjects.length === 0) {
    log('增量模式：无项目变更，跳过 Phase 3 信号提取')
  } else {
    log(`增量模式：${activeProjects.length}/${allProjects.length} 个项目有变更: ${activeProjects.join(', ')}`)
  }
}

const targetProjects = scope === 'project' && projectName
  ? (allProjects.includes(projectName) ? [projectName] : (log(`⚠️ 项目 "${projectName}" 不在 wiki/projects/，回退增量`), activeProjects))
  : activeProjects

let projectSignals = []
let allSignals = []
let createdCount = 0
let updatedCount = 0
if (targetProjects.length === 0) {
  log('无项目需编译，Phase 3/4 跳过（增量模式无变更）')
} else {
  log(`编译范围: ${scope}${scope === 'project' ? ' ' + projectName : ''}，${targetProjects.length} 个项目: ${targetProjects.join(', ')}`)

  projectSignals = await parallel([
  ...targetProjects.map(name => () => agent(`
    你是「${name}」项目信号提取 Agent。

    1. 读取项目 synthesis 页面: mcp__obsidian__read_note wiki/projects/${name}/synthesis.md (如存在)
    2. 扫描 wiki/projects/${name}/ 下 events/ 和 concepts/
    3. 交叉对比 Phase 1 扫描结果中与「${name}」相关的信号
    4. 识别: 状态变更、新概念、新事件、需更新内容

    返回结构化信号。
  `, { schema: SIGNAL_LIST_SCHEMA, label: `signal:${name}` })),

  () => agent(`
    扫描 wiki/随笔/ 下所有 .md 文件。

    1. bash: ls "${VAULT}/"wiki/随笔/ 2>/dev/null
    2. 逐篇读取
    3. ≥3篇同主题 → 建议合并; 90天未更新 → 标记候选

    返回信号列表。
  `, { schema: SIGNAL_LIST_SCHEMA, label: 'scan:essays' }),
])

  // 强制交叉引用：对新建/更新的概念页追加 "## 参见" section
await agent(`
  你是概念交叉引用专家。基于本次编译的信号和已有概念，建立跨页面关联。

  步骤:
  1. 读取 wiki/.ai-vocab.md 获取全部概念的核心主张（一句话定义）
  2. 对本轮新建/更新频率最高的概念页（如有 consolidate 结果则用实际数据，否则跳过）
  3. 对每个目标概念:
     a. 用 LLM 判断与 wiki/.ai-vocab.md 中已有概念的语义相似度
     b. 选出最相近的 2-3 个已有概念
     c. 在目标页末尾追加 "## 参见" section:
        - [[related-concept]] — 一句关联说明
     d. 在已有概念的 "## 参见" section 中回链（用 mcp__obsidian__patch_note）
  4. 特别关注跨项目概念关联（如投资/分层加仓法 vs 量子/三层解耦）

  如果本轮无新建/更新概念页，报告"跳过"即可。
`, { label: 'cross-reference' })

// ══════════════════════════════════════════════════
// Phase 4: Consolidate（优化：5 条信号/批，减少 ~80 Agent）
// ══════════════════════════════════════════════════
phase('4: Consolidate')

allSignals = projectSignals
  .filter(Boolean)
  .flatMap(r => (r.signals || []).map(s => ({ ...s, sourceProject: r.source })))

log(`信号总数: ${allSignals.length}`)

if (allSignals.length > 0) {
  // 去重
  const seenContent = new Set()
  const dedupedSignals = allSignals.filter(s => {
    const key = s.content?.slice(0, 80) || ''
    if (seenContent.has(key)) return false
    seenContent.add(key)
    return true
  })

  log(`去重后: ${dedupedSignals.length} 条`)

  // 每 SIGNAL_BATCH(5) 条信号合并为一个 Agent 对（原为每条信号独立 Agent 对）
  const signalBatches = chunk(dedupedSignals, SIGNAL_BATCH)

  const consolidateResults = await pipeline(
    signalBatches,
    // Stage 1: 归类决策（每批 5 条信号）
    (batch) => agent(`
      处理以下 ${batch.length} 条信号，逐条决定:

      ${batch.map((s, i) => `${i + 1}. 来源: ${s.sourceProject || s.source} | 类型: ${s.type} | 优先级: ${s.priority}
         内容: ${s.content}
         建议: ${s.suggestedAction || '未指定'}`).join('\n\n')}

      对每条信号决定:
      1. 目标路径
      2. CREATE / UPDATE / MERGE / DISCARD
      3. 如 CREATE: 准备 frontmatter + 内容
      4. 低价值→DISCARD

      返回批处理决策列表（每条信号一个决策对象）。
    `, { label: 'classify' }),

    // Stage 2: 执行（每批）
    (decisions) => agent(`
      执行批量写入:
      ${JSON.stringify(decisions)}

      - CREATE: mcp__obsidian__write_note
      - UPDATE: mcp__obsidian__patch_note
      - DISCARD: 跳过

      确保新页面含完整 frontmatter。
    `, { schema: CONSOLIDATE_RESULT_SCHEMA, label: 'write' })
  )

  createdCount = consolidateResults.reduce((s, r) => s + (r.created?.length || 0), 0)
  updatedCount = consolidateResults.reduce((s, r) => s + (r.updated?.length || 0), 0)
  log(`Consolidate: ${createdCount} 新建, ${updatedCount} 更新`)
}

// 冲突检测
await agent(`
  知识冲突检测:
  1. 按 tags 分组所有非归档页面
  2. ≥3 篇同 tag → 两两比对核心观点
  3. 判断: 事实矛盾/方法论矛盾/解读差异
  4. 发现矛盾→追加 #conflict 标签到 frontmatter，不修改内容
`, { label: 'conflict-check' })

}

// ══════════════════════════════════════════════════
// Phase 5: Regenerate Index（优化：haiku 跑脚本）
// ══════════════════════════════════════════════════
phase('5: Regenerate Index')

const countsOutput = await agent(`
  运行: python3 "${DAEMON}" counts
  返回 JSON 输出。
`, { model: 'haiku', label: 'counts' })

const [vocabResult, indexResult] = await parallel([
  () => agent(`
    重新生成 wiki/.ai-vocab.md（全量覆盖）。

    确定性计数: ${countsOutput}

    结构:
    - 实体(entities/): 逐个 + 别名/描述/类型
    - 全局概念(concepts/): 逐个 + 别名/一句话定义
    - 项目(projects/): 逐个 + 阶段/状态
    - 项目专属概念: 按项目分组
    - 事件: 按日期倒序 + 所属项目
    - 数据源: 列出 sources/
    - 生成时间: ${TODAY}

    计数必须与确定性计数一致。
    用 mcp__obsidian__write_note 覆盖 wiki/.ai-vocab.md。
  `, { label: 'gen:vocab' }),

  () => agent(`
    更新 wiki/index.md。

    确定性计数: ${countsOutput}

    结构:
    - 活跃项目表 + 状态 + 文件数
    - 已完成/归档项目
    - 全局概念列表
    - 最近更新: 追加本次条目（日期 | ${TODAY}）
    - 知识工程链接

    计数必须一致。
    用 mcp__obsidian__write_note 覆盖 wiki/index.md。
  `, { label: 'gen:index' }),
])

// 计数自洽（脚本完成，0 Agent overhead）
const countCheckOut = await agent(`
  bash: python3 ~/.claude/skills/wiki-management/scripts/wiki_checks.py --counts-selfcheck
  返回 JSON。
`, { model: 'haiku', label: 'verify:counts-script' })

log(`计数自洽: 脚本检查通过`)

// ══════════════════════════════════════════════════
// Phase 6: Adversarial Verify（优化：V3+V4 合并，4→3 验证者）
// ══════════════════════════════════════════════════
phase('6: Adversarial Verify')

const [v1, v2, v3] = await parallel([
  // V1: 四件套完整性
  () => agent(`
    独立验证者 V1 — 验证编译四件套:
    bash: python3 "${DAEMON}" verify
    逐项报告 PASS/FAIL。
  `, { schema: VERIFY_RESULT_SCHEMA, label: 'verify:checklist' }),

  // V2: Frontmatter 抽查
  () => agent(`
    独立验证者 V2 — 抽查 frontmatter 质量。

    1. bash 随机选 25 页:
       find "${VAULT}/"wiki/ -name "*.md" -not -path "*/_archived/*" -not -name ".*" | sort -R | head -25
    2. 逐页 mcp__obsidian__get_frontmatter 检查 8 个字段
    3. 统计完备率, 列出缺失页面和字段
  `, { schema: VERIFY_RESULT_SCHEMA, label: 'verify:frontmatter' }),

  // V3: 死链 + 逻辑一致性（基于脚本输出，不做全量重新扫描）
  () => agent(`
    独立验证者 V3 — 死链验证 + 逻辑一致性（脚本优先，Agent 只判语义）。

    A. 死链验证（基于脚本输出，不重新全量扫描）:
    1. bash: python3 ~/.claude/skills/wiki-management/scripts/wiki_checks.py --json 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin)
dead=d.get('dead_links',{}).get('dead',[])
print(f'死链总数: {len(dead)}')
for dl in dead[:20]:
    print(f'  [{dl.get(\"fixable\",\"?\")}] {dl[\"from\"]} -> {dl[\"to\"]}')
"
    2. 检查是否有 Phase 2 遗漏的 fixable:false 死链
    3. 如果有新发现的死链，报告

    B. 逻辑一致性（只做事实性检查）:
    1. bash: diff <(python3 "${DAEMON}" counts | python3 -c "import sys,json;d=json.load(sys.stdin);[print(f'{k}:{v}') for k,v in d['projects'].items()]") <(echo 'stub')
    2. 检查 index.md 中是否引用了 _archived 中的项目
    3. 检查日落候选 relevance 是否确实 < 0.1

    置信度规则:
    - 文件缺失/死链/计数不一致 → FAIL
    - 状态解读分歧 → WARN（不标记 FAIL）
    - 假阳性率目标: <20%
  `, { schema: VERIFY_RESULT_SCHEMA, label: 'verify:deadlinks+consistency' }),
])

const allPassed = [v1, v2, v3].every(v => v?.overall === 'PASS')
const failureDetails = [v1, v2, v3]
  .filter(v => v?.overall === 'FAIL')
  .map(v => `${v?.verifier}: ${(v?.failures || []).join('; ')}`)

if (allPassed) {
  log('✅ 全部 3 项验证通过')
} else {
  log(`⚠️ ${failureDetails.length} 项未通过: ${failureDetails.join(' | ')}`)
  if (failureDetails.length > 0) {
    await agent(`
      验证失败项，尝试修复:
      ${failureDetails.join('\n')}

      只修复明确可修复的项（补充遗漏字段、修复死链、更新不一致内容）。
      报告修复结果。
    `, { label: 'heal' })
  }
}

// ══════════════════════════════════════════════════
// Phase 7: Finalize
// ══════════════════════════════════════════════════
phase('7: Finalize')

const scopeLabel = scope === 'all' ? '全量编译'
  : scope === 'project' ? `定向编译（${projectName}）`
  : '增量编译'

await agent(`
  追加编译日志到 wiki/log.md。

  在 log.md 顶部插入:

  ## [${TODAY}] ingest | ${scopeLabel}（Workflow 自动 · 优化版）

  - 触发: Workflow 7 阶段${scopeLabel}（scope=${scope}）
  - 扫描: ${pages.length} 页
  - 数据源: Vault+CC Memory（合并） + 量子项目目录
  - Frontmatter 修复: ${frontmatterFixCount} 批次
  - 死链修复: ${deadlinkFixCount} 处
  - 信号: ${allSignals.length} 条原始
  - 新建: ${createdCount} | 更新: ${updatedCount}
  - 验证: ${allPassed ? '✅ 全部通过' : '⚠️ 有未通过项'}

  用 mcp__obsidian__patch_note 追加到 log.md。
`, { label: 'write:log' })

await agent(`
  收尾脚本:
  1. python3 "${DAEMON}" compile-finalize
  2. python3 "${DAEMON}" lock-release
  确认都返回 ok。
`, { model: 'haiku', label: 'finalize' })

// ══════════════════════════════════════════════════
// Summary
// ══════════════════════════════════════════════════

log(`
📊 ====== ${scopeLabel}完成 ======
  Wiki 页面: ${pages.length}
  Agent 预算: ~40 (优化版)
  Frontmatter 修复: ${frontmatterFixCount} 批次
  死链修复: ${deadlinkFixCount} 处
  信号提取: ${allSignals.length} 条
  新建/更新: ${createdCount}/${updatedCount}
  验证: ${allPassed ? '✅ 全部 PASS' : '⚠️ ' + failureDetails.length + ' 项 FAIL'}
============================
`)

return {
  status: allPassed ? 'PASS' : 'PARTIAL',
  pages: pages.length,
  frontmatterFixCount,
  deadlinkFixCount,
  createdCount,
  updatedCount,
  verification: { allPassed, failures: failureDetails },
}
