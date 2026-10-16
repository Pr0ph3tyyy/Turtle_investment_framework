#!/usr/bin/env python3
"""stage_manifest.py —— 阶段输入/产物清单 + md5，使"下游读到非预期版本"变成机械报错。

为什么需要
----------
各阶段直接读写同一路径，**没有版本交接机制**。06862 三轮回溯中，同一类事故发生了三次：

  1. 我在综合代理运行期间改了 `invariants.json` ⇒ 已落盘的 dims 与报告状态**自相矛盾**
     （审计报 critical F1），额外消耗一次修复遍 + 一次重认证；
  2. 两次在代理**半写状态**下读取产物（读到 137,983 B / `ac6ec017…` 这类中间态）；
  3. 一次审计代理读了 `output/_baselines/**` 里的**陈旧报告**，产出无效审计。

三次都是"下游读了非预期版本的输入"。纸面规则（`known_issues.md` §十二）挡不住，必须机械化。

用法
----
写清单（采集器在做完数据包后调用）::

    python3 scripts/stage_manifest.py --write pack --out-dir output/06862_海底捞 \\
        --input output/06862_海底捞/data_pack_market.md \\
        --input output/06862_海底捞/claims.json

校验（下游阶段开工前 / 交付前）::

    python3 scripts/stage_manifest.py --check output/06862_海底捞/pack_manifest.json

退出码：0 一致 · 1 有变更/缺失 · 2 输入错误。**变更即报错**，不做静默通过。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

SCHEMA = "1.0"


def md5_of(path: Path) -> Tuple[str, int]:
    h = hashlib.md5()
    total = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


def build(stage: str, inputs: Sequence[str | Path]) -> dict:
    entries = []
    for raw in inputs:
        p = Path(raw)
        if not p.is_file():
            entries.append({"path": str(p), "md5": None, "bytes": None,
                            "status": "missing"})
            continue
        digest, size = md5_of(p)
        entries.append({"path": str(p), "md5": digest, "bytes": size, "status": "ok"})
    return {"schema_version": SCHEMA, "stage": stage,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "inputs": entries}


def check(manifest_path: str | Path) -> List[str]:
    """Return a list of human-readable problems (empty ⇒ everything matches)."""
    m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    problems: List[str] = []
    for e in m.get("inputs", []):
        p = Path(e["path"])
        if not p.is_file():
            problems.append(f"缺失：{e['path']}")
            continue
        digest, size = md5_of(p)
        if e.get("md5") and digest != e["md5"]:
            problems.append(f"已变更：{e['path']}（清单 {e['md5'][:12]}… → 现在 {digest[:12]}…，"
                            f"{e.get('bytes')} → {size} B）")
    return problems


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="阶段清单 + md5（fail-closed）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", metavar="STAGE", help="写清单，阶段名如 pack / dims / report")
    g.add_argument("--check", metavar="MANIFEST", help="校验清单")
    ap.add_argument("--out-dir", help="--write 时的输出目录（清单名为 {stage}_manifest.json）")
    ap.add_argument("--input", action="append", default=[], help="--write 时的输入路径，可多次")
    ap.add_argument("--json", dest="json_out", help="--check 时把结果写到该文件")
    args = ap.parse_args(argv)

    if args.write:
        if not args.out_dir:
            print("Error: --write 需要 --out-dir", file=sys.stderr)
            return 2
        payload = build(args.write, args.input)
        out = Path(args.out_dir) / f"{args.write}_manifest.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        missing = [e["path"] for e in payload["inputs"] if e["status"] == "missing"]
        print(f"阶段清单已写：{out}（{len(payload['inputs'])} 项"
              + (f"，其中 {len(missing)} 项缺失" if missing else "") + "）")
        return 1 if missing else 0

    try:
        problems = check(args.check)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error: 无法读取清单 {args.check}: {exc}", file=sys.stderr)
        return 2
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"manifest": args.check, "problems": problems},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if problems:
        print(f"❌ 阶段输入与清单不一致：{len(problems)} 处")
        for p in problems:
            print(f"   - {p}")
        print("下游读到非预期版本 ⇒ 结果不可信；请重跑受影响阶段，不要在其上继续。")
        return 1
    print(f"✅ 阶段清单一致：{args.check}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
