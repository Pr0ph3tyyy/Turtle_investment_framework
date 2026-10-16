"""三市场宇宙枚举 + 洁净度过滤。

实测（2026-10-14）
-----------------
- ``stock_basic()`` → **5574** A 股
- ``hk_basic()``    → **2788** 港股
- ``us_basic()``    → 单页上限 **6000**，且 **offset 翻页可用** ⇒ 美股总量 ≥12000

美股宇宙必须做洁净度过滤（实测踩到）
--------------------------------
``us_basic.classify`` 分布（前 6000）：``EQ`` 5026 / ``ADR`` 555 / ``PF`` 385 / ``GDR`` 34，
且列表里**混入非美股**（``035420`` = NAVER 韩股）与权证/单位（``D06P59``）。
不过滤的话 T0 会被垃圾标的淹没，且"美股"这个标签本身失真。

排除一律带 :mod:`turtle_core.reasons` 受控原因码 —— 没有原因码的排除不可诊断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Optional, Sequence

from turtle_core.reasons import require

__all__ = [
    "UniverseRow",
    "UniverseSource",
    "FetchRecords",
    "DEFAULT_KEEP_CLASSIFY",
    "apply_hygiene",
    "is_st_name",
]

#: 保留的美股证券类别。``PF``（优先股/基金）与 ``GDR`` 参与龟龟链无意义。
DEFAULT_KEEP_CLASSIFY: tuple[str, ...] = ("EQ", "ADR")

#: ``us_basic`` 单页上限（实测：limit=12000 仍返回 6000）
US_PAGE_SIZE = 6000

#: A 股风险标记（名称匹配即排除）
ST_MARKERS = ("*ST", "ST", "PT", "退市")

#: fetcher(api_name, **kwargs) -> list[record]
FetchRecords = Callable[..., list[dict]]


def _txt(value) -> str:
    """把上游返回的缺失值归一成空串。

    ⚠️ **NaN 是真值** —— `float('nan') or ""` 返回 NaN，于是
    `if r.delist_date:` 对**所有在上市股票**都成立，整个宇宙被误判为「已退市」。
    Tushare 的 `stock_basic` 对在上市股票正是返回 NaN 的 `delist_date`（实测 5574 只全被排除）。
    """
    if value is None:
        return ""
    try:
        if value != value:          # NaN
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "nat") else text


def is_st_name(name: Optional[str]) -> bool:
    """名称是否含风险标记。``ST`` 匹配置于 ``*ST`` 之后无影响，但顺序保证可读性。"""
    if not name:
        return False
    up = str(name).upper()
    return any(m.upper() in up for m in ST_MARKERS)


@dataclass
class UniverseRow:
    ts_code: str
    name: str = ""
    market: str = "A"
    industry: str = ""
    list_date: str = ""
    classify: str = ""
    delist_date: str = ""


@dataclass
class HygieneResult:
    kept: list[UniverseRow] = field(default_factory=list)
    #: ts_code → 受控原因码
    excluded: dict[str, str] = field(default_factory=dict)
    #: 原因码 → 计数（日报的"排除原因分布"）
    counts: dict[str, int] = field(default_factory=dict)

    def tally(self) -> "HygieneResult":
        counts: dict[str, int] = {}
        for code in self.excluded.values():
            require(code)
            counts[code] = counts.get(code, 0) + 1
        self.counts = counts
        return self


def apply_hygiene(rows: Iterable[UniverseRow],
                  today: Optional[date] = None,
                  min_listing_years: int = 3,
                  keep_classify: Sequence[str] = DEFAULT_KEEP_CLASSIFY,
                  market: str = "A") -> HygieneResult:
    """宇宙卫生过滤：ST / 退市 / 上市年限 / 美股证券类别。

    每个排除都带受控原因码（``st`` / ``delisted`` / ``too_young`` / ``non_equity_class``）。
    """
    today = today or datetime.now().date()
    cutoff = (today - timedelta(days=int(min_listing_years) * 365)).strftime("%Y%m%d")
    keep_cls = {c.upper() for c in keep_classify}
    res = HygieneResult()

    for r in rows:
        code = str(r.ts_code)
        if r.delist_date:
            res.excluded[code] = "delisted"
            continue
        if is_st_name(r.name):
            res.excluded[code] = "st"
            continue
        if market == "US" and r.classify and r.classify.upper() not in keep_cls:
            res.excluded[code] = "non_equity_class"
            continue
        if min_listing_years > 0 and r.list_date and str(r.list_date) > cutoff:
            res.excluded[code] = "too_young"
            continue
        res.kept.append(r)
    return res.tally()


class UniverseSource:
    """按市场枚举宇宙。``fetcher`` 可注入以便离线测试。"""

    def __init__(self, fetcher: FetchRecords, page_size: int = US_PAGE_SIZE,
                 max_pages: int = 10) -> None:
        self._fetch = fetcher
        self.page_size = page_size
        self.max_pages = max_pages
        self.last_stats: dict[str, int] = {}

    # ---- 各市场 ----

    def fetch_a(self) -> list[UniverseRow]:
        rows = self._fetch("stock_basic",
                           fields="ts_code,name,industry,list_date,delist_date")
        return [UniverseRow(ts_code=_txt(r.get("ts_code")), name=_txt(r.get("name")),
                            market="A", industry=_txt(r.get("industry")),
                            list_date=_txt(r.get("list_date")),
                            delist_date=_txt(r.get("delist_date")))
                for r in rows if _txt(r.get("ts_code"))]

    def fetch_hk(self) -> list[UniverseRow]:
        rows = self._fetch("hk_basic", fields="ts_code,name,list_date,delist_date")
        return [UniverseRow(ts_code=_txt(r.get("ts_code")), name=_txt(r.get("name")),
                            market="HK", list_date=_txt(r.get("list_date")),
                            delist_date=_txt(r.get("delist_date")))
                for r in rows if _txt(r.get("ts_code"))]

    def fetch_us(self) -> list[UniverseRow]:
        """``us_basic`` 按 offset 翻页（单页上限 6000，实测 offset 生效）。"""
        out: list[UniverseRow] = []
        seen: set[str] = set()
        for page in range(self.max_pages):
            rows = self._fetch("us_basic",
                               fields="ts_code,name,classify,list_date,delist_date",
                               limit=self.page_size, offset=page * self.page_size)
            if not rows:
                break
            for r in rows:
                code = r.get("ts_code")
                if not code or code in seen:
                    continue
                seen.add(code)
                out.append(UniverseRow(
                    ts_code=code, name=_txt(r.get("name")), market="US",
                    classify=_txt(r.get("classify")).upper(),
                    list_date=_txt(r.get("list_date")),
                    delist_date=_txt(r.get("delist_date"))))
            if len(rows) < self.page_size:
                break
        return out

    def fetch(self, markets: Sequence[str]) -> dict[str, list[UniverseRow]]:
        out: dict[str, list[UniverseRow]] = {}
        for m in markets:
            if m == "A":
                out["A"] = self.fetch_a()
            elif m == "HK":
                out["HK"] = self.fetch_hk()
            elif m == "US":
                out["US"] = self.fetch_us()
            else:
                raise ValueError(f"未知市场 {m!r}；支持 A/HK/US")
        self.last_stats = {m: len(v) for m, v in out.items()}
        return out

    def clean(self, markets: Sequence[str] = ("A", "HK", "US"),
              today: Optional[date] = None,
              min_listing_years: int = 3) -> dict[str, HygieneResult]:
        """枚举 + 卫生过滤。返回 ``{market: HygieneResult}``。"""
        raw = self.fetch(markets)
        return {
            m: apply_hygiene(rows, today=today, min_listing_years=min_listing_years,
                             market=m)
            for m, rows in raw.items()
        }
