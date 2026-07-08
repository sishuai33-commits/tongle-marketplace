"""消费环：构建注入清单（L0 索引）

提取自 build-asset-manifest.py（Step 2）。SessionStart 静默注入 Wiki 知识域注册表。
路径经 lib/paths，跨域模式库读取经 lib/state，纯逻辑可独立单测。

架构范式 §四消费环 manifest：wiki .ai-vocab + reuse 统计 + cross-domain confirmed
→ 构建优先级清单 → SessionStart 注入内容。
"""
import os
import re
import time
from datetime import datetime

from . import paths, state


# ---------- 路径 ----------

def vocab_path():
    """wiki/.ai-vocab.md 词汇表"""
    return os.path.join(paths.wiki_vault(), "wiki", ".ai-vocab.md")


def wm_file_path():
    """working-memory.md（CC memory 目录）"""
    return os.path.join(paths.cc_memory_dir(), "working-memory.md")


def wiki_projects_dir():
    """wiki/projects/ 目录"""
    return os.path.join(paths.wiki_vault(), "wiki", "projects")


def cross_domain_lib_path():
    """跨域模式库 jsonl"""
    return paths.instincts_file("cross-domain-patterns.jsonl")


# ---------- 纯函数 ----------

def extract_keywords_from_text(text, max_kw=6):
    """从任意文本中提取有意义的短词作为关键词"""
    # 先清理 markdown 标记
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\[([^]]+)\]\([^)]+\)', r'\1', text)
    # 去掉版本号 v2.2.0 等
    text = re.sub(r'\bv?\d+\.\d+(\.\d+)?\b', '', text)
    # 按常见分隔符切分（不切连字符 - – —，保留技术术语完整性）
    parts = re.split(r'[：:,，、+＋·\s/｜|]+', text)
    kws = []
    for p in parts:
        # 清理括号注释：knowledge-engine(ke) → knowledge-engine
        p = re.sub(r'\([^)]*\)$', '', p).strip()
        p = p.strip().strip('"').strip("'")
        # 长度 2-20，过滤纯数字/纯标点/纯英文单字母
        if 2 <= len(p) <= 20 and not p.isdigit() and not re.match(r'^[^\w一-鿿]+$', p):
            if re.match(r'^[a-zA-Z]$', p):  # 单字母
                continue
            skip_words = {'进行中', '已完成', '待启动', '轻量', '项目', '跟踪', '体系', '进行', '活跃',
                          '开发', '方向', '讨论', '研究', '试点', '构建'}
            if p not in skip_words:
                kws.append(p)
    # 去重保序
    seen = set()
    unique = []
    for k in kws:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique[:max_kw]


def has_deadline_signal(status):
    """检测状态描述中是否包含时间死线（排除版本号 v0.6.0, v2.2.0 等）"""
    cleaned = re.sub(r'\bv?\d+\.\d+(\.\d+)?\b', '', status)
    # M.DD / MM-DD / M/DD 日期（1-12月, 1-31日）
    if re.search(r'(?<!\d)(0?[1-9]|1[0-2])[\./\-]([12]\d|3[01]|0?[1-9])(?!\d)', cleaned):
        return True
    # 630/0630/1231 月日连写
    m = re.search(r'(?<!\d)(\d{3,4})(?!\d)', cleaned)
    if m:
        num = m.group(1)
        if len(num) == 4:
            month, day = int(num[:2]), int(num[2:])
        elif len(num) == 3:
            # 630 → 月6日30，也可能月12日3 → 两种都试
            month_a, day_a = int(num[0]), int(num[1:])
            month_b, day_b = int(num[:2]), int(num[2:])
            if 1 <= month_a <= 12 and 1 <= day_a <= 31:
                month, day = month_a, day_a
            elif 1 <= month_b <= 12 and 1 <= day_b <= 31:
                month, day = month_b, day_b
            else:
                month, day = 0, 0
        else:
            month, day = 0, 0
        if 1 <= month <= 12 and 1 <= day <= 31:
            return True
    # N月底/N月初
    if re.search(r'\d+月底|\d+月初', cleaned):
        return True
    return False


def determine_priority(name, is_light, phase, status, has_deadline):
    """从状态推导优先级 P0/P1/P2（phase + status 双重检查）"""
    combined = f"{phase} {status}"
    # P2 优先判定：轻量或暂停（避免版本号误触发死线→P0）
    if is_light:
        return "P2"
    if '暂停' in combined:
        return "P2"
    # P0：有硬死线或冲刺中
    if has_deadline:
        return "P0"
    if any(w in combined for w in ['冲刺', '死线', 'ddl', 'deadline']):
        return "P0"
    if any(w in combined for w in ['活跃追踪', '活跃开发']):
        return "P0"
    # P1：其余活跃项目
    return "P1"


