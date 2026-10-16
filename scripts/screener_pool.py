#!/usr/bin/env python3
"""screener_pool.py —— 常备池的构建与 bootstrap 驱动（P5）。

为什么需要常备池（设计 §C6）
---------------------------
Workflow 1（定性分析）依赖**年报 PDF**（靠 `web_search`，不稳定）+ 6 维度子代理 + 两条审核，
**是年度事件而非每日事件**。日报只有在池内股票**已经**有 `claims.json` 时才跑得快。
所以：池是一次性/年度性投入，日报是每日摊销。

为什么 bootstrap 只做 A 股优先
----------------------------
- **A 股**：`daily_basic(trade_date)` 一次调用即得全市场（5574 行，实测**无限频**），
  含 `total_mv / turnover_rate / pe_ttm / pb / dv_ttm` ⇒ 排序依据完整；
  且市值与股本同属 **同日** `daily_basic` 口径 ⇒ 不受 D-10（HK 的市值取期末、
  现价取当日）影响，验收标准①可在 A 股上先跑通。
- **港股/美股**：没有可用的全市场批量行情（`hk_daily` 1 次/小时、`us_daily` 1 次/分钟、
  yfinance 被限频），且港股受 D-10 影响 ⇒ **先跑通 A 股再扩**。

用法::

    # 1) 构建池（1 次 API 调用）
    .venv/bin/python scripts/screener_pool.py --build --limit 100

    # 2) bootstrap：对池内每只跑一次采集（**长跑**，可中断可续跑）
    .venv/bin/python scripts/screener_pool.py --bootstrap --rf 1.665

    # 3) 查看
    .venv/bin/python scripts/screener_pool.py --show
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from turtle_screener.universe import UniverseSource, apply_hygiene  # noqa: E402

DEFAULT_POOL = "output/screener_pool.json"
DEFAULT_OUTPUT = "output"
POOL_SCHEMA = "1.0"

#: 池构建的默认门槛（可被 CLI 覆盖）
DEFAULTS = {
    "min_market_cap_yi": 50.0,      # 亿元 —— 池是"要花力气做定性分析"的候选，太小的排除
    "min_turnover_pct": 0.3,        # % —— 流动性下限
    "max_pe": 40.0,                 # 主通道 PE 上限
    "max_per_industry_frac": 0.10,  # 单一行业最多占池内 10%
    "min_listing_years": 3,
}

#: 池排序权重。**换手率刻意不作为加分项** —— 它只是流动性**下限**（min_turnover_pct）。
#: 龟龟是深度价值/分红框架（穿透回报率的核心是分配能力），把高换手当正向信号会
#: 把小盘投机股排到前面（实测首版：新华都 57 亿排在中国移动 2.16 万亿之前）。
#: 因此排序只看三个与框架同向的维度，且各取**百分位**消除量纲：
#:   规模（对数市值）· 股息率（分配能力）· 便宜度（1/PE）
POOL_SCORE_WEIGHTS = {"size": 0.35, "dividend": 0.40, "cheap": 0.25}


def _pctiles(values: list[float]) -> list[float]:
    """百分位（0–1，越大越好）。并列取平均位次；空/全同则全给 0.5。"""
    n = len(values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    if n == 1:
        return [0.5]
    return [r / (n - 1) for r in ranks]


def _tushare_fetcher(token: str):
    import tushare as ts
    pro = ts.pro_api(token)

    def fetch(api_name: str, **kwargs) -> list[dict]:
        df = pro.query(api_name, **kwargs)
        return [] if df is None or df.empty else df.to_dict("records")
    return fetch


def _load_env():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import get_token
    return get_token()


def _num(v) -> float | None:
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def build_pool(limit: int, *, output: str = DEFAULT_OUTPUT, rf: float | None = None,
               trade_date: str | None = None, fetch=None, **thresholds) -> dict:
    """枚举 A 股 → 卫生过滤 → 百分位加权排序 → 行业分散 → 返回池 payload。

    ``fetch(api_name, **kwargs) -> list[dict]`` 可注入以便离线测试。
    """
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in thresholds.items() if v is not None})
    fetch = fetch or _tushare_fetcher(_load_env())
    src = UniverseSource(fetch)

    today = datetime.now(timezone.utc).astimezone().date()
    hy = apply_hygiene(src.fetch_a(), today=today,
                       min_listing_years=cfg["min_listing_years"], market="A")
    if not hy.kept:
        raise SystemExit("A 股宇宙为空 —— 检查 Tushare 权限/网络")

    # 行情快照（A 股可整市场一次取回）
    if not trade_date:
        from turtle_screener.calendar import MarketClock
        trade_date = today.strftime("%Y%m%d")
    rows = fetch("daily_basic", trade_date=trade_date,
                 fields="ts_code,close,pe_ttm,pb,total_mv,circ_mv,dv_ttm,turnover_rate")
    if not rows:
        raise SystemExit(f"daily_basic({trade_date}) 为空 —— 该日可能非交易日或数据未就绪"
                         "（A 股当日数据约 19:00 后才完整）")
    quotes = {r["ts_code"]: r for r in rows if r.get("ts_code")}

    cands = []
    excluded = {}
    for r in hy.kept:
        q = quotes.get(r.ts_code)
        if not q:
            excluded[r.ts_code] = "no_price"
            continue
        # ⚠️ 不得用 `or 0` 兜底（known_issues §五十二）：`total_mv` 解析失败会**静默变成 0**，
        # 于是该股以 `too_small` 被排除 —— 一个**数据缺口被伪装成合格排除**，
        # 而且理由码完全指错方向（应查上游字段，不是"市值太小"）。
        # 现在区分"缺失"与"真的是 0"：缺失 ⇒ 单独的 `no_market_cap`（与别处同码）。
        _mv_raw = _num(q.get("total_mv"))
        if _mv_raw is None:
            excluded[r.ts_code] = "no_market_cap"
            continue
        mv_yi = _mv_raw / 10000.0                                 # 万元 → 亿元
        turn = _num(q.get("turnover_rate"))
        pe = _num(q.get("pe_ttm"))
        pb = _num(q.get("pb"))
        dv = _num(q.get("dv_ttm"))
        if mv_yi < cfg["min_market_cap_yi"]:
            excluded[r.ts_code] = "too_small"
            continue
        if turn is None or turn < cfg["min_turnover_pct"]:
            excluded[r.ts_code] = "illiquid"
            continue
        if pe is None or pe <= 0 or pe > cfg["max_pe"]:
            excluded[r.ts_code] = "pe_out_of_band"
            continue
        if pb is None or pb <= 0:
            excluded[r.ts_code] = "pe_out_of_band"
            continue
        if dv is None or dv <= 0:
            excluded[r.ts_code] = "no_dividend"
            continue
        cands.append({"ts_code": r.ts_code, "name": r.name, "industry": r.industry,
                      "list_date": r.list_date, "close": _num(q.get("close")),
                      "total_mv_yi": round(mv_yi, 2), "turnover_rate": turn,
                      "pe_ttm": pe, "pb": pb, "dv_ttm": dv})

    # 百分位加权（换手率不作为加分项，见 POOL_SCORE_WEIGHTS 的注释）
    import math as _math
    size_p = _pctiles([_math.log(c["total_mv_yi"]) for c in cands])
    div_p = _pctiles([c["dv_ttm"] for c in cands])
    cheap_p = _pctiles([1.0 / c["pe_ttm"] for c in cands])
    w = POOL_SCORE_WEIGHTS
    for i, c in enumerate(cands):
        c["score"] = round(w["size"] * size_p[i] + w["dividend"] * div_p[i]
                           + w["cheap"] * cheap_p[i], 4)
    cands.sort(key=lambda x: (-x["score"], x["ts_code"]))
    # 行业分散：单行业不超过池内 frac
    cap = max(1, int(limit * cfg["max_per_industry_frac"]))
    per_ind: dict[str, int] = {}
    picked, dropped = [], {}
    for c in cands:
        ind = c["industry"] or "未分类"
        if per_ind.get(ind, 0) >= cap:
            dropped[c["ts_code"]] = ind
            continue
        per_ind[ind] = per_ind.get(ind, 0) + 1
        picked.append(c)
        if len(picked) >= limit:
            break

    payload = {
        "schema_version": POOL_SCHEMA,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "rf_at_build": rf,
        "markets": ["A"],
        "thresholds": cfg,
        "counts": {"universe": len(hy.kept), "after_quote_filters": len(cands),
                   "picked": len(picked), "industry_capped": len(dropped),
                   "excluded": len(hy.excluded) + len(excluded)},
        "pool": [{"ts_code": c["ts_code"], "name": c["name"], "industry": c["industry"],
                  "dir": f"{output}/{c['ts_code'].split('.')[0]}_{c['name']}",
                  "metrics": {k: c[k] for k in
                              ("close", "total_mv_yi", "turnover_rate", "pe_ttm", "pb",
                               "dv_ttm", "score")}}
                 for c in picked],
    }
    payload["counts"]["universe"] = len(hy.kept)
    return payload


def write_pool(payload: dict, path: str = DEFAULT_POOL) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return p


def show_pool(path: str = DEFAULT_POOL) -> int:
    p = Path(path)
    if not p.exists():
        print(f"池文件不存在：{p}（先跑 --build）", file=sys.stderr)
        return 2
    d = json.loads(p.read_text(encoding="utf-8"))
    print(f"# 常备池 {d.get('schema_version')}  建于 {d.get('built_at')}  "
          f"交易日 {d.get('trade_date')}  市场 {d.get('markets')}")
    print(f"# 计数 {d.get('counts')}")
    print(f"{'#':>3} {'ts_code':<12} {'名称':<10} {'行业':<10} {'市值(亿)':>9} "
          f"{'换手%':>7} {'PE':>7} {'股息%':>7}  {'已有§17':>7}")
    for i, e in enumerate(d.get("pool") or [], 1):
        m = e.get("metrics") or {}
        has = (Path(e["dir"]) / "claims.json").exists() if e.get("dir") else False
        print(f"{i:>3} {e['ts_code']:<12} {str(e.get('name'))[:9]:<10} "
              f"{str(e.get('industry'))[:9]:<10} {m.get('total_mv_yi', 0):>9} "
              f"{m.get('turnover_rate', 0):>7} {m.get('pe_ttm', 0):>7} "
              f"{m.get('dv_ttm', 0):>7}  {'✅' if has else '—':>7}")
    return 0


#: 判定一个产物目录是否含**已审计/定性分析**成果。用于**禁止覆盖**。
#: 实测教训（2026-10-14）：池子按 `output/{code}_{name}` 命名，会与**既有产物目录撞名** ——
#: 池内实际含 `002027_分众传媒`、`600941_中国移动` 两条**已有完整审计链**的产物。
#: 早先的 bootstrap 因"跳过已有 claims.json"而没碰它们（侥幸）；一旦用 `--force`，
#: 就会覆盖 `qualitative_report.md` / `dims/` / 两条审核 —— **不可恢复**。
_AUDIT_MARKERS = ("qualitative_report.md", "dims", "slices", "audit.md",
                  "consistency_report.md", "numeric_audit.md", "viewpoint_audit.md")


def _has_audit_artifacts(d: Path) -> bool:
    return any((d / m).exists() for m in _AUDIT_MARKERS)


def bootstrap(path: str = DEFAULT_POOL, *, rf: float | None = None,
              skip_existing: bool = True, limit: int | None = None,
              allow_overwrite_audited: bool = False) -> int:
    """对池内每只跑一次采集（**长跑**）。可中断、可续跑：已有 claims.json 的跳过。"""
    p = Path(path)
    if not p.exists():
        print(f"池文件不存在：{p}", file=sys.stderr)
        return 2
    pool = json.loads(p.read_text(encoding="utf-8")).get("pool") or []
    todo, protected = [], []
    for e in pool:
        d = Path(e["dir"])
        # **保护**：含审计/定性成果的目录一律不覆盖（除非显式授权）
        if _has_audit_artifacts(d) and not allow_overwrite_audited:
            protected.append(e)
            continue
        if skip_existing and (d / "claims.json").exists():
            continue
        todo.append(e)
    if protected:
        print(f"🛡️  保护 {len(protected)} 只**含审计/定性成果**的产物，不覆盖：")
        for e in protected:
            print(f"      {e['ts_code']:<12} {e['dir']}")
        print("    （要覆盖请显式传 --allow-overwrite-audited；那会销毁报告与审核链）")
    if limit:
        todo = todo[:limit]
    if not todo:
        print("池内全部已有 claims.json —— 无需 bootstrap")
        return 0

    print(f"bootstrap：{len(todo)} 只待采集（已有 claims.json 的已跳过）")
    tallied: dict[str, int] = {}
    for i, e in enumerate(todo, 1):
        d = Path(e["dir"])
        d.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(Path(__file__).resolve().parent / "tushare_collector.py"),
               "--code", e["ts_code"], "--output", str(d / "data_pack_market.md")]
        if rf is not None:
            cmd += ["--rf", str(rf)]
        # 港股需 fx：池里没有时留给调用方补 fx.json（fail-closed，不猜汇率）
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        got = (d / "claims.json").exists()
        # 门禁裁决决定该产物**能不能用**：claims.json 存在 ≠ 可用
        # （采集器对 deliverable=False 的包明说「不得作为分析输入」）
        gate = {}
        try:
            gate = json.loads((d / "gate_status.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        if not got:
            status, kind = f"FAIL(exit={proc.returncode})", "fail"
        elif gate.get("deliverable") is False:
            blk = ",".join(b.get("code", "?") for b in (gate.get("blocking") or []))
            status, kind = f"BLOCKED[{blk}]", "blocked"
        elif gate.get("degraded"):
            status, kind = "DEGRADED", "degraded"
        else:
            status, kind = "OK", "ok"
        tallied[kind] = tallied.get(kind, 0) + 1
        print(f"  [{i}/{len(todo)}] {e['ts_code']:<12} {status}  {time.time()-t0:.0f}s")
        if not got:
            tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
            for line in tail:
                print(f"        {line}")
    print("bootstrap 完成：" + "，".join(f"{k} {v}" for k, v in sorted(tallied.items())))
    if tallied.get("blocked") or tallied.get("fail"):
        print("  ⚠️  BLOCKED/FAIL 的产物**不得作为分析输入**（采集器已判定）；"
              "先按各自 gate_status.json 的 blocking[] 修数据，或确知影响面时显式降级。")
    return 0 if not (tallied.get("blocked") or tallied.get("fail")) else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="常备池构建与 bootstrap（P5）")
    ap.add_argument("--pool", default=DEFAULT_POOL, help="池文件路径")
    ap.add_argument("--output", default=DEFAULT_OUTPUT, help="产物基目录")
    ap.add_argument("--build", action="store_true", help="构建池")
    ap.add_argument("--bootstrap", action="store_true", help="对池内每只跑采集（长跑）")
    ap.add_argument("--show", action="store_true", help="打印池")
    ap.add_argument("--limit", type=int, default=100, help="池大小上限（bootstrap 时=本次最多跑几只）")
    ap.add_argument("--trade-date", default=None, help="行情交易日 YYYYMMDD")
    ap.add_argument("--rf", type=float, default=None, help="无风险利率（写进池并传给采集器）")
    ap.add_argument("--min-market-cap-yi", type=float, default=None)
    ap.add_argument("--min-turnover-pct", type=float, default=None)
    ap.add_argument("--max-pe", type=float, default=None)
    ap.add_argument("--max-per-industry-frac", type=float, default=None)
    ap.add_argument("--force", action="store_true", help="bootstrap 时不跳过已有产物"
                    "（**仍不会**覆盖含审计成果的目录，见 --allow-overwrite-audited）")
    ap.add_argument("--allow-overwrite-audited", action="store_true",
                    help="⚠️ 危险：允许覆盖含 qualitative_report.md/dims/审核的目录")
    args = ap.parse_args()

    if args.build:
        payload = build_pool(args.limit, output=args.output, rf=args.rf,
                             trade_date=args.trade_date,
                             min_market_cap_yi=args.min_market_cap_yi,
                             min_turnover_pct=args.min_turnover_pct,
                             max_pe=args.max_pe,
                             max_per_industry_frac=args.max_per_industry_frac)
        path = write_pool(payload, args.pool)
        c = payload["counts"]
        print(f"池已写入 {path}")
        print(f"  A 股宇宙（卫生过滤后）{c['universe']} → 行情过滤后 {c['after_quote_filters']} "
              f"→ 入选 {c['picked']}（行业上限剔除 {c['industry_capped']}）")
        return
    if args.bootstrap:
        raise SystemExit(bootstrap(args.pool, rf=args.rf,
                                   skip_existing=not args.force, limit=args.limit,
                                   allow_overwrite_audited=args.allow_overwrite_audited))
    if args.show:
        raise SystemExit(show_pool(args.pool))
    ap.print_help()


if __name__ == "__main__":
    main()
