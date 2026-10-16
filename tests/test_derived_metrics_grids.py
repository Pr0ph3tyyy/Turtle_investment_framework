"""Tests for §17.10-§17.13 pre-computation grids (Phase 3).

Covers:
- §17.10 payout M three-method cross-check + recommendation + filler-error fixture
- §17.11 penetrating return-rate grid (R, GG, HH, 目标买入价, ★, II, O note, HK Q cols)
- §17.12 standalone G→维持性Capex grid (12 rows, OE/H identities, bands)
- §17.13 λ revenue sensitivity (echo, scenario rows, critical multiple, degraded path)
- whole-pipeline: compute_derived_metrics contains 17.10-17.13; graceful empty store

Helper pattern (_load_mock / _make_client / store building) copied from
test_derived_metrics.py — kept local so this file is self-contained.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pandas as pd

from tushare_collector import TushareClient

MOCK_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "mock_tushare_responses")


def _load_mock(filename: str) -> pd.DataFrame:
    with open(os.path.join(MOCK_DIR, filename)) as f:
        data = json.load(f)
    if isinstance(data, list):
        return pd.DataFrame(data)
    return pd.DataFrame([data])


def _make_client() -> TushareClient:
    with patch("tushare_collector.ts") as mock_ts:
        mock_ts.pro_api.return_value = MagicMock()
        client = TushareClient("test_token")
    return client


def _base_store(dividend_file: str = "dividend.json") -> dict:
    income_df = _load_mock("income.json").sort_values("end_date", ascending=False)
    bs_df = _load_mock("balancesheet.json").sort_values("end_date", ascending=False)
    cf_df = _load_mock("cashflow.json").sort_values("end_date", ascending=False)
    div_df = _load_mock(dividend_file).sort_values("end_date", ascending=False)
    rf_df = _load_mock("yc_cb.json").sort_values("trade_date", ascending=False)
    return {
        "income": income_df,
        "balance_sheet": bs_df,
        "cashflow": cf_df,
        "dividends": div_df,
        "risk_free_rate": rf_df,
    }


def _make_grid_client(dividend_file: str = "dividend.json",
                      with_basic: bool = True) -> TushareClient:
    """Client with full store, factor3 chain + §17.10 crosscheck already run."""
    client = _make_client()
    client._store = _base_store(dividend_file)
    if with_basic:
        client._store["basic_info"] = _load_mock("daily_basic.json")
    # Run factor3 chain (populates factor3_sensitivity incl. λ) then §17.10
    client._compute_factor3_step1()
    client._compute_factor3_step4()
    client._compute_factor3_sensitivity_base()
    client._compute_payout_crosscheck()
    return client


# ===== §17.10 payout crosscheck =====


class TestPayoutCrosscheck:
    def test_headers(self):
        client = _make_grid_client()
        result = client._compute_payout_crosscheck()
        assert result is not None
        assert "17.10 支付率 M 三重校验" in result
        assert "法1 §6 DPS/EPS" in result
        assert "法2 §5 分配现金/归母" in result
        assert "法3 §17.1 口径" in result

    def test_per_year_values(self):
        """法2 2024 = 5800/10120 = 57.31%; 法1 2024 = 0.97/1.59 = 61.01%."""
        client = _make_grid_client()
        result = client._compute_payout_crosscheck()
        assert "57.31%" in result  # 法2 2024
        assert "61.01%" in result  # 法1 2024

    def test_three_year_means(self):
        """m1=58.26, m2=54.20, m3=58.08 (3yr means of 2024/2023/2022)."""
        client = _make_grid_client()
        result = client._compute_payout_crosscheck()
        pc = client._store["payout_crosscheck"]
        assert round(pc["m1"], 2) == 58.26
        assert round(pc["m2"], 2) == 54.20
        assert round(pc["m3"], 2) == 58.08

    def test_recommendation_default_is_method3(self):
        """With consistent methods (<15% dev), M_rec = 法3."""
        client = _make_grid_client()
        client._compute_payout_crosscheck()
        pc = client._store["payout_crosscheck"]
        assert pc["m_rec_label"] == "法3"
        assert round(pc["m_rec"], 2) == 58.08
        assert "M_rec = 58.08%（法3）" in client._compute_payout_crosscheck()

    def test_filler_error_fixture(self):
        """Identical A-share DPS across ≥3 years → filler warning + M_rec=法2."""
        client = _make_grid_client(dividend_file="dividend_identical.json")
        result = client._compute_payout_crosscheck()
        pc = client._store["payout_crosscheck"]
        assert pc["m_rec_label"] == "法2"
        assert any("填充" in w for w in pc["warnings"])
        assert "填充" in result

    def test_returns_none_no_data(self):
        client = _make_client()
        assert client._compute_payout_crosscheck() is None

    # --- 21b: documented dividend commitment takes priority over history ---

    def test_announced_min_payout_overrides_history(self):
        """A minimum payout written in a filing anchors M outright ([1:09:42])."""
        client = _make_grid_client()
        client._store["dividend_commitment"] = {
            "kind": "公告", "min_payout_pct": 70.0, "period": "2024-2026",
            "source": "年报P.45",
        }
        text = client._compute_payout_crosscheck()
        pc = client._store["payout_crosscheck"]
        assert pc["m_rec"] == 70.0
        assert pc["m_rec_label"] == "承诺(支付率)"
        assert "一票成立" in pc["m_rec_reason"]
        assert any("覆盖三法交叉校验" in w for w in pc["warnings"])
        assert "承诺(支付率)" in text

    def test_verbal_commitment_needs_repetition(self):
        """Oral/earnings-call mentions need ≥2 occurrences to be credible."""
        client = _make_grid_client()
        client._store["dividend_commitment"] = {
            "kind": "口头", "min_payout_pct": 70.0, "occurrences": 1,
        }
        client._compute_payout_crosscheck()
        pc = client._store["payout_crosscheck"]
        assert round(pc["m_rec"], 2) == 58.08   # unchanged → fell back to 法3
        assert pc["m_rec_label"] == "法3"

    def test_verbal_commitment_with_two_occurrences_is_used(self):
        client = _make_grid_client()
        client._store["dividend_commitment"] = {
            "kind": "口头", "min_payout_pct": 65.0, "occurrences": 2,
        }
        client._compute_payout_crosscheck()
        pc = client._store["payout_crosscheck"]
        assert pc["m_rec"] == 65.0
        assert any("口头/电话会议" in w for w in pc["warnings"])

    def test_amount_commitment_is_converted_to_implied_payout(self):
        """A committed *amount* becomes an equivalent payout using latest earnings."""
        client = _make_grid_client()
        client._store["dividend_commitment"] = {
            "kind": "公告", "min_amount_mm": 20000.0, "source": "公告2026-04-01",
        }
        client._compute_payout_crosscheck()
        pc = client._store["payout_crosscheck"]
        assert pc["m_rec_label"] == "承诺(金额折算)"
        # 20,000 / 10,120 (latest 归母净利润) = 197.63%
        assert round(pc["m_rec"], 2) == 197.63

    def test_no_commitment_keeps_three_method_logic(self):
        client = _make_grid_client()
        client._store.pop("dividend_commitment", None)
        client._compute_payout_crosscheck()
        assert client._store["payout_crosscheck"]["m_rec_label"] == "法3"

    def test_hk_duplicate_dividend_rows_are_not_summed(self):
        """HK `dps_hkd` is a per-announcement snapshot, not an additive event.

        Regression (known_issues.md P1-8): 06862 FY2024 had two rows
        {0.7220, 1.2290}; the old code summed both, giving 法1 = 224.25% instead of
        1.2290/EPS. Mock EPS(2024) = 1.59 → annual 77.30%, summed 122.70%.
        """
        client = _make_grid_client()
        client._store["dividends"] = pd.DataFrame([
            {"end_date": "20241231", "cash_div_tax": 1.2290},
            {"end_date": "20240630", "cash_div_tax": 0.7220},
        ])
        client._store["dividends_hk"] = pd.DataFrame([
            {"end_date": "20241231", "dps_hkd": 1.2290, "divi_ratio": 141.26},
            {"end_date": "20240630", "dps_hkd": 0.7220, "divi_ratio": 82.0},
        ])
        text = client._compute_payout_crosscheck()
        cc = client._store["payout_crosscheck"]
        # 现在有更强的保证：港股法1 整列停用 ⇒ 既不会求和（P1-8 的 122.70%），
        # 也不会拿快照单值去当逐年 DPS（00506 数值审核的发现）。
        assert cc["m1"] is None
        assert "122.70%" not in text     # (1.2290 + 0.7220) / 1.59 — P1-8 的旧 bug
        assert "77.30%" not in text      # 1.2290 / 1.59 —— 快照口径，同样不再引用
        assert any("法1 已**停用**" in w for w in cc["warnings"])

    def test_hk_no_annual_row_never_sums_or_trusts_the_snapshot(self):
        """没有 1231 行时，港股法1 同样不出数 —— 既不相加，也不信快照。

        回归锚点（00506 数值审核，路径 B）：上游 `dps_hkd`/`cash_div_tax` 是「按公告
        快照」单值，与年报 DPS 序列不符；`invariants I5` 已点名不得用于派息率。
        故港股法1 停用，逐年派息率由法3（年报宣告口径）承担。
        """
        client = _make_grid_client()
        client._store["dividends"] = pd.DataFrame([
            {"end_date": "20240630", "cash_div_tax": 0.5},
            {"end_date": "20240331", "cash_div_tax": 0.3},
        ])
        client._store["dividends_hk"] = pd.DataFrame([
            {"end_date": "20240630", "dps_hkd": 0.5, "divi_ratio": 30.0},
        ])
        text = client._compute_payout_crosscheck()
        cc = client._store["payout_crosscheck"]
        assert cc["m1"] is None, "港股法1 必须停用（不信快照、也不相加）"
        assert "31.45%" not in text      # 0.5 / 1.59 —— 旧口径
        assert "50.31%" not in text      # 0.8 / 1.59 —— 更早的求和 bug
        assert any("法1 已**停用**" in w for w in cc["warnings"])

    def test_a_share_interim_plus_final_still_summed(self):
        """A-share interim and final are separate cash events — summing is correct."""
        client = _make_grid_client()
        client._store["dividends"] = pd.DataFrame([
            {"end_date": "20241231", "cash_div_tax": 0.6},
            {"end_date": "20240630", "cash_div_tax": 0.37},
        ])
        client._store.pop("dividends_hk", None)
        text = client._compute_payout_crosscheck()
        assert "61.01%" in text          # (0.6 + 0.37) / 1.59


class TestIIThresholdSingleSource:
    """门槛 II must be derived in exactly one place (known_issues.md P1-12)."""

    def test_sections_agree_on_ii(self):
        client = _make_grid_client()
        _rf, ii = client._get_rf_ii("600887.SH")
        assert ii is not None
        assert f"{ii:.2f}%" in client._compute_factor2_inputs("600887.SH")
        assert f"II = {ii:.2f}%" in client._compute_factor4_sensitivity("600887.SH")
        assert f"门槛 II = {ii:.2f}%" in client._compute_penetration_grid("600887.SH")

    def test_uniform_threshold_across_markets(self):
        """One rule for every market: max(5%, Rf+3%) — decided 2026-10-13.

        The source material is self-contradictory (A-share 3% vs 4% vs a 4% worked
        example; HK 4% vs 5%; US undefined), so the project deliberately adopts the
        strictest single rule instead of per-market tiers.
        """
        from tushare_collector import II_FLOOR, II_SPREAD, ii_threshold
        assert (II_FLOOR, II_SPREAD) == (5.0, 3.0)
        for code in ("600941.SH", "06862.HK", "AAPL.US"):
            assert ii_threshold(code, 1.0) == 5.0      # floor binds
            assert ii_threshold(code, 4.0) == 7.0      # spread binds
            assert round(ii_threshold(code, 2.31), 2) == 5.31


# ===== §17.11 penetration grid =====


class TestPenetrationGrid:
    def test_headers_and_ii(self):
        client = _make_grid_client()
        result = client._compute_penetration_grid("600887.SH")
        assert result is not None
        assert "17.11 穿透回报率网格" in result
        assert "表A：粗算穿透回报率 R" in result
        assert "表B：精算穿透回报率 GG" in result
        # II = max(5, 2.315+3) = 5.315 (uniform across markets) -> renders 5.31
        assert "门槛 II = 5.31%" in result

    def test_table_a_r_cell(self):
        """R(m_rec=58.08%, Q=0) = 10120×0.5808/175000×100 = 3.36%."""
        client = _make_grid_client()
        result = client._compute_penetration_grid("600887.SH")
        assert "3.36%" in result

    def test_table_b_gg_and_target(self):
        """AA is now OCF-anchored: OCF 16,850M − Capex 7,850M = 9,000M (2024).

        AA_2y = mean(9000, 8420) = 8710M → GG = 8710×0.5808/175000×100 = 2.89%;
        target = 27.5 × 2.8907/4.315 = 18.42.
        """
        client = _make_grid_client()
        result = client._compute_penetration_grid("600887.SH")
        assert "2.89%" in result
        # II = 5.315 → target = 27.5 × 2.8908/5.315
        assert "14.96" in result  # 目标买入价 AA_2y×法3
        assert "13.41" in result  # 目标买入价 AA_all×法3

    def test_target_buy_price_identity(self):
        """目标买入价 = close × GG_default / II (hand-recompute AA_2y×法3)."""
        client = _make_grid_client()
        client._compute_penetration_grid("600887.SH")
        # rebuild the exact numbers
        close = 27.50
        mkt_cap = 175000.0            # 百万
        aa_2y = 8710.0                # OCF 锚定口径：mean(9000, 8420)
        m_rec = client._store["payout_crosscheck"]["m_rec"]
        _, ii = client._get_rf_ii("600887.SH")
        assert round(ii, 3) == 5.315  # max(5, 2.315+3)
        gg = aa_2y * m_rec / 100 / mkt_cap * 100
        target = close * gg / ii
        assert round(target, 2) == 14.96

    def test_hh_consistency(self):
        """HH(AA_2y×法3, default Q) = R(法3,Q0) − GG(AA_2y,法3,Q0) = 3.36 − 2.89 = +0.47."""
        client = _make_grid_client()
        result = client._compute_penetration_grid("600887.SH")
        assert "+0.47" in result

    def test_star_marker_on_recommended(self):
        client = _make_grid_client()
        result = client._compute_penetration_grid("600887.SH")
        assert "★" in result
        # ★ attached to 法3 (recommended), both table A and B
        assert "法3 58.08% ★" in result

    def test_o_note(self):
        """O=0 default with additive-correction note (+100/mktcap×100)."""
        client = _make_grid_client()
        result = client._compute_penetration_grid("600887.SH")
        assert "O = 0（默认）" in result
        # 100/175000*100 = 0.0571
        assert "0.0571 pct" in result

    def _grid_gg_at_default_q(self, grid: str) -> str:
        """GG of the first AA_2y row at the DEFAULT-Q column.

        The column order is market dependent (A-share Q=0 leads; HK lists 28%
        first with 20% as the default), so locate it from the header rather than
        assuming an index.
        """
        header = [l for l in grid.splitlines() if l.startswith("| AA×M |")][0]
        cols = [c.strip() for c in header.strip("|").split("|")]
        idx = next(i for i, c in enumerate(cols) if "（默认）" in c)
        row = [l for l in grid.splitlines() if l.startswith("| AA_2y×")][0]
        return [c.strip() for c in row.strip("|").split("|")][idx]

    def test_17_9_and_17_11_share_the_same_basis(self):
        """§17.9 and §17.11 must agree numerically on the base scenario.

        Regression (known_issues.md P1-10): §17.9 used §17.1's 3-year mean and no
        (1−Q) while §17.11 used M_rec and the default Q, yet phase3_valuation told
        the agent to quote §17.9's 门槛价格. For HK the two differed by 25%.
        """
        client = _make_grid_client()
        grid = client._compute_penetration_grid("600887.SH")
        sens = client._compute_factor4_sensitivity("600887.SH")

        gg_grid = self._grid_gg_at_default_q(grid)
        base_row = [l for l in sens.splitlines() if l.startswith("| 基准 |")][0]
        gg_sens = [c.strip() for c in base_row.strip("|").split("|")][2]

        assert gg_grid == gg_sens, f"§17.11={gg_grid} vs §17.9={gg_sens}"
        assert "同一 M_rec 与默认 Q" in grid
        assert "口径与 §17.11 表B 一致" in sens

    def test_17_9_matches_17_11_for_hk_default_column(self):
        """HK lists Q=28% before the default Q=20% — the alignment must hold there too."""
        client = _make_grid_client()
        client._currency = "HKD"
        client._store["basic_info"] = pd.DataFrame([{
            "ts_code": "06862.HK", "end_date": "20251231",
            "total_market_cap": 53_343_180_000.0, "close": 11.69,
        }])
        grid = client._compute_penetration_grid("06862.HK")
        sens = client._compute_factor4_sensitivity("06862.HK")
        if grid is None or sens is None:
            pytest.skip("HK grid unavailable in this fixture")

        gg_grid = self._grid_gg_at_default_q(grid)
        base_row = [l for l in sens.splitlines() if l.startswith("| 基准 |")][0]
        gg_sens = [c.strip() for c in base_row.strip("|").split("|")][2]
        assert gg_grid == gg_sens, f"§17.11={gg_grid} vs §17.9={gg_sens}"

    def test_negative_gg_yields_no_target_price(self):
        """GG ≤ 0 ⇒ 目标买入价 must render '—', never a negative price.

        Regression (added 2026-10-13): 600941 中国移动 has AA_2y = -70,808,
        which made §17.11 emit 目标买入价 = -64.79 元.
        """
        client = _make_grid_client()
        client._store["factor3_sensitivity"]["aa_2y"] = -70808.0
        client._store["factor3_sensitivity"]["aa_all"] = -79379.0
        client._store["factor3_sensitivity"]["aa_excl"] = -70808.0
        result = client._compute_penetration_grid("600887.SH")
        assert result is not None

        target_cells = []
        for line in result.splitlines():
            if line.startswith("| AA_") and "×" in line:
                cells = [c.strip() for c in line.strip("|").split("|")]
                target_cells.append(cells[-1])
        assert target_cells, "expected table-B rows"
        assert all(c == "—" for c in target_cells), target_cells
        assert "目标买入价无解" in result

    def test_positive_gg_still_renders_target_price(self):
        """The guard must not suppress the normal (positive-GG) case."""
        client = _make_grid_client()
        result = client._compute_penetration_grid("600887.SH")
        target_cells = []
        for line in result.splitlines():
            if line.startswith("| AA_") and "×" in line:
                cells = [c.strip() for c in line.strip("|").split("|")]
                target_cells.append(cells[-1])
        assert target_cells
        assert all(c != "—" for c in target_cells), target_cells

    def test_none_without_factor3_sensitivity(self):
        client = _make_client()
        client._store = {"income": _load_mock("income.json"),
                         "basic_info": _load_mock("daily_basic.json")}
        assert client._compute_penetration_grid("600887.SH") is None

    def test_none_without_basic_info(self):
        client = _make_grid_client(with_basic=False)
        assert client._compute_penetration_grid("600887.SH") is None

    def test_hk_q_columns(self):
        """HK unique tax rates → Q columns {28, 20}."""
        client = _make_client()
        client._currency = "HKD"
        client._store = _base_store()
        client._store["basic_info"] = pd.DataFrame([
            {"close": 27.50, "total_market_cap": 175000.0}])
        client._compute_factor3_step1()
        client._compute_factor3_step4()
        client._compute_factor3_sensitivity_base()
        client._compute_payout_crosscheck()
        result = client._compute_penetration_grid("00700.HK")
        assert result is not None
        assert "28%" in result
        assert "20%" in result


# ===== §17.12 G grid =====


class TestGGrid:
    def test_headers(self):
        client = _make_grid_client()
        result = client._compute_g_grid("600887.SH")
        assert result is not None
        assert "17.12 G 系数网格" in result
        assert "LLM 仅选行，禁止自算" in result

    def test_twelve_rows(self):
        """G = 0.7 … 1.8 → 12 rows."""
        client = _make_grid_client()
        result = client._compute_g_grid("600887.SH")
        for g in ["0.7", "1.0", "1.4", "1.8"]:
            assert f"| {g} |" in result
        # count data rows: 12 lines starting with "| 0." or "| 1."
        data_rows = [ln for ln in result.splitlines()
                     if ln.startswith("| 0.") or ln.startswith("| 1.")]
        assert len(data_rows) == 12

    def test_oe_equals_c_at_g_one(self):
        """OE = C + D − D×1.0 = C = 10,120.00 at G=1.0."""
        client = _make_grid_client()
        result = client._compute_g_grid("600887.SH")
        g10_row = [ln for ln in result.splitlines() if ln.startswith("| 1.0 |")][0]
        assert "10,120.00" in g10_row  # OE at G=1.0 == C

    def test_h_at_g_max(self):
        """H = D×1.8 = 3200×1.8 = 5,760.00 at G=1.8."""
        client = _make_grid_client()
        result = client._compute_g_grid("600887.SH")
        g18_row = [ln for ln in result.splitlines() if ln.startswith("| 1.8 |")][0]
        assert "5,760.00" in g18_row

    def test_band_labels(self):
        client = _make_grid_client()
        result = client._compute_g_grid("600887.SH")
        for band in ["轻", "轻中", "中", "中重", "重"]:
            assert band in result

    def test_f_reference(self):
        """F (Capex/D&A 5yr median) = 2.48."""
        client = _make_grid_client()
        result = client._compute_g_grid("600887.SH")
        assert "2.48" in result

    def test_none_without_cashflow(self):
        client = _make_client()
        client._store = {"income": _load_mock("income.json").sort_values(
            "end_date", ascending=False)}
        assert client._compute_g_grid("600887.SH") is None


# ===== missing close price (live finding 2026-10-13) =====


class TestMissingClosePrice:
    """A missing close price must degrade the target price, not the whole grid.

    Live run found: `hk_daily`'s quota (5/day) exhausted → §1 当前价格 "—",
    §2/§11 missing — and §17.11/§17.13/§17.14 vanished entirely even though
    GG/R need only the market cap. Only 目标买入价 needs the price.
    """

    def _client_without_close(self):
        client = _make_grid_client()
        bi = client._store["basic_info"].copy()
        bi["close"] = None
        client._store["basic_info"] = bi
        client._compute_payout_crosscheck()
        return client

    def test_market_cap_still_available(self):
        client = self._client_without_close()
        ms = client._get_mktcap_shares("600887.SH")
        assert ms is not None
        assert ms[0] > 0          # market cap present
        assert ms[2] is None      # close absent

    def test_17_11_still_produces_gg_and_hh(self):
        """GG/HH/GG−II are market-cap based — they must survive."""
        client = self._client_without_close()
        out = client._compute_penetration_grid("600887.SH")
        assert out is not None
        assert "### 17.11" in out
        assert "2.89%" in out                      # GG still computed
        assert "当前股价不可用" in out

    def test_17_11_target_price_degrades_to_dash(self):
        client = self._client_without_close()
        out = client._compute_penetration_grid("600887.SH")
        rows = [l for l in out.splitlines() if l.startswith("| AA_2y×法3")]
        assert rows, "recommended row must still render"
        assert rows[0].rstrip().endswith("| — |")

    def test_17_14_still_produces_the_k_grid(self):
        client = self._client_without_close()
        out = client._compute_pessimistic_grid("600887.SH")
        assert out is not None
        assert "### 17.14" in out
        assert "| 0.80 ★ |" in out
        for k in ("1.00", "0.95", "0.90", "0.85", "0.80", "0.75", "0.70"):
            assert f"| {k}" in out

    def test_reason_code_is_no_price_not_hk_field_gap(self):
        """The reason must name the actual cause so the remedy is actionable."""
        client = _make_grid_client()
        bi = client._store["basic_info"].copy()
        bi["close"] = None
        client._store["basic_info"] = bi
        client._store.pop("risk_free_rate", None)
        result = client.compute_derived_metrics("600887.SH")
        payload = client._store["section17_status"]
        entry = payload["sections"]["17.6"]
        assert entry["reason"] == "no_price"
        assert "配额" in entry["remedy"]
        assert "### 17.6" not in result

    def test_close_available_detects_zero_and_none(self):
        client = _make_grid_client()
        assert client._close_available() is True
        bi = client._store["basic_info"].copy()
        bi["close"] = 0
        client._store["basic_info"] = bi
        assert client._close_available() is False


# ===== §17 claim registration (known_issues.md P2-20 改动 1/2) =====


class TestClaimRegistration:
    """§17 decision cells must be registered as verifiable claims and rendered as KV."""

    def test_17_11_registers_gg_hh_vsii_for_recommended_row(self):
        client = _make_grid_client()
        out = client._compute_penetration_grid("600887.SH")
        ledger = client._claim_ledger()
        # AA_2y is the default variant; the recommended M is 法3
        gg = ledger.get("cm17.11.aa2y.m3.gg")
        assert gg is not None
        assert gg.value == 2.89
        assert gg.unit == "%"
        assert gg.policy == "round:2"
        assert "AA_2y=" in gg.basis
        for suffix in ("hh", "vsii", "tbp"):
            assert ledger.get(f"cm17.11.aa2y.m3.{suffix}") is not None, suffix
        assert "决策单元格（KV · 带 claim id）" in out
        assert "claim: cm17.11.aa2y.m3.gg" in out

    def test_17_11_only_registers_the_recommended_method(self):
        """Registering every method would create claims that are never quoted."""
        client = _make_grid_client()
        client._compute_penetration_grid("600887.SH")
        ledger = client._claim_ledger()
        ids = [c.claim_id for c in ledger.claims() if c.claim_id.startswith("cm17.11.")]
        assert any(i.endswith(".m3.gg") for i in ids)
        assert not any(".m1." in i or ".m2." in i for i in ids)

    def test_17_11_target_price_is_null_not_negative_when_gg_non_positive(self):
        """GG ≤ 0 has no target price; the claim must be null WITH a reason."""
        client = _make_grid_client()
        f3 = client._store["factor3_sensitivity"]
        f3.update({"aa_2y": -1_000_000.0, "aa_all": -1_000_000.0,
                   "aa_excl": -1_000_000.0, "aa_selected": -1_000_000.0})
        client._compute_penetration_grid("600887.SH")
        claim = client._claim_ledger().get("cm17.11.aa2y.m3.tbp")
        assert claim.value is None
        assert "无解" in claim.note

    def test_17_14_registers_every_k_row(self):
        """The agent may pick any k, so every row needs a bindable claim."""
        client = _make_grid_client()
        out = client._compute_pessimistic_grid("600887.SH")
        ledger = client._claim_ledger()
        for k in (100, 95, 90, 85, 80, 75, 70):
            assert ledger.get(f"cm17.14.k{k:03d}.ggpess") is not None, k
            assert ledger.get(f"cm17.14.k{k:03d}.tbp") is not None, k
        assert ledger.get("cm17.14.k080.ggpess").value == 2.16   # 8710×0.8
        assert "决策单元格（KV · 带 claim id）" in out

    def test_17_10_registers_mrec_and_mpess(self):
        client = _make_grid_client()
        client._compute_payout_crosscheck()
        ledger = client._claim_ledger()
        assert ledger.get("cm17.10.mrec").value == 58.08
        assert ledger.get("cm17.10.mpess").value == 54.20

    def test_all_claims_have_enum_units_and_policies(self):
        client = _make_grid_client()
        client._compute_factor3_step1()
        client._compute_factor3_step4()
        client._compute_factor3_sensitivity_base()
        client._compute_payout_crosscheck()
        client._compute_penetration_grid("600887.SH")
        client._compute_pessimistic_grid("600887.SH")
        from claim_ledger import UNIT_ENUM, VALID_POLICIES
        for claim in client._claim_ledger().claims():
            assert claim.unit in UNIT_ENUM, claim.claim_id
            assert claim.policy.split(":")[0] in VALID_POLICIES, claim.claim_id

    def test_ledger_resets_between_runs(self):
        """A re-run must not accumulate stale claims from the previous run."""
        client = _make_grid_client()
        client._compute_penetration_grid("600887.SH")
        first = len(client._claim_ledger())
        client.compute_derived_metrics("600887.SH")
        second = len(client._claim_ledger())
        assert second >= first          # full run registers more, never doubles
        ids = [c.claim_id for c in client._claim_ledger().claims()]
        assert len(ids) == len(set(ids))


# ===== §17.15 defensive net asset =====


class TestDefensiveNetAsset:
    """§17.15 防御性净值 —— 假资产剔除（known_issues.md §3.21 21d）。"""

    def test_two_calibers(self):
        """A (goodwill + all intangibles) and B (goodwill only) must both appear.

        Mock: cash = 28,960 + 1,520 = 30,480M; debt = 12,800+5,200+2,000+1,200 = 21,200M
              goodwill 6,780M; intangibles 4,120M
          A = 30,480 − 21,200 − 6,780 − 4,120 = -1,620
          B = 30,480 − 21,200 − 6,780        = 2,500
        """
        client = _make_grid_client()
        result = client._compute_defensive_net_asset("600887.SH")
        assert result is not None
        assert "17.15 防御性净值" in result
        assert "A 严口径" in result
        assert "B 宽口径" in result
        assert "-1,620.00" in result
        assert "2,500.00" in result

    def test_goodwill_always_deducted(self):
        """Goodwill is deducted in BOTH calibers — no ambiguity in the source."""
        client = _make_grid_client()
        result = client._compute_defensive_net_asset("600887.SH")
        assert "6,780.00" in result
        assert "全额扣除（两口径一致）" in result

    def test_documents_the_source_contradiction(self):
        """Deducting ALL intangibles would contradict the doc's own 路权 argument."""
        client = _make_grid_client()
        result = client._compute_defensive_net_asset("600887.SH")
        assert "文档张力" in result
        assert "路权" in result
        assert "经营必需" in result

    def test_none_without_balance_sheet(self):
        client = _make_client()
        assert client._compute_defensive_net_asset("600887.SH") is None