def format_freshness(days_ago):
    """格式化时效戳"""
    if days_ago is None:
        return "N/A"
    if days_ago < 1 / 24:  # < 1 小时
        return "刚刚"
    if days_ago < 1:
        return f"{int(days_ago * 24)}h前"
    if days_ago < 30:
        return f"{int(days_ago)}天前"
    return f"{int(days_ago / 30)}月前"


# ---------- 文件 IO ----------

def parse_vocab(vocab_file=None):
    """解析 .ai-vocab.md，返回 (domains, concept_count, entities, proj_events)

    不存在返回 None（build 检查 is None）。
    修复原 build-asset-manifest.py bug：原 parse_vocab 不存在返回 5 元组 (None,None,None,None,0)，
    但 build 检查 `if parsed is None`（元组非 None，永远 False），unpack 5→4 变量 ValueError。
    生产未触发因 vocab 总存在；lib 单测覆盖不存在分支暴露此 bug。
    """
    vocab_file = vocab_file or vocab_path()
    if not os.path.exists(vocab_file):
        return None
    with open(vocab_file, encoding="utf-8", errors="replace") as f:
        text = f.read()

    # 全局概念数
    gc_start = text.find("## 全局概念")
    gc_end = text.find("## 项目")
    gc_section = text[gc_start:gc_end] if gc_start >= 0 and gc_end > gc_start else ""
    gc_rows = re.findall(r'^\| (.+?) \|', gc_section, re.MULTILINE)
    concept_count = len([r for r in gc_rows if r not in ('概念', '------')])

    # 实体映射
    entities = {}
    ent_start = text.find("## 实体")
    ent_end = text.find("## 全局概念")
    if ent_start >= 0 and ent_end > ent_start:
        ent_section = text[ent_start:ent_end]
        ent_rows = re.findall(r'^\| (.+?) \| (.+?) \| (.+?) \|', ent_section, re.MULTILINE)
        for e in ent_rows:
            name = e[0].strip()
            if name in ('实体', '------'):
                continue
            aliases = [a.strip() for a in e[1].split('、')] if e[1].strip() else []
            entities[name] = {'aliases': aliases, 'type': e[2].strip()}

    # 项目列表
    proj_start = text.find("## 项目")
    rest = text[proj_start:] if proj_start >= 0 else ""
    proj_end = rest.find("已归档：")
    if proj_end < 0:
        proj_end = rest.find("\n\n---", 50)
    proj_section = rest[:proj_end] if proj_end > 0 else rest
    proj_rows = re.findall(r'^\| (.+?) \| (.+?) \| (.+?) \| (.+?) \|', proj_section, re.MULTILINE)

    # 项目专属概念 → 关键词
    proj_concepts = {}
    pc_start = text.find("## 项目专属概念")
    if pc_start > 0:
        pc_text = text[pc_start:]
        next_section = re.search(r'\n## [^#]', pc_text[10:])
        if next_section:
            pc_text = pc_text[:10 + next_section.start()]
        blocks = re.split(r'\n### (?=[^#])', pc_text)
        for block in blocks[1:]:
            m = re.match(r'(.+?) \((\d+)\)\s*\n', block)
            if not m:
                continue
            pname = m.group(1).strip()
            concepts = re.findall(r'^\| (.+?) \| .+ \|$', block, re.MULTILINE)
            concepts = [c for c in concepts if c not in ('概念', '------', '索引')]
            kws = [c for c in concepts if 2 <= len(c) <= 12]
            proj_concepts[pname] = kws[:6]

    # 事件（按项目分组，取最近3条）
    proj_events = {}
    ev_start = text.find("## 事件")
    if ev_start > 0:
        ev_text = text[ev_start:]
        ev_end = ev_text.find("\n## ", 10)
        if ev_end > 0:
            ev_text = ev_text[:ev_end]
        ev_rows = re.findall(r'^\| (.+?) \| (.+?) \| (.+?) \|', ev_text, re.MULTILINE)
        for ev in ev_rows:
            date = ev[0].strip()
            desc = ev[1].strip()
            proj_name = ev[2].strip()
            if date in ('日期', '------'):
                continue
            if proj_name not in proj_events:
                proj_events[proj_name] = []
            proj_events[proj_name].append({'date': date, 'desc': desc})

    # 构建项目 domain 列表
    domains = []
    for p in proj_rows:
        name = p[0].strip()
        if name in ('项目', '------'):
            continue
        is_light = "[轻量]" in name
        clean = name.replace(" [轻量]", "").strip()
        phase = p[1].strip()
        status = p[2].strip()[:120]
        entity_refs = p[3].strip() if len(p) > 3 else ""

        linked_entities = []
        if entity_refs and entity_refs != '—':
            for ent_name in re.split(r'[、,，]', entity_refs):
                ent_name = ent_name.strip()
                if ent_name:
                    linked_entities.append(ent_name)

        # 关键词多源提取
        concept_kws = proj_concepts.get(clean, [])
        name_kws = extract_keywords_from_text(clean, max_kw=2)
        status_kws = extract_keywords_from_text(status, max_kw=4)
        event_kws = []
        if clean in proj_events:
            for ev in proj_events[clean][:3]:
                event_kws.extend(extract_keywords_from_text(ev['desc'], max_kw=2))

        all_kws = list(concept_kws)
        seen = set(all_kws)
        for k in status_kws + event_kws + name_kws:
            if k not in seen:
                all_kws.append(k)
                seen.add(k)
        all_kws = all_kws[:8]

        deadline = has_deadline_signal(status)
        priority = determine_priority(clean, is_light, phase, status, deadline)

        domains.append({
            "name": clean,
            "light": is_light,
            "phase": phase,
            "status": status,
            "keywords": all_kws,
            "entities": linked_entities,
            "priority": priority,
            "has_deadline": deadline,
        })

    return domains, concept_count, entities, proj_events


