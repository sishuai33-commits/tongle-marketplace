#!/usr/bin/env python3
"""慢环结构审计 — 全量扫描 Wiki vault + Memory 体系，输出 JSON"""
import json, os, re, subprocess
from pathlib import Path
from collections import Counter

_home = os.path.expanduser("~")
CC_MEMORY_DIR = os.environ.get("CC_MEMORY_DIR", f"{_home}/.claude/projects/{_home.replace('/', '-')}/memory")
WIKI_VAULT_PATH = os.environ.get("WIKI_VAULT_PATH", f"{_home}/Documents/Obsidian Vault")

VAULT = Path(WIKI_VAULT_PATH)
MEMORY_DIR = Path(CC_MEMORY_DIR)
CLAUDE_MD = Path.home() / ".claude/CLAUDE.md"
WIKI_DIR = VAULT / "wiki"

STRUCTURAL_DUPES = {
    "synthesis.md", "SKILL.md", "_narrative.md", "index.md",
    "方法论.md", "观点演化.md", "投资哲学.md", "指标词典.md"
}

def find_all_md(root):
    """Find all .md files under root, return list of paths."""
    return [p for p in root.rglob("*.md") if p.is_file()]

def main():
    result = {
        "vault_structure": {},
        "wiki_health": {},
        "memory_health": {},
        "cross_system": {},
    }

    # ===== 1. VAULT STRUCTURE =====
    # 1a. Root dirs outside wiki/
    root_entries = [p for p in VAULT.iterdir() if p.is_dir()
                    and p.name not in {".git", ".obsidian", ".claude", ".trash", "wiki"}]
    result["vault_structure"]["root_dirs_outside_wiki"] = [p.name for p in root_entries] or None

    # 1b. Duplicate filenames (exclude structural conventions)
    all_names = [p.name for p in find_all_md(VAULT)]
    name_counts = Counter(all_names)
    real_dupes = [name for name, count in name_counts.items()
                  if count > 1 and name not in STRUCTURAL_DUPES]
    result["vault_structure"]["duplicate_filenames"] = real_dupes or None

    # 1c. Empty files
    empty = [str(p.relative_to(VAULT)) for p in find_all_md(VAULT) if p.stat().st_size == 0]
    result["vault_structure"]["empty_files"] = empty or None

    # 1d. Empty dirs
    empty_dirs = []
    for d in VAULT.rglob("*"):
        if d.is_dir() and not any(d.iterdir()) and ".git" not in str(d):
            empty_dirs.append(str(d.relative_to(VAULT)))
    result["vault_structure"]["empty_dirs"] = empty_dirs or None

    # ===== 2. WIKI HEALTH =====
    # 2a. Broken links
    broken_links = []
    all_wiki_files = {p.stem: p for p in find_all_md(WIKI_DIR)}
    # Also index by full relative path from wiki/
    all_wiki_paths = {}
    for p in find_all_md(WIKI_DIR):
        rel = str(p.relative_to(WIKI_DIR)).replace(".md", "")
        all_wiki_paths[rel] = p
        all_wiki_paths[p.stem] = p

    # Extract all [[links]]
    link_pattern = re.compile(r'\[\[([^\]|#]+)(?:[|#][^\]]+)?\]\]')
    for f in find_all_md(WIKI_DIR):
        content = f.read_text(errors="ignore")
        for match in link_pattern.finditer(content):
            link = match.group(1).strip()
            # Skip external refs
            if link.startswith("http") or link.startswith("memory:"):
                continue
            # Try to resolve
            # 1. Direct filename match anywhere in vault
            if link in all_wiki_paths:
                continue
            # 2. Try with .md extension
            if f"{link}.md" in all_wiki_paths:
                continue
            # 3. Try as relative path from the file's directory
            resolved = (f.parent / link).resolve()
            if resolved.exists() and resolved.suffix == ".md":
                continue
            # 4. Try as path from wiki root
            resolved = (WIKI_DIR / link).resolve()
            if resolved.exists() and resolved.suffix == ".md":
                continue
            broken_links.append({
                "link": link,
                "file": str(f.relative_to(WIKI_DIR))
            })

    result["wiki_health"]["broken_links_count"] = len(broken_links)
    result["wiki_health"]["broken_links_sample"] = broken_links[:30]

    # Classify broken links by pattern
    relative_pattern = sum(1 for b in broken_links if b["link"].startswith(".."))
    memory_pattern = sum(1 for b in broken_links if b["link"].startswith("../../memory"))
    result["wiki_health"]["broken_link_categories"] = {
        "relative_path_links": relative_pattern,
        "memory_ref_links": memory_pattern,
        "other": len(broken_links) - relative_pattern - memory_pattern
    }

    # 2b. Orphan pages (0 incoming links)
    orphan_pages = []
    for f in find_all_md(WIKI_DIR):
        if "/events/" in str(f) or "/timeline/" in str(f) or "/_archived/" in str(f):
            continue  # skip events/timeline/archived
        basename = f.stem
        # Search for references to this page name
        ref_count = 0
        for other_f in find_all_md(WIKI_DIR):
            if other_f == f:
                continue
            content = other_f.read_text(errors="ignore")
            if f"[[{basename}" in content or f"[[{basename}|" in content or f"[[{basename}#" in content or f"[[{basename}]]" in content:
                ref_count += 1
        if ref_count == 0:
            orphan_pages.append(str(f.relative_to(WIKI_DIR)))

    result["wiki_health"]["orphan_pages"] = orphan_pages

    # 2c. Stale pages
    stale_count = 0
    for f in find_all_md(WIKI_DIR):
        content = f.read_text(errors="ignore")
        if "staleness: stale" in content:
            stale_count += 1
    result["wiki_health"]["stale_pages_count"] = stale_count

    # ===== 3. MEMORY HEALTH =====
    # 3a. Run memory-guard.sh
    try:
        guard_output = subprocess.run(
            ["bash", str(Path.home() / ".claude/hooks/memory-guard.sh")],
            capture_output=True, text=True, timeout=10
        )
        orphan_lines = guard_output.stdout.count("孤儿") + guard_output.stderr.count("孤儿")
        violation_count = guard_output.stdout.count("违规") + guard_output.stderr.count("违规")
    except Exception:
        orphan_lines = -1
        violation_count = -1

    result["memory_health"]["orphan_files"] = orphan_lines
    result["memory_health"]["total_violations"] = violation_count

    # 3b. Line counts
    result["memory_health"]["MEMORY_md_lines"] = len(MEMORY_DIR.joinpath("MEMORY.md").read_text().splitlines()) if MEMORY_DIR.joinpath("MEMORY.md").exists() else 0
    result["memory_health"]["CLAUDE_md_lines"] = len(CLAUDE_MD.read_text().splitlines()) if CLAUDE_MD.exists() else 0
    result["memory_health"]["total_memory_files"] = len(list(MEMORY_DIR.glob("*.md")))

    # 3c. Broken refs in CLAUDE.md
    broken_in_claude = []
    if CLAUDE_MD.exists():
        ref_pattern = re.compile(r'memory/([a-zA-Z0-9_\-]+\.md)')
        for match in ref_pattern.finditer(CLAUDE_MD.read_text()):
            ref_file = match.group(1)
            if not (MEMORY_DIR / ref_file).exists():
                broken_in_claude.append(ref_file)
    result["memory_health"]["broken_refs_in_claude_md"] = broken_in_claude or None

    # 3d. Broken refs in MEMORY.md
    broken_in_memory = []
    mem_index = MEMORY_DIR / "MEMORY.md"
    if mem_index.exists():
        ref_pattern = re.compile(r'\[([a-zA-Z0-9_\-]+\.md)\]')
        for match in ref_pattern.finditer(mem_index.read_text()):
            ref_file = match.group(1)
            if ref_file in ("MEMORY.md", "CLAUDE.md"):
                continue
            if not (MEMORY_DIR / ref_file).exists():
                broken_in_memory.append(ref_file)
    result["memory_health"]["broken_refs_in_memory_md"] = broken_in_memory or None

    # ===== 4. CROSS-SYSTEM =====
    # 4a. Memory wiki: refs that point to missing wiki pages
    broken_wiki_refs = []
    for mf in MEMORY_DIR.glob("*.md"):
        content = mf.read_text(errors="ignore")
        wiki_refs = re.findall(r'wiki:\s*(\S+)', content)
        for wref in wiki_refs:
            wref = wref.rstrip(",);")
            wpath = WIKI_DIR / wref
            if not wpath.exists():
                broken_wiki_refs.append(f"memory:{mf.name} → wiki:{wref}")
    result["cross_system"]["broken_memory_wiki_refs"] = broken_wiki_refs or None

    # 4b. Wiki references to deleted memory files
    broken_memory_refs = []
    patterns = [
        re.compile(r'\[\[.*?memory/([a-zA-Z0-9_\-]+\.md)[\]|#]'),
        re.compile(r'\[\[.*?memory/([a-zA-Z0-9_\-]+\.md)\]\]'),
    ]
    for f in find_all_md(WIKI_DIR):
        content = f.read_text(errors="ignore")
        for pattern in patterns:
            for match in pattern.finditer(content):
                mf_name = match.group(1)
                if not (MEMORY_DIR / mf_name).exists():
                    broken_memory_refs.append(f"wiki:{f.relative_to(WIKI_DIR)} → memory:{mf_name}")
    result["cross_system"]["broken_wiki_memory_refs"] = broken_memory_refs[:20] or None

    # Output
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
