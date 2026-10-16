"""Turtle Investment Framework - InfrastructureMixin.

Utility methods: market detection, display formatting, HK pivot, store helpers.
"""

import pandas as pd

from format_utils import format_number
from tushare_modules.constants import field_mode, field_names


class InfrastructureMixin:
    """Mixin providing infrastructure utilities for TushareClient."""

    @staticmethod
    def _detect_trading_currency(ts_code: str) -> str:
        """Currency the stock is **traded** in, from the ticker suffix.

        ⚠️ This is the TRADING currency, **not** the reporting currency. It is
        correct for prices and market cap (06862.HK's price really is HK$11.69),
        but it says nothing about the currency of the financial statements.

        Before 2026-10 the docstring here claimed "Detect reporting currency"
        and the same value was used for both roles — which is the whole of
        known_issues.md P0-2: 06862.HK trades in HKD but reports in CNY
        ("the presentation currency ... was Renminbi"), so
        `R = 净利润(CNY) / 市值(HKD)` understated the return by 15.65%.
        Use :meth:`_reporting_currency` for statement amounts.
        """
        upper = ts_code.upper()
        if upper.endswith(".HK"):
            return "HKD"
        elif upper.endswith(".US"):
            return "USD"
        return "CNY"

    # Backwards-compatible alias. Prefer `_detect_trading_currency` in new code so
    # the trading/reporting distinction is visible at every call site.
    _detect_currency = _detect_trading_currency

    def _reporting_currency(self, ts_code: str = "") -> str | None:
        """Currency the **financial statements** are presented in.

        Only asserted when explicitly confirmed (`fx.json` / `--fx-hkd-cny`, whose
        `reporting_currency` field the coordinator fills from the annual report):
        for an HK listing this returns ``None`` rather than guessing HKD, because
        most HK-listed mainland issuers report in CNY and a wrong guess is exactly
        the P0-2 defect. A-shares report in CNY; US filers in USD.

        Returns ``None`` when unknown — callers must fail loudly, not default.

        The explicit confirmation is only consulted for **HK listings**, the one
        place where the ambiguity is real; A-shares report in CNY and US filers in
        USD regardless of any stale `fx.json` that happens to sit in the store.
        """
        code = ts_code or ""
        if self._is_hk(code):
            return (self._store.get("fx") or {}).get("reporting_currency") or None
        if self._is_us(code):
            return "USD"
        return "CNY"

    @staticmethod
    def _is_hk(ts_code: str) -> bool:
        """Check if stock code is a Hong Kong listing."""
        return ts_code.upper().endswith(".HK")

    @staticmethod
    def _is_us(ts_code: str) -> bool:
        """Check if stock code is a US listing."""
        return ts_code.upper().endswith(".US")

    def _unit_label(self) -> str:
        """Unit label for **financial figures** — i.e. the reporting currency.

        Deliberately not the trading currency: labelling 06862.HK's statements
        "百万港元" mislabels every number in them (P0-2). When an HK listing's
        reporting currency has not been confirmed, the label says so rather than
        guessing — a visible "未确认" is recoverable, a wrong label is not.
        """
        confirmed = (self._store.get("fx") or {}).get("reporting_currency")
        if confirmed:
            return {"CNY": "百万元", "HKD": "百万港元",
                    "USD": "百万美元"}.get(confirmed, f"百万{confirmed}")
        if self._currency == "HKD":
            return "百万·币种未确认"
        return {"USD": "百万美元"}.get(self._currency, "百万元")

    def _price_unit(self) -> str:
        """Price unit — the **trading** currency (prices are quoted in it)."""
        return {"HKD": "港元", "USD": "美元"}.get(self._currency, "元")

    def _per_share_unit(self) -> str:
        """Per-share unit for **financial-statement** items (EPS / BPS).

        Distinct from :meth:`_price_unit`: EPS comes off the income statement, so
        for 06862.HK it is CNY per share, not HKD per share. Labelling it
        "港元/股" was the same conflation as the statement headers (P0-2).
        """
        confirmed = (self._store.get("fx") or {}).get("reporting_currency")
        if confirmed:
            return {"CNY": "元", "HKD": "港元", "USD": "美元"}.get(confirmed, confirmed)
        if self._currency == "HKD":
            return "元·币种未确认"
        return {"USD": "美元"}.get(self._currency, "元")

    # ------------------------------------------------------------------
    # HK market-cap unit normalisation (known_issues.md P0-3)
    #
    # Tushare's `hk_fina_indicator.total_market_cap` is in RAW currency units,
    # not millions. Verified 2026-10 on 10/10 HK tickers, e.g.
    #   00700.HK = 3,862,538,856,756  (HK$3.86tn)
    #   06862.HK =     53,343,180,000 (HK$53.3bn)
    # The docs never stated a unit, and the code assumed "百万港元" in four
    # places, inflating market cap by 1e6 (which zeroes out §17.11's GG and
    # target buy price for HK listings).
    #
    # Any genuine HK listing has a raw-HKD market cap >> 1e6, so a smaller value
    # means the field came back in millions. In that case we normalise and flag
    # it rather than silently producing a 1e6-wrong market cap.
    # ------------------------------------------------------------------
    _HK_MKT_CAP_RAW_FLOOR = 1_000_000.0

    def _hk_market_cap_raw(self, basic_row) -> float | None:
        """Return HK `total_market_cap` normalised to RAW currency units (元).

        Returns None when the field is missing / non-positive, so callers can
        degrade instead of computing with a bogus market cap.
        """
        if basic_row is None:
            return None
        v = self._safe_float(basic_row.get("total_market_cap"))
        if v is None or v <= 0:
            return None
        if v < self._HK_MKT_CAP_RAW_FLOOR:
            self._store.setdefault("_hk_mktcap_warnings", []).append(
                f"total_market_cap={v:,.2f} 低于原始港元下限 "
                f"({self._HK_MKT_CAP_RAW_FLOOR:,.0f})，已按「百万」口径 ×1e6 归一化"
            )
            return v * 1e6
        return v

    # ------------------------------------------------------------------
    # FX: trading currency → reporting currency (known_issues.md P0-2)
    #
    # `_detect_currency` labels financials by ticker suffix (.HK → HKD), but
    # Tushare returns them in the issuer's REPORTING currency — CNY for most
    # mainland A+H / red-chip issuers (06862 / 00506 / 02669 / 06049 annual
    # reports all say "presented in Renminbi").
    #
    # So `R = 净利润(CNY) / 市值(HKD)` mixes currencies and understates the
    # return by 1/0.86466 − 1 = 15.65% (06862 FY2024: 7.225% vs 8.357%).
    #
    # The factor is resolved by scripts/fx_utils.py from, in order:
    #   --fx-hkd-cny → TUSHARE_FX_HKD_CNY → {output}/fx.json → pack 协调器注
    # ------------------------------------------------------------------
    def _fx_to_reporting(self, ts_code: str) -> float:
        """Factor converting a **trading-currency** amount into the reporting currency.

        "multiply by this to get the reporting currency". Returns 1.0 for
        A-shares / US, or for HK when no rate was supplied — in which case a
        warning is recorded so the omission is visible rather than silent.
        """
        if not self._is_hk(ts_code):
            return 1.0
        fx = self._store.get("fx") or {}
        factor = fx.get("factor_to_reporting")
        try:
            factor = float(factor) if factor is not None else None
        except (TypeError, ValueError):
            factor = None
        if factor is None or factor <= 0:
            self._store.setdefault("_fx_warnings", []).append(
                "港股未提供汇率（--fx-hkd-cny / TUSHARE_FX_HKD_CNY / fx.json）；"
                "R/GG 按未换算计算 —— 若报表币种非港元将低估约 15%（known_issues.md P0-2）"
            )
            return 1.0
        return factor

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

    @staticmethod
    def _pivot_hk_line_items(df: pd.DataFrame, field_map: dict) -> pd.DataFrame:
        """Pivot HK ind_name/ind_value rows into one-row-per-period columns.

        HK financial APIs return data in line-item format:
            end_date | ind_name   | ind_value
            20241231 | 营业额     | 60000
            20241231 | 除税后溢利 | 10000
        This pivots them into columnar format matching A-share structure.

        A ``field_map`` value may be a plain ind_name (str), or a multi-source
        spec dict ``{"names": [...], "mode": "sum"|"first"}`` — see
        ``tushare_modules.constants.field_names`` / ``field_mode``. Plain values
        keep the historical "first" semantics, so 1:1 maps are unaffected.
        """
        if df.empty or "ind_name" not in df.columns:
            return pd.DataFrame()

        reverse_map: dict = {}
        modes: dict = {}
        for field, spec in field_map.items():
            names = field_names(spec)
            if not names:
                continue
            modes[field] = field_mode(spec)
            for name in names:
                reverse_map[name] = field
        if not reverse_map:
            return pd.DataFrame()

        df_mapped = df[df["ind_name"].isin(reverse_map)].copy()
        if df_mapped.empty:
            return pd.DataFrame()

        df_mapped["field"] = df_mapped["ind_name"].map(reverse_map)

        # Convert ind_value to numeric
        df_mapped["ind_value"] = pd.to_numeric(df_mapped["ind_value"], errors="coerce")

        key = ["end_date", "ts_code", "field"]
        grouped = df_mapped.groupby(key, sort=True)["ind_value"]
        agg = grouped.first()

        # Multi-source fields whose spec asks for a sum (e.g. HK CapEx =
        # 购建固定资产 + 购建无形资产及其他资产). "first" stays the default so
        # that every pre-existing 1:1 mapping keeps its exact old behaviour.
        sum_fields = {f for f in modes if modes[f] == "sum"}
        if sum_fields:
            summed = grouped.sum()
            lvl = agg.index.get_level_values("field")
            mask = lvl.isin(sum_fields)
            if mask.any():
                agg = agg.copy()
                agg[mask] = summed[mask]

        pivoted = agg.unstack("field").reset_index()
        pivoted.columns.name = None

        # Match pivot_table(dropna=True): drop field columns that are all-NaN.
        field_cols = [c for c in pivoted.columns if c not in ("end_date", "ts_code")]
        drop_cols = [c for c in field_cols if pivoted[c].isna().all()]
        if drop_cols:
            pivoted = pivoted.drop(columns=drop_cols)
        return pivoted

    def _prepare_display_periods(self, df, max_annual=5):
        """Select up to max_annual annual reports + any newer interim reports.

        Returns (display_df, column_labels) where column_labels are like:
        ["2025Q3", "2025H1", "2025Q1", "2024", "2023", "2022", "2021", "2020"]
        """
        if df.empty:
            return df, []

        df = df.drop_duplicates(subset=["end_date"])

        fy_month_str = f"{self._fy_end_month:02d}"

        # Split into annual (FY end month) and non-annual
        annual = df[df["end_date"].str[4:6] == fy_month_str].copy()
        non_annual = df[df["end_date"].str[4:6] != fy_month_str].copy()

        # Sort annual descending, take top max_annual
        annual = annual.sort_values("end_date", ascending=False).head(max_annual)

        latest_annual_date = annual["end_date"].max() if not annual.empty else "00000000"

        # Keep only non-annual entries strictly newer than latest annual
        interim = non_annual[non_annual["end_date"] > latest_annual_date].copy()
        interim = interim.sort_values("end_date", ascending=False)

        # Build labels
        fy_month = self._fy_end_month

        def _label(end_date):
            mm = end_date[4:6]
            mmdd = end_date[4:]
            year = end_date[:4]
            if mm == fy_month_str:
                return year
            elif mmdd == "0630":
                return f"{year}H1"
            elif mmdd == "0331":
                return f"{year}Q1"
            elif mmdd == "0930":
                return f"{year}Q3"
            else:
                return f"{year}_{mmdd}"

        # Combine: interim (desc) + annual (desc)
        display_df = pd.concat([interim, annual], ignore_index=True)
        if display_df.empty:
            return display_df, []

        labels = [_label(d) for d in display_df["end_date"]]
        return display_df, labels

    # --- Feature #90-92: Derived metrics (Section 17) ---

    @staticmethod
    def _safe_float(val) -> float | None:
        """Convert a value to float, returning None for NaN/None."""
        if val is None:
            return None
        try:
            f = float(val)
            return None if f != f else f  # NaN check
        except (TypeError, ValueError):
            return None

    def _get_annual_df(self, store_key: str) -> pd.DataFrame:
        """Get stored DataFrame filtered to annual periods only."""
        df = self._store.get(store_key)
        if df is None or df.empty:
            return pd.DataFrame()
        fy_month_str = f"{self._fy_end_month:02d}"
        annual = df[df["end_date"].str[4:6] == fy_month_str].copy()
        return annual.sort_values("end_date", ascending=False)

    def _get_annual_series(self, store_key: str, col: str) -> list[tuple[str, float | None]]:
        """Extract (year_label, value) pairs for annual periods, sorted desc."""
        df = self._get_annual_df(store_key)
        if df.empty or col not in df.columns:
            return []
        result = []
        for _, r in df.iterrows():
            year = str(r["end_date"])[:4]
            result.append((year, self._safe_float(r.get(col))))
        return result

    @staticmethod
    def _resolve_hk_payout(ts_ratio: float | None, dps: float | None, eps: float | None,
                           dps_to_reporting: float = 1.0) -> float | None:
        """Resolve HK payout ratio (%) with Tushare fix + DPS/EPS cross-validation.

        Steps:
        1. Fix Tushare divi_ratio if < 1 (dirty data: decimal instead of %).
        2. Self-compute from DPS / EPS × 100 if both available.
        3. Cross-validate: prefer Tushare if diff < 20%, else use computed.
        4. Fall back to whichever is available; None if neither.

        `dps_to_reporting` 把 **交易币种**的 DPS 折成 EPS 所在的**报表币种**
        （known_issues.md P0-2 的同一条纪律）。港股 `dps_hkd` 是港元而 `basic_eps`
        是人民币，直接相除会把支付率高估 1/0.86476 − 1 = 15.65%
        （00506 实测 0.177/0.31 = 57.10% vs 折汇后 49.38%）。
        """
        # Step 1: fix Tushare divi_ratio if < 1
        if ts_ratio is not None and ts_ratio < 1:
            ts_ratio *= 100

        # Step 2: self-compute from DPS / EPS —— **先折汇再相除**
        computed = None
        if dps is not None and dps > 0 and eps is not None and eps > 0:
            computed = dps * dps_to_reporting / eps * 100

        # Step 3: cross-validate and pick
        if ts_ratio is not None and computed is not None:
            diff = abs(ts_ratio - computed) / computed
            return ts_ratio if diff < 0.2 else computed
        if computed is not None:
            return computed
        if ts_ratio is not None:
            return ts_ratio
        return None

    def _declared_dividends(self) -> dict:
        """读取 `{output_dir}/dividends_declared.json`（年报口径的**已宣告**每股股息）。

        契约（与 `fx.json` / `shares.json` 同级的来源可核 sidecar）::

            {"declared_dps": {"2025": {"value": 0.154, "currency": "CNY",
                                       "source": "2025年报 p.157 附注14/建议末期股息"}},
             "note": "..."}

        `value` 是**已宣告**的每股股息（含中期+末期时给年度合计），`currency` 为报表币种。
        为什么必须由年报供给：上游 `hk_dividend` 的 DPS 是"按公告快照"的单值
        （00506 九期全是 0.177，而年报是 0.148→0.153→0.154 的逐年序列），
        `invariants I5` 已点名该列不得用于派息率。没有这个 sidecar 就得如实降级，
        而不是拿快照当逐年真值。
        """
        path = self._store.get("_output_path")
        if not path:
            return {}
        import json
        from pathlib import Path
        p = Path(str(path)).parent / "dividends_declared.json"
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._store.setdefault("_data_warnings", []).append(
                f"dividends_declared.json 解析失败，派息率退回现金流口径：{exc}")
            return {}
        return data.get("declared_dps") or {}

    def _dps_is_snapshot(self, hk_df) -> bool:
        """上游 DPS 是否呈「按公告快照」填充（多期同一个数）？

        00506/06862 实测：`dps_hkd` 在**全部**九个期间都渲染成 0.177 —— 它不是一个
        逐年序列，因此任何由它派生的"派息率趋势"都是假的。这里只做可机械判定的检查：
        取值个数 ≤ 1 且期数 ≥ 3。
        """
        try:
            vals = [v for v in hk_df.get("dps_hkd", []) if v == v and v is not None]
        except Exception:
            return False
        return len(vals) >= 3 and len({round(float(v), 6) for v in vals}) <= 1


    def _payout_declared(self, eps_by_year: dict) -> tuple[dict, bool]:
        """（逐年派息率, 是否用了年报口径）。分母是**同币种** EPS。"""
        declared = self._declared_dividends()
        fx = self._fx_to_reporting(self._store.get("dividends_ts_code", ""))
        out: dict[str, float] = {}
        for y, entry in declared.items():
            if not isinstance(entry, dict):
                continue
            dps = self._safe_float(entry.get("value"))
            ccy = str(entry.get("currency") or "CNY").upper()
            eps = eps_by_year.get(str(y))
            if dps is None or not eps or eps <= 0:
                continue
            if ccy == "HKD":                      # 交易币种 → 报表币种（P0-2 纪律）
                dps = dps * fx
            out[str(y)] = dps / eps * 100
        return out, bool(out)

    def _payout_paid_crosscheck(self) -> dict[str, float]:
        """§5 已付股息 ÷ §3 归母净利润 —— **只作交叉核对，不作派息率序列**。

        两处不可当真的原因（00506 实测）：① 分子含少数股东股利（已付 1,577.05 =
        归母 427.61 + 少数股东 1,149.44），是所有者整体的分配，不是上市股东拿到的；
        ② 现金支出年度与归属年度错位（FY2025 1,577.05 = 上年宣告 + 本年中期），把它
        当"当年派息率"会得到 182.96% 这种没有意义的数。
        """
        cf_df = self._get_annual_df("cashflow")
        inc_df = self._get_annual_df("income")
        if cf_df.empty or inc_df.empty:
            return {}
        np_by_year: dict[str, float] = {}
        for _, r in inc_df.iterrows():
            y = str(r["end_date"])[:4]
            v = self._safe_float(r.get("n_income_attr_p"))
            if v:
                np_by_year[y] = v
        out: dict[str, float] = {}
        for _, r in cf_df.iterrows():
            y = str(r["end_date"])[:4]
            dist = self._safe_float(r.get("c_pay_dist_dpcp_int_exp"))
            npv = np_by_year.get(y)
            if dist is not None and dist > 0 and npv and npv > 0:
                out[y] = dist / npv * 100
        return out

    def _legacy_hk_payout_by_year(self) -> dict[str, float]:
        """旧口径：Tushare `divi_ratio` 与 DPS/EPS 交叉校验（**仅作最后兜底**）。

        对港股它系统性偏差：`dps_hkd` 是按公告快照的单值、`divi_ratio` 与年报不符，
        两者各自都错，20% 的交叉校验只会稳定地选中一个错值（00506 实测渲染出
        51.22/56.10/57.41，年报口径 49.98/49.73/49.72）。保留它是为了让"既无
        `dividends_declared.json` 又无现金流"的老包仍能出数并留下警告，
        **不得**作为首选口径。
        """
        hk_df = self._store.get("dividends_hk")
        if hk_df is None or hk_df.empty:
            return {}
        income_df = self._get_annual_df("income")
        eps_lookup: dict[str, float] = {}
        if not income_df.empty and "basic_eps" in income_df.columns:
            for _, r in income_df.iterrows():
                year = str(r["end_date"])[:4]
                eps = self._safe_float(r.get("basic_eps"))
                if eps and eps > 0:
                    eps_lookup[year] = eps
        result: dict[str, float] = {}
        for _, r in hk_df.iterrows():
            year = str(r.get("end_date", ""))[:4]
            if not year:
                continue
            resolved = self._resolve_hk_payout(
                self._safe_float(r.get("divi_ratio")),
                self._safe_float(r.get("dps_hkd")),
                eps_lookup.get(year),
                dps_to_reporting=self._fx_to_reporting(
                    self._store.get("dividends_ts_code", "")))
            if resolved is not None:
                result[year] = resolved
        return result

    def _get_payout_by_year(self) -> dict[str, float]:
        """Get payout ratio (%) by year from stored dividend data.

        HK path: **年报宣告口径**（`dividends_declared.json`）→ 历史 DPS/divi_ratio
                 交叉校验口径（仅在 DPS 不是"快照单值"时才允许）。
        A-share path: computes from cash_div × base_share × 10000 / net_income × 100.
        """
        hk_df = self._store.get("dividends_hk")
        if hk_df is not None and not hk_df.empty:
            eps_by_year: dict[str, float] = {}
            shares_mm = 0.0
            try:
                from shares_utils import resolve_from_store
                shares_mm = float(getattr(resolve_from_store(self), "shares_mm", 0) or 0)
            except Exception:
                shares_mm = 0.0
            income_df = self._get_annual_df("income")
            if not income_df.empty and "basic_eps" in income_df.columns:
                for _, r in income_df.iterrows():
                    # 只认**年报**行（1231）：`_get_annual_df` 还带更新的中报，
                    # 用中报 EPS 去配年报派息会把口径搞混（00506 实测 2026H1 的 0.31
                    # 会顶掉 FY2025 的 0.3082，派息率被压低 0.3 pct）。
                    if not str(r.get("end_date", "")).endswith("1231"):
                        continue
                    y = str(r["end_date"])[:4]
                    # EPS 由 归母净利润 ÷ 已发行股数 **现算**，不用上游 `basic_eps`：
                    # 上游把它四舍五入到 2 位小数（0.3082 → 0.31），而派息率对分母的
                    # 1% 误差很敏感（00506 实测 49.98% 被算成 49.68%）。分子分母同源自
                    # 年报（861,968 千元 ÷ 2,797,223,396 股 = 0.3082 元/股），可核。
                    ni = self._safe_float(r.get("n_income_attr_p"))
                    if ni and ni > 0 and shares_mm > 0:
                        eps_by_year[y] = ni / (shares_mm * 1e6)
                        continue
                    eps = self._safe_float(r.get("basic_eps"))
                    if eps and eps > 0:
                        eps_by_year[y] = eps
            declared, used = self._payout_declared(eps_by_year)
            if used:
                self._store["payout_basis"] = "declared"
                return declared
            legacy = self._legacy_hk_payout_by_year()
            snapshot_bad = self._dps_is_snapshot(hk_df)
            if legacy and not snapshot_bad:
                self._store["payout_basis"] = "legacy_dps_crosscheck"
                self._store.setdefault("_data_warnings", []).append(
                    "港股缺 dividends_declared.json ⇒ 派息率退回 DPS/divi_ratio 交叉校验口径；"
                    "该口径依赖上游公告快照，须与年报附注抽查后再引用")
                return legacy
            self._store.setdefault("_data_warnings", []).append(
                "港股 DPS 呈「按公告快照」单值（多期同值），该列不得用于派息率 —— "
                "请提供 {output_dir}/dividends_declared.json（年报已宣告每股股息）")
            return {}

        # A-share path: compute from _store["dividends"] + _store["income"]
        div_df = self._store.get("dividends")
        income_df = self._get_annual_df("income")
        if div_df is None or div_df.empty or income_df.empty:
            return {}

        # Build dividend total lookup by year (sum multiple payments per year)
        div_lookup: dict[str, float] = {}
        for _, r in div_df.iterrows():
            year = str(r.get("end_date", ""))[:4]
            cash_div = self._safe_float(r.get("cash_div_tax")) or 0
            base_share = self._safe_float(r.get("base_share")) or 0
            payment = cash_div * base_share * 10000  # base_share is 万股
            div_lookup[year] = div_lookup.get(year, 0) + payment

        # Build net income lookup by year
        np_lookup = {}
        for _, r in income_df.iterrows():
            year = str(r["end_date"])[:4]
            np_lookup[year] = self._safe_float(r.get("n_income_attr_p"))

        result = {}
        for year, div_total in div_lookup.items():
            np_val = np_lookup.get(year)
            if div_total and np_val and np_val > 0:
                result[year] = div_total / np_val * 100
        return result