class TestDefensiveNetAssetInclusiveIntangibles:
    """「无形资产」**含商誉**时，A 口径不得把商誉扣两次（06078 实测）。

    两种编报惯例都存在，相加会重复扣减、一刀切会漏扣，故由 sidecar 显式声明成分：
      · `disjoint`（缺省，06862 形态）：商誉 84.84 与无形资产 49.25 各自独立 ⇒ A 相加；
      · `inclusive`（06078 形态）：附註16 栏目头 `商譽｜軟件｜合約權利｜醫療執照｜總計`，
        总计 3,650,802 千元逐位等于 §4「无形资产」行、财务状况表无独立商誉行位 ⇒ A 只扣一次。

    旧实现在 inclusive 下把商誉扣两次：06078 的 A 由 −5,408.29 被压到 −8,849.42
    （虚增 3,441.13 = 一个商誉），而 I12 只验算术自洽 ⇒ 谎报 PASS。
    """

    def _client(self, inclusive: bool):
        from unittest.mock import patch
        from tushare_collector import TushareClient
        client = _make_grid_client()
        client._store["intangibles_includes_goodwill"] = inclusive
        return client

    # mock: cash 30,480 / debt 21,200 / goodwill 6,780 / intangibles 4,120
    def test_inclusive_does_not_double_deduct(self):
        client = self._client(True)
        result = client._compute_defensive_net_asset("600887.SH")
        assert "全扣含商誉的无形资产" in result
        # A = 30,480 − 21,200 − 4,120 = 5,160（**不是** −1,620）
        assert "5,160.00" in result
        assert "-1,620.00" not in result
        # B = 30,480 − 21,200 − 6,780 − max(4,120−6,780,0) = 2,500（无形小于商誉 ⇒ 退化为只扣商誉）
        assert "2,500.00" in result
        assert "重叠科目已处理" in result

    def test_disjoint_keeps_legacy_arithmetic(self):
        client = self._client(False)
        result = client._compute_defensive_net_asset("600887.SH")
        assert "-1,620.00" in result and "2,500.00" in result
        assert "全额扣除（两口径一致）" in result
        assert "重叠科目已处理" not in result


