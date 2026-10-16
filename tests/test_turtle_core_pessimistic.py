"""turtle_core.pessimistic —— 悲观档与仓位矩阵语义锁定测试。

fixture 同 decision：600887.SH（AA=8710、M_pess=54.202%、市值=175000 百万、Q=0）
⇒ GG_pess(k=1.00)=2.70%、GG_pess(k=0.80)=2.16%。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from turtle_core import pessimistic as P  # noqa: E402

AA_MM = 8710.0
MKT_MM = 175000.0
M_PESS = 54.202
C_MM = 5000.0
CLOSE = 20.0
II = 5.0


def _grid(**overrides):
    args = dict(aa_selected=AA_MM, c=C_MM, m_pess=M_PESS, q_pct=0.0, o=0.0,
                mkt_cap=MKT_MM, close=CLOSE, ii=II)
    args.update(overrides)
    return P.pessimistic_grid(**args)


class TestKDials:
    def test_seven_dials_descending(self):
        assert P.PESS_K_VALUES == (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7)

    def test_default_is_eighty_percent(self):
        assert P.PESS_K_DEFAULT == 0.8

    def test_k_slug(self):
        assert P.k_slug(1.0) == "k100"
        assert P.k_slug(0.95) == "k095"
        assert P.k_slug(0.8) == "k080"
        assert P.k_slug(0.7) == "k070"


class TestGrid:
    def test_row_count_and_default_flag(self):
        rows = _grid()
        assert len(rows) == 7
        assert sum(1 for r in rows if r.is_default) == 1
        assert next(r for r in rows if r.is_default).k == 0.8

    def test_reproduces_control_row_k100(self):
        """k=1.00 是未打折对照行（AA_pess = AA），仍走 M_pess。"""
        rows = {r.k: r for r in _grid()}
        assert rows[1.0].aa_pess == pytest.approx(AA_MM)
        assert rows[1.0].gg_pess == pytest.approx(2.70, abs=0.005)

    def test_reproduces_default_row_k080(self):
        rows = {r.k: r for r in _grid()}
        assert rows[0.8].aa_pess == pytest.approx(AA_MM * 0.8)
        assert rows[0.8].gg_pess == pytest.approx(2.16, abs=0.005)

    def test_aa_pess_is_multiplicative_not_one_minus_k(self):
        """AA_pess = AA × k（**不是** AA × (1−k)，也不是从报表重新推导）。"""
        rows = {r.k: r for r in _grid()}
        assert rows[0.7].aa_pess == pytest.approx(AA_MM * 0.7)
        assert rows[0.7].aa_pess != pytest.approx(AA_MM * (1 - 0.7))

    def test_gg_decreases_monotonically_with_k(self):
        vals = [r.gg_pess for r in _grid()]
        assert all(a > b for a, b in zip(vals, vals[1:]))

    def test_margin_is_gg_minus_ii(self):
        for r in _grid():
            assert r.vs_threshold_pct == pytest.approx(r.gg_pess - II)

    def test_target_buy_price_matches_formula(self):
        for r in _grid():
            if r.gg_pess and r.gg_pess > 0:
                assert r.target_buy_price == pytest.approx(CLOSE * r.gg_pess / II)

    def test_r_pess_uses_c_times_k(self):
        rows = {r.k: r for r in _grid()}
        assert rows[0.8].r_pess == pytest.approx(P.penetration_return(
            C_MM * 0.8, M_PESS, 0.0, 0.0, MKT_MM))

    def test_o_is_added(self):
        """O 在 canonical 里被硬编码为 0；本模块作为显式参数接通。"""
        base = {r.k: r for r in _grid()}
        with_o = {r.k: r for r in _grid(o=100.0)}
        assert with_o[0.8].gg_pess - base[0.8].gg_pess == pytest.approx(
            100.0 / MKT_MM * 100.0)

    def test_rows_kept_when_returns_uncomputable(self):
        """行数恒定，便于调用方按 claim_slug 登记缺失原因（fail-closed）。"""
        rows = _grid(mkt_cap=None)
        assert len(rows) == 7
        assert all(r.gg_pess is None for r in rows)
        assert all(r.target_buy_price is None for r in rows)
        assert all(r.claim_slug.startswith("k") for r in rows)

    def test_negative_margin_when_gg_below_ii(self):
        rows = {r.k: r for r in _grid()}
        assert rows[0.8].vs_threshold_pct == pytest.approx(2.16 - 5.0, abs=0.01)
        assert rows[0.8].vs_threshold_pct < 0

    def test_claim_slugs_are_stable_and_unique(self):
        slugs = [r.claim_slug for r in _grid()]
        assert slugs == ["k100", "k095", "k090", "k085", "k080", "k075", "k070"]
        assert len(set(slugs)) == len(slugs)


class TestPositionBand:
    @pytest.mark.parametrize("kk,expected", [
        (3.0, "KK>=1.5"), (1.5, "KK>=1.5"),
        (1.49, "KK0.5-1.5"), (0.5, "KK0.5-1.5"),
        (0.49, "KK0-0.5"), (0.0, "KK0-0.5"),
        (-0.01, "KK<0"), (-3.0, "KK<0"),
    ])
    def test_boundaries(self, kk, expected):
        assert P.position_band(kk) == expected

    def test_none(self):
        assert P.position_band(None) is None


class TestPositionMatrix:
    def test_matrix_is_verbatim_the_audit_copy(self):
        """矩阵现在有两份副本；本测试把它们钉在一起（改一处必须改两处）。"""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        import viewpoint_audit  # noqa: E402
        assert list(P.MATRIX_ROWS) == [tuple(r) for r in viewpoint_audit.MATRIX_ROWS]

    def test_high_cred_low_trap_strong_margin_is_standard(self):
        assert P.position_advice(2.0, "高", "低", 5.0, 5.0) == ["标准仓位"]

    def test_partial_function_zero_hits(self):
        """矩阵是偏函数：0 命中 ⇒ 无对应档位（审计判 UNVERIFIABLE，不得猜）。

        推导（KK>=1.5 档只覆盖 (高,低)/(高,中)/(中,低)，且 `cred=低` 行不覆盖"中"）：
        ``(中, 中)`` 不命中任何一行 —— 这是矩阵的真实缺口，不是测试造的。
        """
        assert P.position_advice(2.0, "中", "中", 5.0, 5.0) == []

    def test_partial_function_multiple_hits(self):
        """>1 命中 ⇒ 矩阵缺优先级规则（审计判 MINOR 并披露）。

        推导：``KK0-0.5`` 有兜底行 ``KK0-0.5|—|—``，而 ``cred=低`` 行与档位无关
        ⇒ 两者同时命中。
        """
        hits = P.position_advice(0.2, "低", "低", 5.0, 5.0)
        assert len(hits) == 2
        assert set(hits) == {"观察或等待", "观察或不建仓"}

    def test_catch_all_rows_always_hit_in_low_bands(self):
        """``KK0-0.5`` / ``KK<0`` 有兜底行 ⇒ 这两档永不为空。"""
        assert P.position_advice(0.2, "高", "低", 5.0, 5.0) != []
        assert P.position_advice(-1.0, "高", "低", 2.0, 5.0) == ["不建仓"]

    def test_low_credibility_row_matches_any_band(self):
        hits = P.position_advice(2.0, "低", "高", 3.0, 5.0)
        assert "观察或不建仓" in hits

    def test_high_trap_requires_gg_below_1_5x_ii(self):
        """`trap=高 & GG<II×1.5` 子句：GG=7.4 ≥ 7.5 时不排除。"""
        assert "排除" in P.position_advice(2.0, "高", "高", 7.0, 5.0)
        assert "排除" not in P.position_advice(2.0, "高", "高", 7.6, 5.0)

    def test_strong_margin_high_cred_mid_trap(self):
        assert P.position_advice(1.8, "高", "中", 9.0, 5.0) == ["70%"]

    def test_mid_band_high_cred(self):
        assert P.position_advice(1.0, "高", "低", 6.0, 5.0) == ["50%"]

    def test_mid_band_mid_cred(self):
        assert P.position_advice(1.0, "中", "低", 6.0, 5.0) == ["观察（30%）"]

    def test_negative_margin_no_position(self):
        assert P.position_advice(-2.0, "高", "低", 2.0, 5.0) == ["不建仓"]

    def test_none_margin_returns_empty(self):
        assert P.position_advice(None, "高", "低", 5.0, 5.0) == []


class TestNormPosition:
    @pytest.mark.parametrize("raw,expected", [
        ("排除（价值陷阱）", "排除"),
        ("标准仓位", "标准仓位"),
        ("不建仓", "不建仓"),
        ("70% 仓位", "70%"),
        ("50%", "50%"),
        ("观察（30%）", "观察"),
        ("等待更好价格", "观察或等待"),
        (None, "（未给出）"),
        ("", "（未给出）"),
    ])
    def test_canonicalization(self, raw, expected):
        assert P.norm_position(raw) == expected

    def test_exclude_wins_over_observe(self):
        """顺序敏感：'排除' 必须先于 '观察' 判定。"""
        assert P.norm_position("排除，观察") == "排除"
"""AA 定义域在**公式单点**侧的镜像（P4）。