def get_synthesis_freshness(project_name, wiki_projects=None):
    """读取 synthesis.md 的修改时间，返回 (exists, days_ago)"""
    wiki_projects = wiki_projects or wiki_projects_dir()
    syn_path = os.path.join(wiki_projects, project_name, "synthesis.md")
    if not os.path.exists(syn_path):
        return False, None
    mtime = os.path.getmtime(syn_path)
    days_ago = (time.time() - mtime) / 86400
    return True, days_ago


def read_access_count(project_name, wiki_projects=None):
    """读 synthesis.md frontmatter 的 access_count（eval access 策略用，无则 0）"""
    wiki_projects = wiki_projects or wiki_projects_dir()
    syn_path = os.path.join(wiki_projects, project_name, "synthesis.md")
    if not os.path.exists(syn_path):
        return 0
    try:
        with open(syn_path, encoding="utf-8", errors="replace") as f:
            in_fm = False
            for line in f:
                if line.strip() == "---":
                    if in_fm:
                        break  # frontmatter 结束
                    in_fm = True
                    continue
                if in_fm and line.startswith("access_count:"):
                    return int(line.split(":", 1)[1].strip().strip("'\""))
    except (ValueError, OSError):
        pass
    return 0


def detect_wm_project_focus(project_names, project_keywords_map, wm_file=None):
    """检测 Working Memory 中引用了哪些项目，返回焦点项目列表（前3）"""
    wm_file = wm_file or wm_file_path()
    if not os.path.exists(wm_file):
        return []
    with open(wm_file, encoding="utf-8", errors="replace") as f:
        wm_text = f.read()

    # 只分析活跃 topic
    topics = []
    current = None
    for line in wm_text.split('\n'):
        if line.startswith('## Topic:'):
            if current and current['signals']:
                topics.append(current)
            title = line.replace('## Topic:', '').strip()
            current = {'title': title, 'signals': []}
        elif current and line.startswith('- [') and len(current['signals']) < 5:
            current['signals'].append(line.strip())
    if current and current['signals']:
        topics.append(current)

    active_topics = [t for t in topics
                     if '[active]' in t['title'].lower()
                     or '[活跃]' in t['title']]
    if not active_topics:
        return []

    # 对每个活跃 topic，匹配项目
    project_hits = {}
    for t in active_topics:
        topic_text = t['title'] + ' ' + ' '.join(t['signals'])
        for pname in project_names:
            kws = project_keywords_map.get(pname, [])
            matches = []
            if pname in topic_text:
                matches.append(pname)
            for kw in kws:
                if kw in topic_text and kw not in matches:
                    matches.append(kw)
            if matches:
                if pname not in project_hits:
                    project_hits[pname] = {'count': 0, 'matches': []}
                project_hits[pname]['count'] += len(matches)
                project_hits[pname]['matches'].extend(matches[:2])

    ranked = sorted(project_hits.items(), key=lambda x: x[1]['count'], reverse=True)
    return [name for name, _ in ranked[:3]]


# ---------- 主入口 ----------

