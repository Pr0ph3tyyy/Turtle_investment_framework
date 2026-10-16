#!/usr/bin/env python3
"""选股器 CLI —— 全市场宽松筛选 → 100 只候选。

用法::

    # A 股（2 次 Tushare 调用，秒级）
    .venv/bin/python scripts/screen.py --market A --limit 100

    # 港股（hk_basic 1 次 + Yahoo 批量 + hk_fina_indicator 逐只 500 次 ≈ 15 分钟）
    .venv/bin/python scripts/screen.py --market HK --stage1 500 --limit 100

产出：``output/screen/{date}/candidates_{market}.csv|md`` + ``source_report_*.json``

**与 analysis 的关系**：只写 ``output/screen/**``，不碰 ``output/{code}_{name}/**``，
零文件重叠、无需锁、无需契约。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from screen import hk as HK                                        # noqa: E402
from screen import rules as R                                      # noqa: E402
from turtle_core.decision import ii_threshold as R_H                # noqa: E402
from screen.quotes import (Quote, SourceReport, fetch_eastmoney_hk,  # noqa: E402
                           fetch_yahoo_batch)
from screen.report import write_candidates                         # noqa: E402
from screen.turtle_quick import compute_a, compute_hk               # noqa: E402
from screen.universe import Row, fetch_a, hygiene                   # noqa: E402
from concurrent.futures import ThreadPoolExecutor                   # noqa: E402

DEFAULT_OUT = "output/screen"


_PRO = {"v": None}


def _pro():
    """惰性单例 Tushare client（逐只阶段要复用同一个，避免重复建连）。"""
    if _PRO["v"] is None:
        from config import get_token
        import tushare as ts
        _PRO["v"] = ts.pro_api(get_token())
    return _PRO["v"]


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _trade_date(as_of: str) -> str:
    return as_of.replace("-", "")


def _quick_batch(codes, fn, *, workers: int, note: str):
    """并发跑逐只龟龟计算（含节流）。返回 ``{code: QuickTurtle}``。"""
    import time
    out = {}
    done = {"n": 0}

    def one(c):
        try:
            r = fn(c)
        except Exception as exc:                              # noqa: BLE001
            from screen.turtle_quick import QuickTurtle
            r = QuickTurtle(code=c, reasons=[f"exception:{type(exc).__name__}"])
        time.sleep(0.15)
        done["n"] += 1
        if done["n"] % 50 == 0:
            print(f"    [{note}] {done['n']}/{len(codes)}", file=sys.stderr)
        return r

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for r in ex.map(one, codes):
            out[r.code] = r
    return out


def run_a(args, rules: R.Rules, source: SourceReport):
    td = args.trade_date or _trade_date(args.as_of)
    raw = fetch_a(td)
    source.note("static", "tushare:stock_basic", count=len(raw))
    source.note("quotes", "tushare:daily_basic", count=len(raw))
    funnel = [("A 股全市场（daily_basic）", len(raw))]

    kept, why0 = hygiene(raw, min_listing_years=rules.min_listing_years)
    ex = R.Exclusions(why0)
    funnel.append(("卫生过滤后", len(kept)))

    # 阶段一：**只做必要条件筛除，不排序**
    s1, ex1 = R.screen_rows(kept, rules, stage=1)
    ex.merge(ex1)
    funnel.append(("必要条件通过（未排序）", len(s1)))

    # 预算上限：必要性条件通过数超过阶段二预算时，**按流动性**取前 N 只进入计算
    # （流动性是**工程约束**不是策略判断 —— 且已在漏斗里如实标注）
    if len(s1) > rules.stage1_target:
        s1 = R.rank_rows(s1, rules, stage=1)[: rules.stage1_target]
        funnel.append((f"超预算 ⇒ 按流动性取前 {rules.stage1_target}", len(s1)))

    # 阶段二：算龟龟的四个量（AA / M_pess / GG_pess / 安全边际）
    map_ = {r.code: r for r in s1}
    ii_by = {c: R_H(c, args.rf_pct) for c in map_}
    print(f"  [阶段二] 逐只算龟龟量：{len(s1)} 只 × 3 接口 …", file=sys.stderr)
    quick = _quick_batch(list(map_), lambda c: compute_a(
        c, pro=_pro(), mkt_cap=getattr(map_[c], "total_mv", None),
        close=getattr(map_[c], "close", None), ii=ii_by[c],
        dv_ttm=getattr(map_[c], "dv_ttm", None)), workers=args.workers, note="阶段二")
    got = sum(1 for q in quick.values() if q.ok)
    funnel.append((f"算得出安全边际", got))

    rows = [map_[c] for c in map_ if quick[c].ok]
    if rules.min_safety_margin is not None:
        before = len(rows)
        rows = [r for r in rows if quick[r.code].safety_margin > rules.min_safety_margin]
        funnel.append((f"安全边际 > {rules.min_safety_margin}", len(rows)))
        if before - len(rows):
            ex.add("safety_margin_below_gate")
    final = R.rank_rows(rows, rules, quick=quick, stage=2)[: rules.limit]
    funnel.append((f"取前 {rules.limit}", len(final)))
    return raw, final, funnel, ex, quick, None


def run_hk(args, rules: R.Rules, source: SourceReport):
    static = HK.fetch_hk_static()
    source.note("static", "tushare:hk_basic", count=len(static))
    funnel = [("港股全市场（hk_basic）", len(static))]

    today = datetime.strptime(args.as_of, "%Y-%m-%d").date()
    kept_static, why = [], {}
    for s_ in static:
        if s_.list_status and s_.list_status != "L":
            why["not_listed"] = why.get("not_listed", 0) + 1; continue
        if rules.main_board_only and s_.market and "主板" not in s_.market:
            why["not_main_board"] = why.get("not_main_board", 0) + 1; continue
        if s_.list_date and len(s_.list_date) >= 8:
            try:
                if (today - datetime.strptime(s_.list_date[:8], "%Y%m%d").date()).days \
                        < rules.min_listing_years * 365:
                    why["too_young"] = why.get("too_young", 0) + 1; continue
            except ValueError:
                pass
        kept_static.append(s_)
    ex = R.Exclusions(why)
    funnel.append(("卫生过滤后", len(kept_static)))

    codes = [s_.code for s_ in kept_static]
    q = fetch_yahoo_batch(codes, workers=args.workers)
    got = sum(1 for c in codes if q.get(c) and q[c].close)
    source.note("quotes", "yahoo:chart", count=got)
    extras: dict[str, dict] = {}
    if got < len(codes) * 0.5:
        try:
            em_q, em_x = fetch_eastmoney_hk()
            merged = 0
            for c in codes:
                if (not q.get(c) or not q[c].close) and em_q.get(c):
                    q[c] = em_q[c]; merged += 1
            extras = {c: em_x[c] for c in codes if c in em_x}
            source.note("quotes", "eastmoney:clist", count=merged,
                        detail=f"Yahoo 仅覆盖 {got}/{len(codes)}，东财补 {merged} 只")
        except Exception as exc:                              # noqa: BLE001
            source.fail("eastmoney", f"{type(exc).__name__}: {exc}")

    rows = [Row(code=s_.code, name=s_.name, market="HK",
                close=(q.get(s_.code).close if q.get(s_.code) else None),
                volume=(q.get(s_.code).volume if q.get(s_.code) else None),
                amount=(q[s_.code].amount if q.get(s_.code) else None),
                list_date=s_.list_date, industry=s_.market,
                source=(q[s_.code].source if q.get(s_.code) else "")) for s_ in kept_static]
    rows = [r for r in rows if r.close]
    funnel.append(("有报价后", len(rows)))
    s1, ex1 = R.screen_rows(rows, rules, stage=1)
    ex.merge(ex1)
    funnel.append(("必要条件通过（价/成交额）", len(s1)))
    if len(s1) > rules.stage1_target * 2:
        s1 = R.rank_rows(s1, rules, extras=extras, stage=1)[: rules.stage1_target * 2]
        funnel.append((f"超预算 ⇒ 按流动性取前 {rules.stage1_target * 2}", len(s1)))

    # 逐只 hk_fina_indicator：既供必要条件（分红/PE），又提供 dps_hkd 给龟龟计算
    print(f"  [阶段二a] hk_fina_indicator：{len(s1)} 只 …", file=sys.stderr)
    funds, failed = HK.fetch_hk_fundamentals([r.code for r in s1], workers=args.workers)
    source.note("fundamentals", "tushare:hk_fina_indicator", count=len(funds))
    if failed:
        source.fail("fundamentals", f"{len(failed)} 只取不到（{failed[:3]}…）")
    s2, ex2 = R.screen_rows(s1, rules, stage=2, fundamentals=funds, extras=extras)
    ex.merge(ex2)
    funnel.append(("必要条件+估值通过", len(s2)))
    if len(s2) > rules.stage1_target:
        s2 = R.rank_rows(s2, rules, extras=extras, stage=1)[: rules.stage1_target]
        funnel.append((f"超预算 ⇒ 取前 {rules.stage1_target}", len(s2)))

    map_ = {r.code: r for r in s2}
    ii_by = {c: R_H(c, args.rf_pct) for c in map_}
    # ── 东财当日市值（**港股当日口径的唯一来源**；不可用则退回 pb_ttm×权益 折当日）──
    em_caps: dict[str, float] = {}
    try:
        _em_q, _em_x = fetch_eastmoney_hk()
        for _c in map_:
            _v = (_em_x.get(_c) or {}).get("total_mv")
            if _v and _v > 0:
                em_caps[_c] = _v
        if em_caps:
            source.note("cap_basis", "eastmoney:clist", count=len(em_caps),
                        detail="当日市值（港股当期口径的唯一来源；已在每行口径里披露）")
    except Exception as _exc:                                 # noqa: BLE001
        source.fail("eastmoney", f"{type(_exc).__name__}: {str(_exc)[:60]}"
                                 " ⇒ 退回 pb_ttm×权益 折当日（期末口径）")
    print(f"  [阶段二b] 逐只算龟龟量：{len(s2)} 只 × 3 接口"
          f"（东财当日市值覆盖 {len(em_caps)} 只）…", file=sys.stderr)
    quick = _quick_batch(list(map_), lambda c: compute_hk(
        c, pro=_pro(), close=getattr(map_[c], "close", None), ii=ii_by[c],
        dps_hkd=(getattr(funds[c], "dps_hkd", None) if c in funds else None),
        # **不再直接传 total_market_cap（港元）** —— 由 compute_hk 用
        # `pb_ttm × 股东权益` 推报表币种、再折到当日（口径政策 2026-10-15）；
        # 东财可用时优先用其**当日市值**（同样折到报表币种）
        em_total_mv=em_caps.get(c), fund=(funds.get(c))),
        workers=args.workers, note="阶段二b")
    funnel.append(("算得出安全边际", sum(1 for x in quick.values() if x.ok)))

    rows2 = [map_[c] for c in map_ if quick[c].ok]
    if rules.min_safety_margin is not None:
        rows2 = [r for r in rows2 if quick[r.code].safety_margin > rules.min_safety_margin]
        funnel.append((f"安全边际 > {rules.min_safety_margin}", len(rows2)))
    final = R.rank_rows(rows2, rules, quick=quick, extras=extras, stage=2)[: rules.limit]
    funnel.append((f"取前 {rules.limit}", len(final)))
    return rows, final, funnel, ex, quick, extras


def main() -> int:
    ap = argparse.ArgumentParser(description="全市场宽松选股（独立模块，与 analysis 零耦合）")
    ap.add_argument("--market", choices=["A", "HK"], default="A")
    ap.add_argument("--stage1", type=int, default=None, help="粗筛保留数量")
    ap.add_argument("--limit", type=int, default=None, help="最终输出数量")
    ap.add_argument("--as-of", default=None, help="基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--trade-date", default=None, help="A 股行情交易日 YYYYMMDD")
    ap.add_argument("--rules", default="screen_rules.toml", help="规则配置文件（可缺省）")
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--rf", type=float, default=1.665, help="无风险利率 %%（II = max(5, rf+3)）")
    args = ap.parse_args()

    args.as_of = args.as_of or _today()
    args.rf_pct = args.rf
    rules = R.load_rules(args.rules, args.market)
    if args.stage1:
        rules.stage1_target = args.stage1
    if args.limit:
        rules.limit = args.limit

    # 首选源**按市场**声明，且要与 note() 里用的名字一致，否则会误报"偏离首选"
    source = SourceReport(primary="yahoo:chart" if args.market == "HK"
                          else "tushare:daily_basic")
    try:
        if args.market == "A":
            _, final, funnel, ex, funds, extras = run_a(args, rules, source)
        else:
            _, final, funnel, ex, funds, extras = run_hk(args, rules, source)
    except Exception as exc:                                      # noqa: BLE001
        print(f"⛔ 取数失败，未产出名单（fail-closed）：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    if not final:
        print("⚠️ 没有标的通过规则 —— 这是结果，不是错误（不要硬凑数量）", file=sys.stderr)

    out = Path(args.out_dir) / args.as_of.replace("-", "")
    csv_p, md_p = write_candidates(out, market=args.market, as_of=args.as_of,
                                   rows=final, rules=rules, funnel=funnel,
                                   exclusions=ex.as_dict(), source=source,
                                   fundamentals=funds, extras=extras)
    print(source.headline())
    print(f"  漏斗: " + " → ".join(f"{k} {v:,}" for k, v in funnel))
    print(f"  → {md_p}")
    print(f"  → {csv_p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
