"""三市场交易日历与「数据就绪」判定。

实测约束（2026-10-14）
----------------------
- ``trade_cal[SSE]`` / ``[SZSE]`` / ``[NYSE]`` ✅ 返回正常（44 行/半月）
- ``trade_cal[HKEX]`` / ``[NASDAQ]`` ❌ **0 行** ⇒ 港股日历必须自建
  （NASDAQ 复用 NYSE 即可；港股只能靠静态节假日表或价格序列反推）

为什么"就绪判定"是必需件
----------------------
日报若在数据未齐时开跑，会拿**昨天的数据当今天**，静默产出错误排名。
实测到的更新时间：
- A 股 ``daily_basic`` 当日数据约 **19:00** 后才完整（仓库 `_get_latest_trade_date`
  里的 ``hour < 19`` 启发式就是这个经验）
- 港股财务指标约 **18:00** 更新
- 美股 16:00 ET 收盘 ⇒ 对应北京时间次日 04:00–05:00（含夏令时切换）

因此本模块把"现在能不能跑某市场"显式化，**判不了就 ABORT**（``calendar_missing`` /
``market_data_not_ready``），绝不用旧数据兜底。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

__all__ = [
    "MARKET_CLOCKS",
    "MarketClock",
    "TradingCalendar",
    "hk_sessions_from_holidays",
    "load_holiday_file",
]

#: 各市场的本地时钟与"数据就绪"时点（本地时间 HH:MM）
MARKET_CLOCKS: dict[str, dict[str, str]] = {
    "A":  {"timezone": "Asia/Shanghai",  "ready_after": "19:00",
           "calendar": "trade_cal[SSE]"},
    "HK": {"timezone": "Asia/Hong_Kong", "ready_after": "18:30",
           "calendar": "自建（trade_cal 无 HKEX）"},
    "US": {"timezone": "America/New_York", "ready_after": "17:00",
           "calendar": "trade_cal[NYSE]"},
}

#: 交易所代码 → 市场
TRADE_CAL_EXCHANGE: dict[str, str] = {"A": "SSE", "US": "NYSE"}


@dataclass(frozen=True)
class MarketClock:
    market: str
    timezone: str
    ready_after: str
    calendar: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def local_now(self, now: Optional[datetime] = None) -> datetime:
        base = now or datetime.now(timezone.utc)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        return base.astimezone(self.tz)

    def ready_minutes(self) -> int:
        hh, mm = self.ready_after.split(":")
        return int(hh) * 60 + int(mm)


def _clock(market: str) -> MarketClock:
    cfg = MARKET_CLOCKS[market]
    return MarketClock(market=market, timezone=cfg["timezone"],
                       ready_after=cfg["ready_after"], calendar=cfg["calendar"])


def is_data_ready(market: str,
                  is_trading_day: Optional[bool],
                  now: Optional[datetime] = None) -> tuple[bool, Optional[str]]:
    """某市场当前是否可跑。返回 ``(ready, reason_code)``。

    - 非交易日 ⇒ ``(False, "market_data_not_ready")``（当天本就不该跑）
    - 交易日但本地时刻早于 ``ready_after`` ⇒ ``(False, "market_data_not_ready")``
    - 交易日且时刻已过 ⇒ ``(True, None)``
    - ``is_trading_day is None``（日历不可用）⇒ ``(False, "calendar_missing")``

    **判不了就不跑** —— 这是 fail-closed 的核心：宁可当天不产出，也不用旧数据假装今天。
    """
    if is_trading_day is None:
        return False, "calendar_missing"
    if not is_trading_day:
        return False, "market_data_not_ready"
    c = _clock(market)
    local = c.local_now(now)
    now_min = local.hour * 60 + local.minute
    if now_min < c.ready_minutes():
        return False, "market_data_not_ready"
    return True, None


# ---------------------------------------------------------------------------
# 交易日历
# ---------------------------------------------------------------------------


def hk_sessions_from_holidays(start: date, end: date,
                              holidays: Iterable[str]) -> list[str]:
    """由静态节假日表生成港股交易日（周一–周五 减去节假日）。

    ``holidays`` 为 ``"YYYY-MM-DD"`` 字符串集合。周末与节假日都休市。
    这是 ``trade_cal`` 无 HKEX 时的**唯一**离线可用来源；节假日表需人工维护
    （香港没有像 A 股那样稳定的公开日历接口）。
    """
    hol = {str(h) for h in holidays}
    out: list[str] = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in hol:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


def load_holiday_file(path: str | Path) -> list[str]:
    """读节假日表。

    接受两种形状：
    - ``{"market": "HK", "holidays": ["2026-01-01", ...]}``
    - ``["2026-01-01", ...]``
    文件缺失或损坏 ⇒ 返回 ``[]``（调用方据此判 ``calendar_missing``，不静默兜底）。
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("holidays") or []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


@dataclass
class TradingCalendar:
    """按市场持有交易日集合。

    ``sessions[market]`` 为 ``set("YYYYMMDD")``；``sources[market]`` 记录来源
    （便于日报回答"港股日历从哪来"）。**缺失的市场不在 dict 里** ——
    查询时返回 ``None`` 而不是空集合（空集合会被误读成"全是休市"）。
    """

    sessions: dict[str, set[str]] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def has(self, market: str) -> bool:
        return bool(self.sessions.get(market))

    def is_trading_day(self, market: str, day: str) -> Optional[bool]:
        """``True``/``False``；日历不可用 ⇒ ``None``（调用方据此 ABORT）。"""
        s = self.sessions.get(market)
        if not s:
            return None
        return str(day) in s

    def last_session(self, market: str, on_or_before: str) -> Optional[str]:
        s = self.sessions.get(market)
        if not s:
            return None
        eligible = [d for d in s if d <= str(on_or_before)]
        return max(eligible) if eligible else None

    # ---- 构造 ----

    @classmethod
    def from_trade_cal_rows(cls, market: str, rows: Iterable[dict],
                            calendar: "TradingCalendar | None" = None) -> "TradingCalendar":
        """由 ``trade_cal`` 的行（``{"cal_date","is_open"}``）构造某市场日历。"""
        cal = calendar or cls()
        sessions = {str(r["cal_date"]) for r in rows
                    if str(r.get("is_open")) in ("1", "1.0", "True", "true")}
        if sessions:
            cal.sessions[market] = sessions
            cal.sources[market] = MARKET_CLOCKS[market]["calendar"]
        return cal

    @classmethod
    def with_hk_holidays(cls, start: date, end: date, holidays: Iterable[str],
                         calendar: "TradingCalendar | None" = None) -> "TradingCalendar":
        cal = calendar or cls()
        sessions = set(hk_sessions_from_holidays(start, end, holidays))
        if sessions:
            cal.sessions["HK"] = sessions
            cal.sources["HK"] = "自建（周一–五 减静态节假日表）"
        return cal

    def readiness(self, market: str, day: str,
                  now: Optional[datetime] = None) -> tuple[bool, Optional[str]]:
        """组合「是否交易日」+「本地时刻是否过就绪点」。"""
        return is_data_ready(market, self.is_trading_day(market, day), now)

    def describe(self) -> str:
        if not self.sessions:
            return "（无可用日历）"
        return "；".join(
            f"{m}: {len(d)} 个交易日（{self.sources.get(m, '未知来源')}）"
            for m, d in sorted(self.sessions.items())
        )
