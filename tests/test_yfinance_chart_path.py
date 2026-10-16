"""HK market data must come from Yahoo's **chart** endpoint, never from `Ticker.info`.

Context (06862, 2026-10-13):
- `Ticker.info` / `.fast_info` are backed by `quoteSummary`, which now needs a crumb and
  answers HTTP 401 → yfinance retries for ~30 s → the old all-`.info` implementation always
  failed and was swallowed into `None`, so HK market data silently degraded to the
  quota-limited `hk_daily` (5/day). That is the real content of known_issues #24.
- yfinance 1.2.1's **own session** is additionally answered with 429 (`YFRateLimitError`) for
  HK tickers, while the same chart endpoint returns data to a plain request with a browser UA
  (verified live: 6862.HK → 421 weekly points, last close 9.30, 10y low 9.08).
So the HK paths now call the chart endpoint **directly** (`_yahoo_chart_df`), with yfinance as
a secondary fallback. These tests pin that order, the symbol conversion, and visible
degradation. They patch `_yahoo_chart_df` / `_yf` so no test touches the network.
"""

import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, "scripts")


def _client():
    from tushare_collector import TushareClient
    with patch("tushare_collector.ts") as mock_ts:
        mock_ts.pro_api.return_value = MagicMock()
        c = TushareClient("test_token")
    c._yf_available = True
    return c


def _chart(n=5, close=9.30):
    return pd.DataFrame({
        "ts_code": ["06862.HK"] * n,
        "trade_date": [f"2026100{i+1}" for i in range(n)],
        "open": [close] * n, "high": [close + 0.2] * n, "low": [close - 0.2] * n,
        "close": [close - 0.01 * i for i in range(n)], "vol": [1_000_000] * n,
    })


class TestSymbolConversion:
    def test_hk_five_digit_becomes_four_digit(self):
        c = _client()
        assert c._yf_ticker("06862.HK") == "6862.HK"
        assert c._yf_ticker("00700.HK") == "0700.HK"
        assert c._yf_ticker("09987.HK") == "9987.HK"

    def test_a_share_and_us_mappings(self):
        c = _client()
        assert c._yf_ticker("600887.SH") == "600887.SS"
        assert c._yf_ticker("000858.SZ") == "000858.SZ"

    def test_chart_url_uses_the_converted_symbol(self):
        c = _client()
        url = c._YAHOO_CHART_URL.format(sym=c._yf_ticker("06862.HK"), rng="10y", itv="1wk")
        assert "6862.HK" in url and "06862.HK" not in url


class TestHkMarketData:
    def test_uses_chart_endpoint_and_never_ticker_info(self):
        c = _client()
        with patch.object(c, "_yahoo_chart_df", return_value=_chart()) as chart, \
             patch("tushare_modules.yfinance_integration._yf") as yf:
            out = c._yf_hk_market_data("06862.HK")
        assert out and abs(out["close"] - 9.29) < 0.05
        assert out["high_52w"] and out["low_52w"] and out["volume_avg"]
        chart.assert_called_once()
        yf.assert_not_called()          # 直连成功就不碰 yfinance 会话

    def test_falls_back_to_yfinance_only_when_chart_is_empty(self):
        c = _client()
        fake = MagicMock()
        hist = pd.DataFrame({"Close": [9.30], "High": [9.5], "Low": [9.0], "Volume": [10]},
                            index=pd.to_datetime(["2026-10-13"]))
        fake.history.return_value = hist
        with patch.object(c, "_yahoo_chart_df", return_value=pd.DataFrame()), \
             patch("tushare_modules.yfinance_integration._yf") as yf:
            yf.return_value.Ticker.return_value = fake
            out = c._yf_hk_market_data("06862.HK")
        assert out and abs(out["close"] - 9.30) < 1e-9

    def test_degrades_to_none_when_both_paths_fail(self):
        c = _client()
        fake = MagicMock()
        fake.history.side_effect = RuntimeError("YFRateLimitError")
        with patch.object(c, "_yahoo_chart_df", return_value=pd.DataFrame()), \
             patch("tushare_modules.yfinance_integration._yf") as yf:
            yf.return_value.Ticker.return_value = fake
            assert c._yf_hk_market_data("06862.HK") is None

    def test_market_cap_comes_from_tushare_not_yfinance(self):
        c = _client()
        c._store["basic_info"] = pd.DataFrame([{"total_market_cap": 51_838_200_000.0}])
        with patch.object(c, "_yahoo_chart_df", return_value=_chart()):
            out = c._yf_hk_market_data("06862.HK")
        assert out["market_cap"] == pytest.approx(51_838_200_000.0)


