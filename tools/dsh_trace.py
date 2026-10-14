#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dsh_trace —— AI 执行「素材本体」抽取器。

把一次 AI 执行（一个主会话 + 它派出的全部子孙 agent 会话）抽成三件可分析的素材：
  1. 上下文 context    —— 模型每一步实际看到的装载面
  2. 思考过程 reasoning —— 每个思考块的位置、时间、字数、原文
  3. 调用链 callchain  —— 工具调用→结果（耗时/成败/体量）＋ agent 拓扑（派发/回执/结算）

产物（落在 --out 目录里）：
  trace.jsonl     规范化事件流（一行一事件，可比对、可 diff）
  manifest.json   执行树 + 体量账 + 派生指标 + 抽取器版本 + 源文件 sha256
  report.md       人读摘要

「稳定」的四个定义（也是 --verify 的四个自检项）：
  1. 幂等：同一源两次抽取，trace.jsonl 字节一致
  2. 完备：源记录的每一条都在 trace.jsonl 里有归宿（无静默丢弃）
  3. 对账：各类字符数之和 == 从源直接统计的值
  4. 诚实：不认识的记录类型一律落成 unknown:<type> 并在 manifest 里列名，绝不猜

用法：
  python dsh_trace.py --session <会话id> --out <目录> [--verify]
  python dsh_trace.py --path "<...>/session.v4.jsonl.zstd" --out <目录>
  python dsh_trace.py --list
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import zstandard

EXTRACTOR = "dsh_trace"
VERSION = "0.2.0"
SUPPORTED_FORMAT = 4
TZ = timezone(timedelta(hours=8))
DEFAULT_SESSIONS_ROOT = Path.home() / ".dsh" / "sessions"

# 只读工具（读完不动世界）；其余工具视为"可能改变世界"
READONLY_TOOLS = {"read", "glob", "grep", "list_agents", "job_list", "job_output",
                  "canvas_state", "view_canvas", "read_image", "get_goal", "skill",
                  "ask_user_question", "browser_inspect", "web_search", "web_fetch",
                  "task_board_list", "task_board_get", "mnemon_recall", "mnemon_status",
                  "mnemon_related", "mnemon_document_search", "mnemon_memory_bodies"}

# shell 命令的只读/变更启发式（**标注为启发式**：判据写进 meta，可复核、可被项目覆盖）
SHELL_READ_HINTS = (
    "get-childitem", "get-content", "select-string", "test-path", "get-item",
    "get-command", "where.exe", "resolve-path", "get-filehash", "get-process",
    "git log", "git status", "git diff", "git show", "git branch", "git grep",
    "git rev-parse", "git remote", "ls ", "dir ", "cat ", "head ", "tail ",
    "type ", "findstr", "measure-object", "compare-object", "get-date",
    "python -c", "python -m pytest", "pytest", "python tools/", "echo", "write-output",
)
SHELL_MUTATE_HINTS = (
    "remove-item", "new-item", "set-content", "add-content", "out-file",
    "move-item", "copy-item", "rename-item", "git add", "git commit", "git checkout",
    "git pull", "git push", "git reset", "git stash", "git mv", "git rm",
    "pip install", "npm install", "mkdir", "rm ", "del ", "curl ", "invoke-restmethod",
    "invoke-webrequest", "set-", "start-process",
)


def classify_shell(cmd: str):
    """返回 (判定, 依据)。判定 ∈ 只读查询/变更动作/未判定。启发式，依据随判定一起留档。"""
    if not cmd:
        return "未判定", ""
    low = cmd.lower()
    m = next((h for h in SHELL_MUTATE_HINTS if h in low), None)
    r = next((h for h in SHELL_READ_HINTS if h in low), None)
    if m and not r:
        return "变更动作", m
    if r and not m:
        return "只读查询", r
    if m and r:
        # 两者都有：按首次出现位置判决，并写明
        im, ir = low.find(m), low.find(r)
        return ("变更动作" if im < ir else "只读查询"), f"{m}@{im} vs {r}@{ir}"
    return "未判定", ""

# 注入分类：把「真人」与「系统/项目/AI 产出」分开
INJECTION_KIND = {
    "user": "human",
    "agent-message": "relay",
    "subagent-settled": "notice",
    "dsh-mnemon": "memory",
    "skill-catalog": "skill_catalog",
    "skill-invocation": "skill_body",
    "agent-instructions": "repo_instructions",
    "runtime-context": "runtime_context",
    "model-selection": "notice",
}


def iso(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, TZ).isoformat()
    except Exception:
        return None


# ────────────────────────────── 源读取 ──────────────────────────────

def read_session(path: Path):
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    dctx = zstandard.ZstdDecompressor()
    with dctx.stream_reader(io.BytesIO(raw)) as r:
        data = r.read()
    recs = []
    for line in data.decode("utf-8", "replace").split("\n"):
        s = line.strip()
        if not s:
            continue
        try:
            recs.append(json.loads(s))
        except Exception:
            recs.append({"__raw__": s})
    return recs, sha


def find_session(sid: str, root: Path):
    hits = list(root.glob(f"*/{sid}/session.v4.jsonl.zstd"))
    if not hits:
        hits = list(root.glob(f"*/session-{sid}*/session.v4.jsonl.zstd"))
    return hits[0] if hits else None


def list_sessions(root: Path):
    out = [(fp.parent.name, fp) for fp in root.glob("*/session.v4.jsonl.zstd")]
    out.sort(key=lambda x: x[1].stat().st_mtime, reverse=True)
    return out


# ────────────────────────────── 规范化 ──────────────────────────────

def blocks_of(o):
    dd = o.get("data") or {}
    t = o.get("type")
    if t == "user/message":
        return dd.get("content") or []
    if t in ("assistant/message", "system/message", "tool/result"):
        return ((dd.get("message") or {}).get("content")) or []
    return []


