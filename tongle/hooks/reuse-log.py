#!/usr/bin/env python3
"""
reuse-log.py — 消费层复用证据（L1 访问层 + L3 调用层）

架构范式 §四消费环 L1 + §四加工环增智判据 L3（Level 3 任务1）：
  注入≠复用（公理1反模式②）—— CC 真读了 synthesis/concept 或真调了 Skill 才算消费证据。
  本 hook 记三类消费证据，把"内容是否被用"从不可验→可查。

PostToolUse(Read|Skill) 触发（matcher 在 hooks.json 配 Read|Skill）：
  - Read wiki/projects/<>/synthesis.md → kind=synthesis（项目级复用，Level 1）
  - Read wiki/concepts/<name>.md → kind=concept（跨域模式被访问，Level 3 断点①弱证据）
  - Skill 调用 → kind=skill（Skill被调用复用，Level 3 断点③调用层证据，写入侧；读取侧消费方待建，只写不读孤儿）

⚠️ 诚实标注证据层级（守反模式②，不虚标"已验复用闭环"，详设张力3 甲范围）：
  - synthesis/concept = 访问层证据（访问≠应用：CC读了不等于用模式做了判断）
  - skill = 调用层证据（调用≠蒸馏链路：Skill被调不等于跨域模式经nuwa蒸馏成Skill被复用）
  - 蒸馏全链路闭环（跨域模式→nuwa→Skill→调用）跨nuwa边界（nuwa自包含原则冲突），
    留远期（nuwa自然演化/A2A时代），L3 不强修

守红线（architecture-paradigm §九）：
  ② 单文件 jsonl 追加，不强建库结构（复用日志自然长出，攒够才回头建库）
  ⑦ 记"读了什么/调了什么"（消费证据），不记判别结果（判别经验库 Level 2 的活）
  Level 1 只记 synthesis。Level 3 扩 concept（断点①）+ skill（断点③）。

vs observe.sh：observe 是采集环原料（所有工具泛记）；
  reuse-log 是消费环证据（专记 synthesis/concept/skill 复用）。分属两环不合并（架构范式 §四）。

关联：architecture-paradigm §四消费环复用日志 + §四加工环增智判据（连接点3，复用日志↔跨域沉淀判据）/ §五原则4 / memory ke-architecture-paradigm
"""
import sys
import json
import os
import re
from datetime import datetime, timezone

_home = os.path.expanduser("~")
INSTINCTS_DIR = os.path.join(_home, ".claude", "instincts")
REUSE_LOG = os.path.join(INSTINCTS_DIR, "reuse-log.jsonl")
SESSION_ID = os.environ.get("CLAUDE_SESSION_ID", "unknown")

# 匹配 wiki/projects/<project>/synthesis.md（兼容绝对/相对路径、正反斜杠）
SYNTHESIS_RE = re.compile(r"wiki[/\\]projects[/\\]([^/\\]+)[/\\]synthesis\.md$", re.IGNORECASE)
# 匹配 wiki/concepts/<name>.md（Level 3：跨域模式被访问证据，断点①）
CONCEPT_RE = re.compile(r"wiki[/\\]concepts[/\\]([^/\\]+)\.md$", re.IGNORECASE)


def _extract_content(resp):
    """从 tool_response 提取 content（真实CC结构 file.content 嵌套，兼容旧形态）。
    CC 给 PostToolUse 的 tool_response 真实结构是
      {"type":"text","file":{"filePath":...,"content":"<文本>"}}（content 嵌在 file 下，非顶层）。
    失败响应含 does not exist / no such file 或 content 为空。
    """
    if isinstance(resp, dict):
        _file = resp.get("file")
        if isinstance(_file, dict):
            return str(_file.get("content", ""))  # 真实结构
        return str(resp.get("content", ""))  # 旧 dict(content) 兼容
    return str(resp)  # 纯字符串兼容


def _ok_from_content(content):
    """Read 是否成功：content 非空且不含失败标记"""
    head = content[:300].lower()
    return bool(content) and "does not exist" not in head and "no such file" not in head


def _append(entry):
    """追加到 reuse-log.jsonl，写失败不阻断主流程（消费证据丢失可接受）"""
    try:
        os.makedirs(INSTINCTS_DIR, exist_ok=True)
        with open(REUSE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        sys.exit(0)


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        sys.exit(0)
    try:
        d = json.loads(raw)
    except Exception:
        sys.exit(0)

    tool = d.get("tool_name", "")
    tool_input = d.get("tool_input", {}) or {}
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    session = d.get("session_id", SESSION_ID)

    # ---- 分流：Read（synthesis/concept）vs Skill（调用层）----
    if tool == "Read":
        file_path = tool_input.get("file_path", "")
        if not file_path:
            sys.exit(0)
        norm = file_path.replace("\\", "/")

        m_syn = SYNTHESIS_RE.search(norm)
        m_con = CONCEPT_RE.search(norm) if not m_syn else None  # synthesis 优先，不重复记
        if not m_syn and not m_con:
            sys.exit(0)  # 既非 synthesis 非 concept，不记

        content = _extract_content(d.get("tool_response", {}))
        ok = _ok_from_content(content)

        if m_syn:
            entry = {
                "ts": ts, "session": session, "kind": "synthesis",
                "project": m_syn.group(1), "file": file_path, "ok": ok,
            }
        else:  # m_con（跨域模式被访问，断点①弱证据）
            entry = {
                "ts": ts, "session": session, "kind": "concept",
                "concept": m_con.group(1), "file": file_path, "ok": ok,
            }
        _append(entry)

    elif tool == "Skill":
        # Skill 调用 = 调用层复用证据（断点③）。skill 名在 tool_input.skill。
        # Skill 调用无"读失败"语义，ok=True（调用发生即消费证据）
        skill_name = tool_input.get("skill", "")
        if not skill_name:
            sys.exit(0)
        entry = {
            "ts": ts, "session": session, "kind": "skill",
            "skill": skill_name, "ok": True,
        }
        _append(entry)

    else:
        sys.exit(0)  # 非 Read 非 Skill，不记

    sys.exit(0)


if __name__ == "__main__":
    main()
