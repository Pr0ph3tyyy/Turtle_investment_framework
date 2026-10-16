"""每日价格源：直连 Yahoo chart 端点（池 + 候选集）。

实测依据见包 docstring。本模块把三件实测踩到的坑固化下来：

1. 港股最后一个 bar 的 ``close`` 是 ``None`` ⇒ 取**最后一个非空** close，
   并优先 ``meta.regularMarketPrice``（有当日实时价时它更准）。
2. A 股 ticker 是 ``.SS``/``.SZ``（``.SH`` → 404）。
3. 价格必须**落当日快照**，否则重跑不幂等。

可测性
------
抓取函数由 ``fetcher`` 注入（默认走 ``urllib``）。单测注入假 fetcher，
**不联网**即可覆盖全部解析与失败分支。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

__all__ = [
    "CHART_URL",
    "to_yahoo_symbol",
    "PriceQuote",
    "YahooChartClient",
    "FetchFn",
]

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={itv}"

DEFAULT_UA = "Mozilla/5.0 (compatible; turtle-screener/1.0)"

#: 默认并发数（实测 8 并发 30 只 1.82s，0 失败）
DEFAULT_WORKERS = 8

FetchFn = Callable[[str, str, str], dict]


def to_yahoo_symbol(ts_code: str) -> str:
    """Tushare 代码 → Yahoo ticker。

    ``600941.SH`` → ``600941.SS``（Yahoo 用 ``.SS``，``.SH`` 会 404）；
    ``000921.SZ`` → ``000921.SZ``；``06862.HK`` → ``6862.HK``（5 位补零去掉）；
    美股 ``AAPL.US`` → ``AAPL``（裸 ticker）。
    """
    raw = str(ts_code or "").strip().upper()
    if "." not in raw:
        return raw
    code, suffix = raw.rsplit(".", 1)
    if suffix == "SH":
        return f"{code}.SS"
    if suffix == "SZ":
        return f"{code}.SZ"
    if suffix == "HK":
        return f"{code.lstrip('0').zfill(4)}.HK"
    if suffix == "US":
        return code
    return raw


@dataclass
class PriceQuote:
    """单只股票的当日价格。

    ``close`` 为**交易币种**/股；抓不到时 ``close is None`` 且 ``error`` 非空
    （fail-closed：绝不填 0 或沿用昨日价）。
    """

    ts_code: str
    close: Optional[float] = None
    currency: Optional[str] = None
    regular_market_price: Optional[float] = None
    bars: int = 0
    as_of: Optional[str] = None
    source: str = "yahoo_chart"
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.close is not None and self.error is None

    @property
    def price(self) -> Optional[float]:
        """优先实时价（规避港股当日未完成 bar 的 None），否则最后一个非空收盘。"""
        if self.regular_market_price is not None:
            return self.regular_market_price
        return self.close


def _default_fetch(symbol: str, rng: str = "5d", itv: str = "1d",
                   timeout: float = 20.0) -> dict:
    """默认抓取器：``urllib`` GET Yahoo chart 端点，返回原始 JSON。"""
    url = CHART_URL.format(sym=symbol, rng=rng, itv=itv)
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_chart(payload: dict) -> tuple[Optional[float], Optional[float], Optional[str], int, Optional[str]]:
    """解析 Yahoo chart JSON → ``(last_close, regular_market_price, currency, bars, as_of)``。

    ``last_close`` 取**最后一个非空** close —— 当日未完成 bar 的 close 是 ``None``，
    直接取 ``[-1]`` 会让港股恒为 ``None``（这正是仓库里那个已知缺陷的成因）。
    """
    result = (payload or {}).get("chart", {}).get("result") or []
    if not result:
        err = (payload or {}).get("chart", {}).get("error")
        raise ValueError(f"chart 无 result：{err}")
    res = result[0]
    meta = res.get("meta") or {}
    quotes = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = [c for c in (quotes.get("close") or []) if c is not None]
    last_close = closes[-1] if closes else None
    rmp = meta.get("regularMarketPrice")
    currency = meta.get("currency")
    bars = len(quotes.get("close") or [])
    as_of = None
    ts = res.get("timestamp") or []
    if ts:
        try:
            as_of = time.strftime("%Y-%m-%d", time.gmtime(int(ts[-1])))
        except (TypeError, ValueError, OSError):
            as_of = None
    return last_close, rmp, currency, bars, as_of


class YahooChartClient:
    """并发抓取池内价格。

    ``fetcher(symbol, range, interval) -> dict`` 可注入以便离线测试。
    """

    def __init__(self, fetcher: Optional[FetchFn] = None,
                 workers: int = DEFAULT_WORKERS,
                 range_: str = "5d", interval: str = "1d",
                 retries: int = 2, backoff: float = 0.8,
                 throttle: float = 0.0) -> None:
        self._fetch = fetcher or _default_fetch
        self.workers = max(1, int(workers))
        self.range = range_
        self.interval = interval
        self.retries = max(0, int(retries))
        self.backoff = backoff
        self.throttle = throttle
        self.last_stats: dict[str, int] = {}

    # ---- 单只 ----

    def quote(self, ts_code: str) -> PriceQuote:
        symbol = to_yahoo_symbol(ts_code)
        q = PriceQuote(ts_code=ts_code)
        last_err: Optional[str] = None
        for attempt in range(self.retries + 1):
            try:
                payload = self._fetch(symbol, self.range, self.interval)
                close, rmp, ccy, bars, as_of = parse_chart(payload)
                if close is None and rmp is None:
                    q.error = "empty_price"
                    return q
                q.close, q.regular_market_price = close, rmp
                q.currency, q.bars, q.as_of = ccy, bars, as_of
                if self.throttle:
                    time.sleep(self.throttle)
                return q
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code}"
            except urllib.error.URLError as exc:
                last_err = f"URLError {exc.reason}"
            except (ValueError, json.JSONDecodeError) as exc:
                last_err = f"parse: {exc}"
            except Exception as exc:                      # noqa: BLE001 - 兜底但不吞
                last_err = f"{type(exc).__name__}: {exc}"
            if attempt < self.retries:
                time.sleep(self.backoff * (attempt + 1))
        q.error = last_err or "unknown"
        return q

    # ---- 批量 ----

    def quotes(self, ts_codes: Sequence[str]) -> dict[str, PriceQuote]:
        """并发抓取。返回 ``{ts_code: PriceQuote}``，失败的也在里面（带 ``error``）。

        **不抛异常、不静默丢股** —— 调用方按 reason code 处置。
        """
        codes = list(dict.fromkeys(str(c) for c in ts_codes))
        if not codes:
            self.last_stats = {"requested": 0, "ok": 0, "failed": 0}
            return {}
        out: dict[str, PriceQuote] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for code, q in zip(codes, pool.map(self.quote, codes)):
                out[code] = q
        self.last_stats = {
            "requested": len(codes),
            "ok": sum(1 for q in out.values() if q.ok),
            "failed": sum(1 for q in out.values() if not q.ok),
        }
        return out

    # ---- 当日快照（幂等） ----

    def snapshot_payload(self, ts_codes: Iterable[str]) -> dict:
        """把抓取结果转成可落盘的当日快照（重跑读快照 ⇒ 幂等）。"""
        qs = self.quotes(list(ts_codes))
        return {
            "schema_version": "1.0",
            "source": "yahoo_chart",
            "stats": dict(self.last_stats),
            "quotes": {
                code: {
                    "close": q.close,
                    "regular_market_price": q.regular_market_price,
                    "currency": q.currency,
                    "bars": q.bars,
                    "as_of": q.as_of,
                    "error": q.error,
                } for code, q in qs.items()
            },
        }

    @staticmethod
    def from_snapshot(payload: dict) -> dict[str, PriceQuote]:
        """读当日快照 → ``{ts_code: PriceQuote}``。"""
        return {
            code: PriceQuote(
                ts_code=code,
                close=rec.get("close"),
                regular_market_price=rec.get("regular_market_price"),
                currency=rec.get("currency"),
                bars=int(rec.get("bars") or 0),
                as_of=rec.get("as_of"),
                error=rec.get("error"),
            )
            for code, rec in (payload.get("quotes") or {}).items()
        }
