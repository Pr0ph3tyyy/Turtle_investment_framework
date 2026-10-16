#!/usr/bin/env python3
"""screener_daily.py —— 龟龟每日选股（单条入口）。

流程（设计 §C2 的慢/快层分离）
---------------------------
::

    慢层：产物目录 claims.json + fx.json + 股本口径  →  financial_snapshot
    快层：直连 Yahoo chart 抓当日价（池 + 候选集）    →  1 次并发抓取
    计算：turtle_core（II / GG_pess / 安全边际 / 目标买入价）
    排序：安全边际 GG_pess − II 降序，门槛制 + N 上限
    交付：output/screener/{trade_date}/ shortlist.json · run_manifest.json · excluded.json · CSV

退出码
------
``0`` 可交付；``1`` 不可交付（阻断级，如 Rf 缺失）；``2`` 用法/输入错误。

**不联网也能干的事**：``--offline-prices <json>`` 读当日价格快照，
便于复跑与幂等验证（快照的存在正是为了让重跑幂等）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from turtle_core import rf as RF                                  # noqa: E402
from turtle_core.decision import ii_threshold                     # noqa: E402
from turtle_screener import manifest as M                          # noqa: E402
from turtle_screener.calendar import (MARKET_CLOCKS, TradingCalendar,  # noqa: E402
                                      is_data_ready)
from turtle_screener.engine import run_screen                      # noqa: E402
from turtle_screener.prices import YahooChartClient                # noqa: E402
from turtle_screener.slow_layer import load_product                # noqa: E402

DEFAULT_BASE = "output"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="龟龟每日选股：慢层快照 × 当日价格 × turtle_core → 按安全边际排序")
    ap.add_argument("--pool-file", help="常备池 JSON（{pool:[{ts_code,dir,name}]}）")
    ap.add_argument("--scan", default=None,
                    help="自动发现产物目录（默认 output；扫含 claims.json 的子目录）")
    ap.add_argument("--markets", default="A,HK,US",
                    help="市场范围（用于日历/就绪判定，逗号分隔）")
    ap.add_argument("--rf", type=float, default=None, help="无风险利率（百分数）")
    ap.add_argument("--n-max", type=int, default=8, help="入选上限（门槛制，非硬取）")
    ap.add_argument("--workers", type=int, default=8, help="价格抓取并发")
    ap.add_argument("--output", default=DEFAULT_BASE, help="输出基目录（默认 output）")
    ap.add_argument("--as-of", default=None, help="归档日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--trade-date", default=None, help="交易日 YYYYMMDD（默认今天）")
    ap.add_argument("--offline-prices", default=None,
                    help="读当日价格快照 JSON（幂等复跑；不联网）")
    ap.add_argument("--write-price-snapshot", default=None,
                    help="把本次抓到的价格落盘到该路径（供次日复跑/审计）")
    ap.add_argument("--skip-readiness", action="store_true",
                    help="跳过交易日/就绪判定（用于补跑历史）")
    ap.add_argument("--allow-degraded", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    return ap.parse_args(argv)


def discover_pool(scan_dir: str) -> list[dict]:
    """扫 ``{scan_dir}/*/claims.json`` → 池条目。"""
    out = []
    for d in sorted(Path(scan_dir).glob("*/")):
        if (d / "claims.json").exists():
            # 产物目录约定 `{code}_{公司}`；目录名里的代码**无交易所后缀**，不可当 ts_code 用
            # （ts_code 一律取自 claims.json 的 stock_code）。这里只取公司名做人读标签。
            _code, _sep, _name = d.name.partition("_")
            out.append({"dir": str(d), "ts_code": None,
                        "name": _name or d.name})
    return out


def load_pool_entries(args) -> list[dict]:
    entries: list[dict] = []
    if args.pool_file:
        payload = json.loads(Path(args.pool_file).read_text(encoding="utf-8"))
        entries.extend(payload.get("pool") or [])
    if args.scan is not None:
        have = {e.get("dir") for e in entries}
        entries.extend(e for e in discover_pool(args.scan) if e.get("dir") not in have)
    return entries


