#!/usr/bin/env python3
"""rules_version.py —— 规则版本锚：让"产物是否陈旧"成为机器可判的事实。

为什么需要（known_issues.md §三十五）
------------------------------------
原先产物只记录 `schema_version`（格式版本），**不记录"由哪一版规则算出"**。
实测代价（2026-10-15）：`600011_华能国际` 的 `section17_status.json` 里挂着一个
`exception` 状态，我花时间去追一个**当前并不存在**的错误 —— 它其实是**陈旧状态**
（重采集后消失）。没有版本锚，就无法区分"这个产物反映了当前规则"与"它是旧规则的化石"。

更大的隐患：`pack_manifest.json` 钉的是**输入**的 md5（证明"输入没被中途改动"），
它**不能**证明"输入与当前规则同代"。规则一改，全部存量产物都可能陈旧，而无人知道。

`rules_version` 的构成
----------------------
* `strategy_rules.json`（阈值/值域/税率的权威）的 sha256；
* 关键计算脚本（公式单点 + 采集器）的 sha256。

只取**前 12 位**：它是"同代性"的指纹，不是安全哈希。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
from typing import Dict, List, Optional

#: 参与版本计算的**规则与公式**文件（不含渲染/审计脚本 —— 那些改了不影响数值）
VERSIONED_FILES = [
    "strategy_rules.json",
    "tushare_modules/constants.py",
    "tushare_modules/derived_metrics.py",
    "tushare_modules/financials.py",
    "tushare_modules/infrastructure.py",
    "tushare_modules/assembly.py",
    "turtle_core/decision.py",
    "turtle_core/pessimistic.py",
    "turtle_core/floors.py",
    "turtle_core/snapshot.py",
    "section_contract.py",
]

#: 需要携带版本锚的产物文件
ANCHORED = ["claims.json", "section17_status.json", "metrics.json",
            "pack_manifest.json", "invariants.json", "field_coverage.json"]


def compute(scripts_dir: Optional[str] = None) -> str:
    """当前规则版本（12 位十六进制）。文件缺失不报错（记为空），保证确定性。"""
    base = scripts_dir or os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for rel in VERSIONED_FILES:
        p = os.path.join(base, rel)
        h.update(rel.encode("utf-8"))
        if os.path.isfile(p):
            h.update(open(p, "rb").read())
        else:
            h.update(b"<missing>")
    return h.hexdigest()[:12]


def read_anchor(product_dir: str) -> Optional[str]:
    """从产物里读回版本锚（以第一个有该字段的文件为准）。"""
    for name in ANCHORED:
        p = os.path.join(product_dir, name)
        if not os.path.isfile(p):
            continue
        try:
            d = json.loads(open(p, encoding="utf-8").read())
        except (OSError, ValueError):
            continue
        v = d.get("rules_version")
        if v:
            return str(v)
    return None


def audit(root: str) -> Dict[str, object]:
    """扫描 `root/*/`，报告每个产物的版本锚与陈旧情况。"""
    current = compute()
    rows: List[dict] = []
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        if not os.path.isdir(d):
            continue
        if not os.path.isfile(os.path.join(d, "data_pack_market.md")):
            continue
        v = read_anchor(d)
        rows.append({
            "product": os.path.basename(d),
            "rules_version": v,
            "status": ("current" if v == current else
                       "stale" if v else "unanchored"),
        })
    counts = {"current": 0, "stale": 0, "unanchored": 0}
    for r in rows:
        counts[r["status"]] += 1
    return {"current_rules_version": current, "counts": counts, "products": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="规则版本锚：显示当前版本 / 体检产物是否陈旧（只读）")
    ap.add_argument("--root", default="output")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--strict", action="store_true",
                    help="存在 stale/unanchored 时退出码 1（交付路径）")
    ap.add_argument("--print-version", action="store_true")
    a = ap.parse_args(argv)

    current = compute()
    if a.print_version:
        print(current)
        return 0

    rep = audit(a.root)
    print(f"当前规则版本: {current}")
    c = rep["counts"]
    print(f"产物版本锚: current {c['current']} · stale {c['stale']} · "
          f"unanchored {c['unanchored']}")
    stale = [r["product"] for r in rep["products"] if r["status"] == "stale"]
    un = [r["product"] for r in rep["products"] if r["status"] == "unanchored"]
    if stale:
        print(f"\n⚠️ 陈旧产物（{len(stale)} 个，其数字反映**旧规则**，不得与当前规则结论混用）:")
        for p in stale[:20]:
            print(f"    {p}")
    if un:
        print(f"\n❓ 无版本锚（{len(un)} 个，产生于本机制之前）:")
        for p in un[:10]:
            print(f"    {p}")
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)
    if a.strict and (stale or un):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
