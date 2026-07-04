#!/usr/bin/env python3
"""
build-asset-manifest.py — Wiki 知识域注册表 v2.0
SessionStart 静默注入，CC 自动匹配话题到 Wiki 域。
全动态：描述来自 .ai-vocab.md，关键词多源提取，零维护。

v2.0 改进（2026-06-22）：
  1. 关键词多源补全（项目名+状态+事件+专属概念）→ 保证每项目 ≥3 关键词
  2. 时效戳：读 synthesis.md mtime → (更新:N天前)
  3. 优先级分级：P0(冲刺/死线) / P1(活跃) / P2(轻量/暂停)
  4. 实体关联：从项目表"涉及实体"列提取 → #关联:商汤 华为...
  5. synthesis.md 存在性校验：缺失项目静默标注，不注入死引用
  6. Working Memory 桥接：检测 WM 中引用的项目 → 🔥 上次焦点
  7. 匹配规则显式化
"""
import re, os, time, argparse

# 环境变量默认值兜底（默认值按 $HOME 推导 CC 的 per-project memory 编码，行为不变）
_home = os.path.expanduser("~")
CC_MEMORY_DIR = os.environ.get("CC_MEMORY_DIR", f"{_home}/.claude/projects/{_home.replace('/', '-')}/memory")
WIKI_VAULT_PATH = os.environ.get("WIKI_VAULT_PATH", f"{_home}/Documents/Obsidian Vault")

VOCAB = os.path.join(WIKI_VAULT_PATH, "wiki", ".ai-vocab.md")
WM_FILE = os.path.join(CC_MEMORY_DIR, "working-memory.md")
WIKI_PROJECTS = os.path.join(WIKI_VAULT_PATH, "wiki", "projects")

# ── 关键词补全：从项目名 + 状态描述提取 ──
def extract_keywords_from_text(text, max_kw=6):
    """从任意文本中提取有意义的短词作为关键词"""
    # 先清理 markdown 标记
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\[([^]]+)\]\([^)]+\)', r'\1', text)
    # 去掉版本号 v2.2.0 等
    text = re.sub(r'\bv?\d+\.\d+(\.\d+)?\b', '', text)
    # 按常见分隔符切分
    # 不切连字符 - – — （保留技术术语完整性: Agent-native, CDP直连 等）
    parts = re.split(r'[：:,，、+＋·\s/｜|]+', text)
    kws = []
    for p in parts:
        # 清理括号注释：knowledge-engine(ke) → knowledge-engine
        p = re.sub(r'\([^)]*\)$', '', p).strip()
        p = p.strip().strip('"').strip("'")
        # 过滤过长/过短/纯数字/纯标点/纯英文单字母
        # 长度上限 20：容纳 knowledge-engine(17) 等长技术名（原 12 卡掉 ke 工程名致注入错配）
        if 2 <= len(p) <= 20 and not p.isdigit() and not re.match(r'^[^\w一-鿿]+$', p):
            if re.match(r'^[a-zA-Z]$', p):  # 单字母
                continue
            # 过滤无意义通用词
            skip_words = {'进行中', '已完成', '待启动', '轻量', '项目', '跟踪', '体系', '进行', '活跃',
                          '开发', '方向', '讨论', '研究', '试点', '构建'}
            if p not in skip_words:
                kws.append(p)
    # 去重，保留顺序
    seen = set()
    unique = []
    for k in kws:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique[:max_kw]


# ── 死线检测（排除版本号） ──
def has_deadline_signal(status):
    """检测状态描述中是否包含时间死线（排除版本号 v0.6.0, v2.2.0 等）"""
    # 先去掉版本号 v0.6.0, v2.2.0, v2.2 等
    cleaned = re.sub(r'\bv?\d+\.\d+(\.\d+)?\b', '', status)
    # 匹配 M.DD 或 MM-DD 或 M/DD 日期模式（1-12 月, 1-31 日）
    if re.search(r'(?<!\d)(0?[1-9]|1[0-2])[\./\-]([12]\d|3[01]|0?[1-9])(?!\d)', cleaned):
        return True
    # 匹配 630/0630/1231 月日连写
    m = re.search(r'(?<!\d)(\d{3,4})(?!\d)', cleaned)
    if m:
        num = m.group(1)
        if len(num) == 4:
            month, day = int(num[:2]), int(num[2:])
        elif len(num) == 3:
            # 630 → 月6日30, 也可能月12日3 → 两种都试
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
    # 匹配 N月底/N月初
    if re.search(r'\d+月底|\d+月初', cleaned):
        return True
    return False