# ===== §17.16 defensive gates =====


class TestDefensiveGates:
    """§17.16 防御性门槛组（known_issues.md §3.21 21c）。"""

    def test_six_gates_present(self):
        client = _make_grid_client()
        client._compute_factor3_step1()
        client._compute_factor3_step4()
        client._compute_factor3_sensitivity_base()
        client._compute_payout_crosscheck()
        result = client._compute_defensive_gates("600887.SH")
        assert result is not None
        assert "17.16 防御性门槛组" in result
        for gate in ("①", "②", "③", "④", "⑤", "⑥"):
            assert gate in result
        assert "因子2 [56:28]" in result
        assert "因子3 [42:08]" in result

    def test_is_not_a_veto(self):
        """Explicitly a tier/marker layer, NOT part of the buy/observe/avoid ruling."""
        client = _make_grid_client()
        client._compute_factor3_sensitivity_base()
        client._compute_payout_crosscheck()
        result = client._compute_defensive_gates("600887.SH")
        assert "非硬否决" in result
        assert "不参与「买入/观察/排除」裁决" in result

    def test_cash_scope_is_labelled(self):
        client = _make_grid_client()
        client._compute_factor3_sensitivity_base()
        result = client._compute_defensive_gates("600887.SH")
        assert "狭义" in result and "广义" in result

    def test_duplicate_cash_component_is_flagged(self):
        """Tushare sometimes returns the same number for cash and short-term deposits.

        Real case: 06862 FY2025 returned 6,602.3 for both. Summing them would
        double count cash.
        """
        client = _make_grid_client()
        bs = client._store["balance_sheet"].copy()
        bs["st_deposit"] = bs["money_cap"]          # force the duplicate
        client._store["balance_sheet"] = bs
        client._compute_factor3_sensitivity_base()
        result = client._compute_defensive_gates("600887.SH")
        assert "疑似 Tushare 重复" in result
        assert "广义现金只计一次" in result

    def test_none_without_balance_sheet(self):
        client = _make_client()
        assert client._compute_defensive_gates("600887.SH") is None


