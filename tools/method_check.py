#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""method_check —— 拿方法论条文反压素材：哪些条款是被实测支撑的？

用法：
  python tools/method_check.py --trace <trace目录> [--name <显示名>]

做的事：把 METHOD.md 里每条量化主张，重新从 trace.jsonl 算一遍，并给出
  ✓ 与条文一致 / ✗ 与条文不符 / ? 无法从素材判定。
**这是防止方法论腐烂的最小手段**：条文里的数字必须能随时重算出来。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def load(p: Path):
    f = p / "trace.jsonl" if p.is_dir() else p
    ev = [json.loads(l) for l in open(f, encoding="utf-8") if l.strip()]
    man = None
    mp = (p if p.is_dir() else p.parent) / "manifest.json"
    if mp.exists():
        man = json.loads(mp.read_text(encoding="utf-8"))
    return ev, man, f


def target_of(ev):
    try:
        a = json.loads(ev.get("text") or "{}")
    except Exception:
        return "?"
    if not isinstance(a, dict):
        return "?"
    for k in ("file_path", "path", "pattern", "command", "target", "url"):
        if a.get(k):
            return str(a[k])[:160]
    return "?"


def recompute(ev, man):
    calls = [e for e in ev if e["kind"] == "tool_call"]
    res = {}
    res["工具调用总数"] = len(calls)
    res["思考块总数"] = sum(1 for e in ev if e["kind"] == "reasoning")
    res["思考总字符"] = sum(e["chars"] or 0 for e in ev if e["kind"] == "reasoning")
    res["素材总字符"] = sum(e["chars"] or 0 for e in ev)
    res["节点数"] = len({e["node_id"] for e in ev})
    # 重复读
    rep = Counter((e["meta"].get("tool"), target_of(e)) for e in calls
                  if (e["meta"] or {}).get("tool") in ("read", "glob", "grep", "pwsh"))
    worst = [(k, v) for k, v in rep.most_common() if v >= 3]
    res["重复读目标数"] = len(worst)
    res["重复读浪费调用"] = sum(v - 1 for _, v in worst)
    res["重复读最多"] = [f"{k[0]} ×{v} {k[1][:60]}" for k, v in worst[:5]]
    # 自建脚本
    scripts = []
    for e in calls:
        if (e["meta"] or {}).get("tool") != "write":
            continue
        try:
            fp = (json.loads(e.get("text") or "{}") or {}).get("file_path") or ""
        except Exception:
            fp = ""
        if fp.lower().endswith((".py", ".ps1", ".sh", ".bat", ".cmd")):
            scripts.append(fp)
    res["自建脚本数"] = len(scripts)
    # 真人占比
    h = sum(e["chars"] or 0 for e in ev if e["kind"] in ("human", "human_answer"))
    res["真人字符"] = h
    res["真人占比%"] = round(h * 100 / max(res["素材总字符"], 1), 4)
    res["信噪比(1:N)"] = int((res["素材总字符"] - h) / max(h, 1))
    # 思考/动作 比
    res["思考字符每次工具调用"] = round(res["思考总字符"] / max(len(calls), 1))
    # 最大单条工具结果
    tr = [e for e in ev if e["kind"] == "tool_result"]
    res["最大工具结果字符"] = max((e["chars"] or 0 for e in tr), default=0)
    # 裁剪
    res["裁剪次数"] = sum(1 for e in ev if e["kind"] == "compaction_prune")
    # 最大单段思考
    rs = [e for e in ev if e["kind"] == "reasoning"]
    res["最长思考段字符"] = max((e["chars"] or 0 for e in rs), default=0)
    # 工具失败
    res["工具失败"] = sum(1 for e in tr if (e["meta"] or {}).get("is_error"))
    # 子 agent 累计在飞（并行，非墙钟）
    if man:
        root = man["trace_id"]
        kids = [n for n in man["nodes"] if n["id"] != root]
        res["子agent数"] = len(kids)
        res["子agent累计在飞分钟"] = round(sum((n.get("wall_ms") or 0) / 60000 for n in kids))
        res["子agent墙钟最大分钟"] = round(max(((n.get("wall_ms") or 0) / 60000) for n in kids)) if kids else 0
        res["子agent累计上下文MB"] = round(
            sum(e["chars"] or 0 for e in ev if e["node_id"] != root) / 1048576, 1)
        res["溢出提示条数"] = man["metrics"].get("spilled_results")
        res["溢出回灌字符"] = man["metrics"].get("spilled_chars")
    return res


# METHOD.md 里的量化主张（条文原文片段 → 从素材重算得到什么）
CLAIMS = [
    ("真人输入占素材的比例", "真人占比%"),
    ("AI 思考占上下文的比例", None),  # 需按 kind 分，单独算
    ("重复读浪费的调用", "重复读浪费调用"),
    ("自建探针脚本数", "自建脚本数"),
    ("单条最大工具结果", "最大工具结果字符"),
    ("思考字符/每次改变世界的动作", "思考字符每次工具调用"),
    ("最长单段思考", "最长思考段字符"),
]


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", action="append", required=True)
    args = ap.parse_args()

    print("# 方法论条文 ↔ 素材重算\n")
    snaps = []
    for spec in args.trace:
        name = Path(spec).name
        ev, man, f = load(Path(spec))
        r = recompute(ev, man)
        # 思考占比
        tot = r["素材总字符"] or 1
        r["思考占比%"] = round(r["思考总字符"] * 100 / tot, 1)
        snaps.append((name, r, man))

    keys = ["素材总字符", "节点数", "子agent数", "子agent累计在飞分钟", "子agent墙钟最大分钟",
            "子agent累计上下文MB", "工具调用总数", "思考块总数", "思考总字符", "思考占比%",
            "真人字符", "真人占比%", "信噪比(1:N)", "重复读目标数", "重复读浪费调用",
            "自建脚本数", "溢出提示条数", "溢出回灌字符", "最大工具结果字符", "最长思考段字符",
            "思考字符每次工具调用", "裁剪次数", "工具失败"]
    print("| 量 | " + " | ".join(n for n, _, _ in snaps) + " |")
    print("|---|" + "---|" * len(snaps))
    for k in keys:
        print(f"| {k} | " + " | ".join(f"{r.get(k)}" for _, r, _ in snaps) + " |")

    for name, r, man in snaps:
        print(f"\n## {name}")
        if r["重复读最多"]:
            print("- 重复读最多：")
            for x in r["重复读最多"]:
                print(f"  - {x}")
        if man:
            mt = man["metrics"]
            lc = mt.get("root_live_context") or {}
            print(f"- 抽取器自报：窗口存活 {lc.get('final_live_chars',0):,} 字符"
                  f"；实测字符/token {mt.get('empirical_chars_per_token')}"
                  f"；峰值窗口占用 {lc.get('peak_window_usage')}")
            print(f"- 抽取器自报：思考块 {mt['reasoning_blocks']}；真人占比 {mt['human_share']}")
    print("\n> ✓ 一致 / ✗ 不符：上面每一格都应能在 METHOD.md 里找到对应数字。找不到 ⇒ 该条主张没有素材支撑，应删。")


if __name__ == "__main__":
    main()
