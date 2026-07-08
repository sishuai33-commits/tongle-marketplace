#!/usr/bin/env python3
"""discriminate-collector.py — L2 判别候选采集器薄壳（Step 2 提取）

逻辑在 lib/discriminate.py，IO 在 lib/state.py，路径在 lib/paths.py。
Step 4 将删此壳由 hooks.json 直调 lib。

架构范式 §四采集环 + §三种子范式闭环命门。守红线②⑤⑦⑧。
触发：SessionEnd hook 自动增量（无参）/ 慢环全量（--full-scan）/ 手动。
原 644 行 → 薄壳调 lib.discriminate.main。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import discriminate
from lib.discriminate import mark_discard_pattern  # re-export 保 M2 接入测试 importlib

if __name__ == "__main__":
    discriminate.main()
