"""Turtle Investment Framework - YFinanceMixin.

All yfinance integration methods for HK/US price and financial data fallback.
"""

import sys
import time

import pandas as pd

from tushare_modules.constants import _YF_INCOME_MAP, _YF_BALANCE_MAP, _YF_CASHFLOW_MAP


def _yf():
    """Access the yfinance module.

    Historically this was ``sys.modules["tushare_collector"].yf`` so that
    ``@patch("tushare_collector.yf")`` works in tests. That broke in production:
    running ``python scripts/tushare_collector.py`` registers the module under
    ``__main__``, so the lookup raised ``KeyError: 'tushare_collector'`` and the
    **whole yfinance fallback silently failed**. Observed live 2026-10-13 for
    06862.HK income/balance/cashflow; tests never caught it because they
    ``import tushare_collector`` first, which creates the very key that is
    missing in the CLI path.

    Resolution order:
      1. ``tushare_collector.yf`` — keeps ``@patch`` working
      2. ``__main__.yf``          — the real CLI entry point
      3. a direct ``import yfinance`` — library use with no collector module
    """
    for name in ("tushare_collector", "__main__"):
        module = sys.modules.get(name)
        candidate = getattr(module, "yf", None) if module is not None else None
        if candidate is not None:
            return candidate
    import yfinance as yf  # last resort: no collector module in play
    return yf


