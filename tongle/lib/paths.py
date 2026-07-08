"""路径解析统一入口（ke 所有路径于此定义，消除散落重复）

提取自 14+ 处散落的路径定义（grep 实测）：
- INSTINCTS_DIR 重复 6 处（expanduser / join(_home,...) 两种写法）
- CC_MEMORY_DIR 3 处 / WIKI_VAULT_PATH 4 处 / CLAUDE_PLUGIN_ROOT 5 处
- .sh 里 $HOME/.claude/instincts 散落 8+ 处

统一入口后，Step 2-3 提取内核模块时路径来源单一可改。
"""
import os

from . import platform


def home():
    """HOME 目录（已规范化跨平台，修 bug3）"""
    return platform.home()


def plugin_root():
    """plugin 根目录：优先 CLAUDE_PLUGIN_ROOT（plugin 调起），回退本文件上溯

    lib/paths.py → lib/ → 项目根
    """
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return env
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hooks_dir():
    """hooks/ 目录全路径"""
    return os.path.join(plugin_root(), "hooks")


def adapters_dir():
    """adapters/ 目录全路径"""
    return os.path.join(plugin_root(), "adapters")


def config_dir():
    """ke config 目录：优先 KE_CONFIG env，否则 plugin_root/config"""
    env = os.environ.get("KE_CONFIG")
    if env:
        return env
    return os.path.join(plugin_root(), "config")


def instincts_dir():
    """~/.claude/instincts — 状态目录（ke 运行态实体唯一存放处）"""
    return os.path.join(home(), ".claude", "instincts")


def instincts_file(name):
    """instincts 目录下文件全路径"""
    return os.path.join(instincts_dir(), name)


def cc_memory_dir():
    """CC memory 目录：env 优先，否则 ~/.claude/projects/{slug}/memory

    slug = home 路径的 / 全替换为 -（对应 CC 的 per-project 目录命名）
    """
    h = home()
    slug = h.replace("/", "-")
    return os.environ.get("CC_MEMORY_DIR", f"{h}/.claude/projects/{slug}/memory")


def wiki_vault():
    """Obsidian Vault 根：env 优先，否则 ~/Documents/Obsidian Vault"""
    return os.environ.get("WIKI_VAULT_PATH", os.path.join(home(), "Documents", "Obsidian Vault"))
