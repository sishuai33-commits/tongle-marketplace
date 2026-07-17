"""守卫：引用完整性 + memory 体系健康（公理1反模式② L2 工程实现）

提取自 ref-integrity-guard.py + memory-guard.sh（Step 3 内核提取2）。
IO 经 lib/state，路径经 lib/paths。

架构范式 §2.2 模块契约：CC事件(stdin) + memory目录 → 凭证/引用/memory完整性检查 → 违规告警/阻断。

ref-integrity（PostToolUse Write|Edit）：
  出度守卫（轻量增量）：只查刚写文件的引用目标在四树(memory/wiki/CLAUDE.md/MEMORY.md)是否悬空。
  全量入度归慢环人裁。只查解析明确的引用（markdown link + memory 路径），
  wikilink [[x]] 归 wiki_checks.py（模糊解析误报率高）。

memory-guard（PostToolUse 后检查）：
  8 项检查（目录/索引/行数/孤儿/毕业/断链/frontmatter）+ severity 判定 + state.json 写入。
  仅违规时输出，无违规则静默。

invest 阈值真值源校验（阶段一 1.3）：编辑 thresholds.yaml 或任一 wiki 阈值页时交叉比对防守线真值。
"""
import json
import os
import re
import sys
from pathlib import Path

from . import paths, state as _state

# === 常量 ===
MEMORY_DIR = Path(paths.cc_memory_dir())
WIKI_DIR = Path(paths.wiki_vault()) / "wiki"
CLAUDE_MD = Path(paths.home()) / ".claude" / "CLAUDE.md"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"

# ── 门槛阈值（集中定义，health.py / guards.py 共用）──
# 两级阈值：预警阈值（maintenance_check，SessionStart 提前提醒）< 硬上限（memory-guard，PostToolUse 才报）
# 预警提前告警给人处理窗口，硬上限是真正超标的底线。
INDEX_WARN_LINES = 140   # MEMORY.md 预警行数（maintenance_check 用）
INDEX_MAX_LINES = 150    # MEMORY.md 硬上限（memory-guard 用）
CLAUDE_WARN_LINES = 180  # CLAUDE.md 预警行数（maintenance_check 用）
CLAUDE_MAX_LINES = 200   # CLAUDE.md 硬上限（memory-guard 用）

# ── invest 阈值真值源校验 ──
KE_THRESHOLDS_YAML = Path(paths.config_dir()) / "invest" / "thresholds.yaml"
INVEST_WIKI = WIKI_DIR / "projects" / "投资跟踪"
INVEST_THRESHOLD_FILES = [
    KE_THRESHOLDS_YAML,
    INVEST_WIKI / "concepts" / "关键阈值速查.md",
    INVEST_WIKI / "concepts" / "防守线机制.md",
    INVEST_WIKI / "strategies" / "交易框架.md",
    INVEST_WIKI / "dashboards" / "dashboard.md",
]
_DEFENSE_LINE_RE = re.compile(
    r'(?:防守线[^|]*?\|\s*|防守位[^|]*?[<(]\s*)(\d+\.\d+)\b'
)
_DASHBOARD_DEFENSE_RE = re.compile(r'收盘\s*[<]\s*(\d+\.\d+)\s*→')

# 引用提取正则
MDLINK_RE = re.compile(r'\]\(([^)]+)\)')
MEMPATH_RE = re.compile(r'(?:^|[^/])memory/([a-zA-Z0-9_\-]+\.md)')

SKIP_PREFIXES = ("http://", "https://", "mailto:", "#", "data:", "ftp://", "obsidian:")

# 全局索引（每次 hook 调用构建）——模块级全局，特征测试 monkey-patch 生效
WIKI_STEMS = {}      # stem -> [paths]
WIKI_PATHS = []      # 相对 wiki 的路径（去 .md）


# ---------- ref-integrity 纯逻辑 ----------

def in_scope(path):
    """文件是否在引用完整性检查范围（仅 wiki/ 下 .md）。"""
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
        WIKI_PATHS.append(str(f.relative_to(WIKI_DIR))[:-3])


