"""Tests for shared/commitment_confidence.py —— 承诺执行置信度与取信额。

背景（2026-10-16，06078 海吉亚医疗）：公司 2025-12 公告「不少于人民币3亿元」回购、
2026-06-17 又公告 2026–2028「每年约5亿元」。若**静态取信公告额**，把 5 亿当年度现金
回报会让 GG 由 0.58% 跳到 10.00%（买点 19.12 > 现价 9.56）⇒ 直接翻转结论。
用户提出的方法是：**先查已实际发生的执行，再评估承诺置信度**。本测试锁定该方法的
三条性质，防止日后被"改松"：
  ① 置信度分档的边界；
  ② **取信额单调不减**（评估承诺绝不能让结论比"什么都不信"更乐观）；
  ③ 覆盖不足时**不外推**。
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared"))

import commitment_confidence as cc  # noqa: E402


class TestConfidenceTiers:
    @pytest.mark.parametrize("executed,expected", [
        (300.0, cc.HIGH),      # 100%
        (180.0, cc.HIGH),      # 60% —— 恰好取下界
        (179.9, cc.MEDIUM),    # 略低于 60%
        (90.0, cc.MEDIUM),     # 30% —— 恰好取下界
        (89.9, cc.LOW),
        (0.0, None),           # 0 ⇒ 未知（不是"低"，因为无进展也可能是还没开始）
    ])
    def test_ratio_boundaries(self, executed, expected):
        assert cc.confidence_of(executed, 300.0) == expected

    def test_missing_inputs_are_unknown_not_high(self):
        assert cc.confidence_of(None, 300.0) is None
        assert cc.confidence_of(100.0, None) is None
        assert cc.confidence_of(100.0, 0.0) is None


class TestEffectiveAmountIsMonotonic:
    """核心不变量：**取信额 ≥ 基线**。评估承诺不得产出比"什么都不信"更乐观的数。"""

    def test_low_confidence_falls_back_to_baseline(self):
        # 执行很少（置信度低）⇒ 不用那个小数，用已证基线
        a = cc.assess(commitment_amount_mm=500.0, executed_shares=1_000_000,
                      avg_price=10.0, fx_to_reporting=0.85702,
                      elapsed_months=10.5, baseline_annual_mm=22.83)
        assert a.confidence == cc.LOW
        assert a.effective_annual_mm == pytest.approx(22.83)

    def test_high_confidence_uses_execution_not_ceiling(self):
        """置信度高也只取**已执行年化**，绝不把承诺总额灌进模型。"""
        a = cc.assess(commitment_amount_mm=300.0, executed_shares=30_000_000,
                      avg_price=10.0, fx_to_reporting=0.85702,
                      elapsed_months=10.5, baseline_annual_mm=22.83)
        assert a.confidence == cc.HIGH
        # 已执行 30M×10×0.85702 = 257.1 百万元；年化 = ×12/10.5
        assert a.effective_annual_mm == pytest.approx(257.106 / 10.5 * 12, rel=1e-6)
        assert a.effective_annual_mm < 300.0, "取信额不得等于/超过承诺额度"
        assert a.effective_annual_mm < 500.0

    def test_effective_never_below_baseline_across_a_sweep(self):
        for shares in (0, 100_000, 5_000_000, 14_899_800, 30_000_000, 60_000_000):
            a = cc.assess(commitment_amount_mm=300.0, executed_shares=shares,
                          avg_price=10.0, fx_to_reporting=0.85702,
                          elapsed_months=10.5, baseline_annual_mm=22.83)
            assert a.effective_annual_mm >= 22.83 - 1e-9, (
                f"shares={shares} 时取信额 {a.effective_annual_mm} < 基线")

    def test_effective_is_non_decreasing_in_execution(self):
        prev = -1.0
        for shares in range(0, 40_000_000, 2_000_000):
            a = cc.assess(commitment_amount_mm=300.0, executed_shares=shares,
                          avg_price=10.0, fx_to_reporting=0.85702,
                          elapsed_months=10.5, baseline_annual_mm=22.83)
            assert a.effective_annual_mm >= prev - 1e-9
            prev = a.effective_annual_mm


class TestAnnualizationGuard:
    def test_does_not_annualize_with_thin_coverage(self):
        a = cc.assess(commitment_amount_mm=300.0, executed_shares=5_000_000,
                      avg_price=10.0, fx_to_reporting=0.85702,
                      elapsed_months=2.0, baseline_annual_mm=0.0)
        assert a.annualized_executed_mm == pytest.approx(a.executed_mm), \
            "覆盖不足 6 个月时不得把 2 个月的执行额放大 6 倍"

    def test_annualizes_with_sufficient_coverage(self):
        a = cc.assess(commitment_amount_mm=300.0, executed_shares=5_000_000,
                      avg_price=10.0, fx_to_reporting=0.85702,
                      elapsed_months=12.0, baseline_annual_mm=0.0)
        assert a.annualized_executed_mm == pytest.approx(a.executed_mm)

    def test_missing_price_degrades_to_baseline_and_says_so(self):
        a = cc.assess(commitment_amount_mm=300.0, executed_shares=14_899_800,
                      avg_price=None, fx_to_reporting=0.85702,
                      elapsed_months=10.5, baseline_annual_mm=22.83)
        assert a.executed_mm is None and a.confidence is None
        assert a.effective_annual_mm == pytest.approx(22.83)
        assert any("无执行均价" in n for n in a.notes)


class TestRealCase06078:
    """06078 实测：2026-01-01→10-15 回购 14,899,800 股（全部库存股、零注销）。"""

    SHARES = 14_899_800

    @pytest.mark.parametrize("px,expect_conf", [(9.40, cc.MEDIUM), (10.5, cc.MEDIUM)])
    def test_three_hundred_million_authority_is_medium_confidence(self, px, expect_conf):
        a = cc.assess(commitment_amount_mm=300.0, executed_shares=self.SHARES,
                      avg_price=px, fx_to_reporting=0.85702,
                      elapsed_months=10.5, baseline_annual_mm=22.83,
                      commitment_label="≥3亿元回购授权")
        assert a.confidence == expect_conf, (
            f"均价 {px} 时进度应落在 30–60% 区间（实测约 {a.execution_ratio:.1%}）")

    def test_five_hundred_million_plan_is_low_confidence(self):
        a = cc.assess(commitment_amount_mm=500.0, executed_shares=self.SHARES,
                      avg_price=10.0, fx_to_reporting=0.85702,
                      elapsed_months=10.5, baseline_annual_mm=22.83,
                      commitment_label="每年约5亿元")
        assert a.confidence == cc.LOW, "对 5 亿年额，实际执行进度远低于 30%"

    def test_effective_annual_is_far_below_the_ceiling(self):
        """实测取信额必须远低于两个公告额 —— 这正是"别静态取信"的量化体现。"""
        a = cc.assess(commitment_amount_mm=300.0, executed_shares=self.SHARES,
                      avg_price=10.0, fx_to_reporting=0.85702,
                      elapsed_months=10.5, baseline_annual_mm=22.83)
        assert 120.0 < a.effective_annual_mm < 160.0, a.describe()
        assert a.effective_annual_mm < 0.6 * 300.0
        assert a.effective_annual_mm < 0.4 * 500.0


class TestExecutionLoader:
    def test_missing_file_returns_empty(self, tmp_path):
        assert cc.load_execution(tmp_path) == {}

    def test_corrupt_file_returns_empty(self, tmp_path):
        (tmp_path / "commitment_execution.json").write_text("{not json", encoding="utf-8")
        assert cc.load_execution(tmp_path) == {}

    def test_totals_periods_and_honours_cutoff(self):
        ex = {"periods": [{"month": "2026-02", "shares": 937400},
                          {"month": "2026-05", "shares": 5356000},
                          {"month": "2026-09", "shares": 3012600}]}
        assert cc.total_executed_shares(ex) == 9_306_000
        assert cc.total_executed_shares(ex, upto_month="2026-05") == 6_293_400


class TestRepoDataIsConsistent:
    """仓库内 06078 的执行记录：逐月增量必须与库存股首尾差**逐位相符**。"""

    def test_monthly_increments_match_treasury_delta(self):
        ex = cc.load_execution(ROOT / "output" / "06078_海吉亚医疗")
        if not ex:
            pytest.skip("未找到 commitment_execution.json")
        periods = cc.periods_of(ex)
        assert periods, "执行期序列为空"
        total = cc.total_executed_shares(ex)
        declared = (ex.get("execution") or {}).get("shares_total") or ex.get("shares_total")
        assert total == declared, f"逐月加总 {total} 与登记的 {declared} 不符"
        assert total == 14_899_800, "06078 实测总回购股数"
        # 库存股快照必须逐位闭合：最后一个快照 − 第一个快照 == 各期增量之和
        snap = [p for p in periods if p.get("treasury_end") is not None]
        assert len(snap) == len(periods), "每期都应有 treasury_end 快照"
        assert snap[-1]["treasury_end"] - snap[0]["treasury_end"] == total - \
            int(snap[0].get("shares") or 0), "库存股快照差与逐月增量不一致"