def normalize(o, node_id, is_root, seen_call_ids, call_tool=None):
    """一条源记录 → 0..n 个规范化事件。seen_call_ids 用于去除 tool/call 的重复登记。"""
    call_tool = call_tool or {}
    t = o.get("type")
    dd = o.get("data") or {}
    tms = o.get("time")
    base = {"node_id": node_id, "seq": o.get("seq"), "t": iso(tms), "t_ms": tms,
            "turn": dd.get("turn"), "step": dd.get("step")}
    ev = []

    if t == "session":
        return [dict(base, kind="session_start", chars=0, meta={
            "session_id": o.get("id"), "format_version": o.get("version"),
            "cwd": o.get("cwd"), "preset": o.get("agentPreset"),
            "created_at": iso(o.get("createdAt")),
            "parent": o.get("parentSession"),
            "delegation_depth": o.get("delegationDepth"),
            "origin": o.get("origin")})]

    if t in ("turn/start", "turn/end"):
        return [dict(base, kind=t.replace("/", "_"), chars=0,
                     meta={"reason": dd.get("reason")})]

    if t == "user/message":
        src = dd.get("source") or {}
        k = src.get("kind") or "unknown"
        # 子 agent 会话里 source.kind=='user' 的第一条 = AI 写给它的任务书，不是真人
        if k == "user":
            kind = "human" if (is_root and src.get("rpcId")) else \
                   ("task_brief" if not is_root else "human")
        else:
            kind = INJECTION_KIND.get(k, "injection_other")
        for i, b in enumerate(blocks_of(o)):
            if not isinstance(b, dict) or b.get("type") != "text":
                continue
            txt = b.get("text") or ""
            ev.append(dict(base, kind=kind, chars=len(txt), text=txt,
                           meta={"source_kind": k, "block_index": i,
                                 "rpc_id": src.get("rpcId"),
                                 "sender": src.get("senderSessionId"),
                                 "form": src.get("form")}))
        if not ev:
            ev.append(dict(base, kind=kind, chars=0, text="", meta={"source_kind": k}))
        return ev

    if t == "assistant/message":
        src = dd.get("source") or {}
        for i, b in enumerate(blocks_of(o)):
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                ev.append(dict(base, kind="assistant_text", chars=len(b.get("text") or ""),
                               text=b.get("text") or "",
                               meta={"provider": src.get("provider"), "model": src.get("model")}))
            elif bt == "reasoning":
                ev.append(dict(base, kind="reasoning", chars=len(b.get("text") or ""),
                               text=b.get("text") or "", meta={}))
            elif bt == "tool-call":
                args = b.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                cid = b.get("id")
                if cid:
                    seen_call_ids.add(cid)
                ev.append(dict(base, kind="tool_call", chars=len(args or ""), text=args or "",
                               meta={"tool": b.get("name"), "call_id": cid}))
        return ev

    if t == "tool/call":
        cid = dd.get("callId")
        if cid and cid in seen_call_ids:
            return [dict(base, kind="dedup:tool_call", chars=0,
                         meta={"call_id": cid, "tool": dd.get("name")})]
        args = dd.get("arguments") or ""
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        return [dict(base, kind="tool_call", chars=len(args), text=args,
                     meta={"tool": dd.get("name"), "call_id": cid, "from": "tool/call"})]

    if t == "tool/result":
        msg = dd.get("message") or {}
        # ⚠ toolCallId 与 source.callId 都在 message 里，不在 data 顶层。
        # 读错层级会导致 ask_user_question 认不出来 ⇒ 真人作答被漏算（实测踩过）。
        cid = msg.get("toolCallId") or (msg.get("source") or {}).get("callId") or dd.get("toolCallId")
        txt = "\n".join(b.get("text") or "" for b in blocks_of(o)
                        if isinstance(b, dict) and b.get("type") == "text")
        tool = call_tool.get(cid)
        # ⚠ 真人的第二路输入：ask_user_question 的**自由文本作答**以工具结果形式回来。
        # 只认 user/message 会把真人说的话漏掉，进而把真话误判成 AI 伪造。
        if tool == "ask_user_question":
            answers, human_text = [], []
            try:
                payload = json.loads(txt)
                for a in payload.get("answers") or []:
                    sel = a.get("selected") or []
                    cust = a.get("custom") or ""
                    answers.append({"id": a.get("id"), "selected": sel, "custom": cust})
                    human_text.append(" ".join(sel))
                    human_text.append(cust)
            except Exception:
                if txt.strip():
                    human_text.append(txt)
                    answers = [{"id": None, "selected": [], "custom": txt}]
            joined = "\n".join(x for x in human_text if x)
            ev = dict(base, kind="human_answer", chars=len(joined), text=joined,
                      meta={"call_id": cid, "answers": answers,
                            "via": "ask_user_question"})
            return [ev, dict(base, kind="tool_result", chars=len(txt), text=txt,
                             meta={"call_id": cid, "is_error": bool(msg.get("isError")),
                                   "error": dd.get("error"), "note": "ask_user_question 的原始回包"})]
        return [dict(base, kind="tool_result", chars=len(txt), text=txt,
                     meta={"call_id": cid,
                           "is_error": bool(msg.get("isError")),
                           "error": dd.get("error")})]

    if t == "system/message":
        txt = "\n".join(b.get("text") or "" for b in blocks_of(o) if isinstance(b, dict))
        return [dict(base, kind="system_prompt", chars=len(txt), text=txt, meta={})]

    if t == "subagent/catalog":
        child = dd.get("childId")
        return [dict(base, kind="subagent_spawn", chars=0,
                     meta={"child_id": child, "label": dd.get("label"),
                           "mode": dd.get("mode")})] if child else []

    if t == "compaction/prune":
        # 平台把已深埋的历史块从窗口里裁掉（tool result clearing）。这是**上下文口径的关键**：
        # 累计流入量 ≠ 窗口里同时存在的量。shadowedSeqs 精确指出被裁掉的是哪几条。
        seqs = dd.get("shadowedSeqs") or []
        return [dict(base, kind="compaction_prune", chars=0,
                     meta={"shadowed_seqs": list(seqs),
                           "shadowed_range": dd.get("shadowedRange"),
                           "shadowed_tokens": dd.get("shadowedTokenCount")})]

    if t == "request/context":
        return [dict(base, kind="context_window", chars=0, meta=dict(dd))]

    if t in ("permission/preset", "sandbox/mode", "approval/policy", "model/selection"):
        return [dict(base, kind="env_notice", chars=0, meta={"type": t, "data": dd})]

    if t == "session/title":
        return [dict(base, kind="session_title", chars=0, meta={"title": dd.get("title")})]

    if t == "todo/write":
        return [dict(base, kind="todo", chars=0, meta={"n": len(dd.get("todos") or [])})]

    if "__raw__" in o:
        return [dict(base, kind="unparsed", chars=len(o["__raw__"]), text=o["__raw__"], meta={})]

    return [dict(base, kind="other:" + str(t), chars=0, meta={})]


