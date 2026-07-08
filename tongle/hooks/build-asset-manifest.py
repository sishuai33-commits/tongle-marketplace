#!/usr/bin/env python3
"""build-asset-manifest.py — Wiki 知识域注入清单 hook（SessionStart）

薄胶水：argparse → lib/manifest.build → print。
逻辑在 lib/manifest.py，路径在 lib/paths.py，跨域模式库读取在 lib/state.py。

eval 反馈环：--strategy 切换注入策略跑增益对比。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import manifest


def main():
    parser = argparse.ArgumentParser(
        description="Wiki 知识域注入清单（eval 反馈环：--strategy 切换注入策略跑增益对比）")
    parser.add_argument("--strategy", choices=["all", "project", "access"], default="all",
                        help="all=全量(默认,SessionStart用) / project=指定项目 / access=按热度取前N")
    parser.add_argument("--project", default=None, help="project 策略下指定项目名")
    parser.add_argument("--limit", type=int, default=3, help="access 策略下取前N个(默认3)")
    args = parser.parse_args()
    print(manifest.build(strategy=args.strategy, project=args.project, limit=args.limit))


if __name__ == "__main__":
    main()
