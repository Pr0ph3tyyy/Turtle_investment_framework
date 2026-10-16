"""报价层 —— 价格/成交额的多源取数与**来源报告**。

数据源取舍（2026-10-15 用户拍板）
---------------------------------
- **主源 Yahoo chart**：不占 Tushare 配额，批量并发（实测 30 只/8 线程/1.82s/0 失败）
- **备选 东财 `clist`**：一次拿全市场（含 PE/PB/市值/成交额/换手），但**无 SLA**
  （实测：能用，但随后 5 个镜像全部 502、20s 重试仍失败）
  ⇒ **一旦使用东财，必须在输出的来源报告里显式声明**（用户要求）

为什么不与 analysis 抢 Tushare
------------------------------
`hk_daily` 只有 **5 次/天**，而采集器的港股 §1/§2/§11 依赖它（实证：`hk_daily` 配额用尽
→ §1 当前价格 "—"、§2/§11 缺失、§17.11/13/14 网格连带消失）。
⇒ **选股器一次都不调 `hk_daily`**，把它完整留给 analysis。
"""

from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

__all__ = ["Quote", "SourceReport", "to_yahoo_symbol", "fetch_yahoo_batch",
           "fetch_eastmoney_hk", "report_date_close"]

_YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

_EM_URL = ("https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz={pz}&po=1&np=1"
           "&fltt=2&invt=2&fid=f6&fs=m:116+t:3&fields=f12,f14,f2,f3,f5,f6,f8,f9,f20,f21,f23")
_EM_HEADERS = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}


@dataclass
class Quote:
    code: str
    close: Optional[float] = None
    volume: Optional[float] = None
    currency: Optional[str] = None
    as_of: Optional[str] = None
    source: str = ""

    @property
    def amount(self) -> Optional[float]:
        """成交额 ≈ 量 × 价（Yahoo 只给量）。单位与 price 一致。"""
        if self.volume and self.close:
            return self.volume * self.close
        return None


@dataclass
class SourceReport:
    """**来源报告** —— 谁被用了、为什么、是否偏离首选。用户明确要求。"""

    primary: str = "yahoo"
    used: dict[str, str] = field(default_factory=dict)
    deviations: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def note(self, stage: str, source: str, *, count: int, detail: str = "") -> None:
        self.used[stage] = source
        if source != self.primary and stage not in ("fundamentals", "static"):
            msg = f"{stage}: 使用备选源 {source}（首选 {self.primary}）"
            if detail:
                msg += f" —— {detail}"
            self.deviations.append(msg)

    def fail(self, stage: str, detail: str) -> None:
        self.failures.append(f"{stage}: {detail}")

    def as_dict(self) -> dict[str, Any]:
        return {"primary_intended": self.primary, "sources_used": self.used,
                "deviations": self.deviations, "failures": self.failures}

    def headline(self) -> str:
        if not self.deviations and not self.failures:
            return f"数据源：全部使用首选源（{self.primary}）"
        parts = []
        if self.deviations:
            parts.append("偏离首选：" + "；".join(self.deviations))
        if self.failures:
            parts.append("失败：" + "；".join(self.failures))
        return "⚠️ 数据源报告 —— " + " | ".join(parts)


# ───────────────────────── Yahoo ─────────────────────────

def to_yahoo_symbol(ts_code: str) -> str:
    """``00700.HK`` → ``0700.HK``；``600887.SH`` → ``600887.SS``；``AAPL.US`` → ``AAPL``。

    港股要去掉前导零到 4 位（Yahoo 用 4 位）；A 股后缀 SH→SS。**实测已用于 30 只港股无失败。**
    """
    code, _, suffix = ts_code.partition(".")
    s = suffix.upper()
    if s == "HK":
        digits = code.lstrip("0") or "0"
        return f"{digits.zfill(4)}.HK"
    if s == "SH":
        return f"{code}.SS"
    if s in ("SZ", "BJ"):
        return f"{code}.{s}"
    return code                     # 美股：裸代码


