"""Tests for derived metrics computation (§17) in TushareClient."""

import json
import math
import os
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tushare_collector import TushareClient
from section_contract import check_pack_consistency, headings_in_pack

MOCK_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "mock_tushare_responses")


def _load_mock(filename: str) -> pd.DataFrame:
    """Load a mock fixture as DataFrame."""
    with open(os.path.join(MOCK_DIR, filename)) as f:
        data = json.load(f)
    if isinstance(data, list):
        return pd.DataFrame(data)
    return pd.DataFrame([data])


def _make_client():
    """Create a TushareClient with mocked tushare module."""
    with patch("tushare_collector.ts") as mock_ts:
        mock_ts.pro_api.return_value = MagicMock()
        client = TushareClient("test_token")
    return client


def _make_client_with_store():
    """Create client with _store populated from mock fixtures."""
    client = _make_client()

    # Load and prepare DataFrames (simulating what get_* methods do)
    income_df = _load_mock("income.json")
    income_df = income_df.sort_values("end_date", ascending=False)
    years = [str(d)[:4] for d in income_df["end_date"]]

    bs_df = _load_mock("balancesheet.json")
    bs_df = bs_df.sort_values("end_date", ascending=False)

    cf_df = _load_mock("cashflow.json")
    cf_df = cf_df.sort_values("end_date", ascending=False)

    div_df = _load_mock("dividend.json")
    div_df = div_df.sort_values("end_date", ascending=False)

    rf_df = _load_mock("yc_cb.json")
    rf_df = rf_df.sort_values("trade_date", ascending=False)

    rep_df = _load_mock("repurchase.json")
    rep_df = rep_df.sort_values("ann_date", ascending=False)
    # Apply same dedup + status filter as get_repurchase()
    if "amount" in rep_df.columns:
        rep_df = rep_df.drop_duplicates(subset=["ann_date", "amount"], keep="first")
    if "proc" in rep_df.columns:
        executed = rep_df[rep_df["proc"].isin(["完成", "实施"])]
        if not executed.empty:
            rep_df = executed
    # Cross-date dedup (mirrors get_repurchase logic)
    if all(c in rep_df.columns for c in ["high_limit", "amount", "proc"]):
        completed = rep_df[rep_df["proc"] == "完成"].copy()
        executing = rep_df[rep_df["proc"] == "实施"].copy()
        other = rep_df[~rep_df["proc"].isin(["完成", "实施"])].copy()
        if not completed.empty:
            completed = completed.drop_duplicates(
                subset=["amount", "high_limit"], keep="first")
        if not executing.empty:
            executing = executing.sort_values("amount", ascending=False)
            executing = executing.drop_duplicates(
                subset=["high_limit"], keep="first")
        if not completed.empty and not executing.empty:
            completed_limits = set(completed["high_limit"].dropna())
            executing = executing[
                ~executing["high_limit"].isin(completed_limits)]
        rep_df = pd.concat(
            [completed, executing, other]).sort_values(
                "ann_date", ascending=False)

    client._store = {
        "income": income_df,
        "income_years": years,
        "balance_sheet": bs_df,
        "balance_sheet_years": years,
        "cashflow": cf_df,
        "cashflow_years": years,
        "dividends": div_df,
        "risk_free_rate": rf_df,
        "repurchase": rep_df,
    }
    return client


# ===== Feature #90: _store initialization + helpers =====


class TestStoreInitialization:
    """Test that _store is initialized and helpers work correctly."""

    def test_store_initialized_empty(self):
        """_store should be an empty dict on init."""
        client = _make_client()
        assert client._store == {}

    def test_safe_float_normal(self):
        client = _make_client()
        assert client._safe_float(42.5) == 42.5
        assert client._safe_float(0) == 0.0

    def test_safe_float_none_and_nan(self):
        client = _make_client()
        assert client._safe_float(None) is None
        assert client._safe_float(float("nan")) is None

    def test_safe_float_string(self):
        client = _make_client()
        assert client._safe_float("not_a_number") is None

    def test_get_annual_df_filters_annual(self):
        client = _make_client_with_store()
        annual = client._get_annual_df("income")
        # All end_dates should end with 1231
        for _, r in annual.iterrows():
            assert str(r["end_date"]).endswith("1231")

    def test_get_annual_df_empty_store(self):
        client = _make_client()
        annual = client._get_annual_df("nonexistent")
        assert annual.empty

    def test_get_annual_series(self):
        client = _make_client_with_store()
        series = client._get_annual_series("income", "revenue")
        assert len(series) == 5
        # First should be latest year (2024), value should be revenue
        assert series[0][0] == "2024"
        assert series[0][1] == 120140000000.0


# ===== Feature #90: 17.1 Financial trends =====


class TestFinancialTrends:
    """Test _compute_financial_trends() for §17.1."""

    def test_revenue_cagr(self):
        """CAGR: (120140/96886)^(1/4) - 1 ≈ 5.55%."""
        client = _make_client_with_store()
        result = client._compute_financial_trends()
        assert result is not None
        assert "营业收入" in result
        # 5-year CAGR should be present
        assert "5年CAGR" in result

    def test_net_profit_cagr(self):
        """Net profit CAGR should be present."""
        client = _make_client_with_store()
        result = client._compute_financial_trends()
        assert "归母净利润" in result

    def test_interest_bearing_debt(self):
        """Interest-bearing debt = st_borr + lt_borr + bond_payable + non_cur_liab_due_1y."""
        client = _make_client_with_store()
        result = client._compute_financial_trends()
        assert "有息负债" in result
        # 2024: 12800 + 5200 + 2000 + 1200 = 21200 百万元
        assert "21,200.00" in result

    def test_debt_ratio(self):
        """Debt/total_assets should be present."""
        client = _make_client_with_store()
        result = client._compute_financial_trends()
        assert "有息负债/总资产" in result

    def test_net_cash(self):
        """Net cash = money_cap - interest_bearing_debt."""
        client = _make_client_with_store()
        result = client._compute_financial_trends()
        assert "广义净现金" in result
        # 2024: 28960 - 21200 = 7760 百万元
        assert "7,760.00" in result

    def test_payout_ratio(self):
        """Payout = (cash_div * base_share * 10000) / n_income_attr_p."""
        client = _make_client_with_store()
        result = client._compute_financial_trends()
        assert "股息支付率" in result

    def test_payout_ratio_value(self):
        """Verify payout ratio for 2024:
        div_total = 0.97 * 636363.64 * 10000 = 6,172,727,308.0
        np = 10,120,000,000
        payout = 6172727308 / 10120000000 * 100 ≈ 61.00%
        """
        client = _make_client_with_store()
        result = client._compute_financial_trends()
        # Should contain ~61.00% for 2024
        assert "61.0" in result or "60.9" in result or "61.00" in result

    def test_returns_none_when_income_missing(self):
        """Should return None if income data is missing."""
        client = _make_client()
        result = client._compute_financial_trends()
        assert result is None

    def test_returns_none_when_insufficient_years(self):
        """Should return None if only 1 year of data."""
        client = _make_client()
        # Only 1 year
        df = pd.DataFrame([{
            "end_date": "20241231", "revenue": 100000000000,
            "n_income_attr_p": 10000000000,
        }])
        client._store["income"] = df
        result = client._compute_financial_trends()
        assert result is None


