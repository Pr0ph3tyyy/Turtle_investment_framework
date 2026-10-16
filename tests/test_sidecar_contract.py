"""决策 sidecar 三道关（schema / 量级 / 来源）—— tests for scripts/sidecar_contract.py。

这 4 个 sidecar 是**决策变量的直接来源**，却由人/LLM 撰写；`pack_manifest` 只钉 md5
（证明"没被改过"），不证明"当初写对了"。下面每条用例都对应一起**真实发生过**的事故形态。
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sidecar_contract as sc  # noqa: E402


PACK_MIN = """## 1. 基本信息

| 项目 | 内容 |
| --- | ---: |
| 总市值 (百万港元) | 10,559.52 |

## 2. 市场行情

| 指标 | 数值 |
| --- | ---: |
| 最新价格 (HKD) | 3.79 |

## 3. 合并利润表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 每股基本盈利 (元) | 0.31 | 0.31 |

## 4. 合并资产负债表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 总资产 | 18,385.36 | 17,598.37 |
"""


def _dir(tmp_path, **files):
    (tmp_path / "data_pack_market.md").write_text(PACK_MIN, encoding="utf-8")
    for name, payload in files.items():
        (tmp_path / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(tmp_path)


def _fails(rep, gate=None):
    return [f for f in rep.fails if gate is None or f.gate == gate]


# ---------------------------------------------------------------------------
# 第 1 关 schema
# ---------------------------------------------------------------------------
class TestSchemaGate:
    def test_missing_required_key_fails(self, tmp_path):
        d = _dir(tmp_path, shares={"kind": "reported"})       # 缺 shares_mm
        rep = sc.validate_dir(d)
        assert _fails(rep, "schema")

    def test_bad_shares_kind_fails(self, tmp_path):
        d = _dir(tmp_path, shares={"shares_mm": 2786.15, "kind": "guessed"})
        rep = sc.validate_dir(d)
        assert any("kind" in f.detail for f in _fails(rep, "schema"))

    def test_overrides_unit_must_be_yuan(self, tmp_path):
        """真实事故：unit 写成「百万元」⇒ 渲染时再除 1e6 ⇒ 渲染成 0.00。"""
        d = _dir(tmp_path, balance_sheet_overrides={
            "unit": "百万元", "overrides": {"goodwill": {"2025": 1846.8}}})
        rep = sc.validate_dir(d)
        assert any("unit" in f.detail for f in _fails(rep, "schema"))

    def test_overrides_metadata_keys_are_not_treated_as_years(self, tmp_path):
        """`source/kind/reason` 是元数据键 —— 不得被当成年度值报"非数值"（校验器初版假阳性）。"""
        d = _dir(tmp_path, balance_sheet_overrides={
            "unit": "元",
            "overrides": {"goodwill": {"2025": 1846799000, "source": "年报附注17",
                                       "kind": "reported", "reason": "上游停更"}}})
        rep = sc.validate_dir(d)
        assert not _fails(rep, "schema")

    def test_unparsable_json_fails(self, tmp_path):
        (tmp_path / "data_pack_market.md").write_text(PACK_MIN, encoding="utf-8")
        (tmp_path / "shares.json").write_text("{not json", encoding="utf-8")
        rep = sc.validate_dir(str(tmp_path))
        assert any("JSON" in f.detail for f in _fails(rep, "schema"))


# ---------------------------------------------------------------------------
# 第 2 关 量级（本会话两起真实事故）
# ---------------------------------------------------------------------------
class TestMagnitudeGate:
    def test_absurd_share_count_fails_against_period_end_implied(self, tmp_path):
        """真实事故：反推股本 3,279.35 被当真值（真值 2,797.22）⇒ 循环喂进当日市值。"""
        d = _dir(tmp_path, shares={"shares_mm": 32793.5, "kind": "reported",
                                   "source": "x", "date": "2025-12-31"})
        rep = sc.validate_dir(d)
        assert any("偏离" in f.detail for f in _fails(rep, "magnitude"))

    def test_consistent_share_count_passes(self, tmp_path):
        # 10,559.52 / 3.79 = 2,786.15
        d = _dir(tmp_path, shares={"shares_mm": 2786.15, "kind": "reported",
                                   "source": "年报", "date": "2025-12-31"})
        rep = sc.validate_dir(d)
        assert not _fails(rep, "magnitude")

    def test_fx_out_of_range_fails(self, tmp_path):
        """单位/方向写反的典型形态（0.86476 写成 86.476）。"""
        d = _dir(tmp_path, fx={"factor_to_reporting": 86.476,
                               "reporting_currency": "CNY", "trading_currency": "HKD",
                               "source": "x", "date": "2025-12-31"})
        rep = sc.validate_dir(d)
        assert any("0.5" in f.detail or "方向" in f.detail for f in _fails(rep, "magnitude"))

    def test_plausible_fx_passes(self, tmp_path):
        d = _dir(tmp_path, fx={"factor_to_reporting": 0.86476,
                               "reporting_currency": "CNY", "trading_currency": "HKD",
                               "source": "x", "date": "2025-12-31"})
        rep = sc.validate_dir(d)
        assert not _fails(rep, "magnitude")

    def test_dps_far_above_eps_fails(self, tmp_path):
        """DPS 当"分"用（0.154 写成 15.4）⇒ 派息率 4967%。"""
        d = _dir(tmp_path, dividends_declared={
            "declared_dps": {"2025": {"value": 15.4, "currency": "CNY",
                                      "source": "x", "date": "2025-12-31"}}})
        rep = sc.validate_dir(d)
        assert any("EPS" in f.detail for f in _fails(rep, "magnitude"))

    def test_plausible_dps_passes(self, tmp_path):
        d = _dir(tmp_path, dividends_declared={
            "declared_dps": {"2025": {"value": 0.154, "currency": "CNY",
                                      "source": "年报", "date": "2025-12-31"}}})
        rep = sc.validate_dir(d)
        assert not _fails(rep, "magnitude")

    def test_override_larger_than_half_of_total_assets_fails(self, tmp_path):
        d = _dir(tmp_path, balance_sheet_overrides={
            "unit": "元",
            "overrides": {"goodwill": {"2025": 99_000_000_000, "source": "x"}}})
        rep = sc.validate_dir(d)
        assert any("总资产" in f.detail for f in _fails(rep, "magnitude"))


# ---------------------------------------------------------------------------
# 第 3 关 来源
# ---------------------------------------------------------------------------
class TestProvenanceGate:
    def test_missing_source_and_date_warns(self, tmp_path):
        d = _dir(tmp_path, shares={"shares_mm": 2786.15, "kind": "reported"})
        rep = sc.validate_dir(d)
        gates = {f.gate for f in rep.warns}
        assert "provenance" in gates
        assert rep.ok, "来源缺失是披露级，不阻断"

    def test_nested_source_satisfies_provenance(self, tmp_path):
        """`overrides`/`declared_dps` 把 source 写在条目内是合法记录方式（不强制顶层重复）。"""
        d = _dir(tmp_path, balance_sheet_overrides={
            "unit": "元",
            "overrides": {"goodwill": {"2025": 1846799000,
                                       "source": "年报附注17 p.161", "date": "2025-12-31"}},
        })
        rep = sc.validate_dir(d)
        assert not [f for f in rep.warns if f.gate == "provenance"]

    def test_full_provenance_is_clean(self, tmp_path):
        d = _dir(tmp_path,
                 shares={"shares_mm": 2786.15, "kind": "reported",
                         "source": "年报", "date": "2025-12-31"},
                 fx={"factor_to_reporting": 0.86476, "reporting_currency": "CNY",
                     "trading_currency": "HKD", "source": "CFETS", "date": "2025-12-31"})
        rep = sc.validate_dir(d)
        assert rep.ok and not [f for f in rep.warns if f.gate == "provenance"]


class TestFailClosed:
    def test_ok_is_false_when_any_fail(self, tmp_path):
        d = _dir(tmp_path, fx={"factor_to_reporting": 86.476,
                               "reporting_currency": "CNY", "trading_currency": "HKD"})
        assert sc.validate_dir(d).ok is False

    def test_absent_files_are_warn_not_fail(self, tmp_path):
        """整目录没有 sidecar 时，降级是采集器的职责（且带标记）——本校验器只提醒。"""
        (tmp_path / "data_pack_market.md").write_text(PACK_MIN, encoding="utf-8")
        rep = sc.validate_dir(str(tmp_path))
        assert rep.ok and len(rep.warns) >= 3

    def test_cli_exit_codes(self, tmp_path):
        d = _dir(tmp_path, fx={"factor_to_reporting": 86.476,
                               "reporting_currency": "CNY", "trading_currency": "HKD"})
        assert sc.main(["--dir", d, "--quiet"]) == 1
        assert sc.main(["--dir", str(tmp_path / "nope"), "--quiet"]) == 2
