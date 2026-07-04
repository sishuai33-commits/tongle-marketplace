#!/usr/bin/env node
/**
 * pre-security.js — PreToolUse 事件钩子（Bash/Write/Edit 执行前触发）
 *
 * 功能：
 *   1. 检测命令/内容中的疑似凭证（sk-*, ghp_*, api_key=, token= 等）
 *   2. 检测是否在修改关键配置文件（settings.json, CLAUDE.md, keybindings.json 等）
 *   3. 首次检测：输出警告到 stderr，exit 0（不阻断）
 *   4. 同会话重复违规/关键阻断：exit 2（阻断；CC PreToolUse 语义：exit 2 阻断工具，exit 1 不阻断）
 *   5. 正常操作：exit 0，静默
 */

'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');

// ============ 常量 ============
const SECURITY_STATE_DIR = path.join(os.homedir(), '.claude', 'instincts');
const VIOLATIONS_FILE = path.join(SECURITY_STATE_DIR, '.security-violations.json');

// 疑似凭证的正则模式
const CREDENTIAL_PATTERNS = [
  { pattern: /sk-[a-zA-Z0-9]{20,}/, label: 'OpenAI/Anthropic API Key (sk-...)' },
  { pattern: /ghp_[a-zA-Z0-9]{36}/, label: 'GitHub Personal Access Token (ghp_...)' },
  { pattern: /github_pat_[a-zA-Z0-9_]{40,}/, label: 'GitHub Fine-grained PAT' },
  { pattern: /api_key\s*[=:]\s*['"][^'"]+['"]/, label: 'API Key 明文赋值 (api_key=...)' },
  { pattern: /token\s*[=:]\s*['"][^'"]+['"]/i, label: 'Token 明文赋值 (token=...)' },
  { pattern: /password\s*[=:]\s*['"][^'"]+['"]/i, label: 'Password 明文赋值 (password=...)' },
  { pattern: /secret\s*[=:]\s*['"][^'"]+['"]/i, label: 'Secret 明文赋值 (secret=...)' },
  { pattern: /Authorization:\s*Bearer\s+[a-zA-Z0-9_\-\.]+/i, label: 'Authorization Bearer Token' },
  { pattern: /x-api-key\s*[=:]\s*[a-zA-Z0-9_\-]+/i, label: 'x-api-key Header' },
  { pattern: /AKIA[0-9A-Z]{16}/, label: 'AWS Access Key ID (AKIA...)' },
  { pattern: /private_key\s*[=:]\s*['"][^'"]+['"]/i, label: 'Private Key 明文' },
];

// 关键配置文件列表（匹配路径末尾）
const CRITICAL_CONFIG_FILES = [
  'settings.json',
  'settings.local.json',
  'CLAUDE.md',
  'keybindings.json',
  '.claude.json',
  '.claude.yaml',
  '.env',
  'AGENTS.md',
  'MCP.md',
];

// ============ 工具函数 ============

/**
 * 确保目录存在
 */
function ensureDir(dirPath) {
  if (!fs.existsSync(dirPath)) {
    fs.mkdirSync(dirPath, { recursive: true });
  }
}

/**
 * 读取违规记录
 */
function readViolations() {
  if (!fs.existsSync(VIOLATIONS_FILE)) return {};
  try {
    return JSON.parse(fs.readFileSync(VIOLATIONS_FILE, 'utf-8'));
  } catch {
    return {};
  }
}

/**
 * 写入违规记录
 */
function writeViolations(data) {
  ensureDir(SECURITY_STATE_DIR);
  fs.writeFileSync(VIOLATIONS_FILE, JSON.stringify(data, null, 2), 'utf-8');
}

/**
 * 清理过期的 session 违规记录（保留最近 50 个）
 */
function cleanOldViolations(data) {
  const sessionIds = Object.keys(data);
  if (sessionIds.length > 50) {
    const toRemove = sessionIds.slice(0, sessionIds.length - 50);
    for (const id of toRemove) {
      delete data[id];
    }
  }
}

/**
 * 递归搜索对象/字符串中的所有文本，用于凭证检测
 */
function extractTexts(obj, depth = 0) {
  if (depth > 10) return []; // 防无限递归
  if (typeof obj === 'string') return [obj];
  if (Array.isArray(obj)) {
    return obj.flatMap((item) => extractTexts(item, depth + 1));
  }
  if (obj && typeof obj === 'object') {
    return Object.values(obj).flatMap((v) => extractTexts(v, depth + 1));
  }
  return [];
}

