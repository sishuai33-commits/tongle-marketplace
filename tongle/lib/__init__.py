"""knowledge-engine 内核层（纯 Python, 零 CC 依赖, 可独立单测）

模块：
- platform: 跨平台兼容原语（Windows Git Bash / MSYS / 原生）
- paths: 路径解析统一入口（CLAUDE_PLUGIN_ROOT/HOME/instincts）
- state: 状态 I/O 统一入口（instincts 目录唯一读写通道）

后续 Step 2-3 提取 observe/discriminate/review/refine/cross_domain/manifest/reuse/health/guards。
"""
