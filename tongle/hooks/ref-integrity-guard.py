#!/usr/bin/env python3
"""引用完整性守卫 — 公理1反模式②(proxy substitution)的 L2 工程实现。

PostToolUse(Write|Edit) 触发：检查刚写文件的所有引用目标在四树
(memory/ + wiki/ + CLAUDE.md + MEMORY.md) 是否存在。悬空引用 → exit(2)
反馈阻断，提示 CC 修复。

设计原则：
- 出度守卫（轻量增量防线）：只查刚写文件的引用目标，不扫全树。
  全量入度扫描归 slow-loop-audit.py 慢环兜底。
- 只查解析明确的引用：markdown link `[text](path)` + 纯 memory 路径
  `memory/xxx.md`。Obsidian wikilink `[[x]]` 解析宽松（模糊/最短路径/
  目录/特殊字符），严格匹配必然误报 → 归 wiki_checks.py 管（SessionStart
  已报）。守卫聚焦解析明确的引用，阻断风险低。
- 宽松优先防误报：阻断正常工作流比漏报更糟。多解析策略任一命中即不报。
- 仅检查 wiki/ 下的 .md 文件（memory 文件的 [[xxx]] 是 CC 关联约定非
  Obsidian 链接，不扫；CLAUDE.md/MEMORY.md 归 memory-guard.sh 管索引）。
"""
import json, os, re, sys
from pathlib import Path

_home = os.path.expanduser("~")
CC_MEMORY_DIR = os.environ.get(
    "CC_MEMORY_DIR", f"{_home}/.claude/projects/{_home.replace('/', '-')}/memory")
WIKI_VAULT_PATH = os.environ.get(
    "WIKI_VAULT_PATH", f"{_home}/Documents/Obsidian Vault")
# KE_CONFIG 指向 plugin 内 config/：优先 CLAUDE_PLUGIN_ROOT（plugin 调起），
# 回退脚本所在目录的上级（手动/测试场景），使朋友 clone 到任意目录均可定位
_PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(Path(__file__).resolve().parent.parent)
KE_CONFIG = os.environ.get("KE_CONFIG", os.path.join(_PLUGIN_ROOT, "config"))

MEMORY_DIR = Path(CC_MEMORY_DIR)
WIKI_DIR = Path(WIKI_VAULT_PATH) / "wiki"
CLAUDE_MD = Path.home() / ".claude/CLAUDE.md"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"

# ── invest 阈值真值源校验（阶段一 1.3，防五六重源漂移）──────────────
# A1 真值源重构前的临时兜底：编辑 thresholds.yaml 或任一 wiki 阈值页时，
# 交叉比对防守线真值在 yaml + 4 wiki 页是否一致，漂移 exit 2。
# 注：当前只校验防守线（defense_lines），加仓/清仓线留阶段二 A1。
# 真值集动态遍历 yaml defense_lines 所有键——不硬编任何特定持仓键名/数值，
# 机制层不假设资产（朋友填 example_index 照样校验，自用持仓键照读不变）。
KE_THRESHOLDS_YAML = Path(KE_CONFIG) / "invest" / "thresholds.yaml"
INVEST_WIKI = WIKI_DIR / "projects" / "投资跟踪"
# 编辑任一文件触发全量交叉比对
INVEST_THRESHOLD_FILES = [
    KE_THRESHOLDS_YAML,
    INVEST_WIKI / "concepts" / "关键阈值速查.md",
    INVEST_WIKI / "concepts" / "防守线机制.md",
    INVEST_WIKI / "strategies" / "交易框架.md",
    INVEST_WIKI / "dashboards" / "dashboard.md",
]
# 锚点正则：匹配 wiki 表格/正文里的"当前生效防守线规则行"。
# 只认带"防守"字样的行（防守线/防守位），排除加仓语境（伏击圈/回踩/破位，
# 如某加仓线值是加仓伏击圈下沿非防守线）。跳过历史记录/实战验证行
# （带日期、@价格、✅、距/差/仅 等历史引用词），避免误报。
# 捕获任意数值（不写死具体真值）——否则只能抓"已知值重复"，抓不到"出现新值漂移"
# （自废武功反模式：测值一致性却写死值）。真值集由 yaml 决定，expected 外的值=漂移。
_DEFENSE_LINE_RE = re.compile(
    r'(?:防守线[^|]*?\|\s*|防守位[^|]*?[<(]\s*)(\d+\.\d+)\b'
)
# dashboard.md 表格特有格式：| 品种 | ... | 收盘<某防守值 → 减X% |
# dashboard 用"收盘<X → 减%"且带→语义，加仓线无此格式，可安全用收盘锚点
_DASHBOARD_DEFENSE_RE = re.compile(r'收盘\s*[<]\s*(\d+\.\d+)\s*→')

