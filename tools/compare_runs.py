#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""compare_runs.py —— 同一道题、两次独立执行的过程对比。

用途：单次读数分不清"结构性缺陷"与"随机波动"。跑第二次、并排比，
才能判断诊断是否成立。

用法：
  python compare_runs.py --a <trace目录> --b <trace目录> [--label-a 名] [--label-b 名]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def load(d: Path):
    f = d / "trace.jsonl" if d.is_dir() else d
    ev = [json.loads(l) for l in open(f, encoding="utf-8") if l.strip()]
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8")) if (d / "manifest.json").exists() else {}
    return ev, man


def summarize(ev):
    """把一次执行压成"它干了什么"的特征向量。"""
    calls = [e for e in ev if e["kind"] == "tool_call"]
    t0 = min(e["t_ms"] for e in ev if e["t_ms"])
    t_end = max(e["t_ms"] for e in ev if e["t_ms"])
    r = {}
    r["总秒"] = round((t_end - t0) / 1000, 1)
    r["工具调用"] = len(calls)
    r["思考块"] = sum(1 for e in ev if e["kind"] == "reasoning")

    # 特征：是否踩了那几个"弯路站"
    marks = {
        "读了 AGENTS": 0, "列了仓根/需求目录": 0, "读了 INDEX 台账": 0,
        "读了 manifest": 0, "用上查询命令": 0, "去找已删除的工具": 0,
        "git 核对": 0, "读产物报告": 0, "列需求目录树": 0,
    }
    first_stat = None
    first_query = None
    for i, c in enumerate(calls, 1):
        tool = (c.get("meta") or {}).get("tool") or ""
        txt = c.get("text") or ""
        low = txt.lower()
        if tool == "read" and "agents.md" in low:
            marks["读了 AGENTS"] = marks["读了 AGENTS"] or i
        if tool == "read" and "index.md" in low and "demands" in low:
            marks["读了 INDEX 台账"] = marks["读了 INDEX 台账"] or i
        if tool == "read" and "manifest" in low:
            marks["读了 manifest"] = marks["读了 manifest"] or i
        if tool == "glob" and "index.md" in low:
            marks["读了 INDEX 台账"] = marks["读了 INDEX 台账"] or i
        if tool == "pwsh":
            if any(k in low for k in ("get-childitem", "list ", "dir ")) and "demand" in low:
                marks["列了仓根/需求目录"] = marks["列了仓根/需求目录"] or i
            if "s4_case_progress" in low or "s4 tool" in low or "s4-progress" in low:
                marks["去找已删除的工具"] = marks["去找已删除的工具"] or i
            if "git " in low:
                marks["git 核对"] = marks["git 核对"] or i
            if "entity.py" in low and ("stat" in low or " ls " in low):
                marks["用上查询命令"] = marks["用上查询命令"] or i
                if first_query is None:
                    first_query = (i, (c["t_ms"] - t0) / 1000)
        if tool == "grep" and "d-2026" in low:
            marks["列需求目录树"] = marks["列需求目录树"] or i
    r["marks"] = marks
    r["首次用上查询命令"] = first_query
    # 首个"推进动作"（用上查询命令）距开工
    r["开工税秒"] = round(first_query[1], 1) if first_query else None
    r["字符"] = sum(e["chars"] or 0 for e in ev)
    return r


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--label-a", default="第一次")
    ap.add_argument("--label-b", default="复测")
    args = ap.parse_args()

    ea, ma = load(Path(args.a))
    eb, mb = load(Path(args.b))
    ra, rb = summarize(ea), summarize(eb)

    print(f"\n# 过程对比（同一道题，两次独立执行）\n")
    print(f"| 特征 | {args.label_a} | {args.label_b} |")
    print("|---|---|---|")
    for k in ["总秒", "工具调用", "思考块", "字符", "开工税秒"]:
        av, bv = ra[k], rb[k]
        av = "—" if av is None else f"{av:,}"
        bv = "—" if bv is None else f"{bv:,}"
        print(f"| {k} | {av} | {bv} |")
    print("\n## 弯路站（值是第几次调用踩到；—＝没踩）\n")
    print(f"| 站点 | {args.label_a} | {args.label_b} |")
    print("|---|---|---|")
    for k in ra["marks"]:
        a = ra["marks"][k] or "—"
        b = rb["marks"][k] or "—"
        flag = " ⚠复现" if (ra["marks"][k] and rb["marks"][k]) else (" 仅一次" if (ra["marks"][k] or rb["marks"][k]) else "")
        print(f"| {k} | {a} | {b} |{flag}")

    both = [k for k in ra["marks"] if ra["marks"][k] and rb["marks"][k]]
    one = [k for k in ra["marks"] if bool(ra["marks"][k]) != bool(rb["marks"][k])]
    print(f"\n- **两次都踩**（结构性证据）：{len(both)} 项 —— {', '.join(both) if both else '无'}")
    print(f"- **只踩一次**（可能随机）：{len(one)} 项 —— {', '.join(one) if one else '无'}")
    print(f"\n> 读法：**两次都踩的站点＝结构性缺陷**（改仓能治）；只踩一次的，先别写进结论。")
    print(f"> 单次读数分不清结构与波动 —— 这就是要复测的理由。")


if __name__ == "__main__":
    main()