def normalize(ref):
    """归一化引用：去尾部 \\、空白、锚点"""
    ref = ref.strip().strip("\\").strip()
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

    # memory 引用
    if "memory/" in ref:
        name = ref.split("memory/")[-1].split()[0].rstrip(").,;\"'")
        if (MEMORY_DIR / name).exists():
            return False
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
        for wp in WIKI_PATHS:
            if wp.endswith(ref[5:]) or ref[5:].endswith(wp):
                return False

    # wiki path 后缀匹配
    for wp in WIKI_PATHS:
        if wp.endswith(ref) or ref.endswith(wp) or wp == ref:
            return False

    # 纯 stem 匹配
    stem = ref.split("/")[-1]
    if stem in WIKI_STEMS:
        return False
    if (MEMORY_DIR / f"{stem}.md").exists():
        return False

    # markdown 相对路径
    if ref.endswith(".md"):
        if (src.parent / ref).resolve().exists():
            return False
        if (WIKI_DIR / ref).resolve().exists():
            return False
        if (MEMORY_DIR / ref).exists():
            return False

    return True


def extract_refs(content):
    """提取 markdown link + memory 路径引用（去重 by 归一化值）。wikilink [[x]] 不提取。"""
    refs = []
    seen = set()
    for m in MDLINK_RE.finditer(content):
        r = m.group(1).strip()
        if r and normalize(r) not in seen:
            refs.append(r)
            seen.add(normalize(r))
    for m in MEMPATH_RE.finditer(content):
        r = f"memory/{m.group(1)}"
        if r not in seen:
            refs.append(r)
            seen.add(r)
    return refs


# ---------- invest 阈值真值源交叉比对 ----------

def _yaml_defense_lines():
    """从 thresholds.yaml 读 defense_lines 全部键值（真值集）。
    动态遍历所有键——不硬编任何特定持仓键名或数值，机制层不假设资产。"""
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
                continue
        return out or None
    except Exception:
        return None


def _wiki_defense_values(md_path):
    """从单个 wiki 页提取防守线出现值（当前生效规则行，历史行已跳过）。"""
    vals = set()
    if not Path(md_path).is_file():
        return vals
    try:
        text = Path(md_path).read_text(errors="ignore")
    except Exception:
        return vals
    for m in _DASHBOARD_DEFENSE_RE.finditer(text):
        vals.add(float(m.group(1)))
    for line in text.splitlines():
        if re.match(r'\s*\|\s*\d{1,2}[/-]\d{1,2}\s*\|', line) or \
           re.match(r'\s*\|\s*\d{4}-\d{1,2}-\d{1,2}\s*\|', line):
            continue
        for m in _DEFENSE_LINE_RE.finditer(line):
            vals.add(float(m.group(1)))
    return vals


def check_invest_threshold_drift(file_path):
    """编辑 invest 阈值真值源文件时，交叉比对防守线真值在五处是否一致。漂移→exit 2。"""
    if Path(file_path).resolve() not in {p.resolve() for p in INVEST_THRESHOLD_FILES}:
        return
    truth = _yaml_defense_lines()
    if truth is None:
        return
    expected = set(truth.values())
    drifts = []
    for p in INVEST_THRESHOLD_FILES:
        if p.suffix != ".md":
            continue
        found = _wiki_defense_values(p)
        if not found:
            continue
        extra = found - expected
        if extra:
            drifts.append((p.name, sorted(found), sorted(extra)))
    if drifts:
        truth_repr = ", ".join(f"{k}={v}" for k, v in sorted(truth.items()))
        print("🚨 invest 阈值真值源漂移：防守线在 wiki 与 thresholds.yaml 不一致", file=sys.stderr)
        print(f"   yaml 真值(thresholds.yaml defense_lines): {truth_repr}", file=sys.stderr)
        for name, found, extra in drifts:
            print(f"   ❌ {name}: 出现 {found}，其中 {extra} 与 yaml 不符", file=sys.stderr)
        print("   修复：统一改 yaml + 全部 wiki 阈值页为同一值，或阶段二 A1 收敛为 yaml 唯一源。", file=sys.stderr)
        sys.exit(2)