# ── 优先级判定 ──
def determine_priority(name, is_light, phase, status, has_deadline):
    """从状态推导优先级 P0/P1/P2（phase + status 双重检查）"""
    phase_lower = phase.lower() if phase else ""
    status_lower = status.lower() if status else ""
    combined = f"{phase} {status}"

    # P2 优先判定: 轻量或暂停（避免版本号 v0.6.0 误触发死线→P0）
    if is_light:
        return "P2"
    if '暂停' in combined:
        return "P2"

    # P0: 有硬死线或冲刺中
    if has_deadline:
        return "P0"
    if any(w in combined for w in ['冲刺', '死线', 'ddl', 'deadline']):
        return "P0"
    if any(w in combined for w in ['活跃追踪', '活跃开发']):
        return "P0"

    # P1: 其余活跃项目
    return "P1"


def parse_vocab():
    """解析 .ai-vocab.md，返回结构化数据"""
    if not os.path.exists(VOCAB):
        return None, None, None, None, 0

    with open(VOCAB) as f:
        text = f.read()

    # ── 全局概念数 ──
    gc_start = text.find("## 全局概念")
    gc_end = text.find("## 项目")
    gc_section = text[gc_start:gc_end] if gc_start >= 0 and gc_end > gc_start else ""
    gc_rows = re.findall(r'^\| (.+?) \|', gc_section, re.MULTILINE)
    concept_count = len([r for r in gc_rows if r not in ('概念', '------')])

    # ── 实体映射 ──
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

    # ── 项目列表 ──
    proj_start = text.find("## 项目")
    rest = text[proj_start:]
    proj_end = rest.find("已归档：")
    if proj_end < 0:
        proj_end = rest.find("\n\n---", 50)
    proj_section = rest[:proj_end] if proj_end > 0 else rest
    proj_rows = re.findall(r'^\| (.+?) \| (.+?) \| (.+?) \| (.+?) \|', proj_section, re.MULTILINE)

    # ── 项目专属概念 → 关键词 ──
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

    # ── 事件（按项目分组，取最近3条） ──
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

    # ── 构建项目 domain 列表 ──
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

        # 解析关联实体
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

        # 合并去重：专属概念优先，再补状态/事件/名称
        all_kws = list(concept_kws)
        seen = set(all_kws)
        for k in status_kws + event_kws + name_kws:
            if k not in seen:
                all_kws.append(k)
                seen.add(k)
        all_kws = all_kws[:8]  # 最多8个，避免膨胀

        # 优先级
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


def get_synthesis_freshness(project_name):
    """读取 synthesis.md 的修改时间，返回 (exists, days_ago)"""
    syn_path = os.path.join(WIKI_PROJECTS, project_name, "synthesis.md")
    if not os.path.exists(syn_path):
        return False, None
    mtime = os.path.getmtime(syn_path)
    days_ago = (time.time() - mtime) / 86400
    return True, days_ago


def read_access_count(project_name):
    """读 synthesis.md frontmatter 的 access_count（eval access 策略用，无则 0）"""
    syn_path = os.path.join(WIKI_PROJECTS, project_name, "synthesis.md")
    if not os.path.exists(syn_path):
        return 0
    try:
        with open(syn_path) as f:
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


def format_freshness(days_ago):
    """格式化时效戳"""
    if days_ago is None:
        return "N/A"
    if days_ago < 1/24:  # < 1 hour
        return "刚刚"
    if days_ago < 1:
        hours = int(days_ago * 24)
        return f"{hours}h前"
    if days_ago < 30:
        return f"{int(days_ago)}天前"
    months = int(days_ago / 30)
    return f"{months}月前"


def detect_wm_project_focus(project_names, project_keywords_map):
    """检测 Working Memory 中引用了哪些项目，返回焦点项目列表"""
    if not os.path.exists(WM_FILE):
        return []

    with open(WM_FILE) as f:
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
            # 检查项目名或关键词是否出现
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

    # 按命中数排序，取前3
    ranked = sorted(project_hits.items(), key=lambda x: x[1]['count'], reverse=True)
    return [name for name, _ in ranked[:3]]