# ===== Feature #91: 17.2 Factor 2 inputs =====


class TestFactor2Inputs:
    """Test _compute_factor2_inputs() for §17.2."""

    def test_contains_c_and_b(self):
        """Output should show C (归母净利润) and B (少数股东损益)."""
        client = _make_client_with_store()
        result = client._compute_factor2_inputs("600887.SH")
        assert result is not None
        assert "C 归母净利润" in result
        assert "B 少数股东损益" in result

    def test_minority_ratio(self):
        """Minority ratio: 330M / 10450M ≈ 3.16% for 2024."""
        client = _make_client_with_store()
        result = client._compute_factor2_inputs("600887.SH")
        assert "少数股东占比" in result
        assert "3.1" in result  # 330/10450 ≈ 3.16%

    def test_da_and_capex(self):
        """D&A and Capex should be present."""
        client = _make_client_with_store()
        result = client._compute_factor2_inputs("600887.SH")
        assert "D 折旧与摊销" in result
        assert "E 资本开支" in result

    def test_capex_da_median(self):
        """F = Capex/D&A median across 5 years."""
        client = _make_client_with_store()
        result = client._compute_factor2_inputs("600887.SH")
        assert "Capex/D&A 5年中位数" in result
        # Verify it's a reasonable number (should be around 2.4-2.6)
        assert "F（Capex/D&A" in result

    def test_payout_mean_and_stdev(self):
        """M and N should be present."""
        client = _make_client_with_store()
        result = client._compute_factor2_inputs("600887.SH")
        assert "M（支付率3年均值）" in result
        assert "N（支付率3年标准差）" in result

    def test_buyback_annual_avg(self):
        """无 `o.json` ⇒ O 仍为 0，但说明文字必须**能区分"确认过"与"没确认"**。

        旧文案无论上游有没有回购记录都印「默认0（无法区分注销型）」，读者无法分辨；
        现由 `turtle_core.o_input.OInput.describe()` 单点给出（findings D-3）。
        """
        client = _make_client_with_store()
        result = client._compute_factor2_inputs("600887.SH")
        assert "O（年均回购金额）" in result
        assert "0.00" in result
        assert "待年报确认" in result          # 未确认状态必须显式
        assert "注销型" in result              # 说明为什么默认 0

    def test_threshold_a_share(self):
        """II is uniform across markets: max(5%, Rf+3%)."""
        client = _make_client_with_store()
        result = client._compute_factor2_inputs("600887.SH")
        assert "II（门槛值）" in result
        # Rf ≈ 2.315% → max(5%, 5.315%) = 5.315% → renders 5.31%
        assert "5.31%" in result
        assert "max(5.0%, 2.31%+3.0%)" in result

    def test_threshold_hk(self):
        """II for HK stock: max(5%, Rf+3%)."""
        client = _make_client_with_store()
        result = client._compute_factor2_inputs("00001.HK")
        # Rf ≈ 2.315%, so II = max(5%, 5.315%) = 5.315%
        assert "5.3" in result

    def test_threshold_us(self):
        """The same uniform rule applies to US listings (no separate tier)."""
        client = _make_client_with_store()
        result = client._compute_factor2_inputs("AAPL.US")
        # Rf ≈ 2.315% → max(5%, 5.315%) = 5.315%
        assert "5.31%" in result
        assert "美股" in result

    def test_oe_base(self):
        """OE_base should reference G=1.0."""
        client = _make_client_with_store()
        result = client._compute_factor2_inputs("600887.SH")
        assert "OE_base" in result
        assert "G=1.0" in result

    def test_returns_none_no_income(self):
        """Should return None if income data missing."""
        client = _make_client()
        result = client._compute_factor2_inputs("600887.SH")
        assert result is None


# ===== Feature #91: 17.6 Factor 4 price percentiles =====


class TestFactor4Inputs:
    """Test _compute_factor4_inputs() for §17.6."""

    def _make_client_with_weekly(self):
        """Client with weekly price data."""
        client = _make_client_with_store()
        # Create synthetic 10yr weekly prices
        dates = pd.date_range("2016-01-01", "2026-01-01", freq="W")
        import numpy as np
        np.random.seed(42)
        prices = 20 + np.cumsum(np.random.randn(len(dates)) * 0.3)
        prices = np.clip(prices, 10, 50)
        df = pd.DataFrame({
            "trade_date": [d.strftime("%Y%m%d") for d in dates],
            "close": prices,
            "high": prices + 0.5,
            "low": prices - 0.5,
            "vol": [100000] * len(dates),
        })
        df = df.sort_values("trade_date", ascending=True)
        client._store["weekly_prices"] = df
        return client

    def test_percentile_output(self):
        """Should output current price percentile and key percentiles."""
        client = self._make_client_with_weekly()
        result = client._compute_factor4_inputs()
        assert result is not None
        assert "当前股价历史分位" in result
        assert "50%分位价格" in result

    def test_data_point_count(self):
        """Should show number of data points."""
        client = self._make_client_with_weekly()
        result = client._compute_factor4_inputs()
        assert "10年数据点数" in result

    def test_returns_none_no_weekly(self):
        """Should return None if no weekly price data."""
        client = _make_client()
        result = client._compute_factor4_inputs()
        assert result is None


# ===== Feature #91: 17.7 SOTP inputs =====


class TestSotpInputs:
    """Test _compute_sotp_inputs() for §17.7."""

    def _make_client_with_parent(self):
        """Client with both consolidated and parent BS data."""
        client = _make_client_with_store()
        # Add parent balance sheet
        parent_df = pd.DataFrame([{
            "end_date": "20241231", "report_type": "6",
            "money_cap": 15000000000.0,
            "st_borr": 3000000000.0,
            "lt_borr": 2000000000.0,
            "bond_payable": 1000000000.0,
            "non_cur_liab_due_1y": 500000000.0,
            "total_assets": 80000000000.0,
            "total_liab": 35000000000.0,
            "total_hldr_eqy_exc_min_int": 45000000000.0,
        }])
        client._store["balance_sheet_parent"] = parent_df
        return client

    def test_sotp_output(self):
        """Should output parent vs consolidated debt comparison."""
        client = self._make_client_with_parent()
        result = client._compute_sotp_inputs()
        assert result is not None
        assert "合并口径" in result
        assert "母公司口径" in result

    def test_subsidiary_debt_ratio(self):
        """Subsidiary debt ratio = (consol - parent) / consol.
        Consol debt = 12800+5200+2000+1200 = 21200M
        Parent debt = 3000+2000+1000+500 = 6500M
        Ratio = (21200-6500)/21200 ≈ 69.3%
        """
        client = self._make_client_with_parent()
        result = client._compute_sotp_inputs()
        assert "子公司层面负债占比" in result
        assert "69." in result

    def test_returns_none_no_parent(self):
        """Should return None if parent BS not available."""
        client = _make_client_with_store()
        # No parent data in store
        result = client._compute_sotp_inputs()
        assert result is None