def build_node(sid, path: Path):
    recs, sha = read_session(path)
    if not recs:
        return None
    hdr = recs[0]
    if hdr.get("type") != "session":
        raise SystemExit(f"文件不是会话头：{path}")
    if hdr.get("version") != SUPPORTED_FORMAT:
        raise SystemExit(
            f"不支持的会话格式版本 {hdr.get('version')}（本抽取器支持 v{SUPPORTED_FORMAT}）：{path}\n"
            f"格式变了就要先确认，不要猜。")
    is_root = (hdr.get("delegationDepth") or 0) == 0

    # 第一遍：建 call_id → 工具名 的映射。
    # 为什么必须先建：**真人的自由文本作答是通过 ask_user_question 的"工具结果"回来的**，
    # 不在 user/message 里。不把这一路算进真人输入，就会把真人说的话误判成 AI 伪造。
    call_tool = {}
    for o in recs:
        t = o.get("type")
        dd = o.get("data") or {}
        if t == "tool/call":
            cid = dd.get("callId")
            if cid:
                call_tool[cid] = dd.get("name")
        elif t == "assistant/message":
            for b in blocks_of(o):
                if isinstance(b, dict) and b.get("type") == "tool-call" and b.get("id"):
                    call_tool[b["id"]] = b.get("name")

    # 去重集合按节点独立：assistant 消息里的 tool-call 块与独立 tool/call 记录会各记一次，
    # 只保留一次（tool_call），第二条记成 dedup:tool_call，保证可对账又不虚增体量。
    seen_call_ids = set()
    events = []
    for o in recs:
        events.extend(normalize(o, sid, is_root, seen_call_ids, call_tool))
    return dict(id=sid, path=str(path), sha256=sha, header=hdr,
                t0=hdr.get("createdAt"), last=recs[-1].get("time"),
                n_records=len(recs), events=events,
                children=[e["meta"]["child_id"] for e in events
                          if e["kind"] == "subagent_spawn"])


def collect_tree(root_sid, sessions_root: Path, _seen=None):
    _seen = _seen if _seen is not None else set()
    if root_sid in _seen:
        return []
    _seen.add(root_sid)
    fp = find_session(root_sid, sessions_root)
    if not fp:
        raise SystemExit(f"找不到会话 {root_sid}（在 {sessions_root} 下）")
    node = build_node(root_sid, fp)
    if not node:
        return []
    out = [node]
    for c in node["children"]:
        out.extend(collect_tree(c, sessions_root, _seen))
    return out


def collect_from_path(path: Path, sessions_root: Path, _seen=None):
    """从一个明确的会话文件路径起抽（子 agent 的会话仍按 id 去会话根里找）。"""
    _seen = _seen if _seen is not None else set()
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"文件不存在：{path}")
    node = build_node(path.parent.name, path)
    if not node:
        raise SystemExit(f"文件不是有效的会话（读不到任何记录）：{path}")
    if node["id"] in _seen:
        return []
    _seen.add(node["id"])
    out = [node]
    for c in node["children"]:
        out.extend(collect_tree(c, sessions_root, _seen))
    return out


# ────────────────────────────── 派生指标 ──────────────────────────────

