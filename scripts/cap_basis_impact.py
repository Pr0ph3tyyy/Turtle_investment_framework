#!/usr/bin/env python3
"""cap_basis_impact.py —— D-10 的**影响量化**（只读，不改任何产物）。

为什么需要它
------------
`products` 里的 `§1 总市值` 对港股取 `hk_fina_indicator` 的**期末**口径，
而 screener 用 `申报股数 × 当日价 × fx`（当日口径）。两者在价格变动后必然不等，
于是同一份报告里 §17.9（用股数）与 §17.11/§17.14（用市值）给出**不同的"同一个数"**。

修它要定一个政策（以哪个日期口径为准），**会改数值与产品语义** ⇒ 不能单方面决定。
本脚本给出**决策所需的数据**：统一口径后 GG 会变多少、短名单会怎么变。

用法::

    .venv/bin/python scripts/cap_basis_impact.py --scan output
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from turtle_screener.slow_layer import load_product                   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="D-10 影响量化（只读）")
    ap.add_argument("--scan", default="output")
    ap.add_argument("--rf", type=float, default=None)
    ap.add_argument("--prices", default=None,
                    help="价格快照 JSON（默认取 output/screener/prices_*.json 里最新的一个）")
    args = ap.parse_args()

    dirs = sorted(Path(p) for p in glob.glob(f"{args.scan}/*/claims.json"))
    from turtle_core import rf as rf_mod
    rf_pct, rf_why, rf_src = rf_mod.resolve_rf(cli_value=args.rf, output_dir=args.scan)
    if rf_pct is None:
        print(f"缺 Rf（{rf_why}）⇒ 无法算 II，退出", file=sys.stderr)
        return 2
    from turtle_core.decision import ii_threshold
    ii = ii_threshold("600887.SH", rf_pct)

    rows = []
    for cj in dirs:
        d = cj.parent
        snap, _ = load_product(str(d))
        if snap is None or snap.total_shares is None:
            continue
        # 报告市值（报表币种，百万元）—— 与 screener 无关，纯读产物
        cap_rep_mm = None
        try:
            claims = json.loads(cj.read_text(encoding="utf-8"))["claims"]
            for c in claims:
                if c.get("claim_id") == "cm17.8.mktcap":
                    cap_rep_mm = c.get("value")
        except Exception:
            pass
        rows.append({"dir": str(d), "code": snap.ts_code, "shares_mm": snap.total_shares / 1e6,
                     "fx": snap.factor_to_reporting, "report_cap_mm": cap_rep_mm,
                     "fy": snap.fy_end})

    if not rows:
        print("没有可分析产物")
        return 0
    # 用**离线价格快照**而不是联网抓价：本脚本是只读诊断，应可重复且无副作用
    if args.prices:
        pf = Path(args.prices)
    else:
        cands = sorted(Path(args.scan).glob("screener/prices_*.json"))
        pf = cands[-1] if cands else None
    if pf is None or not pf.exists():
        print("找不到价格快照（先跑一次 screener_daily.py --write-price-snapshot）", file=sys.stderr)
        return 2
    raw = json.loads(pf.read_text(encoding="utf-8")).get("quotes") or {}
    quotes = {k: type("Q", (), {"close": (v or {}).get("close")})() for k, v in raw.items()}
    print(f"# 价格快照：{pf}")

    print(f"# D-10 影响量化（只读）  II = {ii:.2f}%  Rf = {rf_pct}%")
    print(f"# 扫描 {len(rows)} 只（有 claims.json + 股本）\n")
    print(f"{'ts_code':<12} {'报表市值(百万)':>14} {'自洽市值(百万)':>14} {'偏差%':>8} {'GG 会变':>9}  {'港股?':>5}")
    n_big = 0
    worst = []
    for r in rows:
        q = quotes.get(r["code"])
        if q is None or q.close is None:
            continue
        fx = r["fx"] or 1.0
        self_cap = r["shares_mm"] * q.close * fx
        rep = r["report_cap_mm"]
        if not rep:
            continue
        dev = (self_cap - rep) / rep * 100
        if abs(dev) > 0.5:
            n_big += 1
        worst.append((abs(dev), r["code"], dev))
        print(f"{r['code']:<12} {rep:>14,.2f} {self_cap:>14,.2f} {dev:>+7.2f}% "
              f"{'×%.3f' % (rep / self_cap):>9}  {'✓' if r['code'].endswith('.HK') else '':>5}")
    worst.sort(reverse=True)
    print(f"\n# 偏差 >0.5% 的只数：{n_big} / {len(worst)}")
    print("# GG 会变的倍率 = 报表市值 / 自洽市值（GG 与市值成反比）")
    print("# 决策含义：倍率 >1 表示统一到「当日口径」后 GG 会**下降**，安全边际随之收窄\n")
    print("## 偏差最大的 8 只")
    for dev, code, _ in worst[:8]:
        print(f"  {code:<12} |偏差| {dev:6.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