def main(argv=None) -> int:
    args = parse_args(argv)
    as_of = args.as_of or datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    trade_date = args.trade_date or as_of.replace("-", "")
    markets = [m.strip() for m in args.markets.split(",") if m.strip()]

    # ---- 1. 慢层 ----
    entries = load_pool_entries(args)
    if not entries:
        print("池为空：给 --pool-file 或 --scan（默认 output）", file=sys.stderr)
        return 2
    snaps, warns, names, fy_ends = [], [], {}, {}
    for e in entries:
        snap, w = load_product(e["dir"], ts_code=e.get("ts_code"))
        warns.extend(w)
        if snap is None:
            continue
        snaps.append(snap)
        # 名称优先用池文件给的，否则用目录名里的公司名
        names[snap.ts_code] = e.get("name") or Path(e["dir"]).name.partition("_")[2]
        fy_ends[snap.ts_code] = snap.fy_end
    print(f"[慢层] 池 {len(entries)} 条 → 快照 {len(snaps)} 只")
    for w in warns:
        print(f"  ⚠️  {w}")
    if not snaps:
        print("无可用快照（都需要 claims.json）", file=sys.stderr)
        return 2

    # ---- 2. Rf ----
    rf_pct, rf_src, rf_why = RF.resolve_rf(
        cli_value=args.rf, output_dir=Path(args.output))
    if rf_pct is None:
        print(f"[Rf] 不可用（{rf_why}）：yc_cb 无权限，需 --rf / TUSHARE_RF / "
              f"{args.output}/rf.json", file=sys.stderr)
        print("   ⇒ 按契约 ABORT：II 错则全市场排名口径全错，不出残缺清单。",
              file=sys.stderr)
        res = run_screen(snaps, {}, ii=None, n_max=args.n_max)
        man = M.build_run_manifest(res, as_of, rf_source=rf_src)
        if not args.dry_run:
            M.write_run(args.output, trade_date, M.build_shortlist(res, as_of),
                        man, M.build_excluded(res), result=res)
        return 1
    ii = ii_threshold(snaps[0].ts_code, rf_pct)
    print(f"[Rf] {rf_pct}%（{rf_src}） →  II = max(5%, Rf+3%) = {ii}%")

    # ---- 3. 日历就绪 ----
    cal = TradingCalendar()
    readiness_notes = []
    if not args.skip_readiness:
        for mk in markets:
            ready, why = is_data_ready(mk, True)     # 日历不可得时另行标注
            readiness_notes.append(f"{mk}:{'就绪' if ready else f'未就绪({why})'}")
        print(f"[日历] {'  '.join(readiness_notes)}"
              f"（本地时区参考 {','.join(MARKET_CLOCKS[m]['timezone'] for m in markets if m in MARKET_CLOCKS)}）")
        print("   ⚠️  港股日历需自建（trade_cal 无 HKEX）；本步仅作时钟提示，"
              "未做交易日断言（--skip-readiness 可关）")

    # ---- 4. 快层：价格 ----
    codes = [s.ts_code for s in snaps]
    if args.offline_prices:
        payload = json.loads(Path(args.offline_prices).read_text(encoding="utf-8"))
        quotes = YahooChartClient.from_snapshot(payload)
        print(f"[快层] 读价格快照 {args.offline_prices}：{len(quotes)} 条")
    else:
        client = YahooChartClient(workers=args.workers, retries=1, backoff=0.6)
        payload = client.snapshot_payload(codes)
        quotes = YahooChartClient.from_snapshot(payload)
        print(f"[快层] 抓价 {client.last_stats}")
        if args.write_price_snapshot:
            M._atomic_write(Path(args.write_price_snapshot),
                            json.dumps(payload, ensure_ascii=False, indent=1))
            print(f"   价格快照 → {args.write_price_snapshot}")

    # ---- 5. 计算 + 排序 ----
    res = run_screen(snaps, quotes, ii=ii, n_max=args.n_max, names=names,
                     allow_upstream_degraded=args.allow_degraded)
    if args.allow_degraded:
        print("  ⚠️  --allow-degraded：上游门禁未通过的产物已纳入排名（须随产出披露）")

    for r in res.rows:
        tag = "✓" if r.eligible else "✗"
        gg = "—" if r.gg_pess is None else f"{r.gg_pess:.3f}%"
        mg = "—" if r.margin is None else f"{r.margin:+.3f}pct"
        # 日报**不打买点**：选股器给"买点"会被当成个股建议；排名只需积分。
        # 积分 = margin / II（对 margin 的单调变换，不引入新信息）。
        sc = "—" if r.score is None else f"{r.score:+.3f}"
        print(f"  {tag} {r.ts_code:<12} 价={r.close} 市值={(r.market_cap_yuan or 0)/1e6:,.0f}百万 "
              f"GG_pess={gg:>8} 安全边际={mg:>11} 积分={sc:>8} {r.reasons}")

    # ---- 6. 落盘 ----
    sl = M.build_shortlist(res, as_of, trade_dates={}, rf_pct=rf_pct,
                           snapshot_fy_end=fy_ends)
    man = M.build_run_manifest(res, as_of,
                               price_stats={"requested": len(codes),
                                            "ok": sum(1 for q in quotes.values() if q.ok)},
                               snapshot_stats={"loaded": len(snaps)},
                               allow_degraded=args.allow_degraded)
    ex = M.build_excluded(res)

    print(f"\n[结果] screened={len(res.rows)} eligible={res.eligible_count} "
          f"shortlist={len(res.shortlist)} n_max={res.n_max}")
    print(f"[原因] {man['reason_summary']}")
    print(f"[门禁] deliverable={man['deliverable']} blocking={man['verdict']['blocking_count']} "
          f"disclosure={man['verdict']['disclosure_count']}")

    if args.dry_run:
        print("[dry-run] 未写文件")
    else:
        out = M.write_run(args.output, trade_date, sl, man, ex, result=res)
        print(f"[落盘] {', '.join(str(p) for p in out.values())}")

    return 0 if man["deliverable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
