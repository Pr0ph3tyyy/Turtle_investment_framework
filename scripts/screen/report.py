"""输出层 —— CSV（给程序/表格）+ Markdown（给人看）+ **来源报告**。

为什么 Markdown 里要有「漏斗」和「来源」两块
--------------------------------------------
用户最初的疑问是「为什么是从 100 只左右的池子里筛选」。答案必须**写在产物里**，
而不是靠人解释：所以每次输出都带一行

    全市场 N → 卫生后 N1 → 粗筛 N2 → 精筛 N3 → 取前 K

以及被剔除最多的几类原因。**没有这一行，任何"我们看了全市场"的说法都无法核对。**
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = ["write_candidates"]

_COLUMNS = ("rank", "ts_code", "name", "market", "industry", "close", "total_mv_yi",
            "pe_ttm", "pb", "dividend_yield_pct", "turnover_rate", "amount",
            "aa_yi", "m_pess_pct", "gg_pess_pct", "ii_pct", "safety_margin",
            "roe_avg", "gross_profit_ratio", "debt_asset_ratio", "fy_end",
            "years_used", "degraded", "warnings", "reason_codes", "close_source", "fundamental_source")


def _is_quick(obj) -> bool:
    """判断传入的是 `turtle_quick.QuickTurtle`（有安全边际）还是 `hk.HkFundamental`。"""
    return obj is not None and hasattr(obj, "safety_margin")


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Y" if v else "N"
    if isinstance(v, (int, float)):
        return f"{v:,.{nd}f}"
    return str(v)


def write_candidates(out_dir: str | Path, *, market: str, as_of: str,
                     rows: list[Any], rules: Any, funnel: list[tuple[str, int]],
                     exclusions: dict[str, int], source: Any,
                     fundamentals: Optional[dict] = None,
                     extras: Optional[dict] = None,
                     title_note: str = "") -> tuple[Path, Path]:
    """落盘 ``candidates_{market}.csv`` 与 ``candidates_{market}.md``。"""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    fundamentals = fundamentals or {}
    extras = extras or {}

    # ── 组装行 ──
    recs = []
    for i, r in enumerate(rows, 1):
        code = getattr(r, "code", "")
        f = fundamentals.get(code)
        x = extras.get(code) or {}
        # `qt` 必须在**任何**用到龟龟量的地方之前定义（此前放在 mv 之前，
        # 而 dy 的计算更早 ⇒ UnboundLocalError）
        qt = f if _is_quick(f) else None
        price = getattr(r, "close", None)
        dy = getattr(qt, "dividend_yield_pct", None) if qt is not None else None
        if dy is not None:
            pass
        elif getattr(r, "dv_ttm", None) is not None:
            dy = r.dv_ttm
        elif x.get("dv_ttm") is not None:
            dy = x["dv_ttm"]
        elif f is not None:
            try:
                dy = f.dividend_yield(price)
            except AttributeError:
                dps = getattr(f, "dps_hkd", None)
                dy = (dps / price * 100) if (dps and price) else None
        qt = f if _is_quick(f) else None
        # 市值：A 股取 `row.total_mv`（daily_basic 当日口径）；港股**没有**该字段，
        # 取 `QuickTurtle.mkt_cap`（已折成报表币种·当日口径，见 hk_current_cap_reporting）
        mv = (getattr(r, "total_mv", None) or x.get("total_mv")
              or (qt.mkt_cap if qt is not None else None))
        recs.append({
            "rank": i, "ts_code": code, "name": getattr(r, "name", "") or x.get("name", ""),
            "market": market, "industry": getattr(r, "industry", "") or "",
            "close": price, "total_mv_yi": (mv / 1e8) if mv else None,
            "pe_ttm": getattr(r, "pe_ttm", None) or x.get("pe_ttm")
                      or (getattr(f, "pe_ttm", None) if (f and not qt) else None),
            "pb": getattr(r, "pb", None) or x.get("pb")
                  or (getattr(f, "pb_ttm", None) if (f and not qt) else None),
            "dividend_yield_pct": dy,
            "turnover_rate": getattr(r, "turnover_rate", None) or x.get("turnover_rate"),
            "amount": getattr(r, "amount", None) or x.get("amount"),
            "roe_avg": getattr(f, "roe_avg", None) if (f and not qt) else None,
            "gross_profit_ratio": (getattr(f, "gross_profit_ratio", None)
                                   if (f and not qt) else None),
            "debt_asset_ratio": (getattr(f, "debt_asset_ratio", None)
                                 if (f and not qt) else None),
            # ── 龟龟的四个量（**这才是排序依据**；`quick` 是 turtle_quick.QuickTurtle）──
            "aa_yi": (qt.aa / 1e8) if (qt and qt.aa) else None,
            "m_pess_pct": qt.m_pess if qt else None,
            "gg_pess_pct": qt.gg_pess if qt else None,
            "ii_pct": qt.ii if qt else None,
            "safety_margin": qt.safety_margin if qt else None,
            "years_used": getattr(qt, "years_used", None) if qt else None,
            "degraded": bool(getattr(qt, "degraded", False)) if qt else False,
            "warnings": " ; ".join(getattr(qt, "warnings", []) or []) if qt else "",
            "reason_codes": "|".join(getattr(qt, "reasons", []) or []) if qt else "",
            "fy_end": (getattr(qt, "fy_end", "") if qt else
                       (getattr(f, "end_date", "") if f else "")),
            "close_source": getattr(getattr(r, "source", ""), "split", lambda *_: [""])(":")[0]
                            if isinstance(getattr(r, "source", None), str) else "",
            "fundamental_source": "tushare:hk_fina_indicator" if f else "",
        })

    # ── CSV ──
    csv_path = d / f"candidates_{market.lower()}_{as_of}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(_COLUMNS))
        w.writeheader()
        for rec in recs:
            w.writerow({k: ("" if rec.get(k) is None else rec.get(k)) for k in _COLUMNS})

    # ── Markdown ──
    md_path = d / f"candidates_{market.lower()}_{as_of}.md"
    lines = []
    lines.append(f"# 选股候选 · {market} · {as_of}\n")
    lines.append(f"> 规则：宽松指标两阶段筛选 · 粗筛目标 {rules.stage1_target} · 最终取前 {rules.limit}")
    if title_note:
        lines.append(f"> {title_note}")
    lines.append(f"> 生成时间 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

    # 漏斗
    lines.append("## 漏斗（这是「为什么只剩这些」的可核对答案）\n")
    lines.append("| 阶段 | 只数 |")
    lines.append("|---|---:|")
    for label, n in funnel:
        lines.append(f"| {label} | {n:,} |")
    lines.append("")
    if exclusions:
        lines.append("**剔除原因分布**：" + " · ".join(
            f"{k} {v:,}" for k, v in list(exclusions.items())[:8]))
        lines.append("")

    # 来源报告
    lines.append("## 数据源\n")
    lines.append(f"- {source.headline()}")
    for k, v in (source.used or {}).items():
        lines.append(f"  - `{k}` → `{v}`")
    lines.append("")

    # 表格
    lines.append(f"## 候选名单（{len(recs)} 只）\n")
    n_deg = sum(1 for r_ in recs if r_.get("degraded"))
    lines.append("| # | 代码 | 名称 | 行业 | 现价 | 市值(亿) | **安全边际** | GG_pess% | II% "
                 "| M_pess% | AA(亿) | 股息率% | 年数 | 降级 |")
    lines.append("|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:--:|")
    for rec in recs:
        lines.append("| {rank} | {ts_code} | {name} | {industry} | {close} | {mv} "
                     "| **{m}** | {gg} | {ii} | {mp} | {aa} | {dy} | {yr} | {dg} |".format(
                         rank=rec["rank"], ts_code=rec["ts_code"], name=rec["name"],
                         industry=rec["industry"], close=_fmt(rec["close"]),
                         mv=_fmt(rec["total_mv_yi"], 0),
                         m=_fmt(rec["safety_margin"], 2), gg=_fmt(rec["gg_pess_pct"]),
                         ii=_fmt(rec["ii_pct"]), mp=_fmt(rec["m_pess_pct"]),
                         aa=_fmt(rec["aa_yi"], 0), dy=_fmt(rec["dividend_yield_pct"]),
                         yr=rec["years_used"] if rec["years_used"] is not None else "",
                         dg=("⚠️降级" if rec.get("degraded") else "")))

    lines.append("")
    lines.append("> **安全边际 = GG_pess − II** —— 这是龟龟的决策变量（不是自造评分）。")
    lines.append("> GG_pess = (AA × k × M_pess/100 × (1−Q) + O) / 市值 × 100，k=0.8。")
    lines.append("> ⚠️ 本表是**粗筛近似**（M 用三法均值，未含完整阶梯/承诺锚定）；"
                 "权威数字请以 analysis 模块为准。")
    if n_deg:
        lines.append("")
        lines.append(f"### ⚠️ 降级标的 {n_deg} 只（M_pess > 100%）")
        lines.append("")
        lines.append("这些标的的 `M_pess > 100%`，即 `AA × M/100 > AA` —— **相当于把可支配现金的"
                     "数倍分掉**，GG 读数是公式外推的结果，**经济上不可持续**。")
        lines.append("按用户 2026-10-15 决定：**M 不封顶**（与 analysis 同语义），但**必须告警并降级**。")
        for rec in recs:
            if rec.get("degraded"):
                lines.append(f"- `{rec['ts_code']} {rec['name']}` — {rec['warnings']}")
    lines.append("")
    lines.append("> 下一步：从本表挑你感兴趣的，手动跑 analysis（本选股器与 analysis 无任何文件耦合）。")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 来源报告（机器可读）
    (d / f"source_report_{market.lower()}_{as_of}.json").write_text(
        json.dumps({"market": market, "as_of": as_of,
                    "rules": asdict(rules), "funnel": funnel,
                    "exclusions": exclusions, "source": source.as_dict()},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return csv_path, md_path
