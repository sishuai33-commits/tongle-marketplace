#!/usr/bin/env python3
"""lib/scanner.py — 采集环主动扫描器（三源）

从 hooks/source-scanner.py 提取（Step 3.5）。逻辑在本文，IO 在 lib/state.py，
路径在 lib/paths.py。source-scanner.py 改薄壳调本模块。

架构范式 §四采集环：ke 主动从信息源（本地文件/对话历史/IMA知识库）采集原料。
observe 是被动记工具行为，本扫描器是主动扫信息源，补 observe 单源缺口。

守原则3"判别复用同库不另造"：scanner 只产原料（source-observations.jsonl），不判关系
（关系判别归 collector，scanner 不重复造判别器）。
守红线②：source-observations 随数据长不强建 schema（plain jsonl 追加）。
守红线⑥：只记文件变更信号不总结内容（防"漂亮总结"伪洞察，内容摘录留人裁看原文件）。
守简单方案优先：源1 只扫 .md + mtime 游标，不算 hash/不语义解析。

三源：
  源1 local_file：扫项目根 .md 文件 mtime 增量 → file_change 信号
  源2 transcript：扫对话历史 transcript 提取决策动作信号（决策/修正），补 observe 只记工具
    不记语义的缺口——决策/拍板/推翻/纠偏是判别经验库高价值原料
  源3 ima：IMA 知识库 openapi 接入 → ima_note_change 信号

触发：SessionEnd 串联 / 慢环全量 / 手动。
游标：instincts/.source-scan-cursor-local（源1 浮点epoch秒，避免ISO截断丢亚秒精度致重复采）
      instincts/.source-scan-cursor-ima（源3 毫秒epoch字符串）
      源2 无 mtime 游标（会话级一次性，靠去重键 session+action+context 防重复采）
输出：instincts/source-observations.jsonl
fail-open：扫不到也 exit 0（不阻断 SessionEnd/慢环）

关联：architecture-paradigm §四采集环 / §五原则3 / §九红线②⑥ /
      memory ke-architecture-paradigm / [[knowledge-engine-project]]
"""
import sys
import json
import os
import re
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

from lib import state, paths

# 扫描类型（最小集，守简单方案优先：知识型=.md；.py/.sh 代码变更走 observe 已记）
SCAN_EXTS = {".md"}
# 排除目录（.git/node_modules 噪声 + 归档/学习参考非活跃知识）
EXCLUDE_DIRS = {".git", "node_modules", ".obsidian", "归档", "学习参考"}

# 源1 扫描根（可配置：自用=项目根；朋友=按需）。默认项目开发根。
DEFAULT_SOURCE_ROOT = os.path.join(os.path.expanduser("~"), "Documents", "My_Code_Projects")

# === 源3 IMA openapi 配置（与 ima-skill 契约对齐，共享凭证路径 ~/.config/ima/） ===
# 凭证是资产（1月一换），机制读路径不硬编值（机制vs资产分离，与 invest 读 config 同构）
IMA_BASE = "https://ima.qq.com/openapi/note/v1"
IMA_CRED_DIR = os.environ.get("IMA_CRED_DIR", os.path.join(os.path.expanduser("~"), ".config", "ima"))
IMA_PAGE_LIMIT = 20  # openapi 硬约束 limit ∈ (0, 20]（实测 limit=50 报错 code=51）
IMA_TIMEOUT = 8  # 网络 timeout 秒（实测单次 0.429s，留余量；fail-open 不阻断）

# === 源2 transcript：决策动作信号正则（本项目真实用语，分两类） ===
# decision=决策/采纳类（新增认知落点），revision=修正/推翻类（已有认知修订）
# D'分级标记（宽匹配+人裁兜底不变，加 confidence 供人裁 high 优先 + 经验积累后自动降噪）：
#   high=指挥官定/拍板/否决/砍掉（明确决策动作，真决策率高）
#   low=决定/采纳/暂停/降级/放弃（开发态高频撞词，需人裁甄别）
DECISION_HIGH_RE = re.compile(r"指挥官定|拍板|否决|砍掉")
DECISION_LOW_RE = re.compile(r"决定|采纳|暂停|降级|放弃")
REVISION_RE = re.compile(r"推翻|纠正|修正|纠偏|翻案|改回|回退")

