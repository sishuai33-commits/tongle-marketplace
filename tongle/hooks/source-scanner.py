#!/usr/bin/env python3
"""source-scanner.py — 采集环主动扫描器薄壳（Step 3.5 提取）

逻辑在 lib/scanner.py，IO 在 lib/state.py，路径在 lib/paths.py。
Step 4 将删此壳由 hooks.json 直调 lib 或合并到 session-end.py。

架构范式 §四采集环。守原则3/红线②⑥/简单方案优先。
触发：SessionEnd hook 串联 / 慢环全量 / 手动。
原 458 行 → 薄壳调 lib.scanner.main。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import scanner

if __name__ == "__main__":
    scanner.main()