# 引用提取正则
# markdown link: [text](target) — 解析明确，守卫查
MDLINK_RE = re.compile(r'\]\(([^)]+)\)')
# 纯 memory 路径: memory/xxx.md（表格/frontmatter/正文）— 解析明确，守卫查
MEMPATH_RE = re.compile(r'(?:^|[^/])memory/([a-zA-Z0-9_\-]+\.md)')
# 注：Obsidian wikilink [[x]] 解析宽松（模糊/最短路径/目录/特殊字符），
# 严格匹配必然误报，不在此查 → 归 wiki_checks.py（SessionStart 已报）

SKIP_PREFIXES = ("http://", "https://", "mailto:", "#", "data:", "ftp://", "obsidian:")

# 全局索引（每次 hook 调用构建）
WIKI_STEMS = {}      # stem -> [paths]
WIKI_PATHS = []      # 相对 wiki 的路径（去 .md）


def in_scope(path):
    """文件是否在引用完整性检查范围（仅 wiki/ 下 .md）。
    memory/ 文件的 [[xxx]] 是 CC 关联约定非 Obsidian 链接，不扫；
    CLAUDE.md/MEMORY.md 归 memory-guard.sh 管索引完整性。"""
    try:
        p = Path(path).resolve()
    except Exception:
        return False
    try:
        p.relative_to(WIKI_DIR.resolve())
        return p.suffix == ".md"
    except ValueError:
        return False


def build_wiki_index():
    """构建 wiki stem 索引 + 相对路径列表"""
    global WIKI_STEMS, WIKI_PATHS
    WIKI_STEMS, WIKI_PATHS = {}, []
    if not WIKI_DIR.exists():
        return
    for f in WIKI_DIR.rglob("*.md"):
        sp = str(f)
        if "/.obsidian/" in sp or "/.trash/" in sp or "/.git/" in sp:
            continue
        WIKI_STEMS.setdefault(f.stem, []).append(f)
        WIKI_PATHS.append(str(f.relative_to(WIKI_DIR))[:-3])  # 去 .md


def normalize(ref):
    """归一化引用：去尾部 \\、空白、锚点"""
    ref = ref.strip().strip("\\").strip()
    # 去 #anchor
    if "#" in ref:
        ref = ref.split("#", 1)[0]
    return ref.strip()


def is_external(ref):
    return not ref or ref.startswith(SKIP_PREFIXES)


def is_broken(ref, source_file):
    """宽松判定引用是否悬空（任一解析命中即不报）"""
    ref = normalize(ref)
    if is_external(ref):
        return False

    src = Path(source_file)

    # memory 引用：memory/xxx.md 或 memory:xxx
    if "memory/" in ref:
        name = ref.split("memory/")[-1].split()[0].rstrip(").,;\"'")
        if (MEMORY_DIR / name).exists():
            return False
        # 也可能相对路径 ../../memory/xxx.md
        if (src.parent / ref).resolve().exists():
            return False
        return True
    if ref.startswith("memory:"):
        name = ref.split(":", 1)[1].strip().lstrip("/")
        return not (MEMORY_DIR / name).exists()

    # wiki/ 前缀
    if ref.startswith("wiki/"):
        target = WIKI_DIR.parent / ref
        if target.exists():
            return False
        # path 后缀匹配
        for wp in WIKI_PATHS:
            if wp.endswith(ref[5:]) or ref[5:].endswith(wp):
                return False

    # wiki path 后缀匹配（覆盖 [[量子项目/synthesis]] 这种相对 wiki 根的简写）
    for wp in WIKI_PATHS:
        if wp.endswith(ref) or ref.endswith(wp) or wp == ref:
            return False

    # 纯 stem 匹配（取最后一段）
    stem = ref.split("/")[-1]
    if stem in WIKI_STEMS:
        return False
    if (MEMORY_DIR / f"{stem}.md").exists():
        return False

    # markdown 相对路径（.md 结尾）：相对源文件解析
    if ref.endswith(".md"):
        if (src.parent / ref).resolve().exists():
            return False
        if (WIKI_DIR / ref).resolve().exists():
            return False
        if (MEMORY_DIR / ref).exists():
            return False

    return True  # 所有解析都失败 → 悬空


