#!/usr/bin/env python3
"""Wiki inbound link analyzer for Dream Phase 1.
Calculates link_count and link_factor for all wiki content pages.

Usage: python3 .link-analyzer.py [--update] [--dry-run]
  --dry-run: print results without modifying files
  --update:  write link_factor and link_count to frontmatter (default)
"""

import re, os, glob, sys

WIKI_DIR = os.path.join(os.environ.get("WIKI_VAULT_PATH", os.path.expanduser("~/Documents/Obsidian Vault")), "wiki")

def parse_target(link_text):
    """Extract target from wikilink, handling \| escaped pipes"""
    cleaned = link_text.replace(chr(92) + '|', '|')
    target = cleaned.split('|')[0].strip()
    target = target.split('#')[0].strip()
    return target

def get_all_files():
    """Find all content .md files"""
    all_files = []
    for f in glob.glob(f'{WIKI_DIR}/**/*.md', recursive=True):
        basename = os.path.basename(f)
        if basename in ('log.md', 'index.md', '.ai-vocab.md'):
            continue
        if basename.startswith('.dream-') or basename.startswith('.last_'):
            continue
        if '/.claude/' in f:
            continue
        if basename == 'DATA_SOURCES.md':
            continue
        all_files.append(f)
    return all_files

def build_target_map(all_files):
    """Map every possible wikilink string → file path"""
    target_map = {}
    for f in all_files:
        canonical = f.replace('.md', '')
        without_prefix = canonical.replace(f'{WIKI_DIR}/', '', 1)
        name = os.path.basename(canonical)
        target_map[canonical] = f
        target_map[without_prefix] = f
        if name not in target_map:
            target_map[name] = f
    return target_map

def count_links(all_files, target_map):
    """Count inbound wikilinks for each file"""
    link_count = {f: 0 for f in all_files}

    for src_file in all_files:
        src_rel = src_file.replace('.md', '')
        src_dir = os.path.dirname(src_rel)

        with open(src_file, 'r') as fh:
            content = fh.read()

        wikilinks = re.findall(r'\[\[([^]]+)\]\]', content)
        seen_dests = set()

        for link in wikilinks:
            target_raw = parse_target(link)
            if not target_raw:
                continue
            if target_raw.startswith('../'):
                target = os.path.normpath(os.path.join(src_dir, target_raw))
            else:
                target = target_raw

            if target in target_map:
                dest_file = target_map[target]
                if dest_file != src_file and dest_file not in seen_dests:
                    link_count[dest_file] += 1
                    seen_dests.add(dest_file)

    return link_count

def link_factor(count):
    if count >= 5:
        return 1.2
    elif count >= 2:
        return 1.0
    elif count == 1:
        return 0.7
    else:
        return 0.5

def update_frontmatter(filepath, count, lf):
    """Add/update link_factor and link_count in frontmatter"""
    with open(filepath, 'r') as fh:
        content = fh.read()

    if not content.startswith('---'):
        return False

    end_idx = content.find('---', 3)
    if end_idx == -1:
        return False

    fm_text = content[3:end_idx]
    body = content[end_idx + 3:]

    lines = fm_text.split('\n')
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('link_factor:') or stripped.startswith('link_count:'):
            continue
        new_lines.append(line)

    new_lines.append(f'link_factor: {lf}')
    new_lines.append(f'link_count: {count}')

    new_content = f'---\n{chr(10).join(new_lines)}\n---{body}'

    with open(filepath, 'w') as fh:
        fh.write(new_content)
    return True

def main():
    dry_run = '--dry-run' in sys.argv
    do_update = '--update' in sys.argv or not dry_run

    all_files = get_all_files()
    target_map = build_target_map(all_files)
    link_count = count_links(all_files, target_map)

    results = []
    for f in sorted(all_files):
        count = link_count.get(f, 0)
        lf = link_factor(count)
        short = f.replace(f'{WIKI_DIR}/', '').replace('.md', '')
        results.append((count, lf, f, short))

    results.sort(key=lambda x: (-x[0], x[1], x[2]))

    updated = 0
    for count, lf, f, short in results:
        print(f'{count}|{lf}|{short}')
        if do_update and not dry_run:
            if update_frontmatter(f, count, lf):
                updated += 1

    if do_update and not dry_run:
        print(f'\nUpdated {updated}/{len(all_files)} files.')

    # Summary
    orphans = sum(1 for r in results if r[0] == 0)
    high = sum(1 for r in results if r[1] == 1.2)
    print(f'\nSummary: {len(all_files)} pages | {high} high-ref(>=5) | {orphans} orphans')

if __name__ == '__main__':
    main()