# 游标/输出完整路径（instincts/ 下，与原 source-scanner.py 常量一致保测试兼容）
INSTINCTS_DIR = paths.instincts_dir()
CURSOR_LOCAL = os.path.join(INSTINCTS_DIR, ".source-scan-cursor-local")
CURSOR_IMA = os.path.join(INSTINCTS_DIR, ".source-scan-cursor-ima")
SOURCE_OBS = paths.instincts_file("source-observations.jsonl")


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_cursor_epoch(cursor_path):
    """读游标文件转 float epoch 秒（源1）。不存在/空/非数字返回 None。

    用浮点 epoch 而非 ISO：ISO 截断到秒会丢亚秒精度，导致 mtime 恰在秒内的文件
    每次增量重复采。浮点 epoch 是内部状态非数据，格式自由，mtime 字段仍用 ISO 展示。
    """
    s = state.read_cursor(cursor_path)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _save_cursor(path, value):
    """写游标文件（覆盖，不建父目录，写失败不阻断=fail-open）。

    不用 state.write_cursor（它会 _ensure_parent 建目录）——scanner 守"instincts/ 缺失时不主动建"
    语义（instincts/ 由 session-start mkdir，scanner fail-open 静默吞 OSError），保原行为。
    """
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(value))
    except OSError:
        pass


