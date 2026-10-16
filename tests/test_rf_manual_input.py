"""Regression tests for risk-free-rate plumbing (方案 3-A / 3-B / 3-C).

Defect being locked down
------------------------
§17.9 / §17.11 / §17.13 (业绩下滑敏感性、穿透回报率网格、收入敏感性) all need a
risk-free rate. The rate only reached ``_store["risk_free_rate"]`` when the
``yc_cb`` endpoint returned data, so:

- on an account without that permission the three blocks **silently vanished**
  (no error, no warning) — confirmed on 5 of 5 recent real data packs;
- a §14 value supplied via WebSearch could never be used, because nothing read
  it back from the data pack;
- ``--refresh-market`` overwrote the manual §14 with a "数据缺失" string, since
  a failed fetch returns text instead of raising.

These tests cover the parsing helpers, the refresh-market protection, the
``--rf`` / ``TUSHARE_RF`` / read-back input channels, and the end-to-end effect
on the generated data pack.
"""

import argparse
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from rf_utils import (  # noqa: E402
    extract_section_14,
    make_rf_dataframe,
    parse_rf_from_data_pack,
    parse_rf_from_section,
    rf_text_is_failure,
)
from tushare_collector import TushareClient, _resolve_rf, _seed_risk_free_rate  # noqa: E402
from section_contract import (  # noqa: E402
    check_pack_consistency,
    headings_in_pack,
)

MOCK_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "mock_tushare_responses")

# Real §14 text from output/002984_森麒麟 (coordinator WebSearch fallback)
MANUAL_RF_SECTION = (
    "## 14. 无风险利率\n\n"
    "*[协调器补充 2026-08-25] yc_cb 接口无权限，经 WebSearch 补充："
    "中国十年期国债收益率 2026-08-22 收盘 **1.688%**（活跃券 260010，"
    "东吴证券固收周报；中债估值 1.6839%）*\n\n"
)
FAILURE_RF_SECTION = "## 14. 无风险利率\n\n数据缺失 (接口可能无权限)\n\n"
API_RF_SECTION = (
    "## 14. 无风险利率\n\n"
    "| 日期 | 10年期国债收益率 (%) |\n| --- | ---: |\n| 20260509 | 1.7670 |\n\n"
    "*数据来源: 中债国债收益率曲线 (yc_cb)*\n\n"
)


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
    client._cache_dir = os.path.join(MOCK_DIR, "_should_not_be_used")
    return client


def _make_grid_client(with_rf: bool) -> TushareClient:
    """Full store + factor3 chain + payout crosscheck, with or without Rf."""
    client = _make_client()
    store = {
        "income": _load_mock("income.json").sort_values("end_date", ascending=False),
        "balance_sheet": _load_mock("balancesheet.json").sort_values("end_date", ascending=False),
        "cashflow": _load_mock("cashflow.json").sort_values("end_date", ascending=False),
        "dividends": _load_mock("dividend.json").sort_values("end_date", ascending=False),
        "basic_info": _load_mock("daily_basic.json"),
    }
    if with_rf:
        store["risk_free_rate"] = _load_mock("yc_cb.json").sort_values(
            "trade_date", ascending=False)
    client._store = store
    client._compute_factor3_step1()
    client._compute_factor3_step4()
    client._compute_factor3_sensitivity_base()
    client._compute_payout_crosscheck()
    return client


def _refresh_client(fresh_rf_text: str) -> TushareClient:
    client = _make_client()
    client._detect_currency = MagicMock(return_value="CNY")
    client._is_hk = MagicMock(return_value=False)
    client._is_us = MagicMock(return_value=False)
    client.get_basic_info = MagicMock(
        return_value="## 1. 基本信息\n\n| 项目 | 内容 |\n|:--|:--|\n| 市值 | 200,000.00 |\n")
    client.get_market_data = MagicMock(
        return_value="## 2. 市场行情\n\n| 指标 | 数值 |\n|:--|:--|\n| 最新收盘价 | 30.00 |\n")
    client.get_weekly_prices = MagicMock(
        return_value="## 11. 十年周线行情\n\n| 年份 | 最高 |\n|:--|--:|\n| 2024 | 35.00 |\n")
    client.get_risk_free_rate = MagicMock(return_value=fresh_rf_text)
    return client


