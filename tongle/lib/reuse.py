"""消费环复用证据（L1 访问层 + L3 调用层）

提取自 reuse-log.py（Step 2 内核提取1）。
纯逻辑：正则匹配 + content 提取 + ok 判定 + 工具分流 → entry。
IO 由调用方经 lib/state 写（不强耦合文件系统），路径经 lib/paths。

守红线②单文件 jsonl 追加（不强建库结构）；⑦记"读了什么/调了什么"不记判别结果。
注入≠复用（公理1反模式②）—— CC 真读了 synthesis/concept 或真调了 Skill 才算消费证据。
"""
import re
from datetime import datetime, timezone

# 匹配 wiki/projects/<project>/synthesis.md（兼容绝对/相对路径、正反斜杠）
SYNTHESIS_RE = re.compile(r"wiki[/\\]projects[/\\]([^/\\]+)[/\\]synthesis\.md$", re.IGNORECASE)
# 匹配 wiki/concepts/<name>.md（Level 3 断点①：跨域模式被访问弱证据）
CONCEPT_RE = re.compile(r"wiki[/\\]concepts[/\\]([^/\\]+)\.md$", re.IGNORECASE)


def extract_content(resp):
    """从 tool_response 提取 content（真实CC结构 file.content 嵌套，兼容旧形态）

    CC 给 PostToolUse 的 tool_response 真实结构：
      {"type":"text","file":{"filePath":...,"content":"<文本>"}}（content 嵌在 file 下，非顶层）
    失败响应含 does not exist / no such file 或 content 为空。
    """
    if isinstance(resp, dict):
        _file = resp.get("file")
        if isinstance(_file, dict):
            return str(_file.get("content", ""))  # 真实结构
        return str(resp.get("content", ""))  # 旧 dict(content) 兼容
    return str(resp)  # 纯字符串兼容


def ok_from_content(content):
    """Read 是否成功：content 非空且不含失败标记"""
    head = content[:300].lower()
    return bool(content) and "does not exist" not in head and "no such file" not in head


def classify(tool, tool_input, tool_response=None, session="unknown", ts=None):
    """分类工具调用 → 复用证据 entry，不匹配返回 None

    - Read wiki/projects/<>/synthesis.md → kind=synthesis（项目级复用，Level 1）
    - Read wiki/concepts/<>.md → kind=concept（断点①弱证据，synthesis 优先不重复记）
    - Skill 调用 → kind=skill（断点③调用层证据）
    - 其他 → None

    纯逻辑，不写 IO。调用方经 lib/state.append_jsonl 写入。
    """
    if ts is None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tool_input = tool_input or {}

    if tool == "Read":
        file_path = tool_input.get("file_path", "")
        if not file_path:
            return None
        norm = file_path.replace("\\", "/")
        m_syn = SYNTHESIS_RE.search(norm)
        m_con = CONCEPT_RE.search(norm) if not m_syn else None
        if not m_syn and not m_con:
            return None
        content = extract_content(tool_response or {})
        ok = ok_from_content(content)
        if m_syn:
            return {"ts": ts, "session": session, "kind": "synthesis",
                    "project": m_syn.group(1), "file": file_path, "ok": ok}
        return {"ts": ts, "session": session, "kind": "concept",
                "concept": m_con.group(1), "file": file_path, "ok": ok}

    if tool == "Skill":
        skill_name = tool_input.get("skill", "")
        if not skill_name:
            return None
        return {"ts": ts, "session": session, "kind": "skill",
                "skill": skill_name, "ok": True}

    return None
