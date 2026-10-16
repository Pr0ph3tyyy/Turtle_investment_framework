"""scripts/valuation_engine.py —— WACC 的 Rf 来源标注（findings D-13）。

为什么值得单独测
----------------
`yc_cb` 实测**无权限** ⇒ `risk_free_rate` 常为空 ⇒ `rf_default` 是**实际生效的路径**。
那是 per-market 的**建模假设**，不是 §14 的实测值。旧实现把两者都标成 "§14"，
读者无法分辨 WACC/DCF 用的是实测利率还是编出来的默认值 ——
与本仓库「**不得静默补数**」的纪律冲突（数值本身可以按假设算，但**来源必须如实**）。

这个模块此前**没有任何测试**（`tests/` 下无 valuation 文件），所以改动只能靠静态确认；
本文件补上这条最小回归。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import valuation_engine as V  # noqa: E402


class _FakeClient:
    def __init__(self, store):
        self._store = store


def _engine(store: dict) -> V.ValuationEngine:
    e = V.ValuationEngine.__new__(V.ValuationEngine)
    e.client = _FakeClient(store)
    e.market = "A"
    e.params = dict(V.MARKET_PARAMS["A"])
    e._sf = lambda v: None if v is None else float(v)
    e._basic_info = lambda: {"mkt_cap_mm": 400_000.0}
    # compute_wacc 会读这两张表做税负/债务成本；给最小可用结构
    e._annual_df = lambda name: pd.DataFrame(
        [{"finance_exp": 100.0, "income_tax": 250.0, "total_profit": 1000.0}]
    )
    e._interest_bearing_debt = lambda row: 500.0
    return e


RF_ROW = pd.DataFrame([{"yield": 1.665}])


class TestRfSourceLabelling:
    def test_declares_section14_when_rate_is_measured(self):
        w = _engine({"risk_free_rate": RF_ROW}).compute_wacc()
        assert w["rf"] == pytest.approx(1.665)
        assert w["rf_source"] == "§14"

    def test_falls_back_to_default_and_says_so(self):
        """缺 §14 时**可以**按建模假设算，但必须如实说明它不是实测值。"""
        w = _engine({}).compute_wacc()
        assert w["rf"] == pytest.approx(V.MARKET_PARAMS["A"]["rf_default"])
        assert "§14" in w["rf_source"] and "不可用" in w["rf_source"]
        assert "非实测" in w["rf_source"]

    def test_fallback_label_is_per_market(self):
        e = _engine({}); e.market = "HK"; e.params = dict(V.MARKET_PARAMS["HK"])
        w = e.compute_wacc()
        assert w["rf"] == pytest.approx(V.MARKET_PARAMS["HK"]["rf_default"])
        assert "HK" in w["rf_source"]

    def test_non_default_and_default_are_distinguishable(self):
        """核心断言：两条路径的标注**必须不同** —— 这正是旧实现的缺陷。"""
        a = _engine({"risk_free_rate": RF_ROW}).compute_wacc()["rf_source"]
        b = _engine({}).compute_wacc()["rf_source"]
        assert a != b

    def test_empty_dataframe_counts_as_missing(self):
        w = _engine({"risk_free_rate": pd.DataFrame()}).compute_wacc()
        assert "非实测" in w["rf_source"]

    def test_source_key_always_present(self):
        for store in ({"risk_free_rate": RF_ROW}, {}):
            assert _engine(store).compute_wacc().get("rf_source")