class TestWeeklyHistory:
    def test_direct_chart_is_preferred(self):
        c = _client()
        with patch.object(c, "_yahoo_chart_df", return_value=_chart(n=421)) as chart, \
             patch("tushare_modules.yfinance_integration._yf") as yf:
            df = c._yf_weekly_history("06862.HK")
        assert len(df) == 421
        assert list(df.columns) == ["ts_code", "trade_date", "open", "high", "low", "close", "vol"]
        chart.assert_called_once()
        yf.assert_not_called()

    def test_empty_direct_chart_falls_back(self):
        c = _client()
        fake = MagicMock()
        fake.history.return_value = pd.DataFrame()
        with patch.object(c, "_yahoo_chart_df", return_value=pd.DataFrame()), \
             patch("tushare_modules.yfinance_integration._yf") as yf:
            yf.return_value.Ticker.return_value = fake
            assert c._yf_weekly_history("06862.HK").empty


class TestFallbackPrice:
    def test_uses_chart_endpoint(self):
        c = _client()
        with patch.object(c, "_yahoo_chart_df", return_value=_chart()) as chart:
            out = c._yf_fallback_price("06862.HK")
        assert out and abs(out["close"] - 9.29) < 0.05 and "chart" in out["source"]
        chart.assert_called_once()

    def test_empty_degrades_to_none(self):
        c = _client()
        with patch.object(c, "_yahoo_chart_df", return_value=pd.DataFrame()):
            assert c._yf_fallback_price("06862.HK") is None


class TestStatementFillCurrencyGuard:
    """known_issues #23：Yahoo 基本面报表的计价币种无法核实 ⇒ 港股（报表币种≠交易币种）
    一律**不填补** NaN，避免把来源币种未核实的值混进报表币种框架。"""

    @staticmethod
    def _pivoted():
        return pd.DataFrame({"end_date": ["20251231"], "n_income_attr_p": [float("nan")]})

    def test_refuses_to_fill_when_reporting_differs_from_trading(self):
        c = _client()
        c._currency = "HKD"
        c._store["fx"] = {"factor_to_reporting": 0.85849, "reporting_currency": "CNY"}
        with patch("tushare_modules.yfinance_integration._yf") as yf:
            filled, used = c._yf_fill_missing_hk(self._pivoted(), "06862.HK", "income")
        assert used is False
        assert filled["n_income_attr_p"].isna().all(), "不得填入币种无法核实的值"
        yf.assert_not_called()

    def test_fills_normally_when_currencies_match(self):
        c = _client()
        c._currency = "HKD"
        c._store["fx"] = {"factor_to_reporting": 1.0, "reporting_currency": "HKD"}
        fake = MagicMock()
        fake.income_stmt = pd.DataFrame(
            {"2025-12-31": [123.0]}, index=["Net Income"])
        with patch("tushare_modules.yfinance_integration._yf") as yf, \
             patch("tushare_modules.yfinance_integration._YF_INCOME_MAP",
                   {"Net Income": "n_income_attr_p"}):
            yf.return_value.Ticker.return_value = fake
            _, used = c._yf_fill_missing_hk(self._pivoted(), "00001.HK", "income")
        assert used is True