def derive(all_events, node_by_id, nodes_sorted=None):
    """从素材本身派生指标；只做可复算的确定性计算。"""
    m = {}
    kinds = collections.Counter(e["kind"] for e in all_events)
    chars = collections.Counter()
    for e in all_events:
        chars[e["kind"]] += e["chars"] or 0
    m["chars_by_kind"] = dict(sorted(chars.items(), key=lambda x: -x[1]))

    # 信噪比：真人高信号 / 其它一切
    # ⚠ 真人输入有**两路**：user/message（打字的）＋ ask_user_question 的自由文本作答。
    # 漏掉第二路会低估真人信号、并可能把真人说的话误判成 AI 伪造。
    human = chars.get("human", 0)
    human_ans = chars.get("human_answer", 0)
    total = sum(chars.values())
    m["human_chars"] = human
    m["human_answer_chars"] = human_ans
    m["human_total_chars"] = human + human_ans
    m["total_chars"] = total
    m["human_share"] = round((human + human_ans) / total, 5) if total else None
    m["human_snr"] = round((human + human_ans) / max(total - human - human_ans, 1), 5)

    # 首次推进动作表：只报**素材事实**（可复核），不预判"业务动作"是什么。
    # 「什么算业务动作」由项目自己的链路定义决定，抽取器不越权。
    # 「根」= 本次抽取的入口节点（第一个节点），不假设它一定是 delegationDepth==0。
    # 以子 agent 为根抽取时，depth 是 1；写死 depth==0 会让所有根级指标变成 0（实测踩过）。
    root = nodes_sorted[0]["id"] if nodes_sorted else None
    ev_root = [e for e in all_events if e["node_id"] == root]
    t_human = next((e["t_ms"] for e in ev_root if e["kind"] in ("human", "human_answer")), None)
    if t_human is None:
        # 子会话没有"打字"的真人消息：它的开口 = 任务书到达时刻（task_brief）。
        # 用任务书当计时零点，才能量出"子 agent 接到活之后多久动第一下"。
        t_human = next((e["t_ms"] for e in ev_root if e["kind"] == "task_brief"), None)
    m["timing_origin"] = ("human" if any(e["kind"] in ("human", "human_answer")
                                         for e in ev_root) else "task_brief")
    calls = [e for e in ev_root if e["kind"] == "tool_call"]
    m["root_tool_calls"] = len(calls)

    def since(e):
        return (e["t_ms"] - t_human) if (t_human and e["t_ms"]) else None

    def first_where(pred):
        for i, e in enumerate(calls):
            if pred(e):
                return {"t": e["t"], "ms": e["t_ms"], "index": i + 1,
                        "tool": (e["meta"] or {}).get("tool"),
                        "args": (e["text"] or "")[:220], "since_human_ms": since(e),
                        "turn": e.get("turn")}
        return None

    m["first_tool_call"] = first_where(lambda e: True)
    m["milestones"] = {
        "first_artifact": first_where(
            lambda e: (e["meta"] or {}).get("tool") in
            ("write", "edit", "present", "generate_image", "generate_images")),
        "first_subagent": first_where(lambda e: (e["meta"] or {}).get("tool") == "subagent"),
        "first_ask_human": first_where(lambda e: (e["meta"] or {}).get("tool") == "ask_user_question"),
        "first_http_or_probe": first_where(
            lambda e: (e["meta"] or {}).get("tool") in ("web_fetch", "browser_page", "browser_session")),
    }
    first_reason = next((e for e in ev_root if e["kind"] == "reasoning"), None)
    m["first_reasoning"] = ({"t": first_reason["t"], "chars": first_reason["chars"],
                             "since_human_ms": since(first_reason)} if first_reason else None)
    m["first_human_at"] = iso(t_human) if t_human else None

    # 根会话 shell 动作画像（启发式判定，依据随行留档）
    shell = collections.Counter()
    shell_detail = []
    for e in calls:
        if (e["meta"] or {}).get("tool") != "pwsh":
            continue
        try:
            cmd = json.loads(e["text"]).get("command", "")
        except Exception:
            cmd = e["text"] or ""
        verdict, basis = classify_shell(cmd)
        shell[verdict] += 1
        if verdict == "未判定" and len(shell_detail) < 8:
            shell_detail.append({"t": e["t"], "index": calls.index(e) + 1,
                                 "cmd": cmd[:200]})
    m["root_shell_profile"] = {"counts": dict(shell), "undecided_samples": shell_detail,
                               "method": "关键词启发式（SHELL_READ_HINTS/SHELL_MUTATE_HINTS），"
                                         "判定依据见 classify_shell()，项目可覆盖"}

    # 吃上下文的大块（工具结果 Top）
    # ⚠ 平台对超长工具结果会**溢出到临时文件**，只把一段固定长度的提示放回上下文
    #   （实测该提示恰好 ~49,933 字符）。所以这个榜单量的是「**回灌进上下文的量**」，
    #   不是被读内容的真实体量；真实体量在提示里给的 spill 文件里。
    big = sorted((e for e in all_events if e["kind"] == "tool_result"),
                 key=lambda e: -(e["chars"] or 0))[:10]
    m["biggest_tool_results"] = [{
        "node_id": e["node_id"], "t": e["t"], "turn": e.get("turn"),
        "chars": e["chars"], "tool_call_id": (e["meta"] or {}).get("call_id"),
        "is_spill_notice": "dsh-spill" in (e["text"] or ""),
        "head": (e["text"] or "")[:160],
    } for e in big]
    m["spilled_results"] = sum(1 for e in all_events
                               if e["kind"] == "tool_result" and "dsh-spill" in (e["text"] or ""))
    m["spilled_chars"] = sum(e["chars"] or 0 for e in all_events
                             if e["kind"] == "tool_result" and "dsh-spill" in (e["text"] or ""))

    # 每轮上下文增量（看腐化曲线）
    turn_inflow = collections.Counter()
    turn_calls = collections.Counter()
    for e in all_events:
        if e["node_id"] != root:
            continue
        tn = e.get("turn")
        if tn is None:
            continue
        turn_inflow[tn] += e["chars"] or 0
        if e["kind"] == "tool_call":
            turn_calls[tn] += 1
    m["root_turn_inflow"] = [{"turn": tn, "chars": turn_inflow[tn], "tool_calls": turn_calls[tn]}
                             for tn in sorted(turn_inflow)]

    # ── 窗口存活量（live context）：累计流入 − 被裁掉的
    # shadowedSeqs 指到具体 seq，所以能精确扣除；这是"上下文里同时有多少"的口径。
    live_curves = {}
    prune_info = []
    for nid in node_by_id:
        ev = [e for e in all_events if e["node_id"] == nid]
        by_seq = {}
        for e in ev:
            if e["seq"] is not None:
                by_seq.setdefault(e["seq"], 0)
                by_seq[e["seq"]] += e["chars"] or 0
        live = 0
        peak = 0
        pruned = 0
        n_prune = 0
        curve = []
        for e in ev:
            if e["kind"] == "compaction_prune":
                n_prune += 1
                got = 0
                for s in (e["meta"] or {}).get("shadowed_seqs") or []:
                    got += by_seq.get(s, 0)
                live -= got
                pruned += got
                curve.append({"t": e["t"], "turn": e.get("turn"), "live": live,
                              "pruned_now": got,
                              "pruned_tokens": (e["meta"] or {}).get("shadowed_tokens")})
                prune_info.append({"node_id": nid, "t": e["t"], "seqs": (e["meta"] or {}).get("shadowed_seqs"),
                                   "chars": got, "tokens": (e["meta"] or {}).get("shadowed_tokens")})
                continue
            live += e["chars"] or 0
            peak = max(peak, live)
        live_curves[nid] = {"final_live_chars": live, "peak_live_chars": peak,
                            "pruned_chars": pruned, "prune_events": n_prune,
                            "inflow_chars": sum(x["chars"] or 0 for x in ev),
                            "curve": curve}
    m["live_context"] = live_curves
    m["prune_events_total"] = len(prune_info)
    m["pruned_chars_total"] = sum(p["chars"] for p in prune_info)
    m["pruned_tokens_total"] = sum((p["tokens"] or 0) for p in prune_info)
    # 用平台自己的裁剪记账反推「本语料的字符/token 比」——自校准，不靠猜
    if m["pruned_tokens_total"] > 0 and m["pruned_chars_total"] > 0:
        m["empirical_chars_per_token"] = round(
            m["pruned_chars_total"] / m["pruned_tokens_total"], 3)
    else:
        m["empirical_chars_per_token"] = None
    # 上下文窗口上限（从平台声明里取，取不到就不猜）
    cw = next((e["meta"].get("contextWindow") for e in all_events
               if e["kind"] == "context_window" and (e["meta"] or {}).get("contextWindow")), None)
    m["context_window_tokens"] = cw
    root_lc = live_curves.get(root) or {}
    cpt = m["empirical_chars_per_token"]
    if root_lc and cpt:
        # 只有拿到实测比才折算 tokens；拿不到就只给字符，绝不拿猜的系数编数字
        root_lc["final_live_tokens_est"] = round(root_lc["final_live_chars"] / cpt)
        root_lc["peak_live_tokens_est"] = round(root_lc["peak_live_chars"] / cpt)
        if cw:
            root_lc["final_window_usage"] = round(root_lc["final_live_tokens_est"] / cw, 4)
            root_lc["peak_window_usage"] = round(root_lc["peak_live_tokens_est"] / cw, 4)
    m["root_live_context"] = root_lc
    m["turn_inflow_chars"] = sum(x["chars"] or 0 for x in ev_root)
    m["context_shrink_ratio"] = (
        round(root_lc["pruned_chars"] / max(root_lc["inflow_chars"], 1), 4) if root_lc else None)

    # 思考过程
    rs = [e for e in all_events if e["kind"] == "reasoning"]
    m["reasoning_blocks"] = len(rs)
    m["reasoning_chars"] = chars.get("reasoning", 0)
    m["reasoning_avg_chars"] = round(m["reasoning_chars"] / len(rs)) if rs else 0
    m["reasoning_longest"] = [{"node_id": e["node_id"], "t": e["t"], "chars": e["chars"],
                               "head": (e["text"] or "")[:160]}
                              for e in sorted(rs, key=lambda e: -(e["chars"] or 0))[:5]]

    # 思考过程
    rs = [e for e in all_events if e["kind"] == "reasoning"]
    m["reasoning_blocks"] = len(rs)
    m["reasoning_chars"] = chars.get("reasoning", 0)
    m["reasoning_avg_chars"] = round(m["reasoning_chars"] / len(rs)) if rs else 0

    # 工具失败 / 超时
    tres = [e for e in all_events if e["kind"] == "tool_result"]
    m["tool_results"] = len(tres)
    m["tool_errors"] = sum(1 for e in tres if (e["meta"] or {}).get("is_error"))
    m["tool_timeouts"] = sum(1 for e in tres if "timed out" in (e["text"] or "").lower())
    m["rate_limited"] = sum(1 for e in tres
                            if "访问限制" in (e["text"] or "") or "PI_AI_ERROR" in (e["text"] or ""))

    # 轮次
    m["turns_root"] = kinds.get("turn_start", 0) and sum(
        1 for e in ev_root if e["kind"] == "turn_start")
    m["nodes"] = len(node_by_id)
    m["subagents"] = len(node_by_id) - 1
    return m


