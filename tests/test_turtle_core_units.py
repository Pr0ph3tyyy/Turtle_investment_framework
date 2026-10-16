"""turtle_core.units —— 单位/币种换算语义锁定测试。

这些断言直接来自两条真实事故（known_issues.md P0-2），改动它们等于改动口径：
- 06862 的 ① 1.11 / ② 1.80 是 CNY/股却被当成港元求均值 ⇒ 9.34 vs 9.46
- R = 净利润(CNY) / 市值(HKD) 未折算 ⇒ 低估 15.65%
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from turtle_core import units  # noqa: E402

FX = 0.85753  # 00696.HK / 06862.HK 实测值


class TestAmountUnits:
    def test_yuan_to_mm(self):
        assert units.yuan_to_mm(1_000_000.0) == pytest.approx(1.0)
        assert units.yuan_to_mm(2_341_560_000.0) == pytest.approx(2341.56)

    def test_wan_to_mm(self):
        # 1 百万元 = 100 万元
        assert units.wan_to_mm(100.0) == pytest.approx(1.0)
        assert units.wan_to_mm(5_572_740.0) == pytest.approx(55727.40)

    def test_roundtrip(self):
        assert units.mm_to_yuan(units.yuan_to_mm(12_345_678.0)) == pytest.approx(12_345_678.0)

    def test_none_propagates(self):
        assert units.yuan_to_mm(None) is None
        assert units.wan_to_mm(None) is None
        assert units.mm_to_yuan(None) is None


class TestCurrencyConversion:
    def test_trading_to_reporting_market_cap(self):
        """港元市值 × fx = 人民币市值（R/GG 的分母口径）。"""
        assert units.trading_to_reporting(53_343_180_000.0, FX) == pytest.approx(
            53_343_180_000.0 * FX
        )

    def test_per_share_reporting_to_trading(self):
        """报表币种每股 ÷ fx = 交易币种每股（① 1.11 CNY/股 → 1.294 HKD/股）。"""
        assert units.per_share_to_trading(1.11, FX) == pytest.approx(1.294, abs=1e-3)
        assert units.per_share_to_trading(1.80, FX) == pytest.approx(2.099, abs=1e-3)

    def test_the_two_directions_are_inverses(self):
        v = 100.0
        assert units.per_share_to_trading(
            units.trading_to_reporting(v, FX), FX
        ) == pytest.approx(v)

    def test_direction_is_not_symmetric(self):
        """抄反方向必须能被测出来 —— 这正是 9.34 vs 9.46 的成因。"""
        wrong = units.trading_to_reporting(1.11, FX)  # 0.95，与正确答案 1.29 不同
        assert wrong != pytest.approx(units.per_share_to_trading(1.11, FX))


class TestFailClosed:
    """fx 缺失一律 None —— 绝不按 1.0 兜底。"""

    @pytest.mark.parametrize("bad", [None, 0.0, -0.5])
    def test_missing_or_invalid_factor_returns_none(self, bad):
        assert units.trading_to_reporting(100.0, bad) is None
        assert units.per_share_to_trading(100.0, bad) is None

    def test_factor_of_one_is_accepted_but_explicit(self):
        """同币种必须显式传 1.0；省略是另一回事（返回 None）。"""
        assert units.trading_to_reporting(100.0, 1.0) == pytest.approx(100.0)
        assert units.trading_to_reporting(100.0, None) is None

    def test_value_none_passes_through(self):
        assert units.trading_to_reporting(None, FX) is None
        assert units.per_share_to_trading(None, FX) is None


class TestNeedsConversion:
    def test_same_currency(self):
        assert units.needs_conversion("HKD", "HKD") is False
        assert units.needs_conversion("hkd", "HKD") is False

    def test_different_currency(self):
        assert units.needs_conversion("HKD", "CNY") is True

    def test_unconfirmed_currency_requires_explicit_handling(self):
        """币种未确认时不得假定同币种（否则整包单位标签失去意义）。"""
        assert units.needs_conversion("HKD", None) is True
        assert units.needs_conversion(None, None) is True