def extract_refs(content):
    """提取 markdown link + memory 路径引用（去重 by 归一化值）。
    wikilink [[x]] 不提取（归 wiki_checks.py 管）。"""
    refs = []
    seen = set()
    for m in MDLINK_RE.finditer(content):
        r = m.group(1).strip()
        if r and normalize(r) not in seen:
            refs.append(r); seen.add(normalize(r))
    for m in MEMPATH_RE.finditer(content):
        r = f"memory/{m.group(1)}"
        if r not in seen:
            refs.append(r); seen.add(r)
    return refs


# ── invest 阈值真值源交叉比对（阶段一 1.3）──────────────────────────
def _yaml_defense_lines():
    """从 thresholds.yaml 读 defense_lines 全部键值（真值集）。
    返回 {键: float(值), ...} 或 None（读不出 / 无 defense_lines / 值非法）。
    动态遍历所有键——不硬编任何特定持仓键名或数值，机制层不假设资产：
    朋友填 example_index 照样读，自用持仓键照读，行为不变。"""
    try:
        import yaml
        d = yaml.safe_load(KE_THRESHOLDS_YAML.read_text())
        dl = d.get("defense_lines", {})
        if not isinstance(dl, dict) or not dl:
            return None
        out = {}
        for k, v in dl.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                continue  # 非数值键跳过，不阻断其他键校验
        return out or None
    except Exception:
        return None


def _wiki_defense_values(md_path):
    """从单个 wiki 页提取防守线出现值。
    返回 set{数值, ...}（当前生效规则行的值，历史行已跳过）。
    文件不存在返回空 set。
    """
    vals = set()
    if not Path(md_path).is_file():
        return vals
    try:
        text = Path(md_path).read_text(errors="ignore")
    except Exception:
        return vals
    # dashboard 用专门的 → 格式正则（当前规则行"收盘<X→减%"，历史 event 无 → 格式不被抓）
    for m in _DASHBOARD_DEFENSE_RE.finditer(text):
        vals.add(float(m.group(1)))
    # 通用锚点：按行处理，跳过历史 event 行（行首日期表格单元格 | 6/1 | / | 2026-07-03 |）
    # 修复 bug②锚点太宽复发：历史 event "防守位触发(收盘<2.05)" 曾被整文本正则抓到，但 2.05 是
    # 拆分前旧值（feedback-fund-split-threshold-sync：历史 events 保留原值）。日期行过滤守住"当前 vs 历史"边界
    for line in text.splitlines():
        if re.match(r'\s*\|\s*\d{1,2}[/-]\d{1,2}\s*\|', line) or \
           re.match(r'\s*\|\s*\d{4}-\d{1,2}-\d{1,2}\s*\|', line):
            continue
        for m in _DEFENSE_LINE_RE.finditer(line):
            vals.add(float(m.group(1)))
    return vals