# ────────────────────────────── 产物 ──────────────────────────────

def join_usage(nodes, outdir: Path, ledger: Path):
    """把平台用量账本按**时间窗**并入本次素材。

    诚实前提：账本事件**不带会话号**，只能按时间窗归因，而同机可能有并发会话。
    所以这里给的是「窗口内同机总量」，标注清楚，不假装是本次执行的独占花费。
    """
    if not ledger.exists():
        return None
    raw = ledger.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    data = json.loads(raw.decode("utf-8"))
    ev = data.get("events") or []
    t0 = min((n["t0"] for n in nodes if n["t0"]), default=None)
    t1 = max((n["last"] for n in nodes if n["last"]), default=None)
    inwin = [e for e in ev if t0 <= (e.get("ts") or 0) <= t1] if (t0 and t1) else []
    by_model = collections.Counter()
    cost_by_model = collections.Counter()
    for e in inwin:
        by_model[e.get("model") or "?"] += e.get("tokens") or 0
        cost_by_model[e.get("model") or "?"] += e.get("cost") or 0
    out = {
        "ledger_path": str(ledger), "ledger_sha256": sha,
        "window": {"start": iso(t0), "end": iso(t1), "minutes": round((t1 - t0) / 60000, 1) if (t0 and t1) else None},
        "caveat": "账本事件不带会话号；这是**同机在该时间窗内的总量**，可能含并发会话，"
                  "不可独占归因于本次执行。要精确归因须在受控窗口内单独跑。",
        "events_in_window": len(inwin),
        "tokens_in_window": sum(e.get("tokens") or 0 for e in inwin),
        "cost_in_window": round(sum(e.get("cost") or 0 for e in inwin), 6),
        "tokens_by_model": dict(by_model),
        "cost_by_model": {k: round(v, 6) for k, v in cost_by_model.items()},
        "ledger_total_events": len(ev),
    }
    if out["events_in_window"]:
        mins = out["window"]["minutes"] or 1
        out["tokens_per_minute"] = round(out["tokens_in_window"] / max(mins, 1))
        out["cost_per_hour"] = round(out["cost_in_window"] / max(mins / 60, 1e-9), 3)
    (outdir / "usage.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def write_trace(nodes, outdir: Path, ledger: Path = None):
    outdir.mkdir(parents=True, exist_ok=True)
    nodes = sorted(nodes, key=lambda n: (n["header"].get("delegationDepth") or 0, n["t0"] or 0))
    order = {n["id"]: i for i, n in enumerate(nodes)}
    all_events = [e for n in nodes for e in n["events"]]
    all_events.sort(key=lambda e: (order.get(e["node_id"], 10**6), e["t_ms"] or 0,
                                   e["seq"] or 0, e["kind"]))
    t0map = {n["id"]: n["t0"] for n in nodes}
    with open(outdir / "trace.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for e in all_events:
            rec = dict(e)
            t0 = t0map.get(e["node_id"])
            rec["elapsed_ms"] = (e["t_ms"] - t0) if (t0 and e["t_ms"]) else None
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

    node_by_id = {n["id"]: n for n in nodes}
    derived = derive(all_events, node_by_id, nodes)
    unknown = sorted({e["kind"] for e in all_events if e["kind"].startswith("unknown:")})
    man = {
        "extractor": EXTRACTOR, "extractor_version": VERSION,
        "trace_id": nodes[0]["id"] if nodes else None,
        "nodes": [{
            "id": n["id"], "path": n["path"], "sha256": n["sha256"],
            "parent": n["header"].get("parentSession"),
            "depth": n["header"].get("delegationDepth"),
            "cwd": n["header"].get("cwd"),
            "created_at": iso(n["t0"]), "last_at": iso(n["last"]),
            "wall_ms": (n["last"] - n["t0"]) if (n["last"] and n["t0"]) else None,
            "records": n["n_records"], "events": len(n["events"]),
            "children": sorted(set(n["children"])),
        } for n in nodes],
        "totals": {"events": len(all_events), "nodes": len(nodes),
                   "records": sum(n["n_records"] for n in nodes),
                   "chars": derived["total_chars"]},
        "metrics": derived,
        "unknown_record_kinds": unknown,
        "skipped": [],
    }
    (outdir / "manifest.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")

    # ── 人读摘要
    root = man["nodes"][0] if nodes else {}
    L = [f"# 执行素材摘要 · {man['trace_id']}\n"]
    L.append(f"- 墙钟 **{(root.get('wall_ms') or 0)/3600000:.2f} 小时**"
             f"（{root.get('created_at')} → {root.get('last_at')}）")
    L.append(f"- 节点 **{len(nodes)}**（1 主 + {len(nodes)-1} 子）"
             f"· 源记录 {man['totals']['records']} · 事件 {man['totals']['events']}")
    L.append(f"- 素材总字符 **{man['totals']['chars']:,}**")
    L.append(f"- 计时零点：**{derived.get('timing_origin')}**"
             f"（主会话＝真人开口；子会话＝任务书到达，因为没有'打字'的真人消息）")
    L.append(f"- 思考块 **{derived['reasoning_blocks']}** 段 / **{derived['reasoning_chars']:,}** 字符"
             f"（均值 {derived['reasoning_avg_chars']:,} 字/段）")
    L.append(f"- 真人高信号占素材 **{derived['human_share']*100:.2f}%**"
             f"（打字 {derived['human_chars']:,} ＋ 问答作答 {derived['human_answer_chars']:,} 字符；"
             f"信噪比 {derived['human_snr']}）")
    L.append(f"- 工具失败 {derived['tool_errors']} · 超时 {derived['tool_timeouts']}"
             f" · 限流 {derived['rate_limited']}\n")
    L.append("## 首次推进动作（本执行入口节点，距计时零点）\n")
    L.append("> 只报素材事实，不预判「什么算业务动作」——那由项目自己的链路定义决定。\n")
    L.append("| 里程碑 | 距开口 | 第几个工具调用 | 工具 | 动作摘要 |\n|---|---|---|---|---|")
    ms = derived.get("milestones") or {}
    fh = derived.get("first_reasoning")
    if fh:
        s = fh.get("since_human_ms")
        L.append(f"| 首个思考块 | {(s/60000 if s is not None else 0):.1f} 分 | — | (reasoning) | "
                 f"{fh['chars']:,} 字 |")
    order = [("first_tool_call", "首个工具调用"), ("first_artifact", "首个落盘产物"),
             ("first_subagent", "首次派子 agent"), ("first_ask_human", "首次向人提问"),
             ("first_http_or_probe", "首次开浏览器/抓网页")]
    for k, lab in order:
        v = derived.get(k) if k == "first_tool_call" else ms.get(k)
        if not v:
            L.append(f"| {lab} | — | — | — | 整个执行里没出现 |")
            continue
        s = v.get("since_human_ms")
        ss = f"{s/60000:.1f} 分" if s is not None else "—"
        L.append(f"| {lab} | {ss} | {v['index']} | `{v['tool']}` | "
                 f"{str(v.get('args') or '')[:88].replace('|','/')} |")
    sh = derived.get("root_shell_profile") or {}
    if sh.get("counts"):
        L.append(f"\n### 根会话 shell 动作画像（启发式，依据随判定留档）\n")
        for k, v in sh["counts"].items():
            L.append(f"- {k}：**{v}**")
        for u in sh.get("undecided_samples", []):
            L.append(f"  - 未判定样本 #{u['index']} {u['t']}：`{u['cmd'][:120]}`")
    L.append("")
    L.append("## 上下文构成（按字符，累计流入量）\n")
    L.append("| 成分 | 字符 | 占比 |\n|---|---|---|")
    tot = derived["total_chars"] or 1
    zero = []
    for k, v in derived["chars_by_kind"].items():
        if v == 0:
            zero.append(k)
            continue
        L.append(f"| `{k}` | {v:,} | {v*100/tot:.2f}% |")
    L.append(f"\n> 不承载文本的记账类事件（字符 0，仅计数）：{', '.join('`'+z+'`' for z in zero)}")

    L.append("\n## 回灌进上下文最多的 10 条工具结果\n")
    L.append("> ⚠ 超长结果会被平台**溢出到临时文件**，只回灌一段固定长度提示"
             "（实测约 49,933 字符）。本表量的是**回灌量**，不是被读内容的真实体量。\n")
    L.append("| 节点 | 时刻 | turn | 回灌字符 | 是否溢出提示 | 内容开头 |\n|---|---|---|---|---|---|")
    for b in derived.get("biggest_tool_results", []):
        L.append(f"| {b['node_id'][:8]} | {(b['t'] or '')[11:19]} | {b['turn']} | {b['chars']:,} | "
                 f"{'是' if b.get('is_spill_notice') else '否'} | "
                 f"{str(b['head']).replace(chr(10),' ').replace('|','/')[:60]} |")
    sp_n = derived.get("spilled_results") or 0
    if sp_n:
        trc = max(derived["chars_by_kind"].get("tool_result", 1), 1)
        spc = derived.get("spilled_chars", 0)
        L.append(f"\n- 溢出提示共 **{sp_n}** 条，合计回灌 **{spc:,}** 字符"
                 f"（占全部工具结果 {spc*100//trc}%）"
                 f" —— 这部分是**纯开销**：真内容没进上下文，只进了一句「去读那个文件」。\n")

    L.append("\n## 根会话逐轮流入量（上下文腐化曲线）\n")
    L.append("| turn | 流入字符 | 工具调用 |\n|---|---|---|")
    for r in derived.get("root_turn_inflow", []):
        L.append(f"| {r['turn']} | {r['chars']:,} | {r['tool_calls']} |")

    lc = derived.get("root_live_context") or {}
    if lc:
        cpt = derived.get("empirical_chars_per_token")
        cw = derived.get("context_window_tokens")
        L.append("\n## 根会话「窗口存活量」口径（累计流入 − 被平台裁掉的）\n")
        L.append(f"- 累计流入 **{lc.get('inflow_chars',0):,}** 字符")
        L.append(f"- 被平台裁掉 **{lc.get('pruned_chars',0):,}** 字符"
                 f"（{lc.get('prune_events',0)} 次 compaction/prune，"
                 f"收缩率 **{(derived.get('context_shrink_ratio') or 0)*100:.1f}%**）")
        L.append(f"- **峰值存活 {lc.get('peak_live_chars',0):,}** 字符；"
                 f"结束时存活 **{lc.get('final_live_chars',0):,}** 字符")
        if cpt:
            L.append(f"- 实测字符/token 比 **{cpt}**（由平台裁剪记账反推，本语料自校准）"
                     f"⇒ 峰值约 **{lc.get('peak_live_tokens_est',0):,}** tokens，"
                     f"结束时约 **{lc.get('final_live_tokens_est',0):,}** tokens")
        else:
            L.append("- **本会话没有裁剪记账，字符/token 比无法自校准** ⇒ 不折算 tokens、不算窗口占用率"
                     "（宁缺不编；要 token 口径请看 usage.json 的平台真实账）")
        if cw and lc.get("peak_window_usage") is not None:
            L.append(f"- 上下文窗口 **{cw:,}** tokens ⇒ "
                     f"峰值占用 **{(lc.get('peak_window_usage') or 0)*100:.1f}%**，"
                     f"结束占用 **{(lc.get('final_window_usage') or 0)*100:.1f}%**")
        L.append("\n> 「累计流入量」看的是吞吐，「窗口存活量」看的才是模型每一步真正同时看见多少——"
                 "后者才是上下文腐化的口径。\n")
        L.append("| 裁剪时刻 | turn | 裁掉字符 | 裁掉 tokens | 裁剪后存活 |\n|---|---|---|---|---|")
        for c in lc.get("curve", [])[:40]:
            L.append(f"| {(c['t'] or '')[11:19]} | {c['turn']} | {c['pruned_now']:,} | "
                     f"{c.get('pruned_tokens')} | {c['live']:,} |")

    L.append("\n## 最长的 5 段思考\n")
    for r in derived.get("reasoning_longest", []):
        L.append(f"- {r['node_id'][:8]} {r['t']} **{r['chars']:,} 字** — "
                 f"{str(r['head']).replace(chr(10),' ')[:110]}")
    L.append("\n## 子 agent（按开工时间）\n")
    L.append("| 节点 | 父 | 开工 | 墙钟(分) | turn | 工具调用 | 累计字符 | 思考段 | 落盘 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for n in man["nodes"][1:]:
        ec = [e for e in all_events if e["node_id"] == n["id"]]
        tc = sum(1 for e in ec if e["kind"] == "tool_call")
        tn = sum(1 for e in ec if e["kind"] == "turn_start")
        ch = sum(e["chars"] or 0 for e in ec)
        rs = sum(1 for e in ec if e["kind"] == "reasoning")
        wr = sum(1 for e in ec if e["kind"] == "tool_call"
                 and (e["meta"] or {}).get("tool") in ("write", "edit"))
        par = str(n.get("parent") or "")
        par = par[8:16] if par.startswith("session-") else (par[:8] or "—")
        L.append(f"| {n['id'][:8]} | {par} | {(n.get('created_at') or '')[5:16]} | "
                 f"{(n.get('wall_ms') or 0)/60000:.0f} | {tn} | {tc} | {ch:,} | {rs} | {wr} |")
    L.append("\n## 各节点累计流入量（≈该会话最终上下文体积的上界）\n")
    L.append("| 节点 | 深度 | 累计字符 | 占全部素材 |\n|---|---|---|---|")
    gtot = sum(sum(e["chars"] or 0 for e in all_events if e["node_id"] == n["id"])
               for n in man["nodes"]) or 1
    for n in man["nodes"]:
        ch = sum(e["chars"] or 0 for e in all_events if e["node_id"] == n["id"])
        L.append(f"| {n['id'][:8] if n['id'] != man['trace_id'] else '根 ' + n['id'][8:16]} "
                 f"| {n.get('depth')} | {ch:,} | {ch*100/gtot:.2f}% |")
    if unknown:
        L.append(f"\n> ⚠ 未识别的记录类型（已落 `unknown:` 事件，未丢弃）：{', '.join(unknown)}")
    # 用量账本
    if ledger:
        u = join_usage(nodes, outdir, Path(ledger))
        if u:
            L.append("\n## 平台用量账本（同机同窗口）\n")
            L.append(f"- 窗口 {u['window']['start']} → {u['window']['end']}"
                     f"（{u['window']['minutes']} 分钟）")
            L.append(f"- 窗口内 **{u['events_in_window']}** 笔计费 · "
                     f"**{u['tokens_in_window']:,}** tokens · **¥{u['cost_in_window']}**")
            if u.get("tokens_per_minute"):
                L.append(f"- 折合 **{u['tokens_per_minute']:,} tokens/分钟** · "
                         f"**¥{u['cost_per_hour']}/小时**")
            L.append(f"\n> ⚠ {u['caveat']}\n")
            for k, v in u["tokens_by_model"].items():
                L.append(f"- `{k}`：{v:,} tokens · ¥{u['cost_by_model'].get(k)}")
    (outdir / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    return man


def build_call_tool_map(recs):
    """call_id → 工具名。真人的问答作答（ask_user_question）要靠它才认得出来。"""
    m = {}
    for o in recs:
        t = o.get("type")
        dd = o.get("data") or {}
        if t == "tool/call":
            cid = dd.get("callId")
            if cid:
                m[cid] = dd.get("name")
        elif t == "assistant/message":
            for b in blocks_of(o):
                if isinstance(b, dict) and b.get("type") == "tool-call" and b.get("id"):
                    m[b["id"]] = b.get("name")
    return m


def verify(nodes, outdir: Path):
    """对账自检。

    注意：DSH 会话文件是**边写边追加**的（实测：抽取期间源文件又长出一个子 agent）。
    因此对账前先比 sha256——源变了就明确报「源已变化」，而不是给出一个看似通过的对账。
    """
    problems = []
    warnings = []
    for n in nodes:
        cur = hashlib.sha256(Path(n["path"]).read_bytes()).hexdigest()
        if cur != n["sha256"]:
            warnings.append(
                f"{n['id'][:8]} 源文件在本次抽取之后被追加过"
                f"（抽取时 sha={n['sha256'][:12]}，现在 {cur[:12]}）"
                f"⇒ 本次 trace.jsonl 是当时那份快照，别拿它跟现在的源逐字比")
    for n in nodes:
        recs, sha = read_session(Path(n["path"]))
        is_root = (n["header"].get("delegationDepth") or 0) == 0
        seen = set()
        ctm = build_call_tool_map(recs)
        for i, o in enumerate(recs):
            ev = normalize(o, n["id"], is_root, seen, ctm)
            if not ev:
                problems.append(f"{n['id'][:8]} 第 {i} 条源记录未产出事件: {o.get('type')}")
    man = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))
    mc = man["metrics"]["chars_by_kind"]
    # 字符对账：只对"源未被追加过"的节点严格核对；追加过的只做包含性检查
    for n in nodes:
        appended = any(w.startswith(n["id"][:8]) for w in warnings)
        recs, _ = read_session(Path(n["path"]))
        is_root = (n["header"].get("delegationDepth") or 0) == 0
        seen = set()
        ctm = build_call_tool_map(recs)
        src = collections.Counter()
        for o in recs:
            for e in normalize(o, n["id"], is_root, seen, ctm):
                src[e["kind"]] += e["chars"] or 0
        own = collections.Counter()
        for e in n["events"]:
            own[e["kind"]] += e["chars"] or 0
        for k in set(src) | set(own):
            if appended:
                if own[k] > src[k]:
                    problems.append(
                        f"字符对账不符(节点已追加过，但快照多于现源) kind={k}: 快照={own[k]} 现源={src[k]}")
            elif src[k] != own[k]:
                problems.append(f"字符对账不符 kind={k}: 快照={own[k]} 源={src[k]}")
    # 全量对账（未追加时）
    if not warnings:
        tally = collections.Counter()
        for n in nodes:
            for e in n["events"]:
                tally[e["kind"]] += e["chars"] or 0
        for k, v in tally.items():
            if mc.get(k, 0) != v:
                problems.append(f"全量字符对账不符 kind={k}: manifest={mc.get(k,0)} 快照={v}")
    n_lines = sum(1 for _ in open(outdir / "trace.jsonl", encoding="utf-8"))
    if n_lines != man["totals"]["events"]:
        problems.append(f"事件数不符: trace.jsonl={n_lines} manifest={man['totals']['events']}")
    jl = outdir / "trace.jsonl"
    h1 = hashlib.sha256(jl.read_bytes()).hexdigest()
    write_trace(nodes, outdir)
    h2 = hashlib.sha256(jl.read_bytes()).hexdigest()
    if h1 != h2:
        problems.append("幂等失败：二次抽取 trace.jsonl 不一致")
    return problems, warnings


def main():
    # Windows 控制台默认 GBK，会炸在非 GBK 字符上；自己把输出改成 utf-8，别让用户踩这个坑
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="DSH 会话『素材本体』抽取器")
    ap.add_argument("--session", action="append", default=[])
    ap.add_argument("--path", action="append", default=[])
    ap.add_argument("--out")
    ap.add_argument("--sessions-root", default=str(DEFAULT_SESSIONS_ROOT))
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--usage-ledger", nargs="?",
                    const=str(Path.home() / ".dsh" / ".dshw-usage.json"),
                    help="并入平台用量账本（默认 %(const)s）；按时间窗归因，标注不可独占")
    args = ap.parse_args()
    sroot = Path(args.sessions_root)

    if args.list:
        for sid, fp in list_sessions(sroot)[:80]:
            print(f"{datetime.fromtimestamp(fp.stat().st_mtime, TZ):%m-%d %H:%M}  {sid}")
        return
    if not args.out:
        raise SystemExit("必须给 --out <目录>")

    roots = list(args.session)
    all_nodes = []
    for r in roots:
        all_nodes.extend(collect_tree(r, sroot))
    for p in args.path:
        all_nodes.extend(collect_from_path(Path(p), sroot))
    seen, nodes = set(), []
    for n in all_nodes:
        if n["id"] in seen:
            continue
        seen.add(n["id"])
        nodes.append(n)

    outdir = Path(args.out)
    ledger = Path(args.usage_ledger) if args.usage_ledger else None
    man = write_trace(nodes, outdir, ledger)
    mt = man["metrics"]
    print(f"→ {outdir}  (trace.jsonl / manifest.json / report.md"
          + (" / usage.json" if ledger else "") + ")")
    print(f"节点 {man['totals']['nodes']} · 事件 {man['totals']['events']} · 字符 {man['totals']['chars']:,}")
    ms = mt.get("milestones") or {}

    def fm(v):
        if not v:
            return "未出现"
        s = v.get("since_human_ms")
        return f"{s/60000:.1f}分" if s is not None else "?"
    print(f"首个工具调用 {fm(mt.get('first_tool_call'))} · 首个落盘产物 {fm(ms.get('first_artifact'))}"
          f" · 首次派子agent {fm(ms.get('first_subagent'))}"
          f" · 思考块 {mt['reasoning_blocks']} 段 · 真人占比 {mt['human_share']*100:.2f}%")
    if ledger and (outdir / "usage.json").exists():
        u = json.loads((outdir / "usage.json").read_text(encoding="utf-8"))
        print(f"用量账本（同机同窗口，不可独占归因）：{u['events_in_window']} 笔 · "
              f"{u['tokens_in_window']:,} tokens · ¥{u['cost_in_window']}")

    if args.verify:
        problems, warnings = verify(nodes, outdir)
        for w in warnings:
            print("  ! ", w)
        if problems:
            print("\n自检未通过：")
            for p in problems[:40]:
                print("  ✗", p)
            sys.exit(1)
        print("自检通过：源记录归属无遗漏 · 字符对账一致 · 二次抽取幂等")


if __name__ == "__main__":
    main()
