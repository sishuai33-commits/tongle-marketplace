#!/usr/bin/env python3
"""stop.py — Stop hook（Step 4 Part C）

原 reuse-adoption-judge.py 18行薄壳移位（Step 3.5 已提取逻辑到 lib/adoption.py）。
守"不干扰"硬约束：hook 立即 exit 0，worker 异步 nohup 不阻塞 CC。
Part D 改 hooks.json Stop 指向本脚本 + 删 reuse-adoption-judge.py。
"""
import os
import sys

_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)
from lib import adoption

if __name__ == "__main__":
    adoption.hook_main()