# ===== Feature #92: 17.3 Factor 3 Step 1 true cash revenue =====


class TestFactor3Step1:
    """Test _compute_factor3_step1() for §17.3."""

    def test_output_contains_headers(self):
        """Output should have §17.3 header and table columns."""
        client = _make_client_with_store()
        result = client._compute_factor3_step1()
        assert result is not None
        assert "17.3 因子3·步骤1" in result
        assert "S 营业收入" in result
        assert "T 应收变动" in result
        assert "U 合同负债变动" in result
        assert "收款比率" in result

    def test_ar_change_values(self):
        """AR change (T) for 2024: 1850M - 1720M = 130M yuan → 130.00 百万元."""
        client = _make_client_with_store()
        result = client._compute_factor3_step1()
        assert "130.00" in result  # T for 2024

    def test_true_cash_revenue(self):
        """True cash rev for 2024: 120,140M - 130M - 0 = 120,010M → 120,010.00."""
        client = _make_client_with_store()
        result = client._compute_factor3_step1()
        assert "120,010.00" in result

    def test_collection_ratio(self):
        """Collection ratio ≈ 99.89% for 2024."""
        client = _make_client_with_store()
        result = client._compute_factor3_step1()
        assert "99.8" in result or "99.9" in result

    def test_stores_true_cash_rev(self):
        """_store should contain _true_cash_rev after computation."""
        client = _make_client_with_store()
        client._compute_factor3_step1()
        assert "_true_cash_rev" in client._store
        assert "2024" in client._store["_true_cash_rev"]

    def test_returns_none_insufficient_data(self):
        """Should return None with < 2 years."""
        client = _make_client()
        result = client._compute_factor3_step1()
        assert result is None

    def test_four_years_computed(self):
        """With 5 years of data, should compute 4 year-pairs."""
        client = _make_client_with_store()
        result = client._compute_factor3_step1()
        # Should have years 2024, 2023, 2022, 2021
        assert "2024" in result
        assert "2023" in result
        assert "2022" in result
        assert "2021" in result


# ===== Feature #92: 17.4 Factor 3 Step 4 operating outflows =====


class TestFactor3Step4:
    """Test _compute_factor3_step4() for §17.4."""

    def test_output_contains_headers(self):
        """Output should have §17.4 header and W1-W4."""
        client = _make_client_with_store()
        result = client._compute_factor3_step4()
        assert result is not None
        assert "17.4 因子3·步骤4" in result
        assert "W1 供应商" in result
        assert "W2 员工" in result
        assert "W3 现金税" in result
        assert "W4 利息" in result
        assert "W 合计" in result

    def test_w1_supplier(self):
        """W1 for 2024: oper_cost=85030M + max(0, -(8920M-8150M))=0 → 85030M."""
        client = _make_client_with_store()
        result = client._compute_factor3_step4()
        assert "85,030.00" in result

    def test_w2_employee(self):
        """W2 for 2024: c_pay_to_staff = 8520M."""
        client = _make_client_with_store()
        result = client._compute_factor3_step4()
        assert "8,520.00" in result

    # --- W2 three-level fallback chain (added 2026-10-13, known_issues.md A4) ---

    def test_w2_prefers_income_staff_cost(self):
        """W2 chain (1): 利润表「薪金福利支出」优先于现金流量表字段.

        HK by-nature reports expose staff cost on the income statement only;
        the cashflow field c_pay_to_staff is A-share specific. Real case:
        06862 FY2025 staff cost = 14,073.0 (百万).
        """
        client = _make_client_with_store()
        client._store["income"]["c_pay_to_staff"] = 99_000_000_000.0
        result = client._compute_factor3_step4()
        assert "99,000.00" in result
        # hitting chain (1) must not emit the SGA-fallback dagger
        assert "†" not in result

    def test_w2_sga_fallback_is_daggered_and_documented(self):
        """W2 chain (3): both real sources missing → SGA proxy, flagged with †."""
        client = _make_client_with_store()
        client._store["cashflow"]["c_pay_to_staff"] = None
        result = client._compute_factor3_step4()
        # SGA = sell 10,520 + admin 3,890 + rd 580 = 14,990 (百万)
        assert "14,990.00" in result
        assert "†" in result
        assert "SGA 兜底" in result

    def test_w3_cash_tax(self):
        """W3 for 2024: income_tax - (DTA_chg - DTL_chg)
        = 2483M - (170M - 40M) = 2483M - 130M = 2353M."""
        client = _make_client_with_store()
        result = client._compute_factor3_step4()
        assert "2,353.00" in result

    def test_w4_interest(self):
        """W4 for 2024: finance_exp = 320M."""
        client = _make_client_with_store()
        result = client._compute_factor3_step4()
        assert "320.00" in result

    def test_stores_w_total(self):
        """_store should contain _w_total after computation."""
        client = _make_client_with_store()
        client._compute_factor3_step4()
        assert "_w_total" in client._store
        assert "2024" in client._store["_w_total"]

    def test_returns_none_no_data(self):
        """Should return None with no data."""
        client = _make_client()
        result = client._compute_factor3_step4()
        assert result is None


# ===== Feature #92: 17.5 Factor 3 sensitivity base =====