class YFinanceMixin:
    """Mixin providing yfinance fallback methods for TushareClient."""

    @staticmethod
    def _yf_ticker(ts_code: str) -> str:
        """Convert Tushare stock code to yfinance ticker symbol."""
        code, suffix = ts_code.rsplit(".", 1)
        suffix = suffix.upper()
        if suffix == "SH":
            return f"{code}.SS"
        elif suffix == "SZ":
            return f"{code}.SZ"
        elif suffix == "HK":
            # Tushare 5-digit → YF 4-digit (e.g., 00700 -> 0700)
            return f"{code.lstrip('0').zfill(4)}.HK"
        elif suffix == "US":
            return code  # yfinance uses plain tickers for US stocks
        return ts_code

    def _yf_fallback_price(self, ts_code: str) -> dict | None:
        """Fetch a fallback price via yfinance **chart API** (never `.info`).

        `Ticker.info` / `.fast_info` are backed by Yahoo's `quoteSummary`, which since
        2026-10 requires a crumb and answers **HTTP 401**; yfinance then retries until it
        times out (~30 s) and the old code swallowed that into `None`, so the fallback had
        silently stopped working. The chart endpoint needs no crumb (verified: 06862.HK
        returns close 9.30 and 421 weekly points).
        """
        df = self._yahoo_chart_df(ts_code, "5d", "1d")
        if df is None or df.empty:
            return None
        closes = df["close"].dropna()
        if closes.empty:
            return None
        return {"close": float(closes.iloc[-1]), "source": "yahoo chart (降级)"}

    @staticmethod
    def _is_hk(ts_code: str) -> bool:
        """Check if stock code is a Hong Kong listing."""
        return ts_code.upper().endswith(".HK")

    @staticmethod
    def _is_us(ts_code: str) -> bool:
        """Check if stock code is a US listing."""
        return ts_code.upper().endswith(".US")

    def _unit_label(self) -> str:
        """Return currency-appropriate unit label for display."""
        return {"HKD": "百万港元", "USD": "百万美元"}.get(self._currency, "百万元")

    def _price_unit(self) -> str:
        """Return currency-appropriate price unit for display."""
        return {"HKD": "港元", "USD": "美元"}.get(self._currency, "元")

    def _detect_fy_end_month(self, df: pd.DataFrame) -> int:
        """Infer fiscal-year end month from end_date column.

        Groups by month, finds the month appearing in most distinct years.
        For calendar-year companies -> 12. For AAPL (Sep FY) -> 9.
        """
        if df.empty or "end_date" not in df.columns:
            return 12
        months = df["end_date"].astype(str).str[4:6].astype(int)
        years = df["end_date"].astype(str).str[:4]
        temp = pd.DataFrame({"month": months, "year": years})
        counts = temp.groupby("month")["year"].nunique()
        if counts.empty:
            return 12
        return int(counts.idxmax())

    @staticmethod
    def _us_api_code(ts_code: str) -> str:
        """Strip .US suffix for Tushare US API calls (Tushare uses plain tickers)."""
        return ts_code.rsplit(".", 1)[0]

    # NOTE: `_pivot_hk_line_items` intentionally lives ONLY in InfrastructureMixin.
    # A byte-identical copy used to exist here, but InfrastructureMixin precedes
    # YFinanceMixin in TushareClient's MRO, so this copy was dead code — editing
    # it silently had no effect. Removed 2026-10-13 to eliminate that trap.

    _YAHOO_CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
                        "{sym}?range={rng}&interval={itv}")

    def _yahoo_chart_df(self, ts_code: str, rng: str, itv: str) -> pd.DataFrame:
        """OHLCV straight from Yahoo's **chart** endpoint (no yfinance session, no crumb).

        2026-10 实测：yfinance 1.2.1 的会话对港股被限流（`YFRateLimitError` 429），而同一
        chart 端点在普通带 UA 的请求下正常返回（6862.HK → 10 年周线 421 点、末收盘 9.30、
        最低 9.08）。因此港股行情改为直接请求该端点：既不需要 crumb，也不受 yfinance
        会话限流影响。失败一律返回空 DataFrame，让调用方**可见地**退化。
        """
        import requests
        sym = self._yf_ticker(ts_code)
        try:
            resp = requests.get(self._YAHOO_CHART_URL.format(sym=sym, rng=rng, itv=itv),
                                timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            results = (resp.json().get("chart") or {}).get("result") or []
            if not results:
                return pd.DataFrame()
            res = results[0]
            quote = (res.get("indicators", {}).get("quote") or [{}])[0]
            stamps = res.get("timestamp") or []
            if not stamps:
                return pd.DataFrame()
            df = pd.DataFrame({
                "trade_date": pd.to_datetime(stamps, unit="s"),
                "open": quote.get("open"), "high": quote.get("high"),
                "low": quote.get("low"), "close": quote.get("close"),
                "vol": quote.get("volume"),
            })
            df = df.dropna(subset=["close"]).reset_index(drop=True)
            if df.empty:
                return df
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
            df["ts_code"] = ts_code
            return df[["ts_code", "trade_date", "open", "high", "low", "close", "vol"]]
        except Exception:
            return pd.DataFrame()

    def _yf_hk_market_data(self, ts_code: str) -> dict | None:
        """HK market data via the yfinance **chart API** (price / 52w / volume).

        Why not `.info`: `quoteSummary` now requires a crumb and returns HTTP 401, so the
        previous all-`info` implementation always failed, was swallowed into `None`, and the
        whole HK market-data path degraded to the quota-limited `hk_daily` (5/day) — which is
        the real content of `known_issues.md` #24 "港股行情无冗余源". The chart endpoint
        (`Ticker.history`) needs no crumb.

        Market cap is **not** taken from yfinance: it is already available from Tushare
        (`hk_fina_indicator.total_market_cap`, normalised by `_hk_market_cap_raw`) and is
        reused here when present, so §2 keeps showing 总市值 without the crumb-backed call.
        """
        daily = self._yahoo_chart_df(ts_code, "1y", "1d")
        if daily.empty and self._yf_available:
            try:                      # 二级回退：yfinance 会话（可能被限流）
                hist = _yf().Ticker(self._yf_ticker(ts_code)).history(period="1y", interval="1d")
                if hist is not None and not hist.empty and "Close" in hist:
                    daily = hist.reset_index().rename(columns={
                        "Date": "trade_date", "High": "high", "Low": "low",
                        "Close": "close", "Volume": "vol"})
            except Exception:
                daily = pd.DataFrame()
        if daily is None or daily.empty:
            return None
        try:
            closes = daily["close"].dropna()
            if closes.empty:
                return None
            out = {"close": float(closes.iloc[-1])}
            high = daily["high"].dropna() if "high" in daily else pd.Series(dtype=float)
            low = daily["low"].dropna() if "low" in daily else pd.Series(dtype=float)
            vol = daily["vol"].dropna() if "vol" in daily else pd.Series(dtype=float)
            if not high.empty:
                out["high_52w"] = float(high.max())
            if not low.empty:
                out["low_52w"] = float(low.min())
            if not vol.empty:
                out["volume_avg"] = float(vol.tail(10).mean())
            basic = self._store.get("basic_info")
            if basic is not None and not getattr(basic, "empty", True):
                cap = self._hk_market_cap_raw(basic.iloc[0])
                if cap:
                    out["market_cap"] = cap
            return out
        except Exception:
            return None

    def _yf_weekly_history(self, ts_code: str) -> pd.DataFrame:
        """10-year weekly history — Yahoo chart endpoint first, yfinance as fallback."""
        direct = self._yahoo_chart_df(ts_code, "10y", "1wk")
        if not direct.empty:
            return direct
        if not self._yf_available:
            return pd.DataFrame()
        try:
            ticker = _yf().Ticker(self._yf_ticker(ts_code))
            df = ticker.history(period="10y", interval="1wk")
            if df.empty:
                return df
            # Normalize column names to match Tushare weekly format
            df = df.reset_index()
            df = df.rename(columns={
                "Date": "trade_date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "vol",
            })
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
            df["ts_code"] = ts_code
            return df[["ts_code", "trade_date", "open", "high", "low", "close", "vol"]]
        except Exception:
            return pd.DataFrame()

    def _yf_fill_missing_hk(self, pivoted, ts_code, statement_type):
        """Fill NaN fields in pivoted HK DataFrame using yfinance data.

        Args:
            pivoted: DataFrame with Tushare data (one row per period).
            ts_code: Tushare stock code (e.g. '00700.HK').
            statement_type: 'income', 'balance', or 'cashflow'.

        Returns:
            (filled_df, yf_used_flag) tuple.
        """
        if not self._yf_available:
            return pivoted, False

        # ---- 币种守卫（known_issues #23）----
        # Yahoo 基本面报表的**计价币种**无法在本仓库内核实（quoteSummary 需 crumb，实测 401／无数据）。
        # 港股发行人报表币种常与交易币种不同（06862：报表 CNY／交易 HKD），而 Tushare 框架里装的是
        # **报表币种**数字；填进去就是**静默混币种**（P0-2 那一类）。故 fail-closed：不填、不发请求。
        reporting = self._reporting_currency(ts_code)
        trading = self._detect_trading_currency(ts_code)
        if reporting and trading and reporting != trading:
            print(f"[yfinance] {ts_code} {statement_type}: 跳过 NaN 填补——报表币种 {reporting} "
                  f"≠ 交易币种 {trading}，Yahoo 基本面报表的计价币种无法核实"
                  f"（疑混币种，known_issues #23）", file=sys.stderr)
            return pivoted, False

        # Select mapping dict FIRST (before NaN check)
        yf_map = {
            "income": _YF_INCOME_MAP,
            "balance": _YF_BALANCE_MAP,
            "cashflow": _YF_CASHFLOW_MAP,
        }.get(statement_type, {})
        if not yf_map:
            return pivoted, False

        filled = pivoted.copy()

        # Ensure all mapped target columns exist (allows filling completely absent fields)
        mapped_ts_cols = set(yf_map.values())
        for col in mapped_ts_cols - set(filled.columns):
            filled[col] = float("nan")

        # Check if any NaN in numeric columns (after column expansion)
        numeric_cols = filled.select_dtypes(include="number").columns
        if numeric_cols.empty or not filled[numeric_cols].isna().any().any():
            return pivoted, False  # return ORIGINAL (no extra NaN cols)

        # Build reverse map: tushare_col → list of yfinance field names
        reverse = {}
        for yf_name, ts_col in yf_map.items():
            reverse.setdefault(ts_col, []).append(yf_name)

        yf_df = None
        max_yf_retries = 2
        for yf_attempt in range(1, max_yf_retries + 1):
            try:
                ticker = _yf().Ticker(self._yf_ticker(ts_code))
                if statement_type == "income":
                    yf_df = ticker.income_stmt
                elif statement_type == "balance":
                    yf_df = ticker.balance_sheet
                else:
                    yf_df = ticker.cashflow
                break  # success
            except Exception as e:
                if yf_attempt < max_yf_retries:
                    time.sleep(1)
                else:
                    print(f"[yfinance] {ts_code} {statement_type}: fallback failed after {max_yf_retries} retries: {e}", file=sys.stderr)
                    return pivoted, False

        if yf_df is None or yf_df.empty:
            print(f"[yfinance] {ts_code} {statement_type}: no data returned", file=sys.stderr)
            return pivoted, False

        yf_used = False

        for idx, row in filled.iterrows():
            end_date = str(row.get("end_date", ""))
            if len(end_date) < 8:
                continue

            # Match yfinance column by date
            yf_col = None
            for col in yf_df.columns:
                ts = pd.Timestamp(col)
                col_date = ts.strftime("%Y%m%d")
                if col_date == end_date:
                    yf_col = col
                    break
            # Fallback: same year + FY end month
            if yf_col is None and int(end_date[4:6]) == self._fy_end_month:
                for col in yf_df.columns:
                    ts = pd.Timestamp(col)
                    if ts.year == int(end_date[:4]) and ts.month == self._fy_end_month:
                        yf_col = col
                        break
            if yf_col is None:
                continue

            # Fill NaN fields from yfinance
            for ts_col, yf_names in reverse.items():
                if ts_col not in filled.columns:
                    continue
                val = filled.at[idx, ts_col]
                if val is not None and val == val:  # not NaN
                    continue
                for yf_name in yf_names:
                    if yf_name in yf_df.index:
                        yf_val = yf_df.at[yf_name, yf_col]
                        if yf_val is not None and yf_val == yf_val:
                            filled.at[idx, ts_col] = float(yf_val)
                            yf_used = True
                            break

        return filled, yf_used
