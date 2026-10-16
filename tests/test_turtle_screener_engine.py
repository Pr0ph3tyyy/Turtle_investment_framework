"""turtle_screener.engine —— 每日快层与排序口径测试。"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from turtle_core.snapshot import FinancialSnapshot  # noqa: E402
from turtle_screener import engine as E  # noqa: E402
from turtle_screener.prices import PriceQuote  # noqa: E402


def _snap(code="06862.HK", *, aa=12_000_000_000.0, m_pess=23.29, c=2_341_560_000.0,
          q=20.0, shares=2_931_000_000.0, fx=0.85753, rep="CNY", trd="HKD",
          reasons=None, o=0.0):
    return FinancialSnapshot.new(
        code, "20251231", reporting_currency=rep, trading_currency=trd,
        factor_to_reporting=fx, c_yuan=c,
        aa={"aa_selected": aa, "aa_2y": aa}, payout={"m_pess": m_pess, "m_rec": m_pess},
        q_default=q, o_yuan=o, total_shares=shares, reasons=reasons or {})


def _quote(code="06862.HK", price=9.3, ccy="HKD"):
    return PriceQuote(ts_code=code, close=price, currency=ccy)


class TestMarketCap:
    def test_cross_currency(self):
        cap, why = E.resolve_market_cap(2_931_000_000.0, 9.3, 0.85753, "HKD", "CNY")
        assert why is None
        assert cap == pytest.approx(2_931_000_000.0 * 9.3 * 0.85753)

    def test_same_currency_ignores_missing_fx(self):
        """同币种时 fx 缺失不算错（但跨币种绝不放行）。"""
        cap, why = E.resolve_market_cap(1e9, 10.0, None, "CNY", "CNY")
        assert why is None and cap == pytest.approx(1e10)

    def test_cross_currency_without_fx_is_fail_closed(self):
        cap, why = E.resolve_market_cap(1e9, 10.0, None, "HKD", "CNY")
        assert cap is None and why == "fx_missing"

    @pytest.mark.parametrize("shares,close", [(None, 10.0), (0.0, 10.0), (1e9, None), (1e9, 0.0)])
    def test_missing_inputs(self, shares, close):
        cap, why = E.resolve_market_cap(shares, close, 1.0, "CNY", "CNY")
        assert cap is None and why == "no_market_cap"

    def test_unconfirmed_currency_requires_fx(self):
        cap, why = E.resolve_market_cap(1e9, 10.0, None, "HKD", None)
        assert cap is None and why == "fx_missing"


class TestScreenOne:
    def test_happy_path(self):
        row = E.screen_one(_snap(), _quote(), ii=5.0)
        assert row.eligible is True
        assert row.margin == pytest.approx(row.gg_pess - 5.0)
        assert row.target_buy_price == pytest.approx(9.3 * row.gg_pess / 5.0)
        assert row.reasons == []

    def test_gg_pess_matches_turtle_core(self):
        from turtle_core import decision as D
        row = E.screen_one(_snap(), _quote(), ii=5.0)
        cap = 2_931_000_000.0 * 9.3 * 0.85753
        expect = D.penetration_return(12_000_000_000.0 * 0.8, 23.29, 20.0, 0.0, cap)
        assert row.gg_pess == pytest.approx(expect)

    def test_uses_adopted_k_080(self):
        from turtle_core import decision as D
        row = E.screen_one(_snap(), _quote(), ii=5.0)
        cap = 2_931_000_000.0 * 9.3 * 0.85753
        gg_100 = D.penetration_return(12_000_000_000.0, 23.29, 20.0, 0.0, cap)
        assert row.gg_pess == pytest.approx(gg_100 * 0.8, rel=1e-9)

    def test_rf_missing_aborts_row(self):
        row = E.screen_one(_snap(), _quote(), ii=None)
        assert "rf_missing" in row.reasons
        assert row.eligible is False and row.gg_pess is None

    def test_no_price(self):
        row = E.screen_one(_snap(), PriceQuote(ts_code="X", error="HTTP 404"), ii=5.0)
        assert "no_price" in row.reasons

    def test_missing_quote_object(self):
        row = E.screen_one(_snap(), None, ii=5.0)
        assert "no_price" in row.reasons

    def test_snapshot_reasons_propagate(self):
        row = E.screen_one(_snap(reasons={"aa": "no_cashflow"}), _quote(), ii=5.0)
        assert "no_cashflow" in row.reasons

    def test_missing_aa_or_payout(self):
        s1 = _snap(); s1.aa = {}
        assert "no_cashflow" in E.screen_one(s1, _quote(), ii=5.0).reasons
        s2 = _snap(); s2.payout = {}
        assert "no_payout" in E.screen_one(s2, _quote(), ii=5.0).reasons

    def test_below_threshold_is_eligible_false_but_not_missing_data(self):
        """算得出来但不达标 —— 这是**合格的排除**，与"算不出来"是两回事。"""
        s = _snap(aa=1.0)                       # AA 极小 ⇒ GG 远低于 II
        row = E.screen_one(s, _quote(), ii=5.0)
        assert row.eligible is False
        assert row.reasons == ["below_threshold"]
        assert row.gg_pess is not None

    def test_gg_non_positive_has_no_target_price(self):
        s = _snap(aa=-1e9)
        row = E.screen_one(s, _quote(), ii=5.0)
        assert row.target_buy_price is None
        assert row.eligible is False

    def test_o_override_is_used(self):
        base = E.screen_one(_snap(), _quote(), ii=5.0)
        with_o = E.screen_one(_snap(), _quote(), ii=5.0, o_yuan=1e9)
        assert with_o.gg_pess > base.gg_pess

    def test_position_vs_target(self):
        row = E.screen_one(_snap(), _quote(price=5.0), ii=5.0)
        assert row.position_vs_target == pytest.approx(
            (5.0 / row.target_buy_price - 1) * 100)

    def test_currency_recorded_for_reporting(self):
        row = E.screen_one(_snap(), _quote(ccy="HKD"), ii=5.0)
        assert row.currency == "HKD"

    def test_unknown_snapshot_reason_raises(self):
        s = _snap(reasons={"aa": "bogus_code"})
        with pytest.raises(KeyError):
            E.screen_one(s, _quote(), ii=5.0)


class TestRunScreen:
    def _set(self):
        good = _snap("A.HK", aa=12_000_000_000.0)
        better = _snap("B.HK", aa=24_000_000_000.0)
        bad = _snap("C.HK", aa=1.0)
        quotes = {"A.HK": _quote("A.HK"), "B.HK": _quote("B.HK"), "C.HK": _quote("C.HK")}
        return [good, better, bad], quotes

    def test_ranks_by_margin_desc(self):
        snaps, quotes = self._set()
        res = E.run_screen(snaps, quotes, ii=5.0, n_max=8)
        assert [r.ts_code for r in res.shortlist] == ["B.HK", "A.HK"]
        assert res.shortlist[0].rank == 1 and res.shortlist[1].rank == 2

    def test_n_max_caps_but_threshold_gates(self):
        snaps, quotes = self._set()
        assert len(E.run_screen(snaps, quotes, ii=5.0, n_max=1).shortlist) == 1

    def test_zero_eligible_is_a_valid_result(self):
        """全市场都不便宜的日子 ⇒ 0 只达标，且带说明（不是故障）。"""
        snaps, quotes = self._set()
        res = E.run_screen(snaps, quotes, ii=99.0, n_max=8)
        assert res.shortlist == []
        assert res.eligible_count == 0
        assert any("0 只达标" in n for n in res.notes)

    def test_rf_missing_aborts_whole_run(self):
        """II 错 ⇒ 全市场口径全错 ⇒ 整批 ABORT，不出残缺清单。"""
        snaps, quotes = self._set()
        res = E.run_screen(snaps, quotes, ii=None)
        assert res.aborted == "rf_missing"
        assert res.shortlist == [] and res.rows == []

    def test_reason_counts(self):
        snaps, quotes = self._set()
        res = E.run_screen(snaps, quotes, ii=5.0)
        assert res.reason_counts().get("below_threshold") == 1

    def test_tie_break_is_stable(self):
        a = _snap("AAA.HK"); b = _snap("BBB.HK")   # 完全同参（默认 AA 已过门槛）
        quotes = {"AAA.HK": _quote("AAA.HK"), "BBB.HK": _quote("BBB.HK")}
        res = E.run_screen([b, a], quotes, ii=5.0)
        assert [r.ts_code for r in res.shortlist] == ["AAA.HK", "BBB.HK"]

    def test_rows_include_ineligible_for_diagnostics(self):
        snaps, quotes = self._set()
        res = E.run_screen(snaps, quotes, ii=5.0)
        assert len(res.rows) == 3          # 不在榜也要留痕，便于回答"为什么不选"

    def test_names_and_markets_passed_through(self):
        snaps, quotes = self._set()
        res = E.run_screen(snaps, quotes, ii=5.0,
                           names={"B.HK": "乙"}, markets={"B.HK": "HK"})
        top = res.shortlist[0]
        assert top.name == "乙" and top.market == "HK"

    def test_to_dict_is_json_serializable(self):
        import json
        snaps, quotes = self._set()
        res = E.run_screen(snaps, quotes, ii=5.0)
        json.dumps([r.to_dict() for r in res.rows])


class TestMarketCapConsistency:
    """D-10：市值 ≡ 股本 × 现价 × fx。这是验收标准第 1 条的前置。"""

    def test_consistent_inputs_pass(self):
        shares, close, fx = 2_926.21e6, 8.98, 0.85753
        pack_cap = shares * close * fx
        ok, detail = E.check_market_cap_consistency(pack_cap, shares, close, fx)
        assert ok is True and "偏差 +0.00%" in detail

    def test_00696_real_defect_is_caught(self):
        """实测：产物股本 3228.18 vs 自洽 2926.21 百万股 ⇒ 偏差 +10.32%，必须判否。"""
        pack_cap = 22_533.63e6
        shares, close, fx = 3_228.18e6, 8.98, 0.85753
        ok, detail = E.check_market_cap_consistency(pack_cap, shares, close, fx)
        assert ok is False
        assert "+10.32%" in detail
        assert "screener 与报告 §17 的 GG 将不相等" in detail

    def test_same_currency_none_fx_treated_as_one(self):
        ok, _ = E.check_market_cap_consistency(1e10, 1e9, 10.0, None)
        assert ok is True

    @pytest.mark.parametrize("cap,shares,close", [
        (None, 1e9, 10.0), (1e10, None, 10.0), (1e10, 1e9, None), (1e10, 0, 10.0),
    ])
    def test_missing_inputs_are_not_pass(self, cap, shares, close):
        ok, detail = E.check_market_cap_consistency(cap, shares, close, 1.0)
        assert ok is False and "判不了不等于通过" in detail

    def test_non_positive_pack_cap(self):
        ok, detail = E.check_market_cap_consistency(0.0, 1e9, 10.0, 1.0)
        assert ok is False and "非正" in detail

    def test_bad_fx(self):
        ok, detail = E.check_market_cap_consistency(1e10, 1e9, 10.0, 0.0)
        assert ok is False and "非正" in detail

    def test_tolerance_is_configurable(self):
        ok_loose, _ = E.check_market_cap_consistency(1.0e10, 1.05e9, 10.0, 1.0, tol=0.06)
        ok_tight, _ = E.check_market_cap_consistency(1.0e10, 1.05e9, 10.0, 1.0, tol=0.01)
        assert ok_loose is True and ok_tight is False


class TestUpstreamGate:
    """上游产物的门禁裁决必须被 screener 尊重（采集器说"不得作为分析输入"）。"""

    def _snap(self, deliverable, blocking=("I4",)):
        s = _snap_default()
        s.gate_deliverable = deliverable
        s.gate_blocking = list(blocking)
        return s

    def test_blocked_pack_excluded_by_default(self):
        row = E.screen_one(self._snap(False), _quote(), ii=5.0)
        assert "upstream_gate_failed" in row.reasons
        assert row.eligible is False and row.gg_pess is None

    def test_allow_degraded_includes_but_discloses(self):
        """显式降级后应真的**算出**数字（而不是仍被原因码挡掉）。"""
        row = E.screen_one(self._snap(False), _quote(), ii=5.0,
                           allow_upstream_degraded=True)
        assert row.gg_pess is not None
        assert row.eligible is True
        assert "upstream_gate_failed" in row.reasons      # 仍披露

    def test_deliverable_pack_unaffected(self):
        row = E.screen_one(self._snap(True), _quote(), ii=5.0)
        assert row.eligible is True and "upstream_gate_failed" not in row.reasons

    def test_unknown_gate_status_counts_as_deliverable(self):
        row = E.screen_one(self._snap(None), _quote(), ii=5.0)
        assert row.eligible is True


class TestDisclosureReasonsDoNotBlockComputation:
    """**首版真 bug**：`if reasons:` 一刀切 ⇒ 快照级原因（如某小节的
    `input_inconsistent`）会让所有带原因码的股票都算不出 GG。"""

    def test_snapshot_level_reason_still_computes(self):
        s = _snap_default()
        s.reasons = {"s17.17.13": "input_inconsistent"}
        row = E.screen_one(s, _quote(), ii=5.0)
        assert row.gg_pess is not None, "披露级原因不得阻断计算"
        assert row.eligible is True
        assert "input_inconsistent" in row.reasons       # 但必须披露

    def test_snapshot_reason_does_not_affect_eligibility(self):
        s = _snap_default()
        s.reasons = {"s17.17.12": "hk_field_gap"}
        row = E.screen_one(s, _quote(), ii=5.0)
        assert row.eligible is True

    def test_compute_blocking_reasons_still_block(self):
        s = _snap_default(); s.aa = {}
        assert E.screen_one(s, _quote(), ii=5.0).gg_pess is None
        s2 = _snap_default(); s2.payout = {}
        assert E.screen_one(s2, _quote(), ii=5.0).gg_pess is None


def _snap_default():
    return _snap()


class TestScoreContract:
    """排名积分契约（2026-10-15 决定）：选股器**不给买点**，只给积分。

    背景：`target_buy_price` 原先出现在 `shortlist.json` / CSV / 日报里。它实际上
    **不参与任何判定**（资格 = `gg>0 and margin>0`；排序主键 = `margin`），
    唯一用到它的是"排序次键（现价相对买点）"与展示。一个纯展示的买点会被读者
    当成个股建议 —— 去掉它，排名改用积分。

    积分定义：``score = margin / II``，是 `margin` 的**单调变换**，不引入新信息。
    为什么这样定义：① 设计 §C1 要求排序键与个股报告同源，加新信息就会脱钩；
    ② `margin/II` 是"相对门槛的倍数"，天然归一化三市场的 Rf 差异，可跨市场比较。
    """

    def test_score_equals_margin_over_ii(self):
        row = E.screen_one(_snap(), _quote(), ii=5.0)
        assert row.margin is not None and row.ii
        assert row.score == pytest.approx(row.margin / row.ii)

    def test_score_is_none_without_ii(self):
        """II 缺失 ⇒ 无积分 ⇒ 不参与排名（不得兜底成 0 混进榜）。"""
        row = E.screen_one(_snap(), _quote(), ii=None)
        assert row.score is None

    def test_output_dict_has_no_buy_point(self):
        """**核心**：输出字典里不得出现买点或"现价相对买点"。"""
        d = E.screen_one(_snap(), _quote(), ii=5.0).to_dict()
        assert "target_buy_price" not in d
        assert "position_vs_target_pct" not in d
        assert "score" in d

    def test_buy_point_remains_internal_for_eligibility(self):
        """买点仍在**内部**算（资格判定与账本核验需要），只是不进出口。"""
        row = E.screen_one(_snap(), _quote(), ii=5.0)
        assert row.target_buy_price is not None

    def test_score_is_monotone_in_margin(self):
        """积分必须是 margin 的单调变换 —— 否则排序会与个股报告脱钩。"""
        lo = E.screen_one(_snap(aa=8_000_000_000.0), _quote(), ii=5.0)
        hi = E.screen_one(_snap(aa=14_000_000_000.0), _quote(), ii=5.0)
        assert lo.margin is not None and hi.margin is not None
        assert lo.margin < hi.margin
        assert lo.score < hi.score

    def test_ranking_does_not_use_buy_point(self):
        """排序次键不得依赖买点（否则删了买点排名就变）。"""
        import inspect
        src = inspect.getsource(E.run_screen) if hasattr(E, "run_screen") else ""
        if not src:
            src = inspect.getsource(E)
        assert "position_vs_target if" not in src, "排序次键仍在用买点"