class TestSensitivityBase:
    """Test _compute_factor3_sensitivity_base() for §17.5."""

    def _run_prerequisites(self, client):
        """Run step1 and step4 to populate _store."""
        client._compute_factor3_step1()
        client._compute_factor3_step4()

    def test_output_contains_headers(self):
        """Output should have §17.5 header and the OCF-anchored table."""
        client = _make_client_with_store()
        self._run_prerequisites(client)
        result = client._compute_factor3_sensitivity_base()
        assert result is not None
        assert "17.5 因子3·步骤7" in result
        assert "AA（OCF 锚定）" in result
        assert "倒推结余（审计）" in result
        assert "AA" in result

    def test_base_surplus_2024(self):
        """Audit column keeps the legacy reconstructed base: 120010M - 96223M - 7850M = 15937M."""
        client = _make_client_with_store()
        self._run_prerequisites(client)
        result = client._compute_factor3_sensitivity_base()
        assert "15,937.00" in result

    def test_aa_is_ocf_minus_capex(self):
        """AA = OCF − |Capex|, per the documented factor-3 definition.

        2024: OCF 16,850M − Capex 7,850M = 9,000M.
        """
        client = _make_client_with_store()
        self._run_prerequisites(client)
        result = client._compute_factor3_sensitivity_base()
        assert "9,000.00" in result
        assert client._store["factor3_sensitivity"]["aa_basis"] == "ocf_minus_capex"

    def test_aa_2y_default(self):
        """AA_2y (default): mean of [9000, 8420] = 8710.00."""
        client = _make_client_with_store()
        self._run_prerequisites(client)
        result = client._compute_factor3_sensitivity_base()
        assert "8,710.00" in result

    def test_aa_all_reference(self):
        """AA_all: mean of [9000, 8420, 7880, 7300, 6460] = 7812.00."""
        client = _make_client_with_store()
        self._run_prerequisites(client)
        result = client._compute_factor3_sensitivity_base()
        assert "7,812.00" in result

    def test_aa_latest_exposed(self):
        """AA_latest (short-sighted 口径) must be surfaced for later use.

        Store values are RAW currency units (元); the rendered table divides by 1e6.
        """
        client = _make_client_with_store()
        self._run_prerequisites(client)
        result = client._compute_factor3_sensitivity_base()
        assert "AA_latest" in result
        assert client._store["factor3_sensitivity"]["aa_latest"] == 9_000_000_000.0
        assert client._store["factor3_sensitivity"]["aa_2y"] == 8_710_000_000.0

    def test_reconstructed_path_deviation_is_flagged(self):
        """The old reconstruction diverges hugely → must be flagged, not silently kept."""
        client = _make_client_with_store()
        self._run_prerequisites(client)
        result = client._compute_factor3_sensitivity_base()
        assert "倒推口径与 OCF 锚定口径偏差过大" in result

    def test_audit_column_documents_its_absence(self):
        """AA is independent of §17.3/§17.4; only the audit column needs them."""
        client = _make_client_with_store()
        result = client._compute_factor3_sensitivity_base()
        assert result is not None
        assert "倒推口径不可用" in result
        assert "AA（OCF 锚定）" in result

    def test_cv_present(self):
        """Revenue CV should be present."""
        client = _make_client_with_store()
        self._run_prerequisites(client)
        result = client._compute_factor3_sensitivity_base()
        assert "收入波动率 CV" in result

    def test_lambda_present(self):
        """λ should be present."""
        client = _make_client_with_store()
        self._run_prerequisites(client)
        result = client._compute_factor3_sensitivity_base()
        assert "经营杠杆系数 λ" in result
        assert "λ可靠性" in result

    def test_c3_gate_reports_when_ocf_or_capex_missing(self):
        """C3 hard gate: without OCF+Capex for ≥2 years, say why instead of guessing."""
        client = _make_client_with_store()
        cf = client._store["cashflow"].copy()
        cf["n_cashflow_act"] = None
        client._store["cashflow"] = cf
        result = client._compute_factor3_sensitivity_base()
        # 硬门禁必须**登记为 missing 并返回 None**：返回一段"本节未生成"的正文会让
        # assembly 按"有内容"记成 ok，与 §17 状态契约（heading ⟺ ok）冲突（独立审计 P1）。
        assert result is None
        assert "factor3_sensitivity" not in client._store
        reg = client._section_registry()
        assert reg.status_of("17.5") == "missing", reg.status_of("17.5")
        assert reg.finalize_entries()["17.5"]["reason"] == "no_cashflow" \
            if hasattr(reg, "finalize_entries") else True

    # --- 21e: documented extra deductions (known_issues.md §3.21) ---

    @staticmethod
    def _with_cashflow_fields(client, **fields):
        cf = client._store["cashflow"].copy()
        for name, value in fields.items():
            cf[name] = value
        client._store["cashflow"] = cf
        return client

    def test_documented_deductions_reduce_aa(self):
        """收购/租赁/少数股权/已付利息(融资) must all reduce AA.

        185 (百万) deducted from every year:
          AA_2y = ((9000−185) + (8420−185)) / 2 = 8525
        """
        client = self._with_cashflow_fields(
            _make_client_with_store(),
            c_pay_acq_sub=100_000_000.0,
            c_pay_fin_lease=50_000_000.0,
            c_pay_minority=25_000_000.0,
            c_pay_int_finance=10_000_000.0,
        )
        self._run_prerequisites(client)
        result = client._compute_factor3_sensitivity_base()
        assert "185.00" in result
        assert "8,525.00" in result
        assert client._store["factor3_sensitivity"]["aa_2y"] == 8_525_000_000.0

    def test_net_inflow_is_not_treated_as_outflow(self):
        """Negative value = net inflow — `abs()` would wrongly deduct it.

        06049 FY2025 收购附属公司 = −5.94 (百万) is a real net inflow.
        """
        client = self._with_cashflow_fields(
            _make_client_with_store(), c_pay_acq_sub=-5_940_000.0)
        self._run_prerequisites(client)
        client._compute_factor3_sensitivity_base()
        assert client._store["factor3_sensitivity"]["aa_2y"] == 8_710_000_000.0

    def test_operating_classified_interest_is_not_deducted(self):
        """已付利息(经营) already sits inside OCF — deducting it would double count."""
        client = self._with_cashflow_fields(
            _make_client_with_store(),
            c_pay_int_operating=999_000_000.0,   # must be ignored
            c_pay_int_finance=None,
        )
        self._run_prerequisites(client)
        client._compute_factor3_sensitivity_base()
        assert client._store["factor3_sensitivity"]["aa_2y"] == 8_710_000_000.0

    def test_financing_classified_interest_is_deducted(self):
        """已付利息(融资) is outside OCF, so it must be deducted."""
        client = self._with_cashflow_fields(
            _make_client_with_store(),
            c_pay_int_finance=10_000_000.0,
            c_pay_int_operating=None,
        )
        self._run_prerequisites(client)
        client._compute_factor3_sensitivity_base()
        assert client._store["factor3_sensitivity"]["aa_2y"] == 8_700_000_000.0

    def test_a_share_has_no_deduction_fields(self):
        """A-share standard field set has none of these items → AA unchanged."""
        client = _make_client_with_store()
        for col in ("c_pay_acq_sub", "c_pay_fin_lease", "c_pay_minority",
                    "c_pay_int_finance"):
            assert col not in client._store["cashflow"].columns
        self._run_prerequisites(client)
        result = client._compute_factor3_sensitivity_base()
        assert client._store["factor3_sensitivity"]["aa_2y"] == 8_710_000_000.0
        assert "A 股不支持①" in result

    def test_returns_none_without_cashflow_data(self):
        """No store at all → None (assembly reports the block as not generated)."""
        client = _make_client()
        assert client._compute_factor3_sensitivity_base() is None