def _append_obs(entry):
    """棘轮只升：append source-observations.jsonl（与 observations.jsonl 同形态，collector 统一消费）。

    不用 state.append_jsonl（它会 _ensure_parent 建目录）——同 _save_cursor 守 fail-open 不建目录语义。
    """
    try:
        with open(SOURCE_OBS, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def scan_local_file(root, full_scan):
    """源1：扫 root 下 .md 文件 mtime 增量，产 file_change 信号。

    三态（防首次部署 4670 文件涌入瘫痪人裁，守 M7+红线②）：
    - 首次部署（cursor 不存在 且 非 full_scan）：**建基线**——遍历写 cursor=max mtime，
      **0 信号**。历史文件不是"新变更"，不该当候选。
    - 增量（cursor 存在）：产 mtime > cursor 的变更信号。
    - full_scan（慢环触发）：归零 cursor 全量，产所有信号（人裁批次，非 SessionEnd 路径）。

    返回 (扫描条数, max_mtime_iso)。
    """
    cursor_epoch = None if full_scan else _load_cursor_epoch(CURSOR_LOCAL)
    is_first_deploy = (cursor_epoch is None and not full_scan)

    count = 0
    max_mtime_epoch = None
    max_mtime_iso = None
    now_ts = now_utc_iso()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # 原地改 dirnames 剪枝（os.walk 惯用法）
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SCAN_EXTS:
                continue
            fpath = os.path.join(dirpath, fn)
            try:
                st = os.stat(fpath, follow_symlinks=False)
            except OSError:
                continue
            mtime_epoch = st.st_mtime
            if max_mtime_epoch is None or mtime_epoch > max_mtime_epoch:
                max_mtime_epoch = mtime_epoch
                max_mtime_dt = datetime.fromtimestamp(mtime_epoch, tz=timezone.utc)
                max_mtime_iso = max_mtime_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            # 首次部署=建基线，不产信号（只记 max mtime 推进 cursor）
            if is_first_deploy:
                continue
            # 增量/full-scan：cursor 后的变更才产信号
            if cursor_epoch is not None and mtime_epoch <= cursor_epoch:
                continue  # 增量跳过已扫（浮点比较，无截断）
            mtime_dt = datetime.fromtimestamp(mtime_epoch, tz=timezone.utc)
            # 信号类型：cursor 后 mtime 变更（不区分 new/modified，关系留人裁）
            _append_obs({
                "ts": now_ts,
                "source": "local_file",
                "signal_type": "file_change",
                "path": fpath,
                "mtime": mtime_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size": st.st_size,
            })
            count += 1

    # 游标推进到本次 max mtime（浮点 epoch，增量下次跳过；全量重置基线）
    if max_mtime_epoch is not None:
        _save_cursor(CURSOR_LOCAL, max_mtime_epoch)

    return count, max_mtime_iso


def extract_context(text, match_start, span=60):
    """取匹配处前后各 span 字，人裁定位用（不总结语义，守红线⑥）。"""
    s = max(0, match_start - span)
    e = min(len(text), match_start + span)
    return text[s:e].replace("\n", " ").strip()


def _is_review_session(transcript_path):
    """识别批阅会话跳过采集（防自生成循环）。

    批阅会话高频含决策词（采纳/修正/纠正/指挥官定），但这些是裁决操作的
    话语=判别产物，非判别原料。采集回流致 pending-queue 自我繁殖
    （上轮批阅自产8条候选->下轮再裁->循环）。判别产物不回流为原料。
    宽匹配 discriminate-resolve.py：顺带跳过"讨论ke-review文档"的会话，
    同属开发态噪声，跳过无妨；不引入关键词维护成本（守简单）。
    """
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "discriminate-resolve.py" in line:
                    return True
    except OSError:
        pass
    return False


def scan_transcript(transcript_path, session_id):
    """源2：扫 transcript 提取决策动作信号，写 source-observations.jsonl。

    observe 记工具行为（Read/Write/Search）不记语义——决策/拍板/推翻/纠偏这类
    认知动作是判别经验库高价值原料（范式§四采集环：ke 主动从对话历史采集判别原料），
    本函数补 observe 单源缺口。

    守原则3：只产原料不判关系（关系留 collector --source → 人裁）。
    守红线⑥：只记动作类型+上下文片段，不总结内容（人裁看原 transcript）。
    守简单：一条 assistant 消息含任一决策动作词产 1 条信号（防同消息多次匹配噪声），
            revision 优先于 decision（修正类更稀缺高价值），本次运行内去重
            （跨运行由 collector pending/discard 兜底，同源1 行为）。
    fail-open：transcript 不存在/解析失败返回 0（不阻断 SessionEnd）。
    防循环：批阅会话跳过采集（_is_review_session），判别产物不回流为原料。
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return 0
    if _is_review_session(transcript_path):
        return 0  # 批阅会话不采集，防自生成循环
    now_ts = now_utc_iso()
    seen = set()  # 本次运行内去重 (session, action_type, context_key)
    count = 0
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(t for t in texts if t)
                if not text.strip():
                    continue
                # 决策动作判别：revision 优先（更稀缺），再 decision high/low（D'分级）
                action_type = None
                confidence = None
                match = REVISION_RE.search(text)
                if match:
                    action_type = "revision"
                    confidence = "high"  # revision 更稀缺高价值
                else:
                    match = DECISION_HIGH_RE.search(text)
                    if match:
                        action_type = "decision"
                        confidence = "high"
                    else:
                        match = DECISION_LOW_RE.search(text)
                        if match:
                            action_type = "decision"
                            confidence = "low"
                if not action_type:
                    continue
                context = extract_context(text, match.start())
                context_key = context[:40]  # 去重粒度
                key = (session_id, action_type, context_key)
                if key in seen:
                    continue
                seen.add(key)
                _append_obs({
                    "ts": now_ts,
                    "source": "transcript",
                    "signal_type": "decision_signal",
                    "session": session_id,
                    "action_type": action_type,
                    "confidence": confidence,
                    "context": context,
                    "transcript_path": transcript_path,
                })
                count += 1
    except OSError:
        return 0
    return count


# === 源3 IMA openapi 调用 ===
def _ima_load_creds():
    """读 IMA 凭证（client_id + api_key）。优先 env，回退 ~/.config/ima/ 文件。

    与 ima-skill 契约对齐（共享凭证路径，1月轮换天然支持——更新文件即可，机制下次读自动用新值）。
    守机制vs资产分离：机制读路径不硬编值，凭证不进 ke 代码/git/release。
    返回 (client_id, api_key)，缺失返回 (None, None)。
    """
    cid = os.environ.get("IMA_OPENAPI_CLIENTID")
    akey = os.environ.get("IMA_OPENAPI_APIKEY")
    if cid and akey:
        return cid, akey
    try:
        cid_file = os.path.join(IMA_CRED_DIR, "client_id")
        akey_file = os.path.join(IMA_CRED_DIR, "api_key")
        if not cid:
            cid = open(cid_file).read().strip()
        if not akey:
            akey = open(akey_file).read().strip()
    except OSError:
        return None, None
    return cid, akey


def _ima_call(path, payload, client_id, api_key):
    """调 IMA openapi（POST + JSON）。返回 data dict 或 None（失败/鉴权错）。

    fail-open：网络错/超时/鉴权失败返回 None，调用方按 None 跳过不阻断。
    返回 code!=0（如凭证过期 code=51 等）也返回 None，cursor 不推进等下次重试。
    """
    url = f"{IMA_BASE}/{path}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "ima-openapi-clientid": client_id,
            "ima-openapi-apikey": api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=IMA_TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if d.get("code") != 0:
        return None  # 鉴权失败/参数错等，fail-open
    return d.get("data", {})


def scan_ima(full_scan):
    """源3：IMA 笔记增量同步，提取笔记变更信号写 source-observations.jsonl。

    与 ima-skill 契约对齐（共享凭证路径+接口契约），ke 自写 openapi 调用（urllib 标准库，
    与源1/2 同构），不 import skill 代码（守机制vs资产分离：skill 管交互，ke 管自动采集）。

    链路（首版只用 list，不取内容 get_doc_content 守红线⑥）：
      list_note_by_folder_id(folder_id='', cursor) → 翻页列全部笔记
    （list_note_folder_by_cursor 用于补 folder_name，folder_id='' 列全部笔记跨 folder）

    守原则3：只产原料不判关系（关系留 collector --source → 人裁）。
    守红线⑥：只记笔记元数据（title/modify_time/folder），不取内容（人裁看 IMA 原文）。
    守 M7+红线②：首次部署建基线 0 信号（历史笔记不当新变更），增量后变更产信号。
    fail-open：凭证缺失/网络错/鉴权失败 → 返回 0 不阻断 SessionEnd/慢环。

    游标 .source-scan-cursor-ima：存上次扫到的 max modify_time（毫秒 epoch 字符串）。
    增量：笔记 modify_time > cursor 产信号；--full-scan 归零 cursor 全量。

    返回 (扫描条数, 错误提示)。
    """
    client_id, api_key = _ima_load_creds()
    if not client_id or not api_key:
        return 0, "skip(无 IMA 凭证，按 ~/.config/ima/ 或 env 配置)"

    # 游标：max modify_time（毫秒 epoch 字符串）。首次/全量=None（空字符串也当 None）
    cursor_mtime = state.read_cursor(CURSOR_IMA)
    if not cursor_mtime:
        cursor_mtime = None
    is_first_deploy = (cursor_mtime is None and not full_scan)

    # folder_name 映射（folder_id → name），用 list_note_folder_by_cursor 补
    folder_names = {}
    fdata = _ima_call("list_note_folder_by_cursor", {"cursor": "", "limit": IMA_PAGE_LIMIT},
                      client_id, api_key)
    if isinstance(fdata, dict):
        for f in fdata.get("note_book_folders", []):
            bi = f.get("folder", {}).get("basic_info", {})
            folder_names[bi.get("folder_id", "")] = bi.get("name", "")

    # 翻页列全部笔记（folder_id='' = 全部笔记本，实测确认）
    # 分页机制（实测）：cursor 是数字偏移（首次 ""，后续传已取数累计），非 next_cursor
    # （接口实际不返回 next_cursor，is_end 恒 False；实测 cursor="5" 返回第6-10条）
    now_ts = now_utc_iso()
    seen_keys = set()  # 本次运行内 docid 去重（跨页防重复）
    count = 0
    max_mtime = cursor_mtime or "0"
    offset = 0
    pages = 0
    while True:
        pages += 1
        data = _ima_call("list_note_by_folder_id",
                         {"folder_id": "", "cursor": str(offset), "limit": IMA_PAGE_LIMIT},
                         client_id, api_key)
        if not isinstance(data, dict):
            break  # 网络错/鉴权失败，fail-open 退出（已采的 count 保留，cursor 按已采推进）
        notes = data.get("note_book_list", [])
        if not notes:
            break  # 空页=取完
        for n in notes:
            # 字段双层嵌套 basic_info.basic_info（实测确认，解嵌套取值）
            bi = n.get("basic_info", {}).get("basic_info", {})
            docid = bi.get("docid", "")
            if not docid or docid in seen_keys:
                continue
            seen_keys.add(docid)
            mtime = str(bi.get("modify_time", "0"))
            # 推进 max_mtime（增量下次跳过已扫）
            if mtime > max_mtime:
                max_mtime = mtime
            # 首次部署=建基线，不产信号（只推进 cursor）
            if is_first_deploy:
                continue
            # 增量：cursor 后的变更才产信号
            if cursor_mtime and mtime <= cursor_mtime:
                continue
            _append_obs({
                "ts": now_ts,
                "source": "ima",
                "signal_type": "ima_note_change",
                "session": "ima-scan",
                "docid": docid,
                "title": bi.get("title", ""),
                "modify_time": mtime,
                "folder_id": bi.get("folder_id", ""),
                "folder_name": folder_names.get(bi.get("folder_id", ""), ""),
            })
            count += 1
        offset += len(notes)
        # 翻页终止：取到不足一页=末页（接口 is_end 恒 False 不可靠，用笔记数<limit 判末页）
        if len(notes) < IMA_PAGE_LIMIT or pages > 50:  # 50页安全上限防死循环
            break

    # 游标推进到本次 max mtime（增量下次跳过已扫；全量重置基线）
    _save_cursor(CURSOR_IMA, max_mtime)

    return count, None


def main():
    ap = argparse.ArgumentParser(description="采集环主动扫描器（三源）")
    ap.add_argument("--source", choices=["local_file", "transcript", "ima", "all"],
                    default="all", help="扫描哪个源（all=已实现的都跑）")
    ap.add_argument("--full-scan", action="store_true",
                    help="全量重扫（归零游标，慢环触发）")
    ap.add_argument("--root", default=DEFAULT_SOURCE_ROOT,
                    help=f"源1 扫描根（默认 {DEFAULT_SOURCE_ROOT}）")
    ap.add_argument("--transcript", default=os.environ.get("KE_TRANSCRIPT_PATH", ""),
                    help="源2 transcript 文件路径（SessionEnd 由 shell 从 stdin 解析传入）")
    ap.add_argument("--session", default=os.environ.get("KE_SESSION_ID", "unknown"),
                    help="源2 会话 ID（SessionEnd 由 shell 传入）")
    args = ap.parse_args()

    sources_to_run = []
    if args.source in ("local_file", "all"):
        sources_to_run.append("local_file")
    if args.source in ("transcript", "all"):
        sources_to_run.append("transcript")
    if args.source in ("ima", "all"):
        sources_to_run.append("ima")

    results = {}
    for src in sources_to_run:
        if src == "local_file":
            if not os.path.isdir(args.root):
                results[src] = "skip(根目录不存在)"
                continue
            count, max_mtime = scan_local_file(args.root, args.full_scan)
            results[src] = f"{count}条 file_change 信号, max_mtime={max_mtime}"
        elif src == "transcript":
            if not args.transcript:
                results[src] = "skip(无 --transcript，SessionEnd 自动传入或手动指定)"
                continue
            count = scan_transcript(args.transcript, args.session)
            results[src] = f"{count}条 decision_signal 信号"
        elif src == "ima":
            count, err = scan_ima(args.full_scan)
            results[src] = f"{count}条 ima_note_change 信号" + (f" [{err}]" if err else "")

    # stderr 供日志（不污染 stdout，SessionEnd fail-open）
    for src, r in results.items():
        print(f"[source-scanner] {src}: {r}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
