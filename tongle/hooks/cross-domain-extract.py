#!/usr/bin/env python3
"""cross-domain-extract.py — 加工环·跨域同构识别薄壳（Step 3 提取）

逻辑在 lib/cross_domain.py，IO 在 lib/state.py，路径在 lib/paths.py。Step 4 将删此壳。

从判别经验库识别跨域同构模式，产出跨域模式库。四层架构防 proxy substitution。
LLM fail-open：env 缺/--no-llm 降级纯规则。
用法：python3 hooks/cross-domain-extract.py [--min-sessions 3] [--dry-run] [--no-llm]
原 274 行 → 薄壳调 lib.cross_domain.main。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import cross_domain

if __name__ == "__main__":
    cross_domain.main()