# ===== parsing =====


class TestRfParsing:
    def test_parse_api_table_row(self):
        assert parse_rf_from_section(API_RF_SECTION) == (1.767, "20260509")

    def test_parse_manual_websearch_supplement(self):
        value, date_str = parse_rf_from_section(MANUAL_RF_SECTION)
        assert value == 1.688          # primary figure, not the 1.6839 parenthetical
        assert date_str == "20260822"

    def test_parse_failure_text_returns_none(self):
        assert parse_rf_from_section(FAILURE_RF_SECTION) == (None, None)

    def test_failure_detection(self):
        assert rf_text_is_failure(FAILURE_RF_SECTION)
        assert not rf_text_is_failure(MANUAL_RF_SECTION)
        assert not rf_text_is_failure(API_RF_SECTION)

    def test_section_extraction_stops_at_next_section(self):
        doc = MANUAL_RF_SECTION + "## 15. 股票回购\n\n| a |\n|:--|\n"
        section = extract_section_14(doc)
        assert "1.688" in section
        assert "股票回购" not in section

    def test_parse_from_full_data_pack(self):
        doc = "# 数据包\n\n## 13. 风险\n\n无\n\n" + MANUAL_RF_SECTION + "## 15. 回购\n\n无\n"
        value, _ = parse_rf_from_data_pack(doc)
        assert value == 1.688

    def test_make_rf_dataframe_schema(self):
        df = make_rf_dataframe(1.688, "20260822")
        assert list(df.columns) == ["trade_date", "yield"]
        assert df.iloc[0]["yield"] == 1.688
        assert df.iloc[0]["trade_date"] == "20260822"


# ===== 3-A: --refresh-market must not destroy a manual §14 =====


class TestRefreshMarketRfProtection:
    def test_manual_rf_survives_failed_refresh(self):
        """A failed fetch returns text (no exception) — the old value must stay."""
        client = _refresh_client(FAILURE_RF_SECTION)
        existing = "# 数据包\n\n" + MANUAL_RF_SECTION + "## 15. 股票回购\n\n| a |\n|:--|\n"
        result = client.refresh_market_sections("600887.SH", existing)
        assert "1.688" in result
        assert "保留数据包中已有数值" in result or "1.688" in result

    def test_live_rf_still_wins_over_manual(self):
        """When the API works, fresh data replaces the manual fallback."""
        client = _refresh_client(API_RF_SECTION)
        existing = "# 数据包\n\n" + MANUAL_RF_SECTION + "## 15. 股票回购\n\n| a |\n|:--|\n"
        result = client.refresh_market_sections("600887.SH", existing)
        assert "1.767" in result
        assert "1.688" not in result


# ===== 3-B / 3-C: input channels =====


