"""lib/session_context.py — SessionStart 上下文构建（patterns/wm/context）

提取自 lib/manifest.py（P1 模块瘦身）。原 manifest.py 混杂了三类职责：
消费环注入构建 + 跨域模式库 + SessionStart 上下文构建。
上下文构建的三个函数只被 session-start.py 使用，独立成模块边界清晰。

依赖：lib/state（IO），lib/paths（路径，通过参数传入不硬编）。
"""
import os
import re
from datetime import datetime

from . import state


def parse_patterns(instincts_dir):
    """解析 patterns.yaml 手工 instinct（confidence≥0.5 按 confidence 降序）"""
    patterns_file = os.path.join(instincts_dir, "patterns.yaml")
    if not os.path.isfile(patterns_file):
        return []
    content = state.read_text(patterns_file)
    blocks = re.split(r'\n---\n?', content)
    instincts = []
    i = 0
    while i < len(blocks) - 1:
        fm_block = blocks[i].strip()
        body_block = blocks[i + 1].strip() if i + 1 < len(blocks) else ''
        fm_block = re.sub(r'^---\s*\n?', '', fm_block)
        if not fm_block.startswith('id:'):
            i += 1
            continue
        fm = {}
        for line in fm_block.split('\n'):
            line = line.strip()
            if ':' in line and not line.startswith('#'):
                key, _, val = line.partition(':')
                fm[key.strip()] = val.strip().strip('"')
        action_match = re.search(r'## Action\s*\n(.*?)(\n##|\Z)', body_block, re.DOTALL)
        action = action_match.group(1).strip() if action_match else 'N/A'
        action = ' '.join(action.split())
        instincts.append({
            'id': fm.get('id', '?'),
            'trigger': fm.get('trigger', '?'),
            'confidence': float(fm.get('confidence', 0)),
            'domain': fm.get('domain', '?'),
            'action': action,
        })
        i += 2
    active = [x for x in instincts if x['confidence'] >= 0.5]
    active.sort(key=lambda x: x['confidence'], reverse=True)
    return active


def instinct_line(x):
    """instinct 行格式化（ID|DOMAIN|CONF|TRIGGER|ACTION）"""
    return (f"ID:{x['id']}|DOMAIN:{x['domain']}|CONF:{x['confidence']}"
            f"|TRIGGER:{x['trigger']}|ACTION:{x['action'][:200]}")


def parse_working_memory(cc_memory_dir):
    """解析 working-memory.md 的 [active]/[活跃] topic，返回 (wm_section_str, wm_count)"""
    wm_file = os.path.join(cc_memory_dir, "working-memory.md")
    if not os.path.isfile(wm_file):
        return "", 0
    wm = state.read_text(wm_file)
    topics = []
    current = None
    for line in wm.split('\n'):
        if line.startswith('## Topic:'):
            if current and current['signals']:
                topics.append(current)
            current = {'title': line.replace('## Topic:', '').strip(), 'signals': []}
        elif current and line.startswith('- [') and len(current['signals']) < 3:
            sig = line.strip()
            if len(sig) > 120:
                sig = sig[:117] + '...'
            current['signals'].append(sig)
    if current and current['signals']:
        topics.append(current)
    active = [t for t in topics if '[active]' in t['title'] or '[活跃]' in t['title']]
    if not active:
        return "", 0
    lines = []
    for i, t in enumerate(active[:5]):
        title = t['title'].replace('[active]', '').replace('[活跃]', '').strip()
        lines.append(f'### {title}')
        lines.append('<br>'.join(t['signals']))
        if i < min(len(active), 5) - 1:
            lines.append('')
    wm_count = min(len(active), 5)
    wm_section = (f"## Working Memory ({wm_count} active topic(s))\n\n"
                  + "\n".join(lines) +
                  "\n\n> 建议新会话启动后读取 working-memory.md 获取完整上下文，"
                  "交叉引用 MEMORY.md 项目索引对应 Wiki 页面。")
    return wm_section, wm_count


def gen_active_context(instincts_dir, session_id, manual_instincts, manual_count, wm_section):
    """生成 active-context.md（文件备份，供调试用，原 §3）"""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M") + " UTC"
    lines = [f"# Active Context — {timestamp}", "", f"> Session: {session_id}", "",
             "## Active Instincts (patterns.yaml)", ""]
    if manual_instincts and manual_count > 0:
        for line in manual_instincts.split('\n'):
            if not line.strip():
                continue
            parts = {}
            for seg in line.split('|'):
                if ':' in seg:
                    k, _, v = seg.partition(':')
                    parts[k.strip()] = v
            lines.append(f"- **[{parts.get('ID', '')}]** "
                         f"({parts.get('DOMAIN', '')}, conf={parts.get('CONF', '')}) "
                         f"— {parts.get('ACTION', '')}")
    else:
        lines.append("(none)")
    if wm_section:
        lines.append("")
        lines.append(wm_section)
    try:
        with open(os.path.join(instincts_dir, "active-context.md"),
                  "w", encoding="utf-8", errors="replace") as f:
            f.write('\n'.join(lines) + '\n')
    except OSError:
        pass