# ===== §17.14 pessimistic (k-discount) grid =====


class TestPessimisticGrid:
    """§17.14 悲观基准网格（known_issues.md §3.21 21a）。"""

    def test_headers_and_rows(self):
        client = _make_grid_client()
        result = client._compute_pessimistic_grid("600887.SH")
        assert result is not None
        assert "17.14 悲观基准网格" in result
        assert "表A：悲观精算穿透回报率 GG_pess" in result
        assert "表B：悲观粗算穿透回报率 R_pess" in result
        for k in ("1.00", "0.95", "0.90", "0.85", "0.80", "0.75", "0.70"):
            assert f"| {k}" in result

    def test_uses_m_pess_not_m_rec(self):
        """The pessimistic leg must use M_pess (min of methods), per the methodology.

        AA=8710, M_pess=54.20%, Q=0, cap=175000 → GG(k=1) = 2.70%
        (had it used M_rec=58.08% it would be 2.89%).
        """
        client = _make_grid_client()
        pc = client._store["payout_crosscheck"]
        assert round(pc["m_pess"], 2) == 54.20
        result = client._compute_pessimistic_grid("600887.SH")
        assert "2.70%" in result
        assert "2.89%" not in result

    def test_default_k_is_marked(self):
        """k=0.80 has a documented worked example → marked ★."""
        client = _make_grid_client()
        result = client._compute_pessimistic_grid("600887.SH")
        assert "| 0.80 ★ |" in result
        assert result.count("| 0.80 ★ |") == 2      # one row per table (A and B)

    def test_k_scales_aa_linearly(self):
        client = _make_grid_client()
        result = client._compute_pessimistic_grid("600887.SH")
        assert "8,710.00" in result          # k=1.00
        assert "6,968.00" in result          # k=0.80 = 8710 × 0.8
        assert "6,097.00" in result          # k=0.70

    def test_target_price_comes_from_gg_pess(self):
        """target = close × GG_pess / II = 27.5 × 2.697/5.315 = 13.96."""
        client = _make_grid_client()
        result = client._compute_pessimistic_grid("600887.SH")
        assert "13.96" in result
        assert "11.17" in result             # k=0.80

    def test_matrix_thresholds_are_not_recalibrated(self):
        """The grid is input-side only — it must say so explicitly."""
        client = _make_grid_client()
        result = client._compute_pessimistic_grid("600887.SH")
        assert "仓位矩阵阈值不因折扣而调整" in result

    def test_none_without_factor3_sensitivity(self):
        client = _make_client()
        client._store = {"income": _load_mock("income.json"),
                         "basic_info": _load_mock("daily_basic.json")}
        assert client._compute_pessimistic_grid("600887.SH") is None

    def test_none_without_basic_info(self):
        client = _make_grid_client(with_basic=False)
        assert client._compute_pessimistic_grid("600887.SH") is None

    def test_none_without_payout_crosscheck(self):
        client = _make_grid_client()
        client._store.pop("payout_crosscheck", None)
        assert client._compute_pessimistic_grid("600887.SH") is None


