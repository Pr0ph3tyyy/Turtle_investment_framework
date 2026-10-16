"""Tests for HK trading→reporting currency plumbing (scripts/fx_utils.py).

Covers known_issues.md P0-2: HK listings trade in HKD but most mainland issuers
report in CNY, so an unconverted `R = 净利润(CNY) / 市值(HKD)` understates the
return by ~15.65%.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from fx_utils import (  # noqa: E402
    is_plausible,
    load_fx_json,
    make_fx_store_entry,
    parse_fx_from_data_pack,
    resolve_fx_hkd_cny,
)


class TestPlausible:
    def test_accepts_real_rates(self):
        assert is_plausible(0.86466)
        assert is_plausible(1.0)

    def test_rejects_out_of_band(self):
        for bad in (None, 0.0, 0.4, 1.6, -0.9):
            assert not is_plausible(bad)


class TestParseFromDataPack:
    def test_parses_coordinator_note(self):
        """Rate + date from the `*[协调器注 …]*` line written into 2026-08 packs."""
        text = (
            "④ 汇率 HKD/CNY = 0.86476（2026-08-20 央行中间价；正文定性报告用 "
            "1 HKD = 0.8594 元的 Web 市场价，两价并存均注明来源）"
        )
        rate, date = parse_fx_from_data_pack(text)
        assert rate == 0.86476
        assert date == "2026-08-20"

    def test_returns_none_without_note(self):
        assert parse_fx_from_data_pack("no rate here") == (None, None)
        assert parse_fx_from_data_pack("") == (None, None)

    def test_ignores_implausible_value(self):
        rate, _ = parse_fx_from_data_pack("汇率 HKD/CNY = 7.8")
        assert rate is None


class TestLoadFxJson:
    def test_prefers_factor_to_reporting(self, tmp_path):
        (tmp_path / "fx.json").write_text(json.dumps({
            "factor_to_reporting": 0.86466,
            "date": "2026-08-21",
            "source": "CFETS中间价",
            "reporting_currency": "CNY",
        }), encoding="utf-8")
        factor, date, source, reporting = load_fx_json(tmp_path)
        assert factor == 0.86466
        assert date == "2026-08-21"
        assert source == "CFETS中间价"
        assert reporting == "CNY"          # must not be dropped (live regression)

    def test_accepts_hkd_cny_alias(self, tmp_path):
        (tmp_path / "fx.json").write_text(json.dumps({"hkd_cny": 0.9}), encoding="utf-8")
        factor, _, _, _ = load_fx_json(tmp_path)
        assert factor == 0.9

    def test_hkd_reporter_needs_no_conversion(self, tmp_path):
        """A genuine HKD-reporting issuer (e.g. 00001.HK) → identity."""
        (tmp_path / "fx.json").write_text(json.dumps({
            "factor_to_reporting": 0.86466, "reporting_currency": "HKD",
        }), encoding="utf-8")
        factor, _, source, reporting = load_fx_json(tmp_path)
        assert factor == 1.0
        assert "无需换算" in source
        assert reporting == "HKD"

    def test_missing_or_broken_file(self, tmp_path):
        assert load_fx_json(tmp_path) == (None, None, None, None)
        (tmp_path / "fx.json").write_text("{not json", encoding="utf-8")
        assert load_fx_json(tmp_path) == (None, None, None, None)
        (tmp_path / "fx.json").write_text(json.dumps({"factor_to_reporting": 9.9}),
                                          encoding="utf-8")
        assert load_fx_json(tmp_path) == (None, None, None, None)


class TestResolutionOrder:
    def test_cli_wins(self, tmp_path):
        (tmp_path / "fx.json").write_text(json.dumps({"factor_to_reporting": 0.5}),
                                          encoding="utf-8")
        factor, source, _ccy = resolve_fx_hkd_cny(0.86466, tmp_path / "data_pack_market.md")
        assert factor == 0.86466
        assert source == "--fx-hkd-cny"

    def test_env_beats_files(self, tmp_path, monkeypatch):
        (tmp_path / "fx.json").write_text(json.dumps({"factor_to_reporting": 0.5}),
                                          encoding="utf-8")
        monkeypatch.setenv("TUSHARE_FX_HKD_CNY", "0.92")
        factor, source, _ccy = resolve_fx_hkd_cny(None, tmp_path / "data_pack_market.md")
        assert factor == 0.92
        assert "env" in source

    def test_fx_json_beats_pack_note(self, tmp_path):
        (tmp_path / "fx.json").write_text(json.dumps({"factor_to_reporting": 0.9}),
                                          encoding="utf-8")
        pack = tmp_path / "data_pack_market.md"
        pack.write_text("④ 汇率 HKD/CNY = 0.86476（2026-08-20 央行中间价）", encoding="utf-8")
        factor, source, _ccy = resolve_fx_hkd_cny(None, pack)
        assert factor == 0.9
        assert "fx.json" in source

    def test_falls_back_to_pack_note(self, tmp_path):
        pack = tmp_path / "data_pack_market.md"
        pack.write_text("④ 汇率 HKD/CNY = 0.86476（2026-08-20 央行中间价）", encoding="utf-8")
        factor, source, _ccy = resolve_fx_hkd_cny(None, pack)
        assert factor == 0.86476
        assert "协调器注" in source

    def test_nothing_available(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TUSHARE_FX_HKD_CNY", raising=False)
        assert resolve_fx_hkd_cny(None, tmp_path / "nope.md") == (None, None, None)

    def test_cli_out_of_band_exits(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            resolve_fx_hkd_cny(9.9, tmp_path / "x.md")


class TestStoreEntry:
    def test_shape(self):
        entry = make_fx_store_entry(0.86466, "2026-08-21", "CFETS中间价")
        assert entry["factor_to_reporting"] == 0.86466
        assert entry["date"] == "2026-08-21"
        assert entry["source"] == "CFETS中间价"
        assert "reporting_currency" not in entry      # unknown stays unknown

    def test_carries_reporting_currency_when_known(self):
        """Dropping it silently reverts the pack label to the ticker guess."""
        entry = make_fx_store_entry(0.86466, reporting_currency="CNY")
        assert entry["reporting_currency"] == "CNY"


def test_cli_rate_keeps_reporting_currency_from_fx_json(tmp_path, monkeypatch):
    """CLI 显式给汇率时，仍须保留 fx.json 的**报表币种声明**。

    否则 `_unit_label()` 退回「币种未确认」——整份数据包的单位标签失去意义
    （实测 2026-10-13：`--fx-hkd-cny` + fx.json 已声明 CNY，产物却写「币种未确认」）。
    """
    import fx_utils

    pack = tmp_path / "data_pack_market.md"
    pack.write_text("x", encoding="utf-8")
    monkeypatch.setattr(fx_utils, "load_fx_json",
                        lambda d: (0.85849, "2026-10-08", "CFETS中间价", "CNY"))
    factor, source, reporting = fx_utils.resolve_fx_hkd_cny(0.85849, str(pack))
    assert factor == 0.85849
    assert reporting == "CNY", "报表币种声明不得因走 CLI 路径而丢失"