def ref_main(raw=None):
    """PostToolUse(Write|Edit) 入口：stdin JSON → 引用完整性检查

    raw 参数：合并 hook(post-tool-use.py)读 stdin 后传入；None 时自读（薄壳兼容）。
    """
    if raw is None:
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

    WIKI_MIN = int(os.environ.get("KE_WIKI_MIN_FILES", "5"))
    if broken and len(WIKI_PATHS) < WIKI_MIN:
        print(f"ℹ️ 引用完整性（小wiki降级）：{len(broken)} 处悬空引用暂不阻断", file=sys.stderr)
        print(f"   wiki 成型后(≥{WIKI_MIN} 文件)自动严格阻断；先写引用后建目标是正常的。", file=sys.stderr)
        sys.exit(0)

    if broken:
        print("🚨 引用完整性守卫：检测到悬空引用（公理1反模式② proxy substitution 防线）", file=sys.stderr)
        print(f"   文件：{file_path}", file=sys.stderr)
        for r in broken:
            print(f"   ❌ 引用 [[{r}]] → 目标在四树(memory/wiki/CLAUDE.md/MEMORY.md)均未找到", file=sys.stderr)
        print("   修复：改指正确目标，或删除悬空引用。", file=sys.stderr)
        print("   毕业/迁移/重命名须完成全树引用更新（引用完整性毕业三步法 ②③）。", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)


# ---------- memory-guard（bash→python 重写）----------

# 引用 guards 集中定义的阈值（薄壳消除 P1：阈值常量化）