# ===== §17.13 revenue sensitivity =====


class TestRevenueSensitivity:
    def test_lambda_echo(self):
        """λ = 0.0461 echoed (OCF-anchored AA series).

        λ = median over the latest 3 year-pairs of ΔAA/ΔRevenue:
          [0.04608, -0.04898, 0.07268] → 0.0461
        """
        client = _make_grid_client()
        result = client._compute_revenue_sensitivity("600887.SH")
        assert result is not None
        assert "17.13 收入敏感性" in result
        assert "0.0461" in result

    def test_scenario_rows(self):
        """AA_default = 8710M: GG(1.0×) = 2.89%; GG(0.9×) = 2.71%."""
        client = _make_grid_client()
        result = client._compute_revenue_sensitivity("600887.SH")
        assert "2.89%" in result  # 1.0×
        assert "2.71%" in result  # 0.9×
        for k in ["1.0×", "0.9×", "0.8×", "0.7×"]:
            assert k in result

    def test_critical_multiple(self):
        """k* solving GG(k)=II: with a 2.89% base vs 4.31% threshold, k* = 1.78."""
        client = _make_grid_client()
        result = client._compute_revenue_sensitivity("600887.SH")
        assert "临界收入倍数 k* = 2.32" in result

    def test_default_combo_note(self):
        client = _make_grid_client()
        result = client._compute_revenue_sensitivity("600887.SH")
        assert "默认组合" in result
        assert "比例缩放" in result

    def test_degraded_when_lambda_missing(self):
        client = _make_grid_client()
        client._store["factor3_sensitivity"]["lambda_median"] = None
        result = client._compute_revenue_sensitivity("600887.SH")
        assert result is not None
        assert "降级" in result
        assert result.count("—") >= 4  # dashed scenario rows

    def test_none_without_basic_info(self):
        client = _make_grid_client(with_basic=False)
        assert client._compute_revenue_sensitivity("600887.SH") is None


