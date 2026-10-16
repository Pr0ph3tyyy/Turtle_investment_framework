"""全市场批量取数 —— A 股走 Tushare，港股走东财。

为什么这个模块存在（设计取舍）
------------------------------
选股器的需求是「用**宽松指标**从全市场筛出 ~100 只」，供人挑选后再跑 analysis。
⇒ 一切指标必须来自**批量**接口，**不得**依赖逐只的 §17 数据包
   （那需要每只约 55 秒，正是旧设计复杂度的全部来源）。

两个市场的批量能力**差异很大**，必须分别处理：

| | A 股 | 港股 |
|---|---|---|
| 批量估值 | ✅ `daily_basic` 一次 5,561 行（含 PE/PB/股息/换手/市值） | ❌ Tushare **没有** `hk_daily_basic` |
| 价格/量 | ✅ 同上 | `hk_daily` 但**仅 5 次/天** |
| 本模块采用 | **Tushare `daily_basic`** | **东财 `push2` 批量**（一次 2,623 行，含 PE/PB/市值/成交额/换手） |

⚠️ **港股刻意不用 `hk_daily`**：它每天只有 5 次，而采集器的港股 §1/§2/§11 依赖它
（实证：`derived_metrics._get_mktcap_shares` 注释记载 2026-10-13 `hk_daily` 配额用尽
→ §1 当前价格 "—"、§2/§11 数据缺失、§17.11/13/14 网格连带消失）。
选股器若也调它，会**再次打断 analysis** ⇒ 港股一律走东财，不占 Tushare 配额。
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = ["Row", "fetch_a", "hygiene"]

@dataclass
class Row:
    """一只标的的**批量可得**字段。缺的值一律 ``None``，绝不补 0。"""

    code: str
    name: str
    market: str
    close: Optional[float] = None
    volume: Optional[float] = None
    pct_chg: Optional[float] = None
    amount: Optional[float] = None            # 成交额（元）
    turnover_rate: Optional[float] = None     # %
    pe_ttm: Optional[float] = None
    pb: Optional[float] = None
    total_mv: Optional[float] = None          # 元
    circ_mv: Optional[float] = None           # 元
    dv_ttm: Optional[float] = None            # 股息率 %（**仅 A 股有**）
    industry: Optional[str] = None            # **仅 A 股有**
    list_date: Optional[str] = None
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _num(v: Any) -> Optional[float]:
    """东财/Tushare 的缺失值可能是 ``-``、``""``、``None``、NaN。一律归 None。

    ⚠️ **NaN 是真值**：`float('nan') or 0` 返回 NaN，`if v:` 对 NaN **成立**。
    本仓库已因这个陷阱吃过一次大亏（`stock_basic.delist_date` 是 NaN ⇒ 全宇宙被判退市）。
    """
    if v is None or v == "-" or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _text(v: Any) -> str:
    if v is None:
        return ""
    t = str(v).strip()
    return "" if t.lower() in ("nan", "none", "-") else t


# ─────────────────────────── A 股：Tushare ───────────────────────────

def fetch_a(trade_date: str, *, token: str | None = None,
            pro=None) -> list[Row]:
    """A 股：`stock_basic`（静态）+ `daily_basic`（当日，**一次调用覆盖全市场**）。

    两个接口各 1 次调用 ⇒ 整个 A 股选股只需 2 次 API。
    """
    if pro is None:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from config import get_token
        import tushare as ts
        pro = ts.pro_api(token or get_token())

    basic = pro.query("stock_basic", fields="ts_code,name,industry,list_date,delist_date")
    daily = pro.query("daily_basic", trade_date=trade_date,
                      fields="ts_code,close,turnover_rate,pe_ttm,pb,total_mv,circ_mv,dv_ttm")
    if daily is None or getattr(daily, "empty", True):
        raise RuntimeError(f"daily_basic({trade_date}) 为空 —— 非交易日或数据未就绪"
                           "（A 股当日数据约 19:00 后才完整）")
    meta = {r["ts_code"]: r for r in (basic.to_dict("records") if basic is not None else [])}
    out: list[Row] = []
    for r in daily.to_dict("records"):
        code = _text(r.get("ts_code"))
        m = meta.get(code) or {}
        out.append(Row(
            code=code, name=_text(m.get("name")), market="A",
            close=_num(r.get("close")), turnover_rate=_num(r.get("turnover_rate")),
            amount=None, pe_ttm=_num(r.get("pe_ttm")), pb=_num(r.get("pb")),
            total_mv=(_num(r.get("total_mv")) or 0) * 10000 if _num(r.get("total_mv")) else None,
            circ_mv=(_num(r.get("circ_mv")) or 0) * 10000 if _num(r.get("circ_mv")) else None,
            dv_ttm=_num(r.get("dv_ttm")), industry=_text(m.get("industry")),
            list_date=_text(m.get("list_date")), source="tushare:daily_basic+stock_basic",
        ))
    return out


# ─────────────────────────── 卫生过滤 ───────────────────────────

def hygiene(rows: Iterable[Row], *, today: Optional[date] = None,
            min_listing_years: int = 3) -> tuple[list[Row], dict[str, int]]:
    """逐市场卫生过滤。返回 ``(kept, excluded_by_reason)``。

    排除项**必须计数** —— 这是"为什么只有 100 只"的可核对答案的一部分。
    """
    today = today or datetime.now().date()
    kept, why = [], {}
    for r in rows:
        name = r.name or ""
        # ST / 退市 / 停牌
        if "ST" in name.upper() or "退" in name:
            why["st_or_delisted"] = why.get("st_or_delisted", 0) + 1
            continue
        if not r.close or r.close <= 0:
            why["no_price"] = why.get("no_price", 0) + 1
            continue
        # 上市年限
        if r.list_date and len(r.list_date) >= 8:
            try:
                listed = datetime.strptime(r.list_date[:8], "%Y%m%d").date()
                if (today - listed).days < min_listing_years * 365:
                    why["too_young"] = why.get("too_young", 0) + 1
                    continue
            except ValueError:
                pass
        kept.append(r)
    return kept, why
