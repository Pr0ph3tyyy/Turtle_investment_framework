"""turtle_screener.calendar —— 三市场日历与就绪判定测试（全离线）。"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from turtle_screener import calendar as CAL  # noqa: E402


class TestMarketClocks:
    def test_three_markets_defined(self):
        assert set(CAL.MARKET_CLOCKS) == {"A", "HK", "US"}

    def test_timezones(self):
        assert CAL.MARKET_CLOCKS["A"]["timezone"] == "Asia/Shanghai"
        assert CAL.MARKET_CLOCKS["HK"]["timezone"] == "Asia/Hong_Kong"
        assert CAL.MARKET_CLOCKS["US"]["timezone"] == "America/New_York"

    def test_hk_calendar_is_declared_self_built(self):
        """trade_cal 无 HKEX（实测 0 行）—— 这一点必须写在配置里，不能靠记忆。"""
        assert "自建" in CAL.MARKET_CLOCKS["HK"]["calendar"]
        assert "HK" not in CAL.TRADE_CAL_EXCHANGE

    def test_local_now_converts(self):
        clock = CAL.MarketClock("A", "Asia/Shanghai", "19:00", "trade_cal[SSE]")
        # 2026-10-14 11:00 UTC = 19:00 CST
        local = clock.local_now(datetime(2026, 10, 14, 11, 0, tzinfo=timezone.utc))
        assert (local.hour, local.minute) == (19, 0)


class TestDataReadiness:
    def test_unknown_calendar_aborts_with_calendar_missing(self):
        """判不了就不跑 —— 日历不可用是 fail-closed，不是"当作休市"。"""
        ready, why = CAL.is_data_ready("HK", None)
        assert ready is False and why == "calendar_missing"

    def test_non_trading_day_not_ready(self):
        ready, why = CAL.is_data_ready("A", False)
        assert ready is False and why == "market_data_not_ready"

    def test_a_share_not_ready_before_19_cst(self):
        # 2026-10-14 10:00 UTC = 18:00 CST < 19:00
        now = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
        assert CAL.is_data_ready("A", True, now) == (False, "market_data_not_ready")

    def test_a_share_ready_after_19_cst(self):
        now = datetime(2026, 10, 14, 11, 30, tzinfo=timezone.utc)   # 19:30 CST
        assert CAL.is_data_ready("A", True, now) == (True, None)

    def test_hk_ready_after_18_30_hkt(self):
        before = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)   # 18:00 HKT
        after = datetime(2026, 10, 14, 11, 0, tzinfo=timezone.utc)    # 19:00 HKT
        assert CAL.is_data_ready("HK", True, before)[0] is False
        assert CAL.is_data_ready("HK", True, after) == (True, None)

    def test_us_not_ready_at_a_share_close_time(self):
        """美股 16:00 ET 收盘 ⇒ 北京 19:00 时美股当日尚未收盘完。"""
        now = datetime(2026, 10, 14, 11, 0, tzinfo=timezone.utc)   # 07:00 ET
        assert CAL.is_data_ready("US", True, now)[0] is False

    def test_us_ready_next_morning_cst(self):
        """06:30 CST 次日 = 18:30 ET 前一交易日 ⇒ 已过 17:00 ET 就绪点。"""
        now = datetime(2026, 10, 14, 22, 30, tzinfo=timezone.utc)  # 10-15 06:30 CST
        ready, why = CAL.is_data_ready("US", True, now)
        assert ready is True and why is None

    def test_naive_datetime_treated_as_utc(self):
        naive = datetime(2026, 10, 14, 11, 30)
        assert CAL.is_data_ready("A", True, naive) == (True, None)


class TestHkSessions:
    def test_weekends_excluded(self):
        # 2026-10-12(一) .. 2026-10-18(日)
        sessions = CAL.hk_sessions_from_holidays(date(2026, 10, 12), date(2026, 10, 18), [])
        assert sessions == ["20261012", "20261013", "20261014", "20261015", "20261016"]

    def test_holidays_excluded(self):
        sessions = CAL.hk_sessions_from_holidays(
            date(2026, 10, 12), date(2026, 10, 16), ["2026-10-14"])
        assert "20261014" not in sessions
        assert len(sessions) == 4

    def test_single_day_range(self):
        assert CAL.hk_sessions_from_holidays(date(2026, 10, 17), date(2026, 10, 17), []) == []


class TestHolidayFile:
    def test_dict_shape(self, tmp_path):
        p = tmp_path / "hk.json"
        p.write_text(json.dumps({"market": "HK", "holidays": ["2026-01-01"]}), encoding="utf-8")
        assert CAL.load_holiday_file(p) == ["2026-01-01"]

    def test_list_shape(self, tmp_path):
        p = tmp_path / "hk.json"
        p.write_text(json.dumps(["2026-01-01", "2026-02-17"]), encoding="utf-8")
        assert CAL.load_holiday_file(p) == ["2026-01-01", "2026-02-17"]

    def test_missing_file_is_empty_not_error(self, tmp_path):
        """文件缺失 ⇒ 空表 ⇒ 调用方判 calendar_missing（不静默兜底）。"""
        assert CAL.load_holiday_file(tmp_path / "nope.json") == []

    def test_corrupt_file_is_empty(self, tmp_path):
        p = tmp_path / "hk.json"
        p.write_text("{ broken", encoding="utf-8")
        assert CAL.load_holiday_file(p) == []


class TestTradingCalendar:
    def _cal(self):
        rows = [{"cal_date": "20261012", "is_open": 1},
                {"cal_date": "20261013", "is_open": 1},
                {"cal_date": "20261014", "is_open": 0},
                {"cal_date": "20261015", "is_open": "1"}]
        return CAL.TradingCalendar.from_trade_cal_rows("A", rows)

    def test_is_trading_day(self):
        cal = self._cal()
        assert cal.is_trading_day("A", "20261012") is True
        assert cal.is_trading_day("A", "20261014") is False

    def test_missing_market_returns_none_not_false(self):
        """缺失市场必须返回 None —— 空集合会被误读成"全是休市"。"""
        cal = self._cal()
        assert cal.is_trading_day("HK", "20261012") is None
        assert cal.last_session("HK", "20261031") is None
        assert cal.has("HK") is False

    def test_last_session(self):
        cal = self._cal()
        assert cal.last_session("A", "20261014") == "20261013"
        assert cal.last_session("A", "20261031") == "20261015"
        assert cal.last_session("A", "20260101") is None

    def test_is_open_variants(self):
        rows = [{"cal_date": "20260101", "is_open": "1.0"},
                {"cal_date": "20260102", "is_open": "true"},
                {"cal_date": "20260103", "is_open": 0}]
        cal = CAL.TradingCalendar.from_trade_cal_rows("US", rows)
        assert cal.is_trading_day("US", "20260101") is True
        assert cal.is_trading_day("US", "20260102") is True
        assert cal.is_trading_day("US", "20260103") is False

    def test_hk_built_from_holidays(self):
        cal = CAL.TradingCalendar.with_hk_holidays(
            date(2026, 10, 12), date(2026, 10, 16), ["2026-10-14"])
        assert cal.has("HK")
        assert cal.is_trading_day("HK", "20261014") is False
        assert "自建" in cal.sources["HK"]

    def test_readiness_combines_calendar_and_clock(self):
        cal = self._cal()
        now = datetime(2026, 10, 12, 11, 30, tzinfo=timezone.utc)   # 19:30 CST
        assert cal.readiness("A", "20261012", now) == (True, None)
        assert cal.readiness("A", "20261014", now)[0] is False       # 休市
        assert cal.readiness("HK", "20261012", now) == (False, "calendar_missing")

    def test_describe(self):
        cal = self._cal()
        assert "A: 3 个交易日" in cal.describe()
        assert CAL.TradingCalendar().describe() == "（无可用日历）"

    def test_accumulates_markets(self):
        cal = CAL.TradingCalendar.from_trade_cal_rows(
            "A", [{"cal_date": "20261012", "is_open": 1}])
        cal = CAL.TradingCalendar.with_hk_holidays(
            date(2026, 10, 12), date(2026, 10, 13), [], calendar=cal)
        assert cal.has("A") and cal.has("HK")
