"""Definition-validity and unit-currency guards for §17 (06862 live-run defects).

Two classes of error that the previous code produced silently, and that no existing test
could catch because the old assertions only checked that *labels* appeared:

1. **§17.1 CAGR base substitution.** `_cagr` filtered out non-positive values and then
   computed over whatever remained, so 06862's 归母净利润 "5年CAGR 43.36%" was really a
   3-period (2022–2025) growth rate — the 2021 loss had been dropped — while the revenue
   row used 4 periods under the same column header. A CAGR from a negative base is
   undefined; the honest output is N/A plus a reason.

2. **§17.8 per-share anchors in the wrong currency.** For an HK listing whose reporting
   currency (CNY) differs from its trading currency (HKD), ① 净流动资产/股 and
   ② 每股净资产 were generated as CNY/share but labelled "基准价（港元）" and averaged
   with genuine HKD anchors (③ 10-year low, ④ dividend-implied). The composite 9.34 was
   therefore a mixed-currency mean; converted it is 9.46.
"""

import pathlib

import pandas as pd
import pytest

from tests.test_derived_metrics import _make_client_with_store


def _load_mock(name):
    p = pathlib.Path(__file__).resolve().parent / "fixtures" / "mock_tushare_responses" / name
    import json
    return json.loads(p.read_text(encoding="utf-8"))


# ===== 1. CAGR definition validity =====


class TestCagrDefinitionValidity:
    @staticmethod
    def _client_with_loss_in_oldest_year():
        client = _make_client_with_store()
        inc = client._store["income"].copy()
        # _get_annual_df sorts by end_date desc, so the last row is the oldest year.
        inc.loc[inc.index[-1], "n_income_attr_p"] = -4_163_180_000.0
        client._store["income"] = inc
        return client

    def test_negative_base_yields_na_not_a_shorter_window(self):
        result = self._client_with_loss_in_oldest_year()._compute_financial_trends()
        assert "N/A（基年负）" in result, "负基数不得换基年算出一个 CAGR"
        assert "CAGR 在该窗口无定义" in result, "必须说明原因"
        # the old behaviour produced a numeric CAGR over a shorter, unlabelled window
        assert "43.36%" not in result

    def test_alternative_window_is_labelled_and_fenced(self):
        result = self._client_with_loss_in_oldest_year()._compute_financial_trends()
        assert "首个盈利年" in result
        assert "口径不同，不得与其它行并列或相加" in result

    def test_positive_rows_state_base_year_and_period_count(self):
        result = _make_client_with_store()._compute_financial_trends()
        assert "基年" in result and "期" in result, "正基数行也必须自带期数标注"

    def test_normal_fixture_still_produces_numeric_cagr(self):
        result = _make_client_with_store()._compute_financial_trends()
        assert "5年CAGR" in result
        assert "N/A（基年负）" not in result


# ===== 2. §17.8 per-share currency =====


