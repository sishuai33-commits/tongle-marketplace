"""状态 I/O 统一入口（instincts 目录唯一读写通道）

提取自 149 处散落的 json.load/dump/.jsonl 读写（14 个 .py 文件）。
统一保障（对治散落实现的不一致）：
- encoding="utf-8" + errors="replace"（修 collector UnicodeDecodeError bug：坏字节不崩）
- ensure_ascii=False（中文不转义）
- 父目录不存在自动建（save_cursor/append_jsonl 不再依赖 instincts/ 预存在，
  对治 source-scanner 依赖 instincts/ 预存在 OSError 静默吞的坑）
- 写失败 fail-open（OSError 静默，不阻断主流程，对应 observe/reuse-log 设计）

典型被替换模式（discriminate-collector.py）：
    for ln in open(X, encoding="utf-8"):       →  state.read_jsonl(X)
        d = json.loads(ln)
    with open(X, "a", encoding="utf-8") as f:  →  state.append_jsonl(X, entry)
        f.write(json.dumps(entry, ensure_ascii=False) + "\\n")
"""
import json
import os
from pathlib import Path

from . import paths


def _ensure_parent(path):
    """父目录不存在则建，建失败静默（fail-open）"""
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass


# ---------- jsonl（每行一个 JSON 对象）----------

def read_jsonl(path):
    """读 jsonl 返回 list[dict]，坏行/坏字节跳过不崩

    不存在返回 []。对应散落的 for ln in open(X): json.loads(ln) 模式。
    errors='replace' 修 collector UnicodeDecodeError bug（observations 坏字节崩溃）。
    """
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out


def append_jsonl(path, entry):
    """追加一行 jsonl，父目录不存在则建，写失败不阻断（fail-open）"""
    _ensure_parent(path)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def write_jsonl(path, entries):
    """覆写整个 jsonl（逐行写，非原子；调用方需自行保证一致性）"""
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ---------- json（单对象）----------

def read_json(path, default=None):
    """读 json 文件，不存在/损坏返回 default"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path, obj):
    """写 json 文件（indent=2, ensure_ascii=False）"""
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ---------- 游标文件（纯文本时间戳）----------

def read_cursor(path):
    """读游标文件内容（strip），不存在返回 None

    对应 load_cursor()/load_cursor_file() 模式。
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def write_cursor(path, value):
    """写游标文件（覆盖），父目录不存在则建，写失败不阻断"""
    _ensure_parent(path)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(value))
    except OSError:
        pass


# ---------- instincts 便捷入口 ----------

def read_instincts_jsonl(name):
    """读 instincts/<name> jsonl"""
    return read_jsonl(paths.instincts_file(name))


def append_instincts_jsonl(name, entry):
    """追加到 instincts/<name> jsonl"""
    append_jsonl(paths.instincts_file(name), entry)


# ---------- 文件读取辅助（health/guards 共用，避免3份拷贝）----------

def md_files(mem_dir):
    """列目录下 .md 文件（不匹配 dotfile，与原 bash *.md 一致）

    Python pathlib glob("*.md") 匹配 dotfile（.memory-health-log.md 等），
    原 bash `for f in "$DIR"/*.md` 不匹配 dotfile。bash→python 重写需显式过滤，
    否则 .memory-health-log.md 等日志文件被误报为孤儿（2026-07-06 Step 3 实证）。
    """
    return [f for f in mem_dir.glob("*.md") if not f.name.startswith(".")]


def read_lines(path):
    """数文件行数（不存在返回 0）"""
    if not Path(path).is_file():
        return 0
    n = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for _ in f:
            n += 1
    return n


def read_text(path):
    """读文件文本（不存在返回空串，fail-open）"""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