def build(strategy="all", project=None, limit=3):
    parsed = parse_vocab()
    if parsed is None:
        return ""
    domains, concept_count, entities, proj_events = parsed
    if not domains:
        return ""

    entity_count = len(entities) if entities else 0

    # ── 构建项目关键词映射（供 WM 桥接用） ──
    project_keywords_map = {d['name']: d['keywords'] for d in domains}
    project_names = [d['name'] for d in domains]

    # ── WM 桥接 ──
    wm_focus = detect_wm_project_focus(project_names, project_keywords_map)

    # ── 分离有效/缺失项目 ──
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

    # ── 注入策略过滤（eval 反馈环前置：可切换全量/按项目/按access跑注入增益对比）──
    #   strategy=all     全量注入（SessionStart 默认，回归保护）
    #   strategy=project  只注入指定项目（eval 单项目对照）
    #   strategy=access   按 access_count 热度取前 N（eval 热度策略对照）
    if strategy == "project" and project:
        valid_domains = [d for d in valid_domains if d['name'] == project]
    elif strategy == "access":
        for d in valid_domains:
            d['access_count'] = read_access_count(d['name'])
        valid_domains.sort(key=lambda d: d.get('access_count', 0), reverse=True)
        valid_domains = valid_domains[:limit]

    # ── 生成输出 ──
    lines = ["## Wiki 知识域"]
    lines.append(f"  {len(valid_domains)}项目|{concept_count}概念|{entity_count}实体 — 话题匹配后读 projects/<名>/synthesis.md")
    lines.append("  匹配: 提及项目名/关键词/关联实体→读synthesis.md; 具体概念→读对应concept页; 模糊→读synthesis.md")
    lines.append("")

    # ── WM 焦点强提示（前置置顶）：内容在场≠被采用（反模式②），置顶+英文别名+路径强引导 CC 读对 syn
    # 末尾「🔥 上次会话焦点」保留作回顾，此处前置作行动指令（修复 68e6f20 未闭环：extract 让
    # knowledge-engine 进了关键词，但 CC 仍读 P0 量子 syn——末尾焦点提示不够强）
    if wm_focus:
        for pname in wm_focus:
            kws = project_keywords_map.get(pname, [])
            en_alias = next((k for k in kws if re.match(r'^[a-zA-Z][a-zA-Z0-9_-]+$', k)), None)
            alias_str = f"({en_alias})" if en_alias else ""
            lines.append(f"  ⭐ 本次会话疑似涉及: {pname}{alias_str} — 优先读 projects/{pname}/synthesis.md")
        lines.append("")

    for d in valid_domains:
        tag = " [轻量]" if d["light"] else ""

        # 关键词
        kw_str = " ".join(d['keywords']) if d['keywords'] else ""

        # 关联实体
        ent_str = ""
        if d['entities']:
            ent_names = d['entities'][:4]  # 最多4个，避免膨胀
            ent_str = " #关联:" + " ".join(ent_names)

        # 时效
        freshness = f" (更新:{d['freshness_str']})"

        # L0 索引：只留可发现性元数据（优先级/项目名/轻量标/时效），去 status 业务描述（内容）
        # 触发价值已提取进 #关键词（1.2 防漏验证：业务触发词已覆盖，仅丢版本号/数字无触发价值）
        # #关键词 是 L1 触发判别的【临时关键词兜底】（CC 据它判断是否读 synthesis），
        #   非判别经验库——判别经验库 Level 2 才建（架构范式§九红线⑦：要标清临时兜底）
        # 呼应 architecture-paradigm §四 L0「只推元数据不推内容」+ 指挥官校正"防按需取漏"
        line = f"  {d['priority']} · {d['name']}{tag}{freshness}"
        if ent_str:
            line += ent_str
        if kw_str:
            line += f" #关键词:{kw_str}"

        lines.append(line)

    # ── 缺失项目静默标注 ──
    if missing_domains:
        lines.append("")
        lines.append("  ── ⚠️ 以下项目 synthesis.md 缺失，已跳过 ──")
        for d in missing_domains:
            lines.append(f"  ❌ {d['name']}: synthesis.md 不存在，需在 Wiki 中创建或从 .ai-vocab.md 移除")

    # ── WM 焦点 ──
    if wm_focus:
        lines.append("")
        lines.append(f"  🔥 上次会话焦点: {', '.join(wm_focus)} → 优先读这些项目的 synthesis.md")

    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wiki 知识域注入清单（eval 反馈环：--strategy 切换注入策略跑增益对比）")
    parser.add_argument("--strategy", choices=["all", "project", "access"], default="all",
                        help="all=全量(默认,SessionStart用) / project=指定项目 / access=按热度取前N")
    parser.add_argument("--project", default=None, help="project 策略下指定项目名")
    parser.add_argument("--limit", type=int, default=3, help="access 策略下取前N个(默认3)")
    args = parser.parse_args()
    print(build(strategy=args.strategy, project=args.project, limit=args.limit))