# ===== Integration tests =====


class TestComputeDerivedMetrics:
    """Test the main compute_derived_metrics() method."""

    def test_section_header(self):
        """Output should contain §17 header."""
        client = _make_client_with_store()
        result = client.compute_derived_metrics("600887.SH")
        assert "17. 衍生指标" in result
        assert "Python 预计算" in result

    def test_contains_171(self):
        """Output should contain §17.1 sub-section."""
        client = _make_client_with_store()
        result = client.compute_derived_metrics("600887.SH")
        assert "17.1 财务趋势速览" in result

    def test_contains_172(self):
        """Output should contain §17.2 sub-section."""
        client = _make_client_with_store()
        result = client.compute_derived_metrics("600887.SH")
        assert "17.2 因子2输入参数" in result

    def test_contains_173(self):
        """Output should contain §17.3 sub-section."""
        client = _make_client_with_store()
        result = client.compute_derived_metrics("600887.SH")
        assert "17.3 因子3·步骤1" in result

    def test_contains_174(self):
        """Output should contain §17.4 sub-section."""
        client = _make_client_with_store()
        result = client.compute_derived_metrics("600887.SH")
        assert "17.4 因子3·步骤4" in result

    def test_contains_175(self):
        """Output should contain §17.5 sub-section."""
        client = _make_client_with_store()
        result = client.compute_derived_metrics("600887.SH")
        assert "17.5 因子3·步骤7" in result

    def test_graceful_with_empty_store(self):
        """Must not crash; absent sections must be explicitly enumerated.

        New contract (known_issues.md P2-20): a `### 17.N` heading exists **iff**
        that section's status is `ok`. Absence used to be silent; now it is a
        machine-readable `missing` entry, so "not computed" can never be read as
        "computed as zero" (nor trigger an LLM hand-calculation).
        """
        client = _make_client()
        result = client.compute_derived_metrics("600887.SH")
        assert "17. 衍生指标" in result
        assert headings_in_pack(result) == set()          # no headings at all
        payload = client._store["section17_status"]
        assert payload["sections"], "status contract must enumerate every section"
        assert all(e["status"] == "missing" for e in payload["sections"].values())
        assert "❌ missing" in result
        assert check_pack_consistency(result, payload) == []

    def test_contains_disclaimer(self):
        """Output should contain the disclaimer about deterministic computation."""
        client = _make_client_with_store()
        result = client.compute_derived_metrics("600887.SH")
        assert "确定性计算" in result


# ===== Feature #94: 17.8 EV baseline + "买入就是胜利"基准价 =====


def _make_client_with_ev_store():
    """Create client with full _store including basic_info and weekly_prices."""
    client = _make_client_with_store()

    # Add basic_info
    basic_df = _load_mock("daily_basic.json")
    client._store["basic_info"] = basic_df
    # Add weekly_prices with known min=15.50
    import numpy as np
    np.random.seed(42)
    dates = pd.date_range("2016-01-01", "2026-01-01", freq="W")
    prices = 20 + np.cumsum(np.random.randn(len(dates)) * 0.3)
    prices = np.clip(prices, 15.50, 50)
    # Force one data point at exactly 15.50 to ensure min
    prices[10] = 15.50
    df = pd.DataFrame({
        "trade_date": [d.strftime("%Y%m%d") for d in dates],
        "close": prices,
        "high": prices + 0.5,
        "low": prices - 0.5,
        "vol": [100000] * len(dates),
    })
    df = df.sort_values("trade_date", ascending=True)
    client._store["weekly_prices"] = df
    return client


