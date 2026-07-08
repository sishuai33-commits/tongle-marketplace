#!/usr/bin/env python3
"""discriminate-resolve.py — L2 判别人裁决入口薄壳（Step 2 提取）

逻辑在 lib/review.py。Step 4 将删此壳。
re-export extract_discard_keyword 保特征测试 importlib 不破。

守红线⑤⑦⑧②（人确认环/记人填/只收自己裁决/棘轮只升）。
用法：python3 hooks/discriminate-resolve.py <index> <relation_type> <disposition> [note]
原 253 行 → 薄壳调 lib.review.main。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import review
from lib.review import extract_discard_keyword  # re-export 保特征测试 importlib

if __name__ == "__main__":
    review.main()