# ===== whole-pipeline integration =====


class TestPipelineGrids:
    def test_all_grid_sections_present(self):
        """compute_derived_metrics contains 17.10-17.13 with a full store."""
        client = _make_client()
        client._store = _base_store()
        client._store["basic_info"] = _load_mock("daily_basic.json")
        result = client.compute_derived_metrics("600887.SH")
        for tag in ["17.10", "17.11", "17.12", "17.13"]:
            assert tag in result

    def test_graceful_empty_store(self):
        """Empty store must not crash; the grids must have no headings."""
        client = _make_client()
        result = client.compute_derived_metrics("600887.SH")
        assert "17. 衍生指标" in result
        for tag in ["17.10", "17.11", "17.12", "17.13"]:
            assert f"### {tag}" not in result

    def test_lambda_fields_stored_by_17_5(self):
        """§17.5 must augment factor3_sensitivity with λ fields (for §17.13).

        λ is computed on the OCF-anchored AA series (AA = OCF − |Capex|).
        """
        client = _make_client()
        client._store = _base_store()
        client._compute_factor3_step1()
        client._compute_factor3_step4()
        client._compute_factor3_sensitivity_base()
        f3s = client._store["factor3_sensitivity"]
        assert "lambda_median" in f3s
        assert "lambda_reliability" in f3s
        assert f3s["aa_basis"] == "ocf_minus_capex"
        assert round(f3s["lambda_median"], 4) == 0.0461