报告侧 `derived_metrics._compute_pessimistic_grid` 在 AA<0 时
`mark("17.14","missing","negative_basis")` 并拒绝出数（known_issues §二十九）。
`turtle_core.pessimistic` 是同一公式的纯计算单点 —— 若它不镜像这条守卫，
**选股器会为一个框架已拒绝的标的算出买点**，直接违反验收标准①
（"选股器与 Workflow 2 报告的 §17 claim 逐字段相等"）。
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from turtle_core import pessimistic as P  # noqa: E402


def _grid_aa(aa):
    return P.pessimistic_grid(aa, 861.97, 49.81, 20.0, 0.0,
                              9_155_640_000.0, 3.79, 5.0)


class TestAaDomain:
    def test_negative_aa_is_out_of_domain(self):
        assert P.aa_domain_ok(-204060.0) is False
        assert P.aa_domain_ok(-0.01) is False

    def test_zero_and_positive_are_in_domain(self):
        assert P.aa_domain_ok(0.0) is True
        assert P.aa_domain_ok(2063.87) is True

    def test_none_is_not_judged(self):
        """AA 缺失 ≠ AA 为负：缺失由别的路径处理（标记 no_data），不在此判。"""
        assert P.aa_domain_ok(None) is True

    def test_negative_aa_yields_no_buy_price(self):
        """核心：AA<0 时 gg_pess 与买点必须是 None，不是"折扣后变小"的数。"""
        rows = _grid_aa(-13122.98)
        assert len(rows) == len(P.PESS_K_VALUES), "行数必须恒定（claim 形状依赖它）"
        assert all(r.gg_pess is None for r in rows)
        assert all(r.target_buy_price is None for r in rows)
        assert all(r.vs_threshold_pct is None for r in rows)

    def test_positive_aa_unaffected(self):
        rows = _grid_aa(2063.87)
        assert all(r.gg_pess is not None for r in rows)
        assert all(r.target_buy_price is not None for r in rows)

    def test_discount_direction_was_the_whole_point(self):
        """说明为什么必须拒：负 AA 上「折扣」方向是反的。

        AA = −13,122.98 时，k=0.7 的 AA_pess 是 −9,186.09 —— 比 k=1.0 的
        −13,122.98 **更接近零**，即"折扣后反而改善了"。这正是不能出数的原因，
        不是"数值算不出来"。
        """
        import math
        rows = {r.k: r for r in _grid_aa(-13122.98)}
        assert rows[1.0].aa_pess == pytest.approx(-13122.98)
        assert rows[0.7].aa_pess == pytest.approx(-9186.086)
        # 账目上缺口"变小"⇒ 若照算出 GG_pess，会比 k=1.0 更大（= 更不悲观）
        assert abs(rows[0.7].aa_pess) < abs(rows[1.0].aa_pess)