def check_invest_threshold_drift(file_path):
    """编辑 invest 阈值真值源文件时，交叉比对防守线真值在五处是否一致。
    漂移 → exit 2 阻断（防五六重真值源漂移，A1 重构前的临时兜底）。
    """
    if Path(file_path).resolve() not in {p.resolve() for p in INVEST_THRESHOLD_FILES}:
        return  # 不在阈值真值源集，跳过

    truth = _yaml_defense_lines()
    if truth is None:
        # yaml 读不出（可能正被编辑成半成品），不阻断，让其他检查兜底
        return
    expected = set(truth.values())

    drifts = []
    for p in INVEST_THRESHOLD_FILES:
        if p.suffix != ".md":
            continue  # yaml 自身是真值源，不参与比对
        found = _wiki_defense_values(p)
        if not found:
            continue  # 该页无防守线数值（如关键阈值速查可能未列），跳过非漏报
        # 该页出现的防守线值应 ⊆ expected；出现 expected 外的值 = 漂移
        extra = found - expected
        if extra:
            drifts.append((p.name, sorted(found), sorted(extra)))

    if drifts:
        truth_repr = ", ".join(f"{k}={v}" for k, v in sorted(truth.items()))
        print("🚨 invest 阈值真值源漂移：防守线在 wiki 与 thresholds.yaml 不一致",
              file=sys.stderr)
        print(f"   yaml 真值(thresholds.yaml defense_lines): {truth_repr}", file=sys.stderr)
        for name, found, extra in drifts:
            print(f"   ❌ {name}: 出现 {found}，其中 {extra} 与 yaml 不符", file=sys.stderr)
        print("   修复：统一改 yaml + 全部 wiki 阈值页为同一值，或阶段二 A1 收敛为 yaml 唯一源。",
              file=sys.stderr)
        sys.exit(2)



def main():
    raw = sys.stdin.read()
    if not raw.strip():
        sys.exit(0)
    try:
        d = json.loads(raw)
    except Exception:
        sys.exit(0)

    tool = d.get("tool_name", "")
    if tool not in ("Write", "Edit", "MultiEdit"):
        sys.exit(0)

    inp = d.get("tool_input", {})
    if not isinstance(inp, dict):
        sys.exit(0)
    file_path = inp.get("file_path") or inp.get("path") or inp.get("notePath")
    if not file_path:
        sys.exit(0)
    file_path = os.path.expanduser(str(file_path))

    # invest 阈值真值源漂移校验（独立于 in_scope，thresholds.yaml 不在 wiki/ 下）
    check_invest_threshold_drift(file_path)

    if not in_scope(file_path):
        sys.exit(0)
    if not os.path.isfile(file_path):
        sys.exit(0)

    build_wiki_index()

    try:
        content = Path(file_path).read_text(errors="ignore")
    except Exception:
        sys.exit(0)

    refs = extract_refs(content)
    broken = [r for r in refs if is_broken(r, file_path)]

    # 小 wiki 降级：知识库未成型（< 阈值文件）时悬空引用只警告不阻断，
    # 让用户能正常先写引用后建目标。随 wiki 增长守卫渐进严格激活。
    # 阈值可由 KE_WIKI_MIN_FILES 环境变量覆盖。
    WIKI_MIN = int(os.environ.get("KE_WIKI_MIN_FILES", "5"))
    if broken and len(WIKI_PATHS) < WIKI_MIN:
        print(f"ℹ️ 引用完整性（小wiki降级）：{len(broken)} 处悬空引用暂不阻断",
              file=sys.stderr)
        print(f"   wiki 成型后(≥{WIKI_MIN} 文件)自动严格阻断；先写引用后建目标是正常的。",
              file=sys.stderr)
        sys.exit(0)

    if broken:
        print("🚨 引用完整性守卫：检测到悬空引用（公理1反模式② proxy substitution 防线）",
              file=sys.stderr)
        print(f"   文件：{file_path}", file=sys.stderr)
        for r in broken:
            print(f"   ❌ 引用 [[{r}]] → 目标在四树(memory/wiki/CLAUDE.md/MEMORY.md)均未找到",
                  file=sys.stderr)
        print("   修复：改指正确目标，或删除悬空引用。", file=sys.stderr)
        print("   毕业/迁移/重命名须完成全树引用更新（引用完整性毕业三步法 ②③）。",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
