"""turtle_core.floors —— 底价锚点与「综合底价/参考公允价」语义锁定测试。

fixture 取自仓库既有回归：`tests/test_derived_metrics.py:922-945`
（综合底价 8.74 = mean(1.46, 9.26, 15.50)；参考公允价 36.70），
以及 06862 事故（四项等权平均 9.44 vs 正确的 9.46）。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from turtle_core import floors  # noqa: E402

FX = 0.85753  # 00696/06862 实测 fx.json factor_to_reporting


class TestAnchorsWithFx:
    def test_nla_converts_reporting_to_trading(self):
        """① 报表币种每股必须 ÷fx 折成交易币种，否则与价格/其他锚点混币种。"""
        got = floors.anchor_nla(cash=1000.0, trad_assets=200.0,
                                interest_bearing_debt=300.0,
                                total_shares=100.0, factor_to_reporting=FX)
        per_share_reporting = (1000.0 + 200.0 - 300.0) / 100.0  # 9.0
        assert got == pytest.approx(per_share_reporting / FX)

    def test_bvps_converts(self):
        got = floors.anchor_bvps(equity_parent=857.53, total_shares=100.0,
                                 factor_to_reporting=FX)
        assert got == pytest.approx(10.0)  # 8.5753 /0.85753

    def test_trad_assets_none_is_zero_not_failure(self):
        """交易性金融资产缺失按 0 计（canonical `or 0`），不影响其余项。"""
        got = floors.anchor_nla(cash=100.0, trad_assets=None,
                                interest_bearing_debt=0.0,
                                total_shares=100.0, factor_to_reporting=1.0)
        assert got == pytest.approx(1.0)

    @pytest.mark.parametrize("fx", [None, 0.0, -1.0])
    def test_missing_fx_is_fail_closed(self, fx):
        """fx 缺失 ⇒ None，绝不按 1.0 兜底（否则就是 15.65% 的静默低估）。"""
        assert floors.anchor_nla(1000.0, 0.0, 0.0, 100.0, fx) is None
        assert floors.anchor_bvps(1000.0, 100.0, fx) is None

    def test_shares_missing_or_zero(self):
        assert floors.anchor_nla(1000.0, 0.0, 0.0, None, 1.0) is None
        assert floors.anchor_nla(1000.0, 0.0, 0.0, 0.0, 1.0) is None
        assert floors.anchor_bvps(1000.0, 0.0, 1.0) is None


class TestAnchorLow10y:
    def test_takes_min_and_no_fx(self):
        """③ 本身是交易币种，**不折汇**。"""
        assert floors.anchor_low_10y([12.0, 3.5, 9.9]) == pytest.approx(3.5)

    def test_ignores_none(self):
        assert floors.anchor_low_10y([None, 5.0, None, 2.0]) == pytest.approx(2.0)

    def test_empty_is_none(self):
        assert floors.anchor_low_10y([]) is None
        assert floors.anchor_low_10y([None, None]) is None

    def test_window_is_caller_responsibility(self):
        """窗口由调用方截取 —— 这里锁住「取最小」本身，并证明窗口会影响结果。

        ✅ 采集器**已**保证 10 年窗口（A 股传 start_date、港/美用 range=10y，
        见 `financials.get_weekly_prices`），所以 canonical 的 ③ 就是「近 10 年最低价」。
        ⚠️ 撤回：我曾据 Tushare `weekly` 的 **API 默认**（全量历史）推断 ③ 是
        「上市以来最低价」—— 那是拿 API 默认值推断调用方行为，推断无效。
        """
        wider = [40.0, 12.0, 8.0, 30.0]      # 传更宽的序列
        ten_year = [40.0, 12.0, 30.0]        # 只传近 10 年
        assert floors.anchor_low_10y(wider) == pytest.approx(8.0)
        assert floors.anchor_low_10y(ten_year) == pytest.approx(12.0)
        assert floors.anchor_low_10y(wider) != floors.anchor_low_10y(ten_year)


class TestAnchorDivImplied:
    def test_basic(self):
        # 近3次分红均值 0.3 / max(2%,3%)=3% → 10
        assert floors.anchor_div_implied([0.2, 0.3, 0.4], rf_pct=2.0) == pytest.approx(10.0)

    def test_uses_rf_when_above_floor(self):
        # 0.3 / 6% = 5
        assert floors.anchor_div_implied([0.3], rf_pct=6.0) == pytest.approx(5.0)

    def test_three_percent_floor(self):
        assert floors.anchor_div_implied([0.3], rf_pct=0.5) == pytest.approx(10.0)
        assert floors.anchor_div_implied([0.3], rf_pct=0.0) == pytest.approx(10.0)
        assert floors.anchor_div_implied([0.3], rf_pct=-1.0) == pytest.approx(10.0)

    def test_missing_inputs(self):
        assert floors.anchor_div_implied([], 2.0) is None
        assert floors.anchor_div_implied([None, None], 2.0) is None
        assert floors.anchor_div_implied([0.3], None) is None


class TestAnchorFcfCapitalized:
    def test_ok(self):
        # min=100 / 2% / 100股 / 1.0 = 50
        v, why = floors.anchor_fcf_capitalized([100.0, 200.0], 2.0, 100.0, 1.0)
        assert v == pytest.approx(50.0)
        assert why is None

    def test_applies_fx(self):
        v, why = floors.anchor_fcf_capitalized([100.0], 2.0, 100.0, FX)
        assert v == pytest.approx((100.0 / 0.02 / 100.0) / FX)
        assert why is None

    @pytest.mark.parametrize("series,reason", [
        ([100.0, -5.0], "negative_fcf"),
        ([100.0, 0.0], "negative_fcf"),
        ([], "no_fcf"),
        ([None], "no_fcf"),
    ])
    def test_skip_reasons(self, series, reason):
        """跳过必须给出原因 —— canonical 打印说明，screener 静默省略（正是要修的形态）。"""
        v, why = floors.anchor_fcf_capitalized(series, 2.0, 100.0, 1.0)
        assert v is None
        assert why == reason

    @pytest.mark.parametrize("rf", [None, 0.0, -1.0])
    def test_no_rf(self, rf):
        v, why = floors.anchor_fcf_capitalized([100.0], rf, 100.0, 1.0)
        assert v is None and why == "no_rf"

    def test_no_shares(self):
        v, why = floors.anchor_fcf_capitalized([100.0], 2.0, None, 1.0)
        assert v is None and why == "no_shares"


class TestSemanticSplit:
    """核心：①②③ 与 ④⑤ **永不混算**（06862 事故）。"""

    ANCHORS = floors.FloorAnchors(nla=1.46, bvps=9.26, low_10y=15.50,
                                  div_implied=20.0, fcf_cap=53.40)

    def test_floor_composite_is_123_only(self):
        assert self.ANCHORS.floor_composite == pytest.approx(8.74, abs=0.005)
        assert (1.46 + 9.26 + 15.50) / 3 == pytest.approx(8.74, abs=0.005)

    def test_fair_composite_is_45_only(self):
        assert self.ANCHORS.fair_composite == pytest.approx((20.0 + 53.40) / 2)
        assert self.ANCHORS.fair_composite == pytest.approx(36.70)

    def test_mixing_all_five_is_detectably_different(self):
        """把 5 项一起平均会得到一个**既非底价也非公允价**的数（06862 的 9.44）。"""
        all_five = sum(self.ANCHORS.all_prices) / len(self.ANCHORS.all_prices)
        assert all_five != pytest.approx(self.ANCHORS.floor_composite)
        assert all_five != pytest.approx(self.ANCHORS.fair_composite)

    def test_missing_floor_anchors_gives_no_floor_reading(self):
        """底价型锚点全缺 ⇒ 综合底价 None ⇒ 不得发出「低于底价」信号。"""
        only_fair = floors.FloorAnchors(div_implied=20.0, fcf_cap=53.40)
        assert only_fair.floor_composite is None
        assert only_fair.fair_composite == pytest.approx(36.70)
        assert floors.is_below_floor(close=1.0, anchors=only_fair) is False

    def test_partial_floor_anchors_use_present_only(self):
        partial = floors.FloorAnchors(nla=3.0, bvps=9.0)
        assert partial.floor_composite == pytest.approx(6.0)


class TestPremium:
    def test_premium_pct(self):
        assert floors.premium_pct(9.0, 6.0) == pytest.approx(50.0)
        assert floors.premium_pct(6.0, 6.0) == pytest.approx(0.0)
        assert floors.premium_pct(3.0, 6.0) == pytest.approx(-50.0)

    @pytest.mark.parametrize("composite", [0.0, -1.0, None])
    def test_guard_against_zero_and_negative(self, composite):
        """canonical 无此保护（0 会 ZeroDivisionError，负数静默翻符号）；统一取保护版本。"""
        assert floors.premium_pct(10.0, composite) is None

    def test_below_floor_is_buy_point_semantics(self):
        a = floors.FloorAnchors(nla=1.0, bvps=2.0, low_10y=3.0)  # floor = 2.0
        assert floors.is_below_floor(1.9, a) is True
        assert floors.is_below_floor(2.0, a) is True
        assert floors.is_below_floor(2.1, a) is False

    def test_fair_tier_ladder_has_five_bands(self):
        """canonical 代码 5 档；文档只写 4 档（文档滞后，以代码为准）。"""
        assert floors.fair_tier_verdict(-1.0).startswith("低于公允价")
        assert floors.fair_tier_verdict(10.0).startswith("略低于")
        assert floors.fair_tier_verdict(50.0).startswith("合理溢价")
        assert floors.fair_tier_verdict(100.0).startswith("较高溢价")
        assert floors.fair_tier_verdict(200.0).startswith("显著溢价")
        assert floors.fair_tier_verdict(None) is None