def memory_check():
    """memory 体系守卫：8 项检查 + severity + state.json。违规 exit 1，干净 exit 0。

    原 bash memory-guard.sh 重写（Step 3），stdout 格式保留（特征测试钉）。
    """
    memory_dir = Path(paths.cc_memory_dir())
    index_file = memory_dir / "MEMORY.md"
    claude_md = Path(paths.home()) / ".claude" / "CLAUDE.md"
    guard_state = paths.instincts_file(".memory-guard-state.json")

    violations = 0
    orphans = []
    dangling = []
    stale_graduation = []
    frontmatter_bad = 0
    index_over = 0
    claude_over = 0
    claude_lines = 0
    index_lines = 0

    # 1. 目录存在性
    if not memory_dir.is_dir():
        print(f"🚨 Memory 守卫: memory 目录不存在: {memory_dir}")
        sys.exit(1)

    # 2. 索引文件存在
    if not index_file.is_file():
        print("🚨 Memory 守卫: MEMORY.md 索引文件缺失")
        violations += 1

    index_text = _state.read_text(index_file)

    # 3. 索引行数超限
    if index_file.is_file():
        index_lines = _state.read_lines(index_file)
        if index_lines > INDEX_MAX_LINES:
            print(f"🚨 Memory 守卫: MEMORY.md {index_lines} 行，超过 {INDEX_MAX_LINES} 行上限")
            violations += 1
            index_over = 1

    # 4. CLAUDE.md 行数超限
    if claude_md.is_file():
        claude_lines = _state.read_lines(claude_md)
        if claude_lines > CLAUDE_MAX_LINES:
            print(f"🚨 Memory 守卫: CLAUDE.md {claude_lines} 行，超过 {CLAUDE_MAX_LINES} 行上限")
            violations += 1
            claude_over = 1

    # 5. 孤儿文件（存在但未在 MEMORY.md 索引）
    for f in sorted(_state.md_files(memory_dir)):
        name = f.name
        if name == "MEMORY.md":
            continue
        if name not in index_text:
            print(f"🚨 Memory 守卫: '{name}' 未在 MEMORY.md 中索引（孤儿文件）")
            violations += 1
            orphans.append(name)

    # 6. 毕业不一致：MEMORY.md 标记 ~~已毕业~~ 但 memory 文件仍存在
    if index_file.is_file():
        for line in index_text.splitlines():
            m = re.search(r'~~([^~]+)~~', line)
            if not m:
                continue
            entry_name = m.group(1)
            norm = re.sub(r'[\s/]', '', entry_name).lower()
            for f in _state.md_files(memory_dir):
                fname = f.stem
                fname_norm = re.sub(r'-[0-9][0-9-]*$', '', fname.lower())
                if len(norm) > 4 and norm in fname_norm:
                    stale_graduation.append(f.name)
                    break
    for sg in stale_graduation:
        print(f"⚠️  Memory 守卫: MEMORY.md 标记 ~~已毕业~~ 但文件仍存在: {sg}（应删除或取消标记）")
        violations += 1

    # 7. 断链引用（MEMORY.md 引用但不存在的 .md）
    if index_file.is_file():
        for m in re.finditer(r'\]\(([^)]+\.md)\)', index_text):
            ref = m.group(1)
            if ref.startswith("wiki/"):
                continue
            if not (memory_dir / ref).is_file():
                print(f"🚨 Memory 守卫: MEMORY.md 引用 '{ref}' 但文件不存在")
                violations += 1
                dangling.append(ref)

    # 8. frontmatter 健康（检查 type 字段）
    for f in sorted(_state.md_files(memory_dir)):
        if f.name == "MEMORY.md":
            continue
        text = _state.read_text(f)
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            frontmatter_bad += 1
            continue
        # 提取 frontmatter 段（两个 --- 之间），数 ^  type: 行
        fm_lines = []
        in_fm = False
        for ln in lines:
            if ln.strip() == "---":
                if in_fm:
                    break
                in_fm = True
                continue
            if in_fm:
                fm_lines.append(ln)
        type_count = sum(1 for ln in fm_lines if ln.startswith("  type:"))
        if type_count > 1:
            frontmatter_bad += 1
    if frontmatter_bad > 0:
        print(f"🚨 Memory 守卫: {frontmatter_bad} 个文件 frontmatter 异常（缺 type 或重复 type）")
        violations += 1

    # === 判定严重级别 ===
    orphan_count = len(orphans)
    dangling_count = len(dangling)
    stale_graduation_count = len(stale_graduation)
    severity = "green"
    severity_reason = ""
    if orphan_count >= 5 or index_over == 1 or dangling_count > 0 or \
            claude_over == 1 or stale_graduation_count > 0:
        severity = "red"
        severity_reason = "孤儿≥5/索引超限/断链引用/CLAUDE.md超限/毕业残留"
    elif orphan_count >= 3 or frontmatter_bad >= 3 or stale_graduation_count > 0:
        severity = "yellow"
        severity_reason = "孤儿3-4/frontmatter异常≥3/毕业残留"

    # === 写状态文件 ===
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    total_files = len(list(_state.md_files(memory_dir)))

    # 保留旧 last_reminded（节流）
    last_reminded = 0
    reminder_count = 0
    old = _state.read_json(guard_state)
    if old:
        last_reminded = old.get("last_reminded_epoch", 0)
        reminder_count = old.get("reminder_count", 0)

    state = {
        "last_run": now,
        "last_run_epoch": now_epoch,
        "severity": severity,
        "severity_reason": severity_reason,
        "total_violations": violations,
        "orphans": orphans,
        "orphan_count": orphan_count,
        "dangling_refs": dangling,
        "dangling_count": dangling_count,
        "stale_graduation": stale_graduation,
        "stale_graduation_count": stale_graduation_count,
        "frontmatter_bad": frontmatter_bad,
        "index_over_limit": index_over,
        "index_lines": index_lines,
        "index_max": INDEX_MAX_LINES,
        "claude_md_over_limit": claude_over,
        "claude_md_lines": claude_lines,
        "claude_md_max": CLAUDE_MAX_LINES,
        "total_files": total_files,
        "last_reminded_epoch": last_reminded,
        "reminder_count": reminder_count,
    }
    _state.write_json(guard_state, state)

    if violations > 0:
        print("")
        print(f"⚠️  共 {violations} 项违规 [{severity}]。建议执行 memory 体系维护。")
        sys.exit(1)
    sys.exit(0)
