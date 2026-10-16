"""turtle_core.decision —— 穿透回报率决策层语义锁定测试。

fixture 取自仓库既有回归 `tests/test_derived_metrics_grids.py`
（600887.SH：AA=8710、M_rec=58.081%、M_pess=54.202%、市值=175000 百万、Q=0
 ⇒ §17.11 GG=2.89%、GG_pess(k=1)=2.70%、GG_pess(k=0.80)=2.16%）。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from turtle_core import decision as D  # noqa: E402

# --- 600887.SH fixture（单位：百万元，纯比例运算故与元等价） ---
AA_MM = 8710.0
MKT_MM = 175000.0
M_REC = 58.081
M_PESS = 54.202
GG_NEUTRAL = 2.89       # §17.11 AA_2y×★
GG_PESS_100 = 2.70      # k=1.00
GG_PESS_080 = 2.16      # k=0.80


class TestTaxRates:
    def test_market_kind(self):
        assert D.market_kind("600887.SH") == "A"
        assert D.market_kind("000921.SZ") == "A"
        assert D.market_kind("06862.HK") == "HK"
        assert D.market_kind("AAPL.US") == "US"

    def test_hk_default_is_20(self):
        _, rates, default = D.tax_rates("06862.HK")
        assert rates == [28.0, 20.0]
        assert default == 20.0

    def test_us_default_is_10(self):
        _, rates, default = D.tax_rates("AAPL.US")
        assert rates == [30.0, 10.0]
        assert default == 10.0

    def test_a_share_default_is_0(self):
        _, rates, default = D.tax_rates("600887.SH")
        assert rates == [0.0, 10.0, 20.0]
        assert default == 0.0

    def test_dedup_keeps_order_and_labels(self):
        """去重按值保序，但标签不丢（港股两档都是 20%）。"""
        labels, rates, _ = D.tax_rates("06862.HK")
        assert labels == ["H股直接", "港股通", "红筹/开曼"]
        assert rates == [28.0, 20.0]

    def test_unimplemented_scenario_is_declared_not_silent(self):
        """文档里的"美国来源收入加权"未实现 —— 必须显式登记，不能假装覆盖。"""
        assert any("美国来源" in s for s in D.UNIMPLEMENTED_Q_SCENARIOS)


class TestPenetrationReturn:
    def test_reproduces_gg_neutral(self):
        got = D.penetration_return(AA_MM, M_REC, 0.0, 0.0, MKT_MM)
        assert got == pytest.approx(GG_NEUTRAL, abs=0.005)

    def test_reproduces_gg_pess_control_row(self):
        """k=1.00 是**未打折**的对照行，仍走 M_pess。"""
        got = D.penetration_return(AA_MM, M_PESS, 0.0, 0.0, MKT_MM)
        assert got == pytest.approx(GG_PESS_100, abs=0.005)

    def test_reproduces_gg_pess_k080(self):
        got = D.penetration_return(AA_MM * 0.80, M_PESS, 0.0, 0.0, MKT_MM)
        assert got == pytest.approx(GG_PESS_080, abs=0.005)

    def test_r_and_gg_differ_only_by_base(self):
        """R 用 C、GG 用 AA —— 同一函数。screener 曾把 AA 的结果叫作 R，那是错的。"""
        r = D.penetration_return(5000.0, M_REC, 0.0, 0.0, MKT_MM)
        gg = D.penetration_return(AA_MM, M_REC, 0.0, 0.0, MKT_MM)
        assert r != pytest.approx(gg)

    def test_q_applies_as_after_tax_keep(self):
        """港股 Q=20% ⇒ 到手 80%。"""
        gross = D.penetration_return(AA_MM, M_REC, 0.0, 0.0, MKT_MM)
        net = D.penetration_return(AA_MM, M_REC, 20.0, 0.0, MKT_MM)
        assert net == pytest.approx(gross * 0.8)

    def test_o_is_added_post_tax(self):
        """O（注销型回购）**不打税务折扣**（shared_tables.md:42）。"""
        no_o = D.penetration_return(AA_MM, M_REC, 20.0, 0.0, MKT_MM)
        with_o = D.penetration_return(AA_MM, M_REC, 20.0, 100.0, MKT_MM)
        assert with_o - no_o == pytest.approx(100.0 / MKT_MM * 100.0)

    def test_o_none_is_zero(self):
        assert (D.penetration_return(AA_MM, M_REC, 0.0, None, MKT_MM)
                == pytest.approx(D.penetration_return(AA_MM, M_REC, 0.0, 0.0, MKT_MM)))

    @pytest.mark.parametrize("kwargs", [
        dict(base=None), dict(m_pct=None), dict(q_pct=None), dict(mkt_cap=None),
    ])
    def test_missing_inputs_fail_closed(self, kwargs):
        args = dict(base=AA_MM, m_pct=M_REC, q_pct=0.0, o=0.0, mkt_cap=MKT_MM)
        args.update(kwargs)
        assert D.penetration_return(**args) is None

    @pytest.mark.parametrize("cap", [0.0, -1.0])
    def test_non_positive_market_cap_is_rejected(self, cap):
        assert D.penetration_return(AA_MM, M_REC, 0.0, 0.0, cap) is None


class TestDerivedDecisionNumbers:
    def test_hh_is_r_minus_gg(self):
        assert D.hh(2.89, 2.16) == pytest.approx(0.73)

    def test_hh_treats_missing_gg_as_zero(self):
        """canonical `hh = r_default − (gg_default or 0.0)`。"""
        assert D.hh(2.89, None) == pytest.approx(2.89)

    def test_margin_is_gg_minus_ii(self):
        assert D.margin(2.16, 5.0) == pytest.approx(-2.84)
        assert D.margin(None, 5.0) is None

    def test_target_buy_price(self):
        # 现价 20 × 2.16% / 5% = 8.64
        assert D.target_buy_price(20.0, GG_PESS_080, 5.0) == pytest.approx(8.64)

    @pytest.mark.parametrize("gg", [0.0, -1.0, None])
    def test_gg_non_positive_means_no_solution(self, gg):
        """GG ≤ 0 ⇒ 无解（任何价格都达不到门槛），不是"价格为零"。"""
        assert D.target_buy_price(20.0, gg, 5.0) is None

    @pytest.mark.parametrize("close", [0.0, None])
    def test_close_missing(self, close):
        assert D.target_buy_price(close, 2.16, 5.0) is None

    @pytest.mark.parametrize("ii", [0.0, -1.0, None])
    def test_ii_non_positive(self, ii):
        assert D.target_buy_price(20.0, 2.16, ii) is None

    def test_o_increment(self):
        # 市值 175000 百万 → 每 100 百万回购贡献 100/175000×100 = 0.0571 pct
        assert D.o_increment_pct(175000.0) == pytest.approx(100 / 175000 * 100)
        assert D.o_increment_pct(None) == 0.0
        assert D.o_increment_pct(0.0) == 0.0


class TestThresholdPriceRelation:
    """findings D-1：§17.9 门槛价格 与 §17.14 目标买入价 **不是同一个数**。

    真实关系（A股 fx=1）：``目标买入价 = 门槛价格 × M_pess / M_rec``。
    这正是 `numeric_audit` X1 误报 critical 的根源 —— 它断言二者相等。
    """

    CLOSE = 20.0
    MKT_YUAN = MKT_MM * 1e6
    SHARES = MKT_YUAN / CLOSE

    def test_ratio_equals_m_pess_over_m_rec(self):
        k = 0.80
        aa_k_yuan = AA_MM * 1e6 * k

        tpp = D.threshold_price(aa_k_yuan, M_REC, 0.0, 0.0, 5.0, self.SHARES)
        gg = D.penetration_return(aa_k_yuan, M_PESS, 0.0, 0.0, self.MKT_YUAN)
        tbp = D.target_buy_price(self.CLOSE, gg, 5.0)

        assert tpp is not None and tbp is not None
        assert tbp / tpp == pytest.approx(M_PESS / M_REC, rel=1e-9)
        # 54.202 / 58.081 —— 与子代理报告里的 0.933219 差异来自其四舍五入
        assert M_PESS / M_REC == pytest.approx(0.933214, abs=1e-5)

    def test_they_are_not_equal(self):
        """把 X1 的相等断言写进测试就会失败 —— 这就是它误报 critical 的原因。"""
        aa_k_yuan = AA_MM * 1e6 * 0.80
        tpp = D.threshold_price(aa_k_yuan, M_REC, 0.0, 0.0, 5.0, self.SHARES)
        gg = D.penetration_return(aa_k_yuan, M_PESS, 0.0, 0.0, self.MKT_YUAN)
        tbp = D.target_buy_price(self.CLOSE, gg, 5.0)
        assert tbp != pytest.approx(tpp, rel=0.02)

    def test_relation_holds_when_m_pess_equals_m_rec(self):
        """M_pess == M_rec 时二者才相等（X1 仅在此情形成立）。"""
        aa_k_yuan = AA_MM * 1e6 * 0.80
        tpp = D.threshold_price(aa_k_yuan, M_REC, 0.0, 0.0, 5.0, self.SHARES)
        gg = D.penetration_return(aa_k_yuan, M_REC, 0.0, 0.0, self.MKT_YUAN)
        tbp = D.target_buy_price(self.CLOSE, gg, 5.0)
        assert tbp == pytest.approx(tpp, rel=1e-9)

    def test_threshold_price_guards(self):
        assert D.threshold_price(None, M_REC, 0.0, 0.0, 5.0, 100.0) is None
        assert D.threshold_price(AA_MM, M_REC, 0.0, 0.0, 0.0, 100.0) is None
        assert D.threshold_price(AA_MM, M_REC, 0.0, 0.0, 5.0, 0.0) is None


class TestIIThreshold:
    def test_reexported_single_source(self):
        """II 只转发 constants.ii_threshold，绝不重写 max()（P1-12）。"""
        assert D.ii_threshold("600887.SH", 1.67) == pytest.approx(5.0)
        assert D.ii_threshold("06862.HK", 1.0242) == pytest.approx(5.0)
        assert D.ii_threshold("AAPL.US", 4.20) == pytest.approx(7.20)

    def test_old_screener_formula_would_differ(self):
        """screener 旧式 max(3.5, rf+2)：在 rf=1.0242 时给 3.5，现行给 5.0。"""
        legacy = max(3.5, 1.0242 + 2.0)
        current = D.ii_threshold("000915.SZ", 1.0242)
        assert legacy == pytest.approx(3.5)
        assert current == pytest.approx(5.0)
        assert legacy != current


class TestAAProfile:
    def test_hard_gate_needs_two_years(self):
        """canonical：可用年数 < 2 ⇒ mark missing/no_cashflow 且 return None。"""
        prof, why = D.aa_profile([("2025", 1000.0, -200.0, 0.0)])
        assert prof is None and why == "no_cashflow"

    def test_skips_rows_missing_ocf_or_capex(self):
        prof, why = D.aa_profile([
            ("2025", 1000.0, -200.0, 0.0),
            ("2024", None, -200.0, 0.0),
            ("2023", 900.0, None, 0.0),
        ])
        assert prof is None and why == "no_cashflow"

    def test_formula_ocf_minus_abs_capex_minus_deduct(self):
        prof, why = D.aa_profile([
            ("2025", 1000.0, -200.0, 50.0),
            ("2024", 1000.0, 200.0, 0.0),
        ])
        assert why is None
        # 第 1 年 = 1000 − 200 − 50 = 750；第 2 年 = 1000 − 200 − 0 = 800
        assert dict(prof.series) == {"2025": 750.0, "2024": 800.0}

    def test_variants(self):
        prof, _ = D.aa_profile([
            ("2025", 1000.0, 0.0, 0.0),
            ("2024", 800.0, 0.0, 0.0),
            ("2023", 600.0, 0.0, 0.0),
        ])
        assert prof.aa_latest == pytest.approx(1000.0)
        assert prof.aa_2y == pytest.approx(900.0)
        assert prof.aa_all == pytest.approx(800.0)
        assert prof.aa_excl == pytest.approx(800.0)
        assert prof.aa_selected == pytest.approx(prof.aa_2y)

    def test_aa_excl_drops_negative_years(self):
        """实现是"剔除负值年份"（与文档"剔除非经营性主导年份"不同，findings D-7）。"""
        prof, _ = D.aa_profile([
            ("2025", 1000.0, 0.0, 0.0),
            ("2024", -500.0, 0.0, 0.0),   # 负值年份
            ("2023", 200.0, 0.0, 0.0),
        ])
        assert prof.aa_excl == pytest.approx(600.0)   # mean(1000, 200)
        assert prof.aa_excl > prof.aa_all

    def test_aa_excl_falls_back_to_aa_all_when_all_negative(self):
        prof, _ = D.aa_profile([
            ("2025", -100.0, 0.0, 0.0),
            ("2024", -200.0, 0.0, 0.0),
        ])
        assert prof.aa_excl == pytest.approx(prof.aa_all)

    def test_aa_selected_equals_aa_2y_given_the_gate(self):
        """硬门禁保证 ≥2 年 ⇒ aa_selected 恒等于 aa_2y（numeric_audit D5 依赖这一点）。"""
        for rows in (
            [("2025", 100.0, 0.0, 0.0), ("2024", 50.0, 0.0, 0.0)],
            [("2025", 100.0, 0.0, 0.0), ("2024", 50.0, 0.0, 0.0), ("2023", 10.0, 0.0, 0.0)],
        ):
            prof, _ = D.aa_profile(rows)
            assert prof.aa_selected == pytest.approx(prof.aa_2y)

    def test_deduct_total_only_sums_positives(self):
        """canonical `v > 0` 守卫：负的扣减项不计。"""
        assert D.deduct_total([10.0, -5.0, None, 20.0]) == pytest.approx(30.0)
        assert D.deduct_total([None, None]) == 0.0

    def test_deduct_fields_exclude_operating_interest(self):
        """`c_pay_int_operating` 已在 OCF 内，重复扣会低估 AA。"""
        assert "c_pay_int_operating" not in D.DEDUCT_FIELDS


class TestPayoutAnchor:
    def test_default_ladder_prefers_method3(self):
        a = D.payout_anchor(50.0, 52.0, 51.0)
        assert a.m_rec == pytest.approx(51.0)
        assert a.m_rec_label == "法3"

    def test_m_pess_is_min_of_methods(self):
        a = D.payout_anchor(50.0, 52.0, 51.0)
        assert a.m_pess == pytest.approx(50.0)

    def test_large_deviation_switches_to_method2(self):
        """最大两两相对偏差 > 15% ⇒ 用法2（现金流上界口径）。"""
        a = D.payout_anchor(10.0, 60.0, 51.0)
        assert a.m_rec_label == "法2"
        assert a.m_rec == pytest.approx(60.0)

    def test_identical_dps_switches_to_method2(self):
        a = D.payout_anchor(50.0, 52.0, 51.0, identical_dps=True)
        assert a.m_rec_label == "法2"

    def test_fallback_ladder(self):
        assert D.payout_anchor(None, None, 51.0).m_rec_label == "法3"
        assert D.payout_anchor(None, 52.0, None).m_rec_label == "法2"
        assert D.payout_anchor(50.0, None, None).m_rec_label == "法1"
        assert D.payout_anchor(None, None, None).m_rec is None

    def test_m_pess_falls_back_to_m_rec_when_no_methods(self):
        a = D.payout_anchor(None, None, None,
                            commitment={"kind": "公告", "min_payout_pct": 40.0})
        assert a.m_rec == pytest.approx(40.0)
        assert a.m_pess == pytest.approx(40.0)   # 三法皆缺 ⇒ 退化

    # --- 承诺锚定 ---

    def test_announcement_commitment_wins(self):
        a = D.payout_anchor(50.0, 52.0, 51.0,
                            commitment={"kind": "公告", "min_payout_pct": 40.0,
                                        "period": "2025-2027"})
        assert a.m_rec == pytest.approx(40.0)
        assert a.m_rec_label == "承诺(支付率)"
        assert "2025-2027" in a.m_rec_reason

    def test_oral_commitment_needs_two_occurrences(self):
        once = D.payout_anchor(50.0, 52.0, 51.0,
                              commitment={"kind": "口头", "min_payout_pct": 40.0,
                                          "occurrences": 1})
        assert once.m_rec_label != "承诺(支付率)"
        twice = D.payout_anchor(50.0, 52.0, 51.0,
                               commitment={"kind": "口头", "min_payout_pct": 40.0,
                                           "occurrences": 2})
        assert twice.m_rec == pytest.approx(40.0)
        assert "口头" in twice.m_rec_reason

    def test_invalid_kind_ignored(self):
        a = D.payout_anchor(50.0, 52.0, 51.0,
                            commitment={"kind": "无", "min_payout_pct": 40.0})
        assert a.m_rec_label == "法3"

    def test_amount_commitment_converts_units(self):
        """`min_amount_mm` 是**百万元**，净利润是**元** ⇒ 必须 ÷1e6 对齐。"""
        a = D.payout_anchor(50.0, 52.0, 51.0,
                            commitment={"kind": "公告", "min_amount_mm": 1000.0},
                            net_profit_yuan=2500.0 * 1e6)
        assert a.m_rec == pytest.approx(40.0)     # 1000 / 2500 × 100
        assert a.m_rec_label == "承诺(金额折算)"

    def test_amount_commitment_without_net_profit_falls_through(self):
        a = D.payout_anchor(50.0, 52.0, 51.0,
                            commitment={"kind": "公告", "min_amount_mm": 1000.0},
                            net_profit_yuan=None)
        assert a.m_rec_label == "法3"

    def test_m_pess_ignores_commitment(self):
        """findings D-2/D-3：M_pess 完全忽略承诺值，悲观档按三法最低者定价。"""
        a = D.payout_anchor(50.0, 52.0, 51.0,
                            commitment={"kind": "公告", "min_payout_pct": 10.0})
        assert a.m_rec == pytest.approx(10.0)     # 承诺生效
        assert a.m_pess == pytest.approx(50.0)    # 但仍取三法最低者，不是 10
