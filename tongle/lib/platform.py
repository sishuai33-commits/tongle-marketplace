"""跨平台兼容原语（Windows Git Bash / MSYS / 原生 Python）

从 9 个 .sh 文件头部的 Windows 兼容代码提取（v1.1.0 朋友反馈 bug1/bug2/bug3）：
- bug1: PYTHONUTF8=1（bash export）→ Python 侧用 errors='replace' 兜底（见 state.py）
- bug2: pre-security.js 反斜杠归一化（Node 侧，红线1 不改，留 pre-security）
- bug3: $HOME /c/Users/x → C:/Users/x（Python open() 不认 /c/ 盘符形式）

lib 模块用 Python 侧原语保障跨平台，Step 4 外壳替换为 .py 后可统一收口。
"""
import os


def normalize_home_path(path):
    """Git Bash 下 $HOME=/c/Users/x，Python open() 不认 /c/ 形式 → C:/Users/x

    仅修正形如 /c/ /d/ 的盘符路径（盘符为单个字母），盘符转大写（Windows 约定）。
    对应 .sh 里: case "$HOME" in /[a-zA-Z]/*) HOME="${HOME:1:1}:/${HOME:3}";; esac
    （.sh 保留原大小写；Python 侧统一转大写更规范，Windows 不区分大小写功能等价）
    """
    if len(path) >= 3 and path[0] == "/" and path[2] == "/" and path[1].isalpha():
        return path[1].upper() + ":" + path[2:]
    return path


def home():
    """规范化的 HOME 路径（修 bug3：Git Bash /c/ → C:/）"""
    return normalize_home_path(os.path.expanduser("~"))