/**
 * 检测文本中的凭证
 */
function detectCredentials(texts) {
  const findings = [];
  for (const text of texts) {
    if (!text || typeof text !== 'string') continue;
    for (const { pattern, label } of CREDENTIAL_PATTERNS) {
      if (pattern.test(text)) {
        // 提取匹配的具体内容（截断显示）
        const match = text.match(pattern);
        const snippet = match ? match[0].substring(0, 40) + (match[0].length > 40 ? '...' : '') : '';
        findings.push({ label, snippet });
      }
    }
  }
  return findings;
}

/**
 * 检查是否在修改关键配置文件
 */
function detectCriticalConfig(targetPath) {
  if (!targetPath) return null;
  const basename = path.basename(targetPath);
  // 检查完整路径是否匹配关键文件
  for (const critical of CRITICAL_CONFIG_FILES) {
    if (basename === critical || targetPath.endsWith('/' + critical)) {
      return critical;
    }
  }
  // 检查是否在 .claude/ 目录下
  if (targetPath.includes('/.claude/') || targetPath.includes('\\.claude\\')) {
    // 白名单：CC 既定写入区不当 critical config（工作产物频繁迭代是预期行为）
    // - projects/*/memory/：知识进化既定流程（P1.0 加，memory 同会话多次 Edit 不卡死）
    // - plans/：plan 模式工作产物，迭代编辑是预期行为，非系统配置（阶段二反馈修复）
    // - agents/：Agent 定义文件，迭代是正常工作流，非系统配置（指挥官指示"agent 不应被阻挡"）
    //   注：仅跳过 critical_config 判定；凭证检测照跑（内含 sk-xxx 等密钥仍拦）。
    //   真配置（settings.json/CLAUDE.md/AGENTS.md）走 basename 命中分支不走此处，保护不破
    if ((targetPath.includes('/.claude/projects/') && targetPath.includes('/memory/'))
        || targetPath.includes('/.claude/plans/')
        || targetPath.includes('/.claude/agents/')) {
      return null;
    }
    return path.basename(targetPath);
  }
  return null;
}

// ============ 主逻辑 ============

