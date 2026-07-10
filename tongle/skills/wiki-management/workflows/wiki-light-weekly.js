export const meta = {
  name: 'wiki-light-weekly',
  description: 'Wiki 轻量周检 — 死链扫描修复 + access_count + index 刷新。不含信号提取和对抗验证。~6 Agent / ~150K token',
  phases: [
    { title: '0: Pre-flight', detail: '锁 + wiki_checks.py --fix --json' },
    { title: '1: Scan & Fix', detail: '死链扫描 + Agent 修复 + access_count 更新' },
    { title: '2: Regenerate', detail: '.ai-vocab + index 并行生成' },
    { title: '3: Finalize', detail: 'log + 释放锁 + 摘要' },
  ],
}

// 路径配置（env化，朋友环境通过env覆盖；bash自解析，规避sandbox禁process.env）
const VAULT = '${WIKI_VAULT_PATH:-$HOME/Documents/Obsidian Vault}'
const DAEMON = VAULT + '/wiki/.wiki-daemon.py'


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

// Phase 0: Pre-flight
phase('0: Pre-flight')

const preflight = await agent(`
  在 "${VAULT}/"wiki/ 目录下按顺序执行（一次一行，等前一行成功再执行下一行）：

  1. python3 "${DAEMON}" lock-acquire

  如果返回 "ok": false，报告 "BLOCKED" 并停止。
  如果返回 "ok": true，继续执行：

  2. python3 "${DAEMON}" preflight --fix-stray --fix-empty
  3. python3 "${DAEMON}" counts
  4. python3 "${DAEMON}" access-update

  返回所有命令的完整输出。明确指出是否 BLOCKED。
`, { label: 'preflight' })

log(`Pre-flight: ${preflight}`)

if (preflight.includes('BLOCKED') || preflight.includes('blocked')) {
  return { status: 'BLOCKED', reason: '另一个编译实例正在运行' }
}

// Phase 1: Scan & Fix deadlinks
phase('1: Scan & Fix')

const scan = await agent(`
  扫描 "${VAULT}/"wiki/ 下所有 .md 文件中的内部链接（[[...]] 格式）。
  检查每个链接是否指向存在的页面或文件。

  对每个死链报告：
  - sourcePage: 源页面路径
  - deadTarget: 失效的目标
  - context: 链接周围的文字上下文（1行）
  - suggestedFix: 推荐的修复方案（移除链接/更新目标/标记待处理）

  只报告确实断开的链接，不要报告外部 URL。
`, { schema: DEADLINK_SCHEMA })

log(`死链扫描: ${scan.totalCount} 个`)

if (scan.deadlinks && scan.deadlinks.length > 0) {
  const fixResult = await agent(`
    以下是 ${scan.totalCount} 个死链，逐一修复：

    ${JSON.stringify(scan.deadlinks, null, 2)}

    规则：
    - 能确定新路径的 → 更新为目标路径
    - 无法确定但内容存在 → 搜索相似标题，找到后更新
    - 完全找不到 → 移除 [[ ]] 包裹，保留文字
    - 每次修改后用 mcp__obsidian__read_note 确认修改成功

    返回修复结果：{ fixed: number, skipped: number, details: string[] }
  `, { label: 'fix-deadlinks' })

  log(`死链修复: ${JSON.stringify(fixResult)}`)
}

// Phase 2: Regenerate index
phase('2: Regenerate')

await parallel([
  () => agent(`
    扫描 "${VAULT}/"wiki/ 下所有 .md 文件。
    对每个文件提取 title + 前 3 个 tags + 100 字以内的摘要。

    生成或更新 .ai-vocab.json 文件，格式：
    {
      "generated": "<ISO timestamp>",
      "entries": [
        { "path": "相对路径", "title": "标题", "tags": ["tag1"], "summary": "摘要" }
      ]
    }

    用 mcp__obsidian__write_note 写入 wiki/.ai-vocab.json。
  `, { label: 'vocab', model: 'haiku' }),

  () => agent(`
    扫描 "${VAULT}/"wiki/ 下的目录结构。
    生成 wiki/.index.json：
    {
      "generated": "<ISO timestamp>",
      "totalPages": N,
      "totalTags": N,
      "directories": ["dir1", "dir2", ...],
      "pagesByTag": { "tag": ["page1", "page2", ...] }
    }

    用 mcp__obsidian__write_note 写入 wiki/.index.json。
  `, { label: 'index', model: 'haiku' }),
])

log('Index regenerated')

// Phase 3: Finalize
phase('3: Finalize')

const countsRaw = await agent(`
  在 "${VAULT}/"wiki/ 执行:
  python3 "${DAEMON}" counts

  返回 JSON 输出。
`, { model: 'haiku', label: 'counts' })

const summary = await agent(`
  汇总本次 Wiki 轻量周检结果：

  Pre-flight 输出:
  ${preflight}

  死链扫描: ${scan.totalCount} 个
  死链修复: ${scan.deadlinks ? scan.deadlinks.length : 0} 个处理

  Counts:
  ${countsRaw}

  生成一段简洁的摘要（中文，3-5行），格式：
  - 总页数 / 死链修复数 / 新增页数
  - 特别事项（如有）
  - 下次全量编译建议（如发现较多问题则建议提前）

  然后执行收尾:
  1. python3 "${DAEMON}" lock-release
  2. mcp__obsidian__write_note 追加 wiki/.compile-log.md（追加到文件顶部，时间戳 + 摘要 + "---" 分隔线）

  最后更新飞轮健康仪表盘 wiki/.flywheel-health.md:
  3. 用 mcp__obsidian__patch_note 更新 "距上次编译" 行（如果本次是一周内最新的一次维护）
  4. 更新 "最近更新" section 追加本次周检记录
`, { label: 'finalize' })

log(summary)

return {
  status: 'OK',
  deadlinksFound: scan.totalCount,
  deadlinksFixed: scan.deadlinks ? scan.deadlinks.length : 0,
  summary,
}