def _parse_chart(payload: dict, code: str) -> Quote:
    res = ((payload or {}).get("chart") or {}).get("result") or []
    if not res:
        return Quote(code=code, source="yahoo:chart")
    r0 = res[0]
    meta = r0.get("meta") or {}
    close = meta.get("regularMarketPrice")
    if close is None:
        try:
            closes = ((r0.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
            closes = [c for c in closes if c is not None]
            close = closes[-1] if closes else None
        except (IndexError, AttributeError):
            close = None
    vol = meta.get("regularMarketVolume")
    if vol is None:
        try:
            vols = ((r0.get("indicators") or {}).get("quote") or [{}])[0].get("volume") or []
            vols = [v for v in vols if v is not None]
            vol = vols[-1] if vols else None
        except (IndexError, AttributeError):
            vol = None
    ts = meta.get("regularMarketTime")
    as_of = None
    if ts:
        from datetime import datetime, timezone
        as_of = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    return Quote(code=code, close=close, volume=vol,
                 currency=meta.get("currency"), as_of=as_of, source="yahoo:chart")


def fetch_yahoo_batch(codes: Iterable[str], *, workers: int = 8,
                      fetch: Optional[Callable[[str], dict]] = None) -> dict[str, Quote]:
    """并发取 Yahoo chart。单只失败不影响其余（返回空 Quote）。"""
    codes = list(codes)

    def one(code: str) -> Quote:
        sym = to_yahoo_symbol(code)
        try:
            if fetch is not None:
                payload = fetch(sym)
            else:
                req = urllib.request.Request(_YF_CHART.format(sym=sym), headers={"User-Agent": _UA})
                with urllib.request.urlopen(req, timeout=15) as r:
                    payload = json.loads(r.read().decode())
            return _parse_chart(payload, code)
        except Exception:                                     # noqa: BLE001
            return Quote(code=code, source="yahoo:chart")

    out: dict[str, Quote] = {}
    if not codes:
        return out
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for q in ex.map(one, codes):
            out[q.code] = q
    return out


# ───────────────────────── 东财（备选） ─────────────────────────

def fetch_eastmoney_hk(*, pages: int = 30, page_size: int = 100, sleep: float = 0.2,
                       fetch: Optional[Callable[[str], dict]] = None) -> tuple[dict[str, Quote], dict]:
    """东财 `clist` 批量取港股。返回 ``(quotes, extras)``。

    ``extras`` 带 **PE/PB/市值/成交额/换手** —— 这是东财唯一优于 Yahoo 的地方
    （Yahoo chart 只有价与量）。**调用方必须把它记进来源报告。**
    """
    import time
    quotes: dict[str, Quote] = {}
    extras: dict[str, dict] = {}
    total = None
    for pn in range(1, pages + 1):
        url = _EM_URL.format(pn=pn, pz=page_size)
        try:
            if fetch is not None:
                payload = fetch(url)
            else:
                req = urllib.request.Request(url, headers=_EM_HEADERS)
                with urllib.request.urlopen(req, timeout=25) as r:
                    payload = json.loads(r.read().decode())
        except Exception as exc:                              # noqa: BLE001
            if not quotes:
                raise RuntimeError(f"东财取数失败：{type(exc).__name__}: {exc}") from exc
            break
        data = payload.get("data") or {}
        if total is None:
            total = data.get("total")
        diff = data.get("diff") or []
        if not diff:
            break
        for raw in diff:
            code = str(raw.get("f12") or "").strip()
            if not code:
                continue
            ts_code = f"{code}.HK"
            quotes[ts_code] = Quote(
                code=ts_code, close=_num(raw.get("f2")), volume=_num(raw.get("f5")),
                currency="HKD", source="eastmoney:clist")
            extras[ts_code] = {
                "name": raw.get("f14"), "pe_ttm": _num(raw.get("f9")),
                "pb": _num(raw.get("f23")), "total_mv": _num(raw.get("f20")),
                "circ_mv": _num(raw.get("f21")), "amount": _num(raw.get("f6")),
                "turnover_rate": _num(raw.get("f8")), "pct_chg": _num(raw.get("f3")),
            }
        if total is not None and len(quotes) >= total:
            break
        time.sleep(sleep)
    return quotes, extras


def _num(v: Any) -> Optional[float]:
    """``-``/``""``/``None``/NaN 一律 None（**NaN 是真值**这个陷阱本仓库已吃过亏）。"""
    if v is None or v == "-" or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


_YF_HIST = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
            "?range=5y&interval=1mo")


def report_date_close(ts_code: str, end_date: str, *,
                      fetch: Optional[Callable[[str], dict]] = None) -> Optional[float]:
    """取**报告期附近**（同月或之前最近一个月）的收盘价。

    用途：港股没有申报股数字段（`hk_balancesheet` 的「股本」是**股本金额**不是股数），
    只能 `股数 = 期末市值 ÷ 期末价` 反推。而 `pe_ttm × eps_ttm` 反推不可靠
    （实测 06862：得 63.04 亿股，而年报申报 55.74 亿股，**差 13%** —— 因为 pe/pb 与
    `total_market_cap` 可能不同期）。用同一支股票在**报告期当月**的真实收盘价更可靠。

    走 **Yahoo**（不占 Tushare 配额）。
    """
    sym = to_yahoo_symbol(ts_code)
    try:
        if fetch is not None:
            payload = fetch(sym)
        else:
            req = urllib.request.Request(_YF_HIST.format(sym=sym), headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                payload = json.loads(r.read().decode())
    except Exception:                                          # noqa: BLE001
        return None
    res = ((payload or {}).get("chart") or {}).get("result") or []
    if not res:
        return None
    r0 = res[0]
    stamps = r0.get("timestamp") or []
    closes = ((r0.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    if not stamps or not closes:
        return None
    from datetime import datetime, timezone
    target = str(end_date or "")[:8]
    best = None
    for ts_, c in zip(stamps, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(ts_, tz=timezone.utc).strftime("%Y%m%d")
        if d <= target and (best is None or d >= best[0]):
            best = (d, float(c))
    return best[1] if best else None