def build(strategy="all", project=None, limit=3):
    """构建注入清单：解析 vocab + WM 桥接 + 优先级排序 + 跨域模式库 → 注入文本"""
    parsed = parse_vocab()
    if parsed is None:
        return ""
    domains, concept_count, entities, proj_events = parsed
    if not domains:
        return ""

    entity_count = len(entities) if entities else 0

    project_keywords_map = {d['name']: d['keywords'] for d in domains}
    project_names = [d['name'] for d in domains]

    wm_focus = detect_wm_project_focus(project_names, project_keywords_map)

    # 分离有效/缺失项目
    valid_domains = []
    missing_domains = []
    for d in domains:
        exists, days_ago = get_synthesis_freshness(d['name'])
        if exists:
            d['freshness_str'] = format_freshness(days_ago)
            d['freshness_days'] = days_ago
            valid_domains.append(d)
        else:
            missing_domains.append(d)

    # 按优先级排序：P0 → P1 → P2
    prio_order = {"P0": 0, "P1": 1, "P2": 2}
    valid_domains.sort(key=lambda d: (prio_order.get(d['priority'], 9), d['name']))

    # 注入策略过滤（eval 反馈环前置：全量/按项目/按access）
    if strategy == "project" and project:
        valid_domains = [d for d in valid_domains if d['name'] == project]
    elif strategy == "access":
        for d in valid_domains:
            d['access_count'] = read_access_count(d['name'])
        valid_domains.sort(key=lambda d: d.get('access_count', 0), reverse=True)
        valid_domains = valid_domains[:limit]

    lines = ["## Wiki 知识域"]
    lines.append(f"  {len(valid_domains)}项目|{concept_count}概念|{entity_count}实体 — 话题匹配后读 projects/<名>/synthesis.md")
    lines.append("  匹配: 提及项目名/关键词/关联实体→读synthesis.md; 具体概念→读对应concept页; 模糊→读synthesis.md")
    lines.append("")

    # WM 焦点强提示（前置置顶）：内容在场≠被采用（反模式②），置顶+英文别名+路径强引导 CC 读对 syn
    if wm_focus:
        for pname in wm_focus:
            kws = project_keywords_map.get(pname, [])
            en_alias = next((k for k in kws if re.match(r'^[a-zA-Z][a-zA-Z0-9_-]+$', k)), None)
            alias_str = f"({en_alias})" if en_alias else ""
            lines.append(f"  ⭐ 本次会话疑似涉及: {pname}{alias_str} — 优先读 projects/{pname}/synthesis.md")
        lines.append("")

    for d in valid_domains:
        tag = " [轻量]" if d["light"] else ""
        kw_str = " ".join(d['keywords']) if d['keywords'] else ""
        ent_str = ""
        if d['entities']:
            ent_names = d['entities'][:4]
            ent_str = " #关联:" + " ".join(ent_names)
        freshness = f" (更新:{d['freshness_str']})"
        # L0 索引：只留可发现性元数据，去 status 业务描述
        line = f"  {d['priority']} · {d['name']}{tag}{freshness}"
        if ent_str:
            line += ent_str
        if kw_str:
            line += f" #关键词:{kw_str}"
        lines.append(line)

    # 缺失项目静默标注
    if missing_domains:
        lines.append("")
        lines.append("  ── ⚠️ 以下项目 synthesis.md 缺失，已跳过 ──")
        for d in missing_domains:
            lines.append(f"  ❌ {d['name']}: synthesis.md 不存在，需在 Wiki 中创建或从 .ai-vocab.md 移除")

    # WM 焦点（末尾回顾）
    if wm_focus:
        lines.append("")
        lines.append(f"  🔥 上次会话焦点: {', '.join(wm_focus)} → 优先读这些项目的 synthesis.md")

    # 跨域模式库（M4 消费侧接口，加工环产出供消费环按需取）
    # 只注入 human_confirmed=true 且 verdict=confirm 的模式（守红线5），reject 不注入
    cd_patterns = state.read_jsonl(cross_domain_lib_path())
    if cd_patterns:
        confirmed = [p for p in cd_patterns
                     if p.get("human_confirmed") and p.get("human_verdict") == "confirm"]
        pending_count = sum(1 for p in cd_patterns if not p.get("human_confirmed"))
        if confirmed:
            lines.append("")
            lines.append(f"  ## 跨域模式库(加工环产出,已确认{len(confirmed)}条)")
            for p in confirmed:
                doms = "/".join(p.get("domain_source", [])[:3])
                lines.append(f"    ◆ {p.get('pattern_id')}: {p.get('pattern')}/{p.get('disposition')} "
                             f"跨{p.get('cross_project_count', 0)}域 [{doms}] kw={p.get('common_keywords', [])[:3]}")
        elif pending_count:
            lines.append("")
            lines.append(f"  ## 跨域模式库: {pending_count}条待人确认(确认后自动注入,守红线5)")

    return "\n".join(lines)


# ---------- SessionStart 资产感知（原 session-start.py §1-§3 搬入，ponytail-audit 批3）----------

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
