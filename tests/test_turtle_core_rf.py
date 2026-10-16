"""turtle_core.rf —— Rf 解析与 rf.json 契约测试（含 fail-closed 面）。"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from turtle_core import rf as RF  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("TUSHARE_RF", raising=False)


class TestPlausibility:
    @pytest.mark.parametrize("v,ok", [
        (1.67, True), (1.0242, True), (0.01, True), (14.99, True),
        (0.0, False), (-1.0, False), (15.0, False), (16.0, False), (None, False),
    ])
    def test_range(self, v, ok):
        assert RF.is_plausible(v) is ok

    def test_rejects_percent_as_fraction(self):
        """把 1.67% 写成 0.0167 是典型单位错 —— 0.0167 会被当 1.67e-2% 接受吗？"""
        # 0.0167 在 (0,15) 内，故**技术上合理**；模块不假装能识别这种歧义，
        # 但区间下界能挡住 0 与负数。这里记录该已知局限。
        assert RF.is_plausible(0.0167) is True


class TestResolutionOrder:
    def test_cli_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TUSHARE_RF", "9.9")
        RF.write_rf_json(tmp_path, 2.5)
        v, src, why = RF.resolve_rf(cli_value=1.67, output_dir=tmp_path)
        assert v == pytest.approx(1.67) and src == "--rf" and why is None

    def test_env_beats_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TUSHARE_RF", "9.9")
        RF.write_rf_json(tmp_path, 2.5)
        v, src, why = RF.resolve_rf(output_dir=tmp_path)
        assert v == pytest.approx(9.9) and src == "TUSHARE_RF" and why is None

    def test_json_used_when_no_cli_or_env(self, tmp_path):
        RF.write_rf_json(tmp_path, 2.5, date="2026-10-14", source="中债")
        v, src, why = RF.resolve_rf(output_dir=tmp_path)
        assert v == pytest.approx(2.5) and why is None
        assert "rf.json" in src and "中债" in src and "2026-10-14" in src

    def test_data_pack_is_last_resort(self, tmp_path):
        pack = tmp_path / "data_pack_market.md"
        pack.write_text("| 无风险利率（10年期国债） | 1.7670% |\n", encoding="utf-8")
        v, src, why = RF.resolve_rf(data_pack_path=pack)
        assert v == pytest.approx(1.7670) and src == "数据包 §14" and why is None

    def test_json_beats_data_pack(self, tmp_path):
        pack = tmp_path / "data_pack_market.md"
        pack.write_text("| 无风险利率 | 1.7670% |\n", encoding="utf-8")
        RF.write_rf_json(tmp_path, 2.5)
        v, _src, why = RF.resolve_rf(output_dir=tmp_path, data_pack_path=pack)
        assert v == pytest.approx(2.5) and why is None


class TestFailClosed:
    def test_all_sources_missing_gives_rf_missing(self, tmp_path):
        """唯一失败形态：返回 rf_missing，调用方必须 ABORT（绝不用 0 兜底）。"""
        v, src, why = RF.resolve_rf(output_dir=tmp_path)
        assert v is None and src is None and why == "rf_missing"

    def test_rf_missing_is_in_the_controlled_vocabulary(self):
        from turtle_core import reasons
        assert reasons.require("rf_missing") == "rf_missing"
        assert reasons.is_global("rf_missing") is True

    def test_out_of_range_cli_is_rejected_not_used(self, tmp_path):
        """超界几乎必然单位错 —— 宁可 ABORT 也不用一个错的 II。"""
        v, _src, why = RF.resolve_rf(cli_value=167.0, output_dir=tmp_path)
        assert v is None and why == "rf_missing"

    def test_zero_is_rejected(self, tmp_path):
        v, _src, why = RF.resolve_rf(cli_value=0.0, output_dir=tmp_path)
        assert v is None and why == "rf_missing"

    def test_corrupt_json_falls_through(self, tmp_path):
        (tmp_path / "rf.json").write_text("{ not json", encoding="utf-8")
        v, _src, why = RF.resolve_rf(output_dir=tmp_path)
        assert v is None and why == "rf_missing"

    def test_json_with_out_of_range_value_falls_through(self, tmp_path):
        (tmp_path / "rf.json").write_text(json.dumps({"rf_pct": 99.0}), encoding="utf-8")
        v, _src, why = RF.resolve_rf(output_dir=tmp_path)
        assert v is None and why == "rf_missing"


class TestRfJsonContract:
    def test_write_then_load_round_trip(self, tmp_path):
        RF.write_rf_json(tmp_path, 1.0242, date="2026-10-14", source="CFETS")
        rf, date, source = RF.load_rf_json(tmp_path)
        assert rf == pytest.approx(1.0242)
        assert date == "2026-10-14" and source == "CFETS"

    def test_alias_rf_key_accepted(self, tmp_path):
        (tmp_path / "rf.json").write_text(json.dumps({"rf": 1.5}), encoding="utf-8")
        rf, _d, _s = RF.load_rf_json(tmp_path)
        assert rf == pytest.approx(1.5)

    def test_percent_suffix_tolerated(self, tmp_path):
        (tmp_path / "rf.json").write_text(json.dumps({"rf_pct": "1.67%"}), encoding="utf-8")
        rf, _d, _s = RF.load_rf_json(tmp_path)
        assert rf == pytest.approx(1.67)

    def test_write_is_atomic(self, tmp_path):
        RF.write_rf_json(tmp_path, 1.67)
        assert list(tmp_path.glob("*.tmp")) == []

    def test_make_entry_validates(self):
        with pytest.raises(ValueError):
            RF.make_rf_json_entry(167.0)

    def test_accepts_dir_or_file_path(self, tmp_path):
        p = RF.write_rf_json(tmp_path, 1.67)
        rf_a, _, _ = RF.load_rf_json(tmp_path)
        rf_b, _, _ = RF.load_rf_json(p)
        assert rf_a == rf_b == pytest.approx(1.67)