class TestFactor4EVBaseline:
    """Test _compute_factor4_ev_baseline() for §17.8."""

    def test_section_header(self):
        """Output should contain §17.8 header."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert result is not None
        assert "17.8" in result

    def test_ev(self):
        """EV = 175000 + 21200 - 28960 = 167,240.00."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "167,240.00" in result

    def test_ebitda(self):
        """EBITDA = 12890 + 320 + 3200 = 16,410.00."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "16,410.00" in result

    def test_ev_ebitda(self):
        """EV/EBITDA = 167240/16410 = 10.19x."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "10.19" in result

    def test_cash_adjusted_pe(self):
        """(175000 - 7760) / 10120 = 16.53x."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "16.53" in result

    def test_fcf_yield(self):
        """FCF/市值 = 9000/175000 = 5.14%."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "5.14" in result

    def test_pb(self):
        """P/B = 175000/58940 = 2.97x."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "2.97" in result

    def test_net_debt_ebitda(self):
        """(21200 - 28960) / 16410 = -0.47x."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "-0.47" in result

    def test_goodwill_ratio(self):
        """6780/133890 = 5.06%."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "5.06" in result

    def test_ibd_ratio(self):
        """21200/133890 = 15.83%."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "15.83" in result

    def test_dividend_yield(self):
        """0.97/27.50 = 3.53%."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "3.53" in result or "3.5" in result

    def test_baseline_bvps(self):
        """BVPS = 58940M yuan / 6363636400 shares = 9.26."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "9.26" in result

    def test_baseline_10yr_low(self):
        """10yr low should be 15.50."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "15.50" in result

    def test_composite_baseline(self):
        """基准价按**语义分组**，不给跨语义的单一均值。

        旧实现把五项等权平均（一个既不是底价也不是公允价的数），06862 上因此得到 9.44，
        高于悲观档买点 8.67 ⇒ "低于基准线 — 买入就是胜利" 会在模型判定"不可买"的价位触发。
        现在分列：**综合底价** = ①②③ 均值（1.46/9.26/15.50 → 8.74）；
        **参考公允价** = ④⑤ 均值（29.56/43.85 → 36.70）。
        """
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "综合底价（底价型锚点算术平均，3 项）= 8.74" in result
        assert "参考公允价（收益率型锚点算术平均，2 项" in result
        assert "36.70" in result

    def test_premium(self):
        """(27.50/15.50 - 1) × 100 = 77.4%."""
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "77.4" in result or "溢价" in result

    def test_premium_verdict(self):
        """档位只挂在**收益率型公允价**读数上；底价读数只回答"是否低于底价"。

        该夹具：27.50 / 36.70 − 1 = −25.1% ⇒ "安全边际充足"；
        底价读数 27.50 / 8.74 − 1 = +214.6% ⇒ 明确**不构成**「低于底价 — 买入就是胜利」。
        """
        client = _make_client_with_ev_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert "安全边际充足" in result
        # 小节标题本身含"买入就是胜利"，故断言**判定串**而非该短语
        assert "低于底价 — 买入就是胜利" not in result, "高于底价时不得发出买点语义的读数"
        assert "不构成" in result

    def test_returns_none_no_basic_info(self):
        """Should return None if basic_info missing."""
        client = _make_client_with_store()
        result = client._compute_factor4_ev_baseline("600887.SH")
        assert result is None

    def test_integration(self):
        """§17.8 should appear in compute_derived_metrics output."""
        client = _make_client_with_ev_store()
        result = client.compute_derived_metrics("600887.SH")
        assert "17.8" in result


# ===== Non-calendar fiscal year tests =====

class TestGetAnnualDfNonCalendarFY:
    """Tests for _get_annual_df with non-calendar fiscal years."""

    def test_get_annual_df_september_fy(self):
        """With _fy_end_month=9, only September dates should be annual."""
        client = _make_client()
        client._fy_end_month = 9
        client._store["income"] = pd.DataFrame([
            {"end_date": "20240928", "revenue": 100},
            {"end_date": "20230930", "revenue": 90},
            {"end_date": "20220924", "revenue": 80},
            {"end_date": "20250331", "revenue": 50},  # interim
        ])
        annual = client._get_annual_df("income")
        assert len(annual) == 3
        for _, r in annual.iterrows():
            assert str(r["end_date"])[4:6] == "09"

    def test_get_annual_df_default_fy(self):
        """Default _fy_end_month=12 should filter to 1231."""
        client = _make_client()
        client._store["income"] = pd.DataFrame([
            {"end_date": "20241231", "revenue": 100},
            {"end_date": "20240930", "revenue": 80},
            {"end_date": "20231231", "revenue": 90},
        ])
        annual = client._get_annual_df("income")
        assert len(annual) == 2
        for _, r in annual.iterrows():
            assert str(r["end_date"]).endswith("1231")


class TestUnitLabelsInDerivedMetrics:
    """Tests for currency-aware unit labels in §17."""

    def test_unit_label_in_financial_trends_usd(self):
        """§17.1 should use 百万美元 for US stocks."""
        client = _make_client()
        client._currency = "USD"
        client._fy_end_month = 9
        # Populate store with minimal data
        client._store["income"] = pd.DataFrame([
            {"end_date": "20240928", "revenue": 100e9, "n_income_attr_p": 20e9},
            {"end_date": "20230930", "revenue": 90e9, "n_income_attr_p": 18e9},
            {"end_date": "20220924", "revenue": 80e9, "n_income_attr_p": 15e9},
            {"end_date": "20210925", "revenue": 70e9, "n_income_attr_p": 12e9},
            {"end_date": "20200926", "revenue": 60e9, "n_income_attr_p": 10e9},
        ])
        client._store["balance_sheet"] = pd.DataFrame([
            {"end_date": "20240928", "st_borr": 1e9, "lt_borr": 5e9, "bond_payable": 0,
             "non_cur_liab_due_1y": 0, "money_cap": 10e9, "trad_asset": 0, "total_assets": 100e9},
            {"end_date": "20230930", "st_borr": 1e9, "lt_borr": 5e9, "bond_payable": 0,
             "non_cur_liab_due_1y": 0, "money_cap": 10e9, "trad_asset": 0, "total_assets": 95e9},
        ])
        client._store["cashflow"] = pd.DataFrame([
            {"end_date": "20240928", "n_cashflow_act": 30e9, "c_pay_acq_const_fiolta": 5e9,
             "c_pay_dist_dpcp_int_exp": 2e9},
            {"end_date": "20230930", "n_cashflow_act": 28e9, "c_pay_acq_const_fiolta": 4e9,
             "c_pay_dist_dpcp_int_exp": 1.5e9},
        ])
        client._store["dividends"] = pd.DataFrame(columns=["end_date", "cash_div_tax"])
        result = client._compute_financial_trends()
        assert result is not None
        assert "百万美元" in result
        assert "百万元" not in result.replace("百万美元", "")


class TestFactor4EVBaselineCaliber:
    """§17.8 must use ONE caliber, and Tushare's fina_indicator must not silently override it.

    Live-run regression (002984, 2026-10-13). The old code overwrote ``ebitda``/``net_debt``/
    ``fcf`` with Tushare ``fina_indicator`` fields whose definitions differ. Observed
    consequences in the real data pack:

    - the EV row implied a **third** cash caliber (2,101.73 vs 货币资金 2,242.01), so §17.8
      disagreed with §17.7 (控股结构辅助) inside the same data pack;
    - FCF yield was reported as **+2.88%** while the real FCF (经营现金流 − 资本开支) was
      −231.85 百万元 = **−1.59%** — the sign flipped, because Tushare's ``fcff`` is free cash
      flow to the *firm*, not FCF;
    - EBITDA was reported as 1,926.79 although the stated formula
      (营业利润 + 财务费用 + D&A) yields 1,879.95.

    The pre-existing fixture never populated ``fina_indicators``, which is exactly why the
    original tests could not catch this.
    """

    @staticmethod
    def _client_with_conflicting_fina():
        import pandas as pd

        client = _make_client_with_ev_store()
        client._store["fina_indicators"] = pd.DataFrame({
            "end_date": ["20251231"],
            "ebitda": [1.92679e9],   # ≠ 营业利润+财务费用+D&A (16,410)
            "netdebt": [6.1753e8],   # ≠ 有息负债−货币资金 (which yields −7,760)
            "fcff": [4.2e8],         # positive, unlike this stock's real FCF
        })
        return client

    def test_primary_rows_keep_the_self_computed_caliber(self):
        """Tushare's differently-defined fields must not move the primary ratios."""
        result = self._client_with_conflicting_fina()._compute_factor4_ev_baseline("600887.SH")
        assert "167,240.00" in result, "EV 必须仍为 市值+有息负债−货币资金"
        assert "16,410.00" in result, "EBITDA 必须仍为 营业利润+财务费用+D&A"
        assert "5.14" in result, "FCF收益率 必须仍为 (经营现金流−资本开支)/市值"
        assert "-0.47" in result, "净负债/EBITDA 必须仍为 (有息负债−货币资金)/EBITDA"

    def test_tushare_values_appear_only_as_labelled_reference_rows(self):
        result = self._client_with_conflicting_fina()._compute_factor4_ev_baseline("600887.SH")
        assert "· 对照" in result, "Tushare 值必须以对照行出现"
        assert "1,926.79" in result, "Tushare EBITDA 应作为对照行出现"
        assert "617.53" in result, "Tushare netdebt 应作为对照行出现"
        assert "420.00" in result, "Tushare FCFF 应作为对照行出现"
        assert "不是" in result and "FCFF" in result, "必须声明 FCFF ≠ FCF"

    def test_caliber_note_is_present(self):
        result = self._client_with_conflicting_fina()._compute_factor4_ev_baseline("600887.SH")
        assert "口径说明" in result
        assert "货币资金" in result

    def test_ev_never_exceeds_market_cap_when_net_cash(self):
        """With net cash the narrow caliber must produce EV < 市值 (not a third caliber)."""
        result = self._client_with_conflicting_fina()._compute_factor4_ev_baseline("600887.SH")
        # 市值 175,000 + 净负债 (21,200 − 28,960) = 167,240
        assert "167,240.00" in result
        assert "185,175.30" not in result, "不得出现 Tushare netdebt 造成的 EV"