class TestRfInputChannels:
    def test_explicit_rf_argument(self):
        args = argparse.Namespace(rf=1.688, output="/nonexistent/pack.md")
        assert _resolve_rf(args) == (1.688, "--rf")

    def test_out_of_range_rf_exits(self):
        args = argparse.Namespace(rf=99.0, output="/nonexistent/pack.md")
        with pytest.raises(SystemExit):
            _resolve_rf(args)

    def test_env_var_fallback(self, monkeypatch):
        monkeypatch.setenv("TUSHARE_RF", "2.05")
        args = argparse.Namespace(rf=None, output="/nonexistent/pack.md")
        assert _resolve_rf(args) == (2.05, "TUSHARE_RF env")

    def test_read_back_from_existing_pack(self, tmp_path, monkeypatch):
        """3-B: a manual §14 already in the pack is reused automatically."""
        monkeypatch.delenv("TUSHARE_RF", raising=False)
        pack = tmp_path / "data_pack_market.md"
        pack.write_text("# 数据包\n\n" + MANUAL_RF_SECTION, encoding="utf-8")
        args = argparse.Namespace(rf=None, output=str(pack))
        value, source = _resolve_rf(args)
        assert value == 1.688
        assert "§14" in source

    def test_no_rf_anywhere(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TUSHARE_RF", raising=False)
        args = argparse.Namespace(rf=None, output=str(tmp_path / "missing.md"))
        assert _resolve_rf(args) == (None, None)

    def test_seed_populates_store(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TUSHARE_RF", raising=False)
        client = _make_client()
        args = argparse.Namespace(rf=1.688, output=str(tmp_path / "x.md"))
        _seed_risk_free_rate(client, args)
        assert client._store["risk_free_rate"].iloc[0]["yield"] == 1.688


# ===== end-to-end effect on §17 =====


class TestDerivedBlocksFollowRf:
    def test_seeded_rf_produces_penetration_grid(self):
        """With a rate (e.g. from --rf), the grid is generated."""
        client = _make_grid_client(with_rf=True)
        result = client.compute_derived_metrics("600887.SH")
        assert "17.11 穿透回报率网格" in result

    def test_missing_rf_is_reported_not_silent(self):
        """Without a rate the block is absent BUT explicitly reported, with a remedy.

        known_issues.md P2-20: absence must carry a **machine-readable reason code**
        so the consumer stops instead of falling back to hand-arithmetic.
        """
        client = _make_grid_client(with_rf=False)
        result = client.compute_derived_metrics("600887.SH")
        assert "### 17.11" not in result          # block genuinely absent
        payload = client._store["section17_status"]
        entry = payload["sections"]["17.11"]
        assert entry["status"] == "missing"
        assert entry["reason"] == "no_rf"         # controlled vocabulary, not prose
        assert "无风险利率" in entry["reason_label"]
        assert "--rf" in entry["remedy"]          # must point at the fix
        assert "穿透回报率网格" in result
        assert "❌ missing" in result

    def test_seeding_unblocks_the_grid(self):
        """The exact 3-B flow: no API rate -> seed -> grid appears.

        Also guards the registry reset: a stale `missing` must not survive a
        successful recompute on the same client.
        """
        client = _make_grid_client(with_rf=False)
        first = client.compute_derived_metrics("600887.SH")
        assert "### 17.11" not in first
        assert client._store["section17_status"]["sections"]["17.11"]["status"] == "missing"

        client._store["risk_free_rate"] = make_rf_dataframe(1.688, "20260822")
        result = client.compute_derived_metrics("600887.SH")
        assert "### 17.11 穿透回报率网格" in result
        assert client._store["section17_status"]["sections"]["17.11"]["status"] == "ok"
        assert check_pack_consistency(result, client._store["section17_status"]) == []

    def test_heading_iff_ok_invariant(self):
        """New contract: `### 17.N` exists **iff** section N has status ok.

        Supersedes the old rule ("the missing notice must contain no section
        numbers"). That rule existed because absence used to be inferred from a
        string NOT appearing; now absence is explicitly enumerated, and the
        status table references sections as `§17.N` so it cannot be mistaken for
        a heading. Checked mechanically by
        `section_contract.check_pack_consistency`.
        """
        client = _make_grid_client(with_rf=False)
        result = client.compute_derived_metrics("600887.SH")
        payload = client._store["section17_status"]
        assert check_pack_consistency(result, payload) == []

        heads = headings_in_pack(result)
        for tag in ("17.9", "17.11", "17.13"):   # RF-dependent -> absent
            assert tag not in heads
        assert "§17.11" in result                # ...but referenced in the status table
        assert "### 17.11" not in result
