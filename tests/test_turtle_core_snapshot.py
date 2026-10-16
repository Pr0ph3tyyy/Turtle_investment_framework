"""turtle_core.reasons / turtle_core.snapshot —— 受控原因码与慢层快照测试。"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from turtle_core import reasons as R  # noqa: E402
from turtle_core import snapshot as S  # noqa: E402


class TestReasonVocabulary:
    def test_require_known(self):
        assert R.require("no_cashflow") == "no_cashflow"

    def test_require_unknown_raises(self):
        """未知码是硬错误 —— 否则词表退化成注释，排除就不可诊断。"""
        with pytest.raises(KeyError):
            R.require("nope_not_a_code")

    def test_every_code_has_label_and_remedy(self):
        for code, meta in R.SCREEN_REASONS.items():
            assert meta.get("label"), f"{code} 缺 label"
            assert meta.get("remedy"), f"{code} 缺 remedy"

    def test_global_codes(self):
        assert R.is_global("rf_missing") is True
        assert R.is_global("calendar_missing") is True
        assert R.is_global("market_data_not_ready") is True
        assert R.is_global("no_cashflow") is False

    def test_is_global_rejects_unknown(self):
        with pytest.raises(KeyError):
            R.is_global("bogus")

    def test_describe_sorted_by_count(self):
        txt = R.describe({"illiquid": 3, "st": 10})
        assert txt.startswith("st×10")
        assert "illiquid×3" in txt

    def test_describe_empty(self):
        assert R.describe({}) == "（无排除）"

    def test_covers_the_designs_required_codes(self):
        """设计 §C3 点名的必备码必须都在（防止后续被删）。"""
        for code in ("st", "illiquid", "no_annual_3y", "no_payout", "no_payout",
                     "hk_field_gap", "rf_missing", "fx_missing", "stale_snapshot"):
            assert code in R.SCREEN_REASONS


class TestSnapshotPersistence:
    def _snap(self, **kw):
        base = dict(c_yuan=2_341_560_000.0, q_default=20.0, q_scenarios=[28.0, 20.0],
                    total_shares=2_931_000_000.0, shares_kind="reported")
        base.update(kw)
        return S.FinancialSnapshot.new("06862.HK", "20251231", **base)

    def test_round_trip(self, tmp_path):
        snap = self._snap(reasons={"aa": "no_cashflow"},
                          payout={"m_rec": 46.68, "m_pess": 40.0})
        path = snap.save(tmp_path)
        assert path.exists()
        back = S.FinancialSnapshot.load("06862.HK", tmp_path)
        assert back is not None
        assert back.c_yuan == pytest.approx(2_341_560_000.0)
        assert back.reasons == {"aa": "no_cashflow"}
        assert back.payout["m_rec"] == pytest.approx(46.68)
        assert back.schema_version == S.SNAPSHOT_SCHEMA_VERSION
        assert back.built_at

    def test_atomic_write_leaves_no_tmp(self, tmp_path):
        """半写文件会被下游读到 —— 仓库已有真实事故记录。"""
        self._snap().save(tmp_path)
        leftovers = list(S.snapshot_dir(tmp_path).glob("*.tmp"))
        assert leftovers == []

    def test_load_missing_returns_none(self, tmp_path):
        assert S.FinancialSnapshot.load("999999.SH", tmp_path) is None

    def test_load_corrupt_returns_none_not_raise(self, tmp_path):
        p = S.snapshot_path("06862.HK", tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ this is not json", encoding="utf-8")
        assert S.FinancialSnapshot.load("06862.HK", tmp_path) is None

    def test_unknown_reason_code_is_rejected_on_save(self, tmp_path):
        snap = self._snap(reasons={"aa": "made_up_code"})
        assert snap.validate()           # 非空
        with pytest.raises(ValueError):
            snap.save(tmp_path)

    def test_validate_reports_scope(self):
        snap = self._snap(reasons={"aa": "made_up_code"})
        msg = "；".join(snap.validate())
        assert "aa" in msg and "made_up_code" in msg

    def test_snapshot_path_is_per_stock(self, tmp_path):
        a = S.snapshot_path("06862.HK", tmp_path)
        b = S.snapshot_path("600941.SH", tmp_path)
        assert a != b
        assert a.parent == b.parent

    def test_dir_helper_accepts_bare_or_snapshots_dir(self, tmp_path):
        assert S.snapshot_dir(tmp_path).name == "snapshots"
        assert S.snapshot_dir(S.snapshot_dir(tmp_path)).name == "snapshots"


class TestStaleness:
    def _snap(self, fy_end):
        return S.FinancialSnapshot.new("06862.HK", fy_end)

    def test_newer_period_is_stale(self):
        """新财报期一出，旧快照必须判过期 —— 用旧盈利排名比不排名更危险。"""
        assert self._snap("20241231").is_stale("20251231") is True

    def test_same_period_not_stale(self):
        assert self._snap("20251231").is_stale("20251231") is False

    def test_older_period_not_stale(self):
        assert self._snap("20251231").is_stale("20250630") is False

    def test_unknown_current_period_is_not_stale(self):
        """'无法判定'不等于'过期'；此时应由调用方登记 no_annual_3y。"""
        assert self._snap("20251231").is_stale(None) is False
        assert self._snap("20251231").is_stale("") is False

    def test_unknown_snapshot_period_is_not_stale(self):
        assert self._snap("").is_stale("20251231") is False


class TestRequiredScopes:
    @pytest.mark.parametrize("code", [
        "no_cashflow", "no_payout", "no_market_cap", "fx_missing", "no_rf",
    ])
    def test_blocking_scopes(self, code):
        snap = S.FinancialSnapshot.new("06862.HK", "20251231", reasons={"x": code})
        assert snap.required_scopes_ok is False

    def test_non_blocking_reason_is_ok(self):
        snap = S.FinancialSnapshot.new("06862.HK", "20251231",
                                       reasons={"floor": "hk_field_gap"})
        assert snap.required_scopes_ok is True

    def test_clean_snapshot_is_ok(self):
        assert S.FinancialSnapshot.new("06862.HK", "20251231").required_scopes_ok is True


class TestSnapshotDefaults:
    def test_o_defaults_to_zero_but_is_explicit(self):
        """canonical 把 O 硬编码为 0（findings D-3）；快照里它是**显式字段**，
        从而让"接通 §15 的年均回购"成为调用方的选择而不是被掩盖的缺口。"""
        snap = S.FinancialSnapshot.new("06862.HK", "20251231")
        assert snap.o_yuan == 0.0
        d = snap.to_dict()
        assert "o_yuan" in d

    def test_to_dict_is_json_serializable(self):
        snap = S.FinancialSnapshot.new("06862.HK", "20251231",
                                       floor={"nla": 1.11, "bvps": 1.80})
        json.dumps(snap.to_dict(), ensure_ascii=False)

    def test_from_dict_ignores_unknown_keys(self):
        snap = S.FinancialSnapshot.from_dict(
            {"ts_code": "X.HK", "future_field": 1, "fy_end": "20251231"})
        assert snap.ts_code == "X.HK"