class TestSectionStatusContractOnMissing:
    """§17 状态契约：`### 17.N` heading 存在 ⟺ 该节 status == ok。

    重建时 §17.5 在硬门禁下把"本节未生成"渲染进正文**并返回非空字符串**，
    于是 assembly 记成 ok —— heading 与 ok 同时成立、却什么都没算出来（独立审计 P1）。
    """

    def test_gate_failure_registers_missing_and_omits_the_heading(self):
        client = _make_client_with_store()
        cf = client._store["cashflow"].copy()
        cf["n_cashflow_act"] = None
        client._store["cashflow"] = cf
        reg = client._section_registry()
        before = reg.status_of("17.5")
        assert client._compute_factor3_sensitivity_base() is None
        # 未登记 ⇒ 契约按 missing 兜底；显式登记后仍是 missing，绝不是 ok
        assert reg.status_of("17.5") == "missing"
        assert reg.status_of("17.5") != before or before != "ok"

class TestBuybackAnnualAvgFromSidecar:
    """`o.json` 接线（findings D-3）：默认行为不变；**非零 O 必须有来源**。"""

    def _client(self, tmp_path, payload, monkeypatch):
        client = _make_client_with_store()
        client._store["_output_path"] = str(tmp_path / "data_pack_market.md")
        if payload is not None:
            (tmp_path / "o.json").write_text(json.dumps(payload, ensure_ascii=False),
                                             encoding="utf-8")
        return client

    def test_default_is_zero_and_unchanged(self, tmp_path):
        client = self._client(tmp_path, None, None)
        o = client._o_input()
        assert o.o_annual_mm == 0.0 and o.is_default

    def test_nonzero_with_source_flows_into_section(self, tmp_path):
        client = self._client(tmp_path, {
            "o_annual_mm": 123.4, "years": 5, "source": "2025年报 p.56",
            "confirmed_by": "human"}, None)
        result = client._compute_factor2_inputs("600887.SH")
        assert "123.40" in result and "p.56" in result
        assert client._o_input().o_annual_mm == 123.4

    def test_nonzero_without_source_rejected_and_logged(self, tmp_path):
        """**核心**：没有来源的非零 O 比 0 更糟 ⇒ 拒绝并按 0，且必须登记。"""
        client = self._client(tmp_path, {"o_annual_mm": 999.0}, None)
        o = client._o_input()
        assert o.o_annual_mm == 0.0 and o.is_default
        warns = client._store.get("_o_input_warnings") or []
        assert any("o_unsourced" in w for w in warns), "被拒的 o.json 必须留下痕迹"

    def test_all_sections_share_one_value(self, tmp_path):
        """§15/§17.9/§17.11/§17.14 必须用**同一个** O —— 三次独立字面量曾是隐患。"""
        client = self._client(tmp_path, {
            "o_annual_mm": 50.0, "source": "年报 p.9", "confirmed_by": "llm"}, None)
        assert client._o_input() is client._o_input()      # 缓存 ⇒ 同对象
        assert client._o_input().o_annual_mm == 50.0

    def test_rejection_does_not_raise(self, tmp_path):
        """坏文件不得让整节炸掉（那样 46 个测试全红，正是我这次踩的坑）。"""
        client = self._client(tmp_path, None, None)
        (tmp_path / "o.json").write_text("{not json", encoding="utf-8")
        client._store.pop("_o_input_cache", None)
        result = client._compute_factor2_inputs("600887.SH")
        assert "O（年均回购金额）" in result


class TestPayoutCrosscheckSingleMethod:
    """§17.10：**只有一路可用时偏差无从谈起 ≠ 偏差为 0**。

    实测事故（06078 海吉亚医疗，2026-10-16）：港股法1 停用 + 法3 缺
    `dividends_declared.json` ⇒ 只有法2 出数。旧实现 `max_dev` 初值 0.0、
    `max_pair` 初值 None，于是"单值"与"零偏差"同时成立；渲染稳定性一行时
    `max_pair[0]` 下标 None → `TypeError: 'NoneType' object is not subscriptable`
    ⇒ §17.10 整节 missing ⇒ §17.9/§17.11/§17.14（穿透回报率网格、悲观基准网格）
    连带消失 —— 交付物里最核心的决策数字全部缺失，而门禁只报 6 节 missing。

    canonical 口径见 `turtle_core/decision.py::_max_pairwise_dev`：可用值 < 2 时
    返回 None。本节必须与之同口径。
    """

    @staticmethod
    def _client(monkeypatch, years=("2025", "2024", "2023")):
        import pandas as pd
        client = _make_client()
        income = pd.DataFrame([
            {"end_date": f"{y}1231", "n_income_attr_p": 100_000_000.0 * (i + 1),
             "basic_eps": 0.5 * (i + 1)}
            for i, y in enumerate(years)
        ])
        cashflow = pd.DataFrame([
            {"end_date": f"{y}1231", "c_pay_dist_dpcp_int_exp": 10_000_000.0}
            for y in years
        ])
        client._store["income"] = income
        client._store["cashflow"] = cashflow
        # 法1 停用（港股路径）/ 法3 无 declared 数据 ⇒ 只余法2
        client._store.pop("dividends", None)
        client._store.pop("dividends_hk", None)
        monkeypatch.setattr(client, "_get_payout_by_year", lambda: {})
        return client

    def test_single_method_does_not_raise_and_falls_back_to_method2(self, monkeypatch):
        client = self._client(monkeypatch)
        md = client._compute_payout_crosscheck()          # 旧实现：TypeError
        assert md is not None
        pc = client._store["payout_crosscheck"]
        assert pc["m1"] is None and pc["m3"] is None
        assert pc["m2"] is not None
        assert pc["m_rec"] == pc["m2"] and pc["m_rec_label"] == "法2"
        assert pc["m_pess"] == pc["m2"]
        # 必须**明说**未经交叉校验，而不是假装"偏差 0% 一致通过"
        assert "未经交叉校验" in pc["m_rec_reason"]
        assert "偏差" not in pc["m_rec_reason"]

    def test_single_method_is_not_recorded_as_a_stability_reading(self, monkeypatch):
        """单值不得登记成 stability 项 —— 那会读成"偏差 0%，稳定"。"""
        client = self._client(monkeypatch)
        client._compute_payout_crosscheck()
        assert "M_method" not in (client._store.get("stability") or {})

    def test_two_methods_still_compute_deviation(self, monkeypatch):
        """回归护栏：≥2 路可用时偏差与 pair 标签必须照旧。"""
        client = self._client(monkeypatch)
        monkeypatch.setattr(client, "_get_payout_by_year",
                            lambda: {"2025": 40.0, "2024": 40.0, "2023": 40.0})
        client._compute_payout_crosscheck()
        pc = client._store["payout_crosscheck"]
        assert pc["m2"] is not None and pc["m3"] == 40.0
        assert "法2" in pc["m_rec_reason"] and "法3" in pc["m_rec_reason"]
        assert client._store["stability"]["M_method"]["value"] > 15.0


