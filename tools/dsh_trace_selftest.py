#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dsh_trace 稳定性自检 —— 把「稳定」变成可复跑的主张。

跑五类用例，每类给出通过/失败：
  T1 正常主会话     抽取成功、自检通过、产物三件齐
  T2 以子 agent 为根 抽取成功（不假设根一定是 depth 0）
  T3 重复抽取        trace.jsonl 字节一致（幂等）
  T4 不存在的会话     明确报错、非零退出、不产生半个产物目录
  T5 空/损坏源       明确报错，不静默产出空素材

用法：
  python dsh_trace_selftest.py [--sessions-root <dir>] [--session <id>] [--out <临时目录>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXTRACTOR = HERE / "dsh_trace.py"


def run(args):
    p = subprocess.run([sys.executable, str(EXTRACTOR)] + args,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def sha(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def pick_session(sroot: Path, want_root=True):
    """找一个会话：want_root=True 取 delegationDepth==0 且有子 agent 的。"""
    import io
    import json as _j
    import zstandard
    d = zstandard.ZstdDecompressor()
    best = None
    for fp in sorted(sroot.glob("*/session.v4.jsonl.zstd"),
                     key=lambda p: p.stat().st_mtime, reverse=True):
        raw = fp.read_bytes()
        try:
            with d.stream_reader(io.BytesIO(raw)) as r:
                head = r.read(20000).decode("utf-8", "replace").split("\n")[0]
            hdr = _j.loads(head)
        except Exception:
            continue
        depth = hdr.get("delegationDepth") or 0
        if want_root and depth != 0:
            continue
        if not want_root and depth == 0:
            continue
        # 有子 agent 的优先
        txt = d.decompress(raw).decode("utf-8", "replace")
        has_kids = '"subagent/catalog"' in txt
        if want_root and has_kids:
            return fp.parent.name
        if not want_root:
            return fp.parent.name
        best = best or fp.parent.name
    return best


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions-root", default=str(Path.home() / ".dsh" / "sessions"))
    ap.add_argument("--session", default=None)
    ap.add_argument("--child", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    sroot = Path(args.sessions_root)
    tmp = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="dshtrace_selftest_"))
    tmp.mkdir(parents=True, exist_ok=True)
    results = []

    root_sid = args.session or pick_session(sroot, True)
    child_sid = args.child or pick_session(sroot, False)
    ledger = str(Path.home() / ".dsh" / ".dshw-usage.json")

    # ── T1 正常主会话
    d1 = tmp / "t1"
    rc, out = run(["--session", root_sid, "--out", str(d1), "--verify", "--usage-ledger", ledger])
    files_ok = all((d1 / f).exists() for f in ("trace.jsonl", "manifest.json", "report.md"))
    m1 = json.loads((d1 / "manifest.json").read_text(encoding="utf-8")) if (d1 / "manifest.json").exists() else {}
    t1 = rc == 0 and files_ok and m1.get("totals", {}).get("events", 0) > 0
    results.append(("T1 正常主会话", t1,
                    f"rc={rc} 事件={m1.get('totals',{}).get('events')} 节点={m1.get('totals',{}).get('nodes')} "
                    f"产物齐={files_ok}"))

    # ── T2 以子 agent 为根
    t2 = False
    note2 = "跳过（没找到子 agent 会话）"
    if child_sid:
        d2 = tmp / "t2"
        rc2, out2 = run(["--session", child_sid, "--out", str(d2), "--verify"])
        m2 = json.loads((d2 / "manifest.json").read_text(encoding="utf-8")) if (d2 / "manifest.json").exists() else {}
        t2 = rc2 == 0 and m2.get("totals", {}).get("events", 0) > 0
        note2 = f"rc={rc2} 会话={child_sid[:8]} 事件={m2.get('totals',{}).get('events')}"
    results.append(("T2 以子 agent 为根", t2, note2))

    # ── T3 幂等
    d3 = tmp / "t3"
    run(["--session", root_sid, "--out", str(d3)])
    h1 = sha(d3 / "trace.jsonl")
    run(["--session", root_sid, "--out", str(d3)])
    h2 = sha(d3 / "trace.jsonl")
    t3 = h1 == h2
    results.append(("T3 重复抽取字节一致", t3, f"{h1[:12]} vs {h2[:12]}"))

    # ── T4 不存在的会话
    d4 = tmp / "t4_should_not_exist"
    rc4, out4 = run(["--session", "session-00000000-0000-0000-0000-000000000000", "--out", str(d4)])
    t4 = rc4 != 0 and ("找不到会话" in out4) and not (d4 / "trace.jsonl").exists()
    results.append(("T4 不存在的会话明确报错", t4, f"rc={rc4} 没产半成品={not (d4 / 'trace.jsonl').exists()}"))

    # ── T5 不支持的格式版本
    d5 = tmp / "t5"
    d5.mkdir(exist_ok=True)
    fake = d5 / "fake.jsonl.zstd"
    import zstandard
    c = zstandard.ZstdCompressor()
    fake.write_bytes(c.compress(
        (json.dumps({"type": "session", "version": 99, "id": "x"}) + "\n").encode("utf-8")))
    rc5, out5 = run(["--path", str(fake), "--out", str(d5 / "o")])
    t5 = rc5 != 0 and "不支持的会话格式版本" in out5
    results.append(("T5 格式版本变了就报错不猜", t5, f"rc={rc5}"))

    print(f"\n会话根目录 {sroot}")
    print(f"测试会话 主={root_sid} 子={child_sid}\n")
    ok = 0
    for name, passed, note in results:
        print(f"  {'✓' if passed else '✗'} {name} — {note}")
        ok += bool(passed)
    print(f"\n{ok}/{len(results)} 通过")
    if args.out is None:
        print(f"（临时目录保留在 {tmp}，可自行清理）")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()
