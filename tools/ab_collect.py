#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ab_collect —— 批量对照实验：收数并出对照表。

输入：实验目录（ab_plan.py 产出）＋ 派发那次会话的 id（dispatcher）。
做法：
  1. 从 dispatcher 会话里读 `subagent/catalog` 事件，拿到 (child_id, label)
  2. 用 label 里的 `exp|变体|第几次` 把子会话认领到配方上（认不上的一律报警，不硬塞）
  3. 对每个子会话跑 dsh_trace，抽成素材
  4. 出对照表：完成时间 / 工具调用 / 思考体量 / 落地动作 / 判据（人工填）
     ＋ 每个变体的均值与离散度

用法：
  python ab_collect.py --exp experiments/exp-001 --dispatcher session-xxxx
  python ab_collect.py --exp experiments/exp-001 --dispatcher session-xxxx --judge verdicts.json
"""
from __future__ import annotations

import argparse
import io
import json
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import zstandard

HERE = Path(__file__).resolve().parent
EXTRACTOR = HERE / "dsh_trace.py"


def read_jsonl_zstd(p: Path):
    d = zstandard.ZstdDecompressor()
    raw = p.read_bytes()
    with d.stream_reader(io.BytesIO(raw)) as r:
        data = r.read()
    out = []
    for line in data.decode("utf-8", "replace").split("\n"):
        s = line.strip()
        if s:
            try:
                out.append(json.loads(s))
            except Exception:
                pass
    return out


def find_session_file(sroot: Path, sid: str):
    hits = list(sroot.glob(f"*/{sid}/session.v4.jsonl.zstd"))
    return hits[0] if hits else None


def spawn_map(sroot: Path, dispatcher: str):
    """读派发会话，返回 (spawns, settled_ids)。
    spawns = [(child_id, label)]；settled_ids = 已收到 subagent-settled 通知的子会话。
    ⚠ 只有 settled 的子会话才适合纳入对照——未 settled 的是**半成品**，
      量它的耗时/工具数会得出错误的配方优劣结论。
    """
    fp = find_session_file(sroot, dispatcher)
    if not fp:
        raise SystemExit(f"找不到派发会话 {dispatcher}（在 {sroot}）")
    out, settled = [], set()
    for o in read_jsonl_zstd(fp):
        t = o.get("type")
        dd = o.get("data") or {}
        if t == "subagent/catalog" and dd.get("childId"):
            out.append((dd["childId"], dd.get("label") or ""))
        elif t == "user/message":
            src = dd.get("source") or {}
            if src.get("kind") == "subagent-settled" and src.get("senderSessionId"):
                settled.add(src["senderSessionId"])
    return out, settled


def extract(sid: str, outdir: Path, sroot: Path, ledger: str):
    p = subprocess.run(
        [sys.executable, str(EXTRACTOR), "--session", sid, "--out", str(outdir),
         "--sessions-root", str(sroot), "--verify", "--usage-ledger", ledger],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="对照实验收数")
    ap.add_argument("--exp", required=True)
    ap.add_argument("--dispatcher", required=True)
    ap.add_argument("--sessions-root", default=str(Path.home() / ".dsh" / "sessions"))
    ap.add_argument("--usage-ledger", default=str(Path.home() / ".dsh" / ".dshw-usage.json"))
    ap.add_argument("--judge", help="人工判据结果 JSON：{\"<key>\": {\"pass\": true, \"note\": \"...\"}}")
    args = ap.parse_args()

    expdir = Path(args.exp)
    plan = json.loads((expdir / "plan.json").read_text(encoding="utf-8"))
    sroot = Path(args.sessions_root)
    expid = plan["experiment"]

    spawns, settled = spawn_map(sroot, args.dispatcher)
    # 认领：label 形如 "<expid>|<变体>|r<次>"
    claimed, unmatched, missing = {}, [], []
    want = {k: v for k, v in plan["runs"].items()}
    for cid, label in spawns:
        hit = None
        for k, v in want.items():
            if v["dispatch_label"] == label:
                hit = k
                break
        if hit:
            claimed.setdefault(hit, []).append(cid)
        elif label and label.startswith(expid):
            unmatched.append((cid, label))
        # 与本次实验无关的子会话：忽略（正常，派发会话里常有别的活）
    for k in want:
        if k not in claimed:
            missing.append(k)

    unsettled = sorted({c for cs in claimed.values() for c in cs if c not in settled})
    if unsettled:
        print(f"⚠ 有 {len(unsettled)} 个子会话**尚未结束**（没收到 subagent-settled）：")
        for c in unsettled[:10]:
            print(f"    {c}")
        print("  它们的耗时/工具数是**半成品读数**，纳入对照会得出错误的配方优劣。")
        print("  建议等它们结束再收数；若确要收，请只看逐次明细、不要看聚合结论。\n")

    traces = expdir / "traces"
    traces.mkdir(parents=True, exist_ok=True)
    rows = []
    for k, cids in sorted(claimed.items()):
        for i, cid in enumerate(cids):
            key = k if i == 0 else f"{k}#{i+1}"
            od = traces / key.replace("#", "_")
            rc, out = extract(cid, od, sroot, args.usage_ledger)
            man_p = od / "manifest.json"
            if rc != 0 or not man_p.exists():
                rows.append({"key": key, "variant": want[k]["variant"], "session": cid,
                             "error": out[-400:]})
                continue
            man = json.loads(man_p.read_text(encoding="utf-8"))
            mt = man["metrics"]
            u = {}
            up = od / "usage.json"
            if up.exists():
                u = json.loads(up.read_text(encoding="utf-8"))
            ms = mt.get("milestones") or {}
            root = man["nodes"][0]
            rows.append({
                "key": key, "variant": want[k]["variant"], "run": want[k]["run"],
                "session": cid,
                "wall_min": round((root.get("wall_ms") or 0) / 60000, 1),
                "tool_calls": mt.get("root_tool_calls"),
                "reasoning_blocks": mt.get("reasoning_blocks"),
                "reasoning_chars": mt.get("reasoning_chars"),
                "reasoning_avg": mt.get("reasoning_avg_chars"),
                "subagents": mt.get("subagents"),
                "artifacts": sum(1 for n in man["nodes"] for e in []
                                 ) or None,
                "first_artifact_min": (round((ms.get("first_artifact") or {}).get("since_human_ms", 0) / 60000, 1)
                                       if ms.get("first_artifact") else None),
                "first_artifact_index": (ms.get("first_artifact") or {}).get("index"),
                "tool_errors": mt.get("tool_errors"),
                "tool_timeouts": mt.get("tool_timeouts"),
                "human_share": mt.get("human_share"),
                "chars": man["totals"]["chars"],
                "tokens_in_window": u.get("tokens_in_window"),
                "cost_in_window": u.get("cost_in_window"),
                "trace_dir": str(od),
            })

    # 判据（人工填，可后补）
    verdicts = {}
    if args.judge and Path(args.judge).exists():
        verdicts = json.loads(Path(args.judge).read_text(encoding="utf-8"))
    for r in rows:
        v = verdicts.get(r["key"]) or {}
        r["pass"] = v.get("pass")
        r["verdict_note"] = v.get("note")

    # 按变体聚合
    agg = defaultdict(list)
    for r in rows:
        if "error" not in r:
            agg[r["variant"]].append(r)

    def stat(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        return {"n": len(vals), "mean": round(statistics.mean(vals), 1),
                "min": min(vals), "max": max(vals),
                "stdev": round(statistics.pstdev(vals), 1) if len(vals) > 1 else 0.0}

    summary = {}
    for var, rs in agg.items():
        summary[var] = {
            "n": len(rs),
            "wall_min": stat([r["wall_min"] for r in rs]),
            "tool_calls": stat([r["tool_calls"] for r in rs]),
            "reasoning_chars": stat([r["reasoning_chars"] for r in rs]),
            "first_artifact_min": stat([r["first_artifact_min"] for r in rs]),
            "cost_in_window": stat([r["cost_in_window"] for r in rs]),
            "pass_rate": (round(sum(1 for r in rs if r.get("pass")) / len(rs), 2)
                          if any(r.get("pass") is not None for r in rs) else None),
        }

    results = {"experiment": expid, "dispatcher": args.dispatcher,
               "rows": rows, "summary": summary,
               "unmatched_labels": [{"session": c, "label": l} for c, l in unmatched],
               "missing_runs": missing,
               "unsettled_sessions": unsettled,
               "settled_ok": not unsettled}
    (expdir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                        encoding="utf-8")

    # ── 人读对照表
    L = [f"# 对照实验结果 · {expid}\n"]
    L.append(f"- 场景：{plan.get('scenario')}")
    L.append(f"- 窗口起点：{plan.get('started_at')}")
    L.append(f"- 派发会话：`{args.dispatcher}`；认领 {len(claimed)}/{len(want)} 份")
    if unsettled:
        L.append(f"- 🔴 **有 {len(unsettled)} 份尚未结束**（半成品读数）⇒ 下面的聚合结论**不可用**，"
                 f"只能看逐次明细。等结束后重新收数。")
    if missing:
        L.append(f"- ⚠ **未认领到的**（可能还没派/已失败）：{', '.join(missing)}")
    if unmatched:
        L.append(f"- ⚠ 标签像本次实验但对不上配方的：{unmatched}")
    L.append("\n## 逐次明细\n")
    L.append("> ⚠ 「同机窗口tokens」是**该子会话时间窗内、同一台机器上的全部计费量**，"
             "四次对照并发跑时会互相叠加，**不能当单次成本对比**；单次成本请用 `思考字符`＋`工具调用` 对比。\n")
    L.append("| key | 配方 | 墙钟(分) | 工具调用 | 思考字符 | 首个产物(分) | 子agent | 报错 | 同机窗口tokens | 判据 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if "error" in r:
            L.append(f"| {r['key']} | {r['variant']} | — | — | — | — | — | {r['error'][:60]} | | ✗ |")
            continue
        L.append(f"| {r['key']} | {r['variant']} | {r['wall_min']} | {r['tool_calls']} | "
                 f"{r['reasoning_chars']:,} | {r['first_artifact_min']} | {r['subagents']} | "
                 f"{r['tool_errors']} | {r['tokens_in_window']:,} | "
                 f"{'✓' if r.get('pass') else ('✗' if r.get('pass') is False else '待判')} |")
    L.append("\n## 按配方聚合（均值 / 区间）\n")
    if unsettled:
        L.append("> 🔴 本表含未结束的读数，**不要据此下结论**。\n")
    L.append("| 配方 | n | 墙钟(分) | 工具调用 | 思考字符 | 首个产物(分) | 通过率 |")
    L.append("|---|---|---|---|---|---|---|")
    for var, s in sorted(summary.items()):
        def f(x):
            if not x:
                return "—"
            return f"{x['mean']} ({x['min']}~{x['max']})"
        L.append(f"| {var} | {s['n']} | {f(s['wall_min'])} | {f(s['tool_calls'])} | "
                 f"{f(s['reasoning_chars'])} | {f(s['first_artifact_min'])} | "
                 f"{s['pass_rate'] if s['pass_rate'] is not None else '待判'} |")
    L.append("\n> 读法：**只看同一实验内的配方间差异**。跨实验比绝对值没有意义（环境/时段/任务不同）。")
    L.append("> 判据若为「待判」，需人工按实验定义的 verdict 逐条判，再写进 --judge 的 JSON 复跑本工具。")
    (expdir / "results.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    print("\n".join(L))
    print(f"\n→ {expdir / 'results.json'} / results.md / traces/")


if __name__ == "__main__":
    main()
