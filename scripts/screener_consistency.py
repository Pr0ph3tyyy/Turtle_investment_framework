#!/usr/bin/env python3
"""screener_consistency.py —— 验收标准①：screener 与既有 Workflow 2 报告 §17 claim 的**同源一致性**检查。

为什么这一个检查最重要
--------------------
个股报告的结论由 `GG_pess − II → 目标买入价 → 仓位` 决定。若 screener 用另一套口径排名，
自动化每天都会产出"screener 说第一、报告说不可买"的自相矛盾产物。所以设计把
**逐字段相等**定为验收标准，而不是"看起来差不多"。

本脚本对同一只股票做**两次**比对，从而把"公式错"与"输入口径错"分开：

============  ==========================  ==========================================
比对          用的市值                     判读
============  ==========================  ==========================================
(a) 自洽市值   股本 × 现价 × fx             不等 ⇒ 公式或输入错（**严重**）
(b) 产物市值   报告 §17.8 的总市值           不等 ⇒ **D-10** 股本口径降级（产物缺陷）
============  ==========================  ==========================================

判读规则：
- (a) ✅ 且 (b) ✅ ⇒ **PASS**（screener 与报告同源）
- (a) ✅ 且 (b) ❌ ⇒ **D-10**：公式没问题，是产物的 `市值 ≠ 股本×现价×fx`。
  此时 screener 与报告**在真实运行中仍会不等**，所以整体判 FAIL，但病因明确、可修
- (a) ❌ ⇒ 公式或输入不一致，**必须查**

用法::

    .venv/bin/python scripts/screener_consistency.py \
        --product output/00696_中国民航信息网络
    # 退出码 0 = PASS；1 = FAIL（逐条打印病因与归属）

只读：不联网、不写文件。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from turtle_core.decision import penetration_return, target_buy_price  # noqa: E402
from turtle_screener.engine import check_market_cap_consistency          # noqa: E402
from turtle_screener.slow_layer import load_product                      # noqa: E402

TOL_PCT = 0.015        # 账本保留 2 位小数
TOL_PRICE = 0.02


def parse_close(pack_text: str) -> Optional[float]:
    """从数据包取当前价格（``| 当前价格 (HKD) | 8.98 |`` 之类）。"""
    for pat in (r"\|\s*当前价格\s*\([A-Z]{3}\)\s*\|\s*([\d.]+)",
                r"\|\s*当前价格\s*\|\s*([\d.]+)",
                r"\|\s*最新价格[^|]*\|\s*([\d.]+)"):
        m = re.search(pat, pack_text or "")
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _claim(payload: dict, cid: str) -> Optional[float]:
    for c in payload.get("claims", []):
        if c.get("claim_id") == cid:
            v = c.get("value")
            try:
                return None if v is None else float(v)
            except (TypeError, ValueError):
                return None
    return None


def compare(product_dir: str, adopted_k: float = 0.8, close_override: Optional[float] = None,
            claims_path: Optional[str] = None, pack_path: Optional[str] = None) -> int:
    d = Path(product_dir)
    claims_file = Path(claims_path) if claims_path else d / "claims.json"
    pack_file = Path(pack_path) if pack_path else d / "data_pack_market.md"
    if not claims_file.exists():
        print(f"缺 claims.json：{claims_file}", file=sys.stderr)
        return 2
    payload = json.loads(claims_file.read_text(encoding="utf-8"))
    pack_text = pack_file.read_text(encoding="utf-8") if pack_file.exists() else ""

    snap, warns = load_product(d)
    if snap is None:
        print("无法构造快照（见 warning）", file=sys.stderr)
        for w in warns:
            print(f"  ⚠️  {w}", file=sys.stderr)
        return 2

    close = close_override if close_override is not None else parse_close(pack_text)
    ii = _claim(payload, "cm17.2.ii")
    m_pess = snap.payout.get("m_pess")
    aa = snap.aa.get("aa_selected")
    q = snap.q_default
    slug = f"k{int(round(adopted_k * 100)):03d}"

    print(f"# screener ↔ 报告 §17 同源一致性 — {snap.ts_code}")
    print(f"产物目录: {d}")
    print(f"财报期: {snap.fy_end}   现价: {close}   股本来源: {snap.shares_kind}")
    for w in warns:
        print(f"  ⚠️  {w}")
    print()

    if close is None or ii is None or m_pess is None or aa is None:
        missing = [n for n, v in (("现价", close), ("II", ii), ("M_pess", m_pess),
                                  ("AA_selected", aa)) if v is None]
        print(f"输入不全（{', '.join(missing)}）⇒ UNVERIFIABLE，不凑数（验收标准① 的三条纪律之一）")
        return 2

    # 报告侧
    rep_gg = _claim(payload, f"cm17.14.{slug}.ggpess")
    rep_tbp = _claim(payload, f"cm17.14.{slug}.tbp")

    rows: list[tuple[str, str, Optional[float], Optional[float], bool]] = []

    def add(label, what, stated, expect, tol):
        ok = (stated is not None and expect is not None and abs(stated - expect) <= tol)
        rows.append((label, what, stated, expect, ok))

    # (a) 自洽市值
    cap_self = (snap.total_shares or 0) * close * (snap.factor_to_reporting or 1.0) or None
    # (b) 产物市值
    cap_art = snap.artifact_market_cap_yuan

    for tag, cap in (("(a) 自洽市值", cap_self), ("(b) 产物市值", cap_art)):
        if not cap:
            rows.append((tag, "市值不可用", None, None, False))
            continue
        # ⚠️ 必须读 `snap.o`（`_MM_KEYS` 已归一到元）；`snap.o_yuan` 是**陈旧字段**，
        # 默认恒为 0.0（旧注释「canonical 硬编码 0」），读它会让 O 静默失效 ——
        # 实测该误读使检查器算 1.4067 而真实引擎算 1.735（findings D-3b）。
        gg = penetration_return(aa * adopted_k, m_pess, q,
                                snap.o if snap.o is not None else 0.0, cap)
        tbp = target_buy_price(close, gg, ii)
        add(tag, f"GG_pess(k={adopted_k:g})", rep_gg, gg, TOL_PCT)
        add(tag, f"目标买入价(k={adopted_k:g})", rep_tbp, tbp, TOL_PRICE)

    # II 无需比对（它是自身的基准）；安全边际在 §17.14 里**没有 claim**
    # （只渲染成 `vs 门槛 pct` 列），故按 `viewpoint_audit` V1a 的口径用账本 GG_pess 现算。
    if rep_gg is not None and ii is not None:
        margin = rep_gg - ii
        rows.append(("—", "安全边际 = GG_pess − II", margin, margin, True))

    print(f"{'比对':<14} {'口径':<26} {'报告':>10} {'screener':>11}  判定")
    print("-" * 78)
    for label, what, stated, expect, ok in rows:
        s = "—" if stated is None else f"{stated:.4f}"
        e = "—" if expect is None else f"{expect:.4f}"
        print(f"{label:<14} {what:<26} {s:>10} {e:>11}  {'✅' if ok else '❌'}")
    print()

    self_ok = all(r[4] for r in rows if r[0] == "(a) 自洽市值")
    art_ok = all(r[4] for r in rows if r[0] == "(b) 产物市值")

    # D-10 归因
    mc_ok, mc_detail = check_market_cap_consistency(
        cap_art, snap.total_shares, close, snap.factor_to_reporting)
    print(f"市值恒等式（D-10）: {'✅' if mc_ok else '❌'} {mc_detail}")
    print()

    # 判读（注意方向：(b) 才是**公式**的检验，(a) 是**端到端一致**的检验）
    #   (b) 通过 ⇒ 用同一市值时公式与报告逐字段相等 ⇒ 公式没问题
    #   (a) 不通过 ⇒ screener 自算市值与产物市值不同 ⇒ 用哪个市值都不能同时满足两者
    if self_ok and art_ok:
        print("screener_consistency: PASS —— screener 与报告 §17 同源（逐字段相等）")
        return 0
    if art_ok and not self_ok:
        print("screener_consistency: FAIL —— **D-10 归因（公式无误，市值口径不同源）**")
        print("  (b) 产物市值下逐字段相等 ⇒ 公式与报告同源 ✅")
        print("  (a) 自洽市值下不等 ⇒ 两侧用了不同的市值。")
        print("  成因（实测 00696）：产物市值取 fina_indicator 的**期末**口径，")
        print("  而 screener 用**当日**价 × 股本 ⇒ 价格一变动，两者必然不同。")
        print("  ⚠️ 因此这**不是**包级不变式能拦的缺陷（会系统性误报）；正解是让报告侧")
        print("     的门槛价格与目标买入价同用**一个**市值口径（§17.9 现用股数、")
        print("     §17.11/§17.14 用市值）。见 turtle_core_extraction_findings.md D-10。")
        return 1
    print("screener_consistency: FAIL —— **公式或输入不一致**（(b) 都不过，不止是 D-10）")
    print("  逐条核对上表；若 (b) 的 GG_pess 不符，优先查 M_pess / Q / AA_selected 的取值口径。")
    return 1


def main() -> None:
    ap = argparse.ArgumentParser(description="screener ↔ 报告 §17 同源一致性检查（验收标准①）")
    ap.add_argument("--product", required=True, help="产物目录（含 claims.json / fx.json / 数据包）")
    ap.add_argument("--adopted-k", type=float, default=0.8, help="采纳档 k（默认 0.80 ★）")
    ap.add_argument("--close", type=float, default=None, help="覆盖现价（默认从数据包解析）")
    ap.add_argument("--claims", default=None, help="覆盖 claims.json 路径")
    ap.add_argument("--pack", default=None, help="覆盖数据包路径")
    args = ap.parse_args()
    raise SystemExit(compare(args.product, args.adopted_k, args.close,
                             args.claims, args.pack))


if __name__ == "__main__":
    main()
