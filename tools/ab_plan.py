#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ab_plan —— 批量对照实验：规划「同场景 × 多上下文配方」的派发。

要解决的问题：怎么证明「改上下文配方真的让 AI 更快走对链路」？
做法：**同一个业务场景、同一个任务，只换上下文配方**，并发派发多份，然后比完成时间、调用链、思考过程。

本工具只做第 1 步（规划 + 出任务书 + 开窗口）；派发由 agent 用 subagent 工具做（见生成的 DISPATCH.md）；
收数由 ab_collect.py 做。

实验定义（YAML）：
  id: exp-001
  scenario: 一句话业务场景（给人看的）
  task: |
    给 AI 的任务正文（所有配方共用，一字不改）
  verdict: |
    机械判据：怎么算成功（必须能被脚本或人一眼判）
  repeats: 2
  timeout_minutes: 25
  variants:
    - id: A
      label: 现状（只给任务）
      inject: []                      # 要注入的上下文正文（写进任务书的"上下文"段）
    - id: B
      label: 现状 + 一处权威指针
      inject:
        - file: docs/知识/<业务面>/<链路>.md   # 相对 repo 根的路径，内容**不**内联，只给指针
          note: 本节点所在链的权威正文

用法：
  python ab_plan.py --spec experiments/exp-001.yaml --repo <业务仓绝对路径> --out experiments
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

TZ = timezone(timedelta(hours=8))


def sha(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else None


def brief_for(spec, variant, run_idx, repo: Path):
    """生成一份可直接当 subagent prompt 的任务书。

    ⚠ 刻意保持短：方法论的核心主张之一就是任务书不该塞必读清单。
    """
    L = []
    L.append(f"【对照实验 {spec['id']} · 配方 {variant['id']} · 第 {run_idx} 次】")
    L.append("")
    L.append("## 任务")
    L.append(str(spec["task"]).strip())
    L.append("")
    L.append("## 上下文")
    inj = variant.get("inject") or []
    if not inj:
        L.append("（无额外上下文）")
    else:
        for it in inj:
            if isinstance(it, str):
                L.append(it)
                continue
            if it.get("file"):
                rel = it["file"]
                note = it.get("note") or ""
                L.append(f"- 权威正文：`{repo}\\{rel}`" + (f"（{note}）" if note else ""))
            if it.get("text"):
                L.append(str(it["text"]))
    L.append("")
    L.append("## 交回")
    L.append(str(spec.get("verdict") or "结论一行 + 关键实锚（文件路径/命令/输出片段）。").strip())
    L.append("")
    L.append(f"严格按上面的做，不要扩大范围、不要另建产物、不要问我；做完直接交回。")
    return "\n".join(L)


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="对照实验规划器")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--repo", default=None, help="业务仓根（用于把注入项解析成绝对路径并取 sha）")
    ap.add_argument("--out", default="experiments")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    args = ap.parse_args()

    spec_path = Path(args.spec)
    if not spec_path.exists():
        raise SystemExit(f"实验定义文件不存在：{spec_path}")
    try:
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SystemExit(f"实验定义不是合法 YAML：{spec_path}\n{e}")
    if not isinstance(spec, dict):
        raise SystemExit(f"实验定义顶层必须是键值表（YAML mapping）：{spec_path}")
    for k in ("id", "task", "variants"):
        if k not in spec:
            raise SystemExit(f"实验定义缺少必填字段：{k}（在 {spec_path}）")
    if not isinstance(spec["variants"], list) or not spec["variants"]:
        raise SystemExit(f"variants 必须是非空列表（在 {spec_path}）")
    repo = Path(args.repo).resolve() if args.repo else None
    repeats = int(spec.get("repeats") or 1)
    started = datetime.now(TZ)

    expdir = Path(args.out) / str(spec["id"])
    briefs = {}
    inject_ledger = []
    for v in spec["variants"]:
        if "id" not in v:
            raise SystemExit("每个 variant 必须有 id")
        for it in (v.get("inject") or []):
            if isinstance(it, dict) and it.get("file") and repo:
                fp = repo / it["file"]
                inject_ledger.append({
                    "variant": v["id"], "file": it["file"], "exists": fp.exists(),
                    "sha256_16": sha(fp),
                    "bytes": fp.stat().st_size if fp.exists() else None,
                })
        for r in range(1, repeats + 1):
            key = f"{v['id']}-r{r}"
            briefs[key] = {
                "variant": v["id"], "label": v.get("label") or v["id"], "run": r,
                # 派发时把这段原样用作 subagent 的 description，收数时靠它认领
                "dispatch_label": f"{spec['id']}|{v['id']}|r{r}",
                "brief": brief_for(spec, v, r, repo) if repo else brief_for(spec, v, r, Path(".")),
            }

    plan = {
        "experiment": spec["id"],
        "scenario": spec.get("scenario"),
        "started_at": started.isoformat(),
        "repo": str(repo) if repo else None,
        "repeats": repeats,
        "timeout_minutes": spec.get("timeout_minutes"),
        "variants": [{"id": v["id"], "label": v.get("label") or v["id"],
                      "inject": v.get("inject") or []} for v in spec["variants"]],
        "inject_ledger": inject_ledger,
        "runs": {k: {"variant": b["variant"], "run": b["run"],
                     "dispatch_label": b["dispatch_label"]} for k, b in briefs.items()},
        "note": "窗口起点=started_at。收数只认窗口内、且 dispatch_label 命中的子会话，避免把无关会话算进来。",
    }

    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=1))
        for k, b in briefs.items():
            print(f"\n===== {k} =====\n{b['brief']}")
        return

    (expdir / "briefs").mkdir(parents=True, exist_ok=True)
    (expdir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    for k, b in briefs.items():
        (expdir / "briefs" / f"{k}.md").write_text(b["brief"], encoding="utf-8")

    disp = [f"# 派发清单 · {spec['id']}\n",
            f"窗口起点：**{started.isoformat()}**",
            f"重复 {repeats} 次 × {len(spec['variants'])} 个配方 = **{len(briefs)} 份任务**\n",
            "## 怎么发（并发，一次发完）\n",
            "对每一份任务调一次 `subagent`，参数如下：\n",
            "```",
            "description: <下表 dispatch_label 原样填>",
            "prompt: <briefs/<key>.md 的全文>",
            "run_in_background: true",
            "```\n",
            "> ⚠ `description` 必须**逐字**用下表的值——收数靠它把子会话认领到配方上。\n",
            "| key | dispatch_label | 配方 | 任务书 |",
            "|---|---|---|---|"]
    for k, b in briefs.items():
        disp.append(f"| {k} | `{b['dispatch_label']}` | {b['label']} | `briefs/{k}.md` |")
    disp.append("\n## 收数\n")
    disp.append("```")
    disp.append(f"python tools/ab_collect.py --exp {expdir} --dispatcher <本会话id>")
    disp.append("```")
    (expdir / "DISPATCH.md").write_text("\n".join(disp) + "\n", encoding="utf-8")

    print(f"实验目录 → {expdir}")
    print(f"窗口起点 {started.isoformat()} · 待派发 {len(briefs)} 份")
    print(f"派发说明见 {expdir / 'DISPATCH.md'}")


if __name__ == "__main__":
    main()