function main() {
  ensureDir(SECURITY_STATE_DIR);

  // 从 stdin 读取 CC 传入的 JSON 上下文
  let context;
  try {
    const buffer = fs.readFileSync(0, 'utf-8');
    context = buffer && buffer.trim() ? JSON.parse(buffer) : null;
  } catch {
    // 无法解析 stdin，静默退出
    process.exit(0);
  }

  if (!context || !context.tool_name) {
    process.exit(0);
  }

  const toolName = context.tool_name;
  const toolInput = context.tool_input || {};
  const sessionId = context.session_id || process.env.CLAUDE_CODE_SESSION_ID || 'unknown';

  // 收集要检测的文本
  let textsToCheck = [];

  if (toolName === 'Bash') {
    textsToCheck.push(toolInput.command || '');
  } else if (toolName === 'Write') {
    textsToCheck.push(toolInput.file_path || '');
    textsToCheck.push(toolInput.content || '');
  } else if (toolName === 'Edit') {
    textsToCheck.push(toolInput.file_path || '');
    textsToCheck.push(toolInput.old_string || '');
    textsToCheck.push(toolInput.new_string || '');
  }

  // 1. 检测凭证
  const credFindings = detectCredentials(textsToCheck);

  // 2. 检测关键配置文件修改
  let targetPath = null;
  // skills 治理守卫仅对装了 skills-management 的环境生效（指挥官自用治理）
  // 朋友未装 → 跳过 skills 目录/SKILL.md 守卫，graceful degrade（不阻断朋友正常写 skill）
  const hasSkillsMgmt = fs.existsSync(path.join(os.homedir(), '.claude', 'skills', 'skills-management'));
  if (toolName === 'Write' && toolInput.file_path) {
    targetPath = toolInput.file_path;
  } else if (toolName === 'Edit' && toolInput.file_path) {
    targetPath = toolInput.file_path;
  }
  const criticalConfig = detectCriticalConfig(targetPath);

  // 汇总本次发现的违规
  const violations = [];
  for (const cf of credFindings) {
    violations.push({
      type: 'credential_leak',
      detail: `${toolName}: ${cf.label}`,
      snippet: cf.snippet,
    });
  }
  if (criticalConfig) {
    violations.push({
      type: 'critical_config_modification',
      detail: `${toolName}: ${criticalConfig}`,
      path: targetPath,
    });
  }


  // 3. Skills 目录卫生守卫（v1.6.0 新增）
  //    ~/.claude/skills/ 必须是激活层（只有软链接），禁止直接创建真实文件/目录
  //    所有新 skill 源码必须先放到 技能库_自建/ 或 社区第三方/，再通过 ln -s 激活
  if (targetPath && hasSkillsMgmt) {
    const skillsDir = path.join(os.homedir(), ".claude", "skills");
    const sep = path.sep;
    const normTarget = path.resolve(targetPath);
    const normSkills = path.resolve(skillsDir) + sep;

    if (normTarget.startsWith(normSkills)) {
      const rel = normTarget.slice(normSkills.length);
      const firstSep = rel.indexOf(sep);
      const firstComp = firstSep > 0 ? rel.slice(0, firstSep) : rel;
      const linkPath = path.join(skillsDir, firstComp);

      let isSymlink = false;
      try {
        if (fs.existsSync(linkPath)) isSymlink = fs.lstatSync(linkPath).isSymbolicLink();
      } catch {}

      if (!isSymlink) {
        const bypassFile = "/tmp/cc-skills-bypass.token";
        let hasBypass = false;
        try {
          if (fs.existsSync(bypassFile)) {
            const ts = parseInt(fs.readFileSync(bypassFile, "utf-8").trim()) * 1000; // s→ms
            if (Date.now() - ts < 5 * 60 * 1000) hasBypass = true;
          }
        } catch {}

        if (!hasBypass) {
          console.error("[pre-security] 🔒 BLOCKED: ~/.claude/skills/ 是激活层，禁止直接创建/写入");
          console.error("[pre-security] 目标: " + targetPath);
          console.error("[pre-security] 规则: 源码先放 ~/Documents/My_Skills_Library/，再 ln -s 激活");
          console.error('[pre-security] 操作: 先执行 Skill("skills-management") 走规范流程');
          process.exit(2);
        }
      }
    }
  }

  // 4. SKILL.md 修改守卫（v1.5.0 流程硬约束）
  //    直接修改 SKILL.md 必须通过 skills-management 授权（一次性绕过令牌）
  if (targetPath && hasSkillsMgmt && /SKILL\.md$/i.test(targetPath)) {
    const bypassFile = "/tmp/cc-skills-bypass.token";
    let hasBypass = false;
    try {
      if (fs.existsSync(bypassFile)) {
        const ts = parseInt(fs.readFileSync(bypassFile, 'utf-8').trim()) * 1000; // s→ms
        if (Date.now() - ts < 5 * 60 * 1000) { // 5 分钟窗口期
          hasBypass = true;
        }
      }
    } catch { /* 文件读取失败视为无令牌 */ }

    if (!hasBypass) {
      console.error(`[pre-security] 🔒 BLOCKED: 修改 SKILL.md 必须先调用 skills-management skill`);
      console.error(`[pre-security] 文件: ${targetPath}`);
      console.error(`[pre-security] 操作: 先执行 Skill("skills-management") 获取授权令牌，再修改 SKILL.md`);
      process.exit(2);
    }
    // 令牌有效（5分钟内），不消费，允许本窗口期内多次编辑
  }

  // 无违规，静默退出
  if (violations.length === 0) {
    process.exit(0);
  }

  // 有违规，检查是否首次
  const allViolations = readViolations();
  cleanOldViolations(allViolations);

  const sessionViolations = allViolations[sessionId] || { violations: [] };

  // 检查本次违规中是否已有同类型在本次会话中出现过
  const existingTypes = new Set(sessionViolations.violations.map((v) => v.type + '|' + v.detail));
  const isRepeat = violations.some((v) => existingTypes.has(v.type + '|' + v.detail));

  // 记录本次违规
  const now = new Date().toISOString();
  for (const v of violations) {
    sessionViolations.violations.push({
      ...v,
      timestamp: now,
    });
  }
  allViolations[sessionId] = sessionViolations;
  writeViolations(allViolations);

  // 输出警告
  for (const v of violations) {
    console.error(`[pre-security] WARNING: ${v.detail}${v.path ? ' -> ' + v.path : ''}`);
  }

  if (isRepeat) {
    console.error(
      `[pre-security] BLOCKED: 会话 ${sessionId} 中重复检测到同类违规，已阻断操作。` +
      `请排查后重试。`
    );
    process.exit(2);
  } else {
    console.error(
      `[pre-security] 首次检测到以上违规（会话 ${sessionId}），本次放行。` +
      `再次出现将被阻断。`
    );
    process.exit(0);
  }
}

main();