# ===== §17.9 门槛价格：跨币种必须**单元格级** fail-closed =====
class TestSection179ThresholdPriceCurrency:
    """findings D-2：`tp = numerator/(ii×总股本)` 是**报表币种**，而表头写 `_price_unit()`。

    旧实现只把该列的 **claim** 置 null，**单元格照印** ⇒ 报告与审计读的是单元格，
    「已封锁」在纸面上成立、在产物里不成立（实测 06862：人民币数值印在「门槛价格（港元）」下，
    `vs当前股价` 拿它比港元价，6 行里 3 行符号翻转）。

    测法说明：跨币种判定 `_cross_ccy = |fx − 1| > 1e-9` **只看 fx**，与 ticker 无关；
    A 股代码的其余数据路径在 mock 里是完整的，所以用「A 股代码 + 强制 fx ≠ 1」
    能精确打到该逻辑，而不必补齐港股专有数据。
    """

    def _client(self, fx: float):
        client = _make_grid_client()
        client._fx_to_reporting = lambda ts_code: fx      # type: ignore[assignment]
        return client

    def _rows(self, out: str):
        return [ln for ln in out.splitlines()
                if ln.startswith("| 基准 ") or ln.startswith("| 下滑1年 ")]

    def test_cross_currency_blanks_cells_not_just_claims(self):
        out = self._client(0.85753)._compute_factor4_sensitivity("600887.SH")
        assert out is not None
        rows = self._rows(out)
        assert rows, "未找到表1数据行"
        for ln in rows:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            assert cells[4] == "—", f"门槛价格单元格应为 —，实为 {cells[4]!r}"
            assert cells[5] == "—", f"vs当前股价单元格应为 —，实为 {cells[5]!r}"

    def test_cross_currency_emits_the_reason_note(self):
        out = self._client(0.85753)._compute_factor4_sensitivity("600887.SH")
        assert "门槛价格列已整体置" in out and "不可引用" in out
        # 与实现不符的旧声明不得再无条件出现
        assert "两表数值可直接比较" not in out.split("门槛价格列已整体置")[0]

    def test_claims_stay_null_on_cross_currency(self):
        client = self._client(0.85753)
        client._compute_factor4_sensitivity("600887.SH")
        ledger = client._store["_claim_ledger"]
        tpps = [c for c in ledger.claims() if c.claim_id.endswith(".tpp")]
        assert tpps, "未登记任何 .tpp claim"
        assert all(c.value is None for c in tpps)

    def test_same_currency_keeps_the_numbers(self):
        """fx=1（A 股真实情形）不受影响：单元格照常给数，旧声明照旧。"""
        out = self._client(1.0)._compute_factor4_sensitivity("600887.SH")
        assert out is not None
        assert "门槛价格列已整体置" not in out
        assert "两表数值可直接比较" in out
        rows = self._rows(out)
        assert rows, "未找到表1基准行"
        cells = [c.strip() for c in rows[0].strip("|").split("|")]
        assert cells[4] not in ("", "—"), f"A 股门槛价格应有数值，实为 {cells[4]!r}"

    def test_near_one_fx_is_treated_as_same_currency(self):
        """阈值 1e-9：真·同币种（fx 恰为 1）不得被误判为跨币种。"""
        out = self._client(1.0)._compute_factor4_sensitivity("600887.SH")
        assert "门槛价格列已整体置" not in out


