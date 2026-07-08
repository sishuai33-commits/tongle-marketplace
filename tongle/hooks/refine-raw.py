#!/usr/bin/env python3
"""refine-raw.py — 加工环·规整薄壳（Step 3 提取）

逻辑在 lib/refine.py，IO 在 lib/state.py，路径在 lib/paths.py。Step 4 将删此壳。

对原料库做减熵规整：去重检测 + 冲突裁决标记 + 死链扫描。
用法：python3 hooks/refine-raw.py
原 118 行 → 薄壳调 lib.refine.main。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import refine

if __name__ == "__main__":
    refine.main()
