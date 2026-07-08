#!/usr/bin/env python3
"""runtime-health-check.py — ke 运行态健康检查薄壳（Step 3 提取）

逻辑在 lib/health.py runtime_check。Step 4 将删此壳。
检查对象：运行态实体（~/.claude/instincts/），非 test 层、非文档。
用法：python3 hooks/runtime-health-check.py [--json]
退出码：0=全绿(A5 pass) / 1=有warn / 2=有fail
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import health

if __name__ == "__main__":
    health.runtime_check()