class TestSection178PerShareCurrency:
    FX = 0.85849          # fx.json 的 factor_to_reporting（HKD → CNY）
    CLOSE = 9.30
    RAW_MKT_CAP_HKD = 51_838_200_000.0

    @classmethod
    def _client(cls):
        client = _make_client_with_store()
        client._currency = "HKD"
        client._store["fx"] = {
            "factor_to_reporting": cls.FX, "reporting_currency": "CNY",
            "trading_currency": "HKD", "date": "2026-10-08",
        }
        client._store["basic_info"] = pd.DataFrame([{
            "close": cls.CLOSE, "total_market_cap": cls.RAW_MKT_CAP_HKD,
        }])
        return client

    @classmethod
    def _expected(cls):
        """Independently recompute the two per-share anchors in trading currency."""
        bs = _make_client_with_store()._store["balance_sheet"].iloc[0]
        shares_m = cls.RAW_MKT_CAP_HKD / cls.CLOSE / 1e6        # 百万股
        cash = float(bs["money_cap"]) / 1e6
        trad = float(bs.get("trad_asset") or 0) / 1e6
        ibd = sum(float(bs.get(c) or 0) for c in
                  ("st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y")) / 1e6
        equity = float(bs["total_hldr_eqy_exc_min_int"]) / 1e6
        return (cash + trad - ibd) / shares_m / cls.FX, equity / shares_m / cls.FX

    def test_anchors_are_converted_into_the_price_unit(self):
        out = self._client()._compute_factor4_ev_baseline("06862.HK")
        assert out is not None
        nla, bvps = self._expected()
        assert f"{nla:.2f}" in out, f"① 应为折算后的 {nla:.2f} 港元/股"
        assert f"{bvps:.2f}" in out, f"② 应为折算后的 {bvps:.2f} 港元/股"

    def test_reporting_currency_values_are_not_used_raw(self):
        """未经折算的元/股不得出现在「基准价（港元）」列（06862: 1.11 / 1.80）。"""
        out = self._client()._compute_factor4_ev_baseline("06862.HK")
        nla, bvps = self._expected()
        raw_nla, raw_bvps = nla * self.FX, bvps * self.FX
        assert f"| ① 净流动资产/股 | {raw_nla:.2f} |" not in out
        assert f"| ② 每股净资产 | {raw_bvps:.2f} |" not in out

    def test_currency_note_is_emitted(self):
        out = self._client()._compute_factor4_ev_baseline("06862.HK")
        assert "币种说明" in out
        assert "交易币种（港元）" in out
        assert "不得" in out and "求均值" in out

    def test_floor_composite_equals_mean_of_floor_rows_only(self):
        """综合底价 = ① ② ③（底价型）的算术平均，**不含** ④ ⑤（收益率型）——
        混算正是 06862 上 9.34 的成因（①② 是元/股），也是"买入就是胜利"越过买点的成因。"""
        import re
        out = self._client()._compute_factor4_ev_baseline("06862.HK")
        rows = dict(re.findall(r"^\| ([①②③④⑤]) [^|]*\| (-?[\d.]+) \|", out, re.M))
        floor = [float(rows[k]) for k in ("①", "②", "③") if k in rows]
        assert floor, "应至少有底价型锚点"
        m = re.search(r"综合底价（底价型锚点算术平均，([\d]+) 项）= ([\d.]+)", out)
        assert m, "综合底价应由 Python 输出"
        assert int(m.group(1)) == len(floor)
        assert abs(float(m.group(2)) - sum(floor) / len(floor)) < 0.005 * len(floor)

    def test_a_share_path_is_unchanged(self):
        """A 股报表币种 = 交易币种，折算系数为 1，输出不得出现币种说明。"""
        client = _make_client_with_store()
        client._store["basic_info"] = pd.DataFrame([{
            "close": 27.5, "total_mv": 175000 * 10000, "total_share": 636363.64,
        }])
        out = client._compute_factor4_ev_baseline("600887.SH")
        assert out is not None and "币种说明" not in out


# ===== 3. §17.13 fail-closed =====


class TestRevenueSensitivityFailClosed:
    """λ 方向不自洽时该小节必须拒绝出厂，而不是"附个警告照常给出数字"。"""

    @staticmethod
    def _client(lam, warnings):
        from tests.test_derived_metrics import _make_client_with_ev_store
        client = _make_client_with_ev_store()
        client._store["factor3_sensitivity"] = {
            "aa_selected": 4110.05,
            "lambda_median": lam,
            "lambda_reliability": "多项警告或异常" if warnings else "可信",
            "lambda_warnings": warnings,
        }
        client._store["payout_crosscheck"] = {"m_rec": 81.11}
        return client

    def test_negative_lambda_emits_no_section_and_marks_reason(self):
        client = self._client(-1.4587, ["ΔAA/ΔRevenue符号不一致，成本结构可能变化"])
        assert client._compute_revenue_sensitivity("600887.SH") is None, "不得输出方向错误的表"
        mark = client._section_registry().finalize("600887.SH", {})["sections"]["17.13"]
        assert mark["status"] == "missing"
        assert mark["reason"] == "input_inconsistent"
        assert "方向不自洽" in mark.get("note", "")

    def test_reliability_warning_with_positive_lambda_still_emits(self):
        """可靠性警告只压低可信度评级，不构成方向性错误 ⇒ 仍出表并携带警告。"""
        client = self._client(0.5, ["ΔAA/ΔRevenue符号不一致，成本结构可能变化"])
        out = client._compute_revenue_sensitivity("600887.SH")
        assert out is not None and "ΔAA/ΔRevenue符号不一致" in out

    def test_usable_lambda_still_produces_monotone_rows(self):
        client = self._client(0.5, [])
        out = client._compute_revenue_sensitivity("600887.SH")
        assert out is not None and "17.13" in out
        import re
        ggs = [float(x) for x in re.findall(r"\| ([\d.]+)% \|", out)]
        assert ggs and ggs == sorted(ggs, reverse=True), "收入下滑时 GG 必须下降"

    def test_zero_lambda_keeps_the_degraded_path(self):
        client = self._client(0.0, [])
        out = client._compute_revenue_sensitivity("600887.SH")
        assert out is not None and "降级" in out