class TestPayoutWindowZeroFill:
    """§17.10 的三年均值：窗口 = **最近 3 个财年**，窗口内**有证据的零**必须按 0 计入。

    实测事故（06078 海吉亚医疗，2026-10-16）：公司 FY2023–FY2025 **连续三个财年零派息**，
    而 §5 现金流量表**没有对应的「已付股息」行**（真零 = 没有行）。旧实现用
    「前 3 个非空值」取样 ⇒ 把两个真零当缺失剔除、**跨到更早的财年**：
      法2 由 4.61% 被抬到 7.64%；法3 更因窗口内只剩一个点而**根本没做除法**，由 4.53% 抬到 13.58%。
    同一个洞把 M_pess 抬高约 68%，直接放大 §17.14 的买点。
    """

    @staticmethod
    def _client(monkeypatch, dist, ni, declared=None, extra_cf_years=None):
        import pandas as pd
        client = _make_client()
        income = pd.DataFrame([
            {"end_date": f"{y}1231", "n_income_attr_p": v, "basic_eps": v / 618.499e6}
            for y, v in ni.items()])
        rows = [{"end_date": f"{y}1231", "c_pay_dist_dpcp_int_exp": v}
                for y, v in dist.items()]
        # ⚠️ 真实链路上，某财年**有现金流量表行但没有「已付股息」科目**（pivot 出 NaN），
        # 与"整个财年没有行"是两回事。用 `dist` 里值为 None 的年份模拟前者。
        for y, v in (extra_cf_years or {}).items():
            rows.append({"end_date": f"{y}1231", "c_pay_dist_dpcp_int_exp": v})
        cf = pd.DataFrame(rows) if rows else pd.DataFrame(
            columns=["end_date", "c_pay_dist_dpcp_int_exp"])
        # 直接把年报表交给 `_get_annual_df`：真实链路里 `end_date` 经长表→宽表 pivot 后
        # 可能是数值型，用 store 注入会让 `str[4:6]` 匹配不上而静默取到空表
        # （我第一版测试就栽在这里 —— 测试装置必须钉住输入形状，否则测的是装置）。
        client._store["income"] = income
        client._store["cashflow"] = cf
        monkeypatch.setattr(client, "_get_annual_df",
                            lambda key: income if key == "income" else cf)
        client._store.pop("dividends", None)
        client._store.pop("dividends_hk", None)
        monkeypatch.setattr(client, "_get_payout_by_year",
                            lambda: dict(declared or {}))
        monkeypatch.setattr(client, "_declared_dividends",
                            lambda: dict(declared or {}))
        return client

    def test_missing_flat_year_inside_window_counts_as_zero(self, monkeypatch):
        # FY2024 在 §3 里存在、但 §5 无分配行 ⇒ 有证据的零，而不是"缺失"
        client = self._client(
            monkeypatch,
            dist={"2025": 2_393_000.0, "2023": 94_479_000.0, "2021": 74_081_000.0},
            ni={"2025": 164_526_000.0, "2024": 598_261_000.0, "2023": 682_928_000.0,
                "2022": 476_784_000.0, "2021": 441_457_000.0},
            extra_cf_years={"2024": None, "2022": None})
        client._compute_payout_crosscheck()
        m2 = client._store["payout_crosscheck"]["m2"]
        # 窗口 {2025,2024,2023}；2024 补 0 ⇒ (1.454 + 0 + 13.834)/3 = 5.10（**不是** 7.64）
        assert abs(m2 - 5.10) < 0.05, f"法2 应为 5.10%，实为 {m2:.2f}%（旧实现给 7.64%）"

    def test_flat_year_outside_income_window_is_not_filled(self, monkeypatch):
        """§3 没有那一年 ⇒ 无从判定，**不得**补零（避免凭空虚造一个零派息事实）。"""
        client = self._client(
            monkeypatch,
            dist={"2025": 2_393_000.0, "2023": 94_479_000.0},
            ni={"2025": 164_526_000.0, "2024": 598_261_000.0, "2023": 682_928_000.0})
        client._compute_payout_crosscheck()
        # 只有 2025/2023 有值且都在窗口内 ⇒ (1.45+13.83)/2，未凭空补 0
        assert abs(client._store["payout_crosscheck"]["m2"] - 7.64) < 0.05

    def test_declared_sidecar_absent_means_method_unavailable_not_zero(self, monkeypatch):
        """sidecar 缺失 ⇒ 法3「不可用」，**不是** 0%（0 会假装公司已宣告零派息）。"""
        client = self._client(
            monkeypatch,
            dist={"2025": 2_393_000.0, "2023": 94_479_000.0},
            ni={"2025": 164_526_000.0, "2024": 598_261_000.0, "2023": 682_928_000.0})
        client._compute_payout_crosscheck()
        assert client._store["payout_crosscheck"]["m3"] is None


class TestDividendsDeclaredZeroIsASupportedFact:
    """`value = 0` + `kind = "none"` = **已宣告不派付**，与"缺这条记录"语义不同。"""

    def test_zero_with_kind_none_passes_schema(self, tmp_path):
        import json
        import sidecar_contract as sc
        (tmp_path / "dividends_declared.json").write_text(json.dumps({
            "declared_dps": {"2025": {"value": 0, "currency": "CNY", "kind": "none",
                                      "source": "年报附注13：概无派付或建议派付任何股息",
                                      "date": "2026-03-30"}}}), encoding="utf-8")
        rep = sc.validate_dir(str(tmp_path))
        assert rep.ok, f"显式零派息必须被接受，实得 {[f.detail for f in rep.findings]}"

    def test_negative_still_rejected(self, tmp_path):
        import json
        import sidecar_contract as sc
        (tmp_path / "dividends_declared.json").write_text(json.dumps({
            "declared_dps": {"2025": {"value": -1, "currency": "CNY", "kind": "none",
                                      "source": "s", "date": "d"}}}), encoding="utf-8")
        assert not sc.validate_dir(str(tmp_path)).ok
