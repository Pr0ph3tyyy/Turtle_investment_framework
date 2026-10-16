"""Tests for Phase 3 prompt wiring of §17.10-§17.13 pre-computation grids.

Substring assertions on strategies/turtle/ prompt files verifying:
- "你不做数学计算" discipline constraint
- §17.10-§17.13 table-lookup references
- the §17 status contract replacing the old "降级路径" hand-calculation fallbacks
- M 来源 / GG 来源 provenance fields
- target_buy_price wired into valuation step 4-1 + factor_interface schema
"""

import pathlib

import pytest

TURTLE_DIR = pathlib.Path(__file__).resolve().parent.parent / "strategies" / "turtle"
QUANT = TURTLE_DIR / "phase3_quantitative.md"
VALUATION = TURTLE_DIR / "phase3_valuation.md"
FACTOR_INTERFACE = TURTLE_DIR / "references" / "factor_interface.md"


@pytest.fixture(scope="module")
def quant_text():
    return QUANT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def valuation_text():
    return VALUATION.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def factor_interface_text():
    return FACTOR_INTERFACE.read_text(encoding="utf-8")


class TestQuantPrompt:
    def test_no_manual_math_constraint(self, quant_text):
        assert "你不做数学计算" in quant_text

    def test_references_all_five_sections(self, quant_text):
        for tag in ["§17.10", "§17.11", "§17.12", "§17.13", "§17.14"]:
            assert tag in quant_text

    def test_pessimistic_k_selection_is_wired(self, quant_text):
        """§17.14 k-discount must be a select-a-row task, not a hand calculation."""
        assert "悲观基准网格" in quant_text
        assert "k ∈ {1.00 … 0.70}" in quant_text
        assert "M_pess" in quant_text
        assert "仓位矩阵阈值**不因折扣而调整**" in quant_text

    def test_hand_calculation_paths_are_gone(self, quant_text):
        """Design reversal (known_issues.md P2-20): no hand-calculation fallbacks.

        The old contract kept 4 "降级路径（§17.X 缺失时）" blocks with manual
        formulas as a safety net, so that a missing grid still produced a number.
        That net was the mechanism by which the core invariant "LLM 不做算术"
        got violated: the LLM re-derived values with drifting conventions, and
        P1-11 is a recorded instance (prompt formula contradicted the code).

        The new contract prefers **no number plus an explicit missing reason**
        over a hand-derived number, so the fallbacks are removed outright.
        """
        assert "降级路径（§17.12 缺失时）" not in quant_text
        assert "降级路径（§17.10 缺失时）" not in quant_text
        assert "降级路径（§17.11 缺失时）" not in quant_text
        assert "降级路径（§17.13 缺失时）" not in quant_text
        # The old manual formula must not survive as a fallback instruction
        assert "GG = [AA × M × (1−Q%) + O] / 市值 = [值]%" not in quant_text

    def test_status_contract_is_wired(self, quant_text):
        """Missing sections must route to the status contract, not to arithmetic."""
        assert "section17_status.json" in quant_text
        assert "§17 小节状态" in quant_text
        for tag in ("§17.10", "§17.11", "§17.12", "§17.13"):
            assert f"若 {tag} 状态非 `ok`" in quant_text
        assert quant_text.count("禁止手算") >= 4

    def test_provenance_fields_in_summary(self, quant_text):
        assert "M 来源" in quant_text
        assert "GG 来源" in quant_text


class TestValuationPrompt:
    def test_step_4_1_references_17_11(self, valuation_text):
        assert "§17.11" in valuation_text
        assert "target_buy_price" in valuation_text
        assert "目标买入价" in valuation_text

    def test_decision_uses_pessimistic_leg(self, valuation_text):
        """Step 3 must consume GG_pess, and say the matrix is NOT recalibrated."""
        assert "§17.14" in valuation_text
        assert "GG_pess" in valuation_text
        assert "target_buy_price_pess" in valuation_text
        assert "仓位矩阵阈值不因悲观折扣而调整" in valuation_text


class TestFactorInterface:
    def test_target_buy_price_row(self, factor_interface_text):
        assert "target_buy_price" in factor_interface_text

    def test_m_source_row(self, factor_interface_text):
        assert "M_source" in factor_interface_text

    def test_gg_source_row(self, factor_interface_text):
        assert "GG_source" in factor_interface_text