class TestNegativeAA:
    """AA<0 ⇒ 悲观档在定义域外（known_issues.md §二十九）。

    AA 是可支配现金结余，**可以为负**：资本开支超过经营现金流即真实现金缺口
    （实测 600011 华能国际 2024 AA = −13,122.98 百万元、601186 中国铁建 2025 = −44,643.57）。
    此时：
      · 「税后回报率」不是可比收益率（负的"收益率"是亏损）；
      · 「按 k 折扣取悲观」没有方向意义 —— 账目上 ¥1 缺口按 k=0.7 缩到 ¥0.7，
        看起来"改善了"，与"更悲观"相反。故 §17.14 **拒绝输出数值**。
    §17.11 相反：负 GG 有诊断价值（"按此价买入为负回报"），只禁止推出负的目标买入价。
    """

    def test_17_14_refuses_when_aa_selected_negative(self):
        client = _make_grid_client()
        client._store["factor3_sensitivity"]["aa_selected"] = -1_000_000.0
        assert client._compute_pessimistic_grid("600887.SH") is None
        entry = client._section_registry().finalize("600887.SH")["sections"]["17.14"]
        assert entry["status"] == "missing"
        assert entry["reason"] == "negative_basis"

    def test_17_11_renders_with_negative_basis_banner(self):
        client = _make_grid_client()
        for k in ("aa_2y", "aa_all", "aa_excl", "aa_latest"):
            client._store["factor3_sensitivity"][k] = -70_808.0
        out = client._compute_penetration_grid("600887.SH")
        assert out is not None, "§17.11 不应整节拒绝（负 GG 有诊断价值）"
        assert "本节基调为负" in out
        # 负 GG 不得推出负的目标买入价
        assert "-64.79" not in out

    def test_positive_aa_is_unaffected(self):
        client = _make_grid_client()
        assert client._compute_pessimistic_grid("600887.SH") is not None
        out = client._compute_penetration_grid("600887.SH")
        assert out is not None and "本节基调为负" not in out
