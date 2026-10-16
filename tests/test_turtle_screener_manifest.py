"""turtle_screener.manifest —— 交接契约、刷新策略、门禁裁决与幂等写盘测试。"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from turtle_core.snapshot import FinancialSnapshot  # noqa: E402
from turtle_screener import engine as E  # noqa: E402
from turtle_screener import manifest as M  # noqa: E402
from turtle_screener.prices import PriceQuote  # noqa: E402

AS_OF = "2026-10-14"


def _snap(code, aa=12_000_000_000.0):
    return FinancialSnapshot.new(
        code, "20251231", reporting_currency="CNY", trading_currency="HKD",
        factor_to_reporting=0.85753, c_yuan=2_341_560_000.0,
        aa={"aa_selected": aa}, payout={"m_pess": 23.29}, q_default=20.0,
        total_shares=2_931_000_000.0)


def _result(ii=5.0, n_max=8):
    snaps = [_snap("A.HK", 12_000_000_000.0), _snap("B.HK", 24_000_000_000.0),
             _snap("C.HK", 1.0)]
    quotes = {c: PriceQuote(ts_code=c, close=9.3, currency="HKD")
              for c in ("A.HK", "B.HK", "C.HK")}
    return E.run_screen(snaps, quotes, ii=ii, n_max=n_max,
                        names={"A.HK": "甲", "B.HK": "乙", "C.HK": "丙"},
                        markets={"A.HK": "HK", "B.HK": "HK", "C.HK": "HK"})


class TestRunDir:
    def test_path_by_trade_date(self, tmp_path):
        assert M.run_dir(tmp_path, "2026-10-14") == tmp_path / "screener" / "20261014"

    def test_dashes_stripped(self, tmp_path):
        assert M.run_dir(tmp_path, "20261014") == M.run_dir(tmp_path, "2026-10-14")


class TestDecideAction:
    def test_no_report_needs_workflow1(self):
        assert M.decide_action(None, "20251231", 9.3) == "needs_workflow1"
        assert M.decide_action({}, "20251231", 9.3) == "needs_workflow1"

    def test_same_period_and_small_move_is_refresh_only(self):
        ex = {"report_path": "p.md", "fy_end": "20251231", "last_price": 9.0}
        assert M.decide_action(ex, "20251231", 9.3) == "refresh_only"

    def test_new_fiscal_period_reruns(self):
        ex = {"report_path": "p.md", "fy_end": "20241231", "last_price": 9.0}
        assert M.decide_action(ex, "20251231", 9.3) == "rerun_turtle"

    def test_price_move_at_threshold_reruns(self):
        """已定参数：价格变动 ≥10% ⇒ 报告过期。边界值取 ≥。"""
        ex = {"report_path": "p.md", "fy_end": "20251231", "last_price": 10.0}
        assert M.decide_action(ex, "20251231", 11.0) == "rerun_turtle"

    def test_price_move_below_threshold_refreshes(self):
        ex = {"report_path": "p.md", "fy_end": "20251231", "last_price": 10.0}
        assert M.decide_action(ex, "20251231", 10.5) == "refresh_only"

    def test_missing_last_price_defaults_to_refresh(self):
        ex = {"report_path": "p.md", "fy_end": "20251231"}
        assert M.decide_action(ex, "20251231", 9.3) == "refresh_only"

    def test_zero_last_price_reruns_not_crashes(self):
        ex = {"report_path": "p.md", "fy_end": "20251231", "last_price": 0}
        assert M.decide_action(ex, "20251231", 9.3) == "refresh_only"


class TestShortlist:
    def test_shape_and_ranking(self):
        sl = M.build_shortlist(_result(), AS_OF,
                               trade_dates={"HK": "20261014"}, rf_pct=1.0242)
        assert sl["schema_version"] == M.SCHEMA_VERSION
        assert sl["shortlisted"] == 2 and sl["eligible_count"] == 2
        assert [e["ts_code"] for e in sl["shortlist"]] == ["B.HK", "A.HK"]
        assert sl["shortlist"][0]["rank"] == 1
        assert sl["rf_pct"] == pytest.approx(1.0242)
        assert sl["ii_pct"] == pytest.approx(5.0)

    def test_action_and_needs_workflow1(self):
        existing = {"A.HK": {"report_path": "output/A/A.md", "fy_end": "20251231",
                             "last_price": 9.0}}
        sl = M.build_shortlist(_result(), AS_OF, existing=existing,
                               snapshot_fy_end={"A.HK": "20251231", "B.HK": "20251231"})
        by = {e["ts_code"]: e for e in sl["shortlist"]}
        assert by["A.HK"]["action"] == "refresh_only"
        assert by["A.HK"]["needs_workflow1"] is False
        assert by["B.HK"]["action"] == "needs_workflow1"
        assert by["B.HK"]["needs_workflow1"] is True

    def test_aborted_shortlist_has_no_entries(self):
        sl = M.build_shortlist(_result(ii=None), AS_OF)
        assert sl["aborted"] == "rf_missing"
        assert sl["shortlist"] == []

    def test_json_serializable(self):
        json.dumps(M.build_shortlist(_result(), AS_OF), ensure_ascii=False)

    def test_notes_surfaced_for_zero_eligible(self):
        sl = M.build_shortlist(_result(ii=99.0), AS_OF)
        assert any("0 只达标" in n for n in sl["notes"])


class TestExcluded:
    def test_only_ineligible(self):
        ex = M.build_excluded(_result())
        assert ex["excluded_count"] == 1
        assert ex["excluded"][0]["ts_code"] == "C.HK"
        assert ex["reason_counts"]["below_threshold"] == 1

    def test_reason_labels_present(self):
        ex = M.build_excluded(_result())
        assert ex["excluded"][0]["reason_labels"]
        assert "安全边际" in ex["excluded"][0]["reason_labels"][0]

    def test_reason_summary_rendered(self):
        assert "below_threshold" in M.build_excluded(_result())["reason_summary"]

    def test_empty_when_all_eligible(self):
        res = _result(n_max=8)
        for r in res.rows:
            if r.ts_code == "C.HK":
                r.eligible = True
                r.reasons = []
        ex = M.build_excluded(res)
        assert ex["excluded_count"] == 0
        assert ex["qualified_not_selected"] == 1      # 达标但未进 top-N ≠ 被排除

    def test_qualified_but_cut_by_n_max_is_not_excluded(self):
        """三类别必须分清：达标 / 达标但被 N 截掉 / 因原因码排除。"""
        res = _result(n_max=1)                        # 2 只达标，只取 1
        ex = M.build_excluded(res)
        assert res.eligible_count == 2 and len(res.shortlist) == 1
        assert ex["qualified_not_selected"] == 1
        assert {e["ts_code"] for e in ex["excluded"]} == {"C.HK"}


class TestVerdict:
    def test_aborted_is_blocking_and_not_deliverable(self):
        v = M.verdict_for(_result(ii=None))
        assert v["deliverable"] is False
        assert v["blocking_count"] >= 1
        assert any(b["code"] == "rf_missing" for b in v["blocking"])

    def test_data_gaps_are_disclosure_not_blocking(self):
        """逐股数据缺口是"合格地排除"，不阻断交付。"""
        v = M.verdict_for(_result())
        assert v["deliverable"] is True
        assert v["blocking_count"] == 0

    def test_allow_degraded_flips_but_records(self):
        v = M.verdict_for(_result(ii=None), allow_degraded=True)
        assert v["deliverable"] is True
        assert v["degraded"] is True
        assert v["blocking_count"] >= 1      # 仍然列出来，不隐藏


class TestRunManifest:
    def test_counts_and_ratio(self):
        man = M.build_run_manifest(_result(), AS_OF,
                                   trade_dates={"HK": "20261014"},
                                   universe_stats={"HK": 2788},
                                   calendar_desc="HK: 21 个交易日",
                                   price_stats={"requested": 3, "ok": 3},
                                   snapshot_stats={"loaded": 3})
        assert man["counts"] == {"screened": 3, "eligible": 2, "shortlisted": 2,
                                 "excluded": 1, "qualified_not_selected": 0,
                                 "data_gap": 0}
        assert man["data_gap_ratio"] == 0.0
        assert man["deliverable"] is True
        assert man["calendar"] == "HK: 21 个交易日"

    def test_data_gap_ratio_counts_only_gaps(self):
        res = _result()
        res.rows[2].reasons = ["no_cashflow"]
        man = M.build_run_manifest(res, AS_OF)
        assert man["counts"]["data_gap"] == 1
        assert man["data_gap_ratio"] == pytest.approx(1 / 3, abs=1e-4)

    def test_aborted_manifest_not_deliverable(self):
        man = M.build_run_manifest(_result(ii=None), AS_OF)
        assert man["aborted"] == "rf_missing"
        assert man["deliverable"] is False

    def test_json_serializable(self):
        json.dumps(M.build_run_manifest(_result(), AS_OF), ensure_ascii=False)


class TestCsv:
    def test_header_and_rows(self):
        text = M.results_csv(_result())
        lines = text.strip().splitlines()
        assert lines[0].startswith("rank,ts_code,name")
        assert len(lines) == 4          # header + 3
        assert "C.HK" in text and "below_threshold" in text


class TestWriteRun:
    def test_writes_all_files_atomically(self, tmp_path):
        res = _result()
        sl = M.build_shortlist(res, AS_OF)
        man = M.build_run_manifest(res, AS_OF)
        ex = M.build_excluded(res)
        out = M.write_run(tmp_path, "2026-10-14", sl, man, ex, result=res)
        assert set(out) == {"manifest", "shortlist", "excluded", "csv"}
        d = M.run_dir(tmp_path, "2026-10-14")
        assert list(d.glob("*.tmp")) == []
        assert json.loads((d / "shortlist.json").read_text(encoding="utf-8"))["shortlisted"] == 2

    def test_rerun_overwrites_same_dir(self, tmp_path):
        """按 trade_date 幂等：重跑覆盖，绝不追加第二份。"""
        for ii in (5.0, 99.0):
            res = _result(ii=ii)
            M.write_run(tmp_path, "20261014", M.build_shortlist(res, AS_OF),
                        M.build_run_manifest(res, AS_OF), M.build_excluded(res), result=res)
        dirs = list((tmp_path / "screener").iterdir())
        assert len(dirs) == 1
        sl = json.loads((dirs[0] / "shortlist.json").read_text(encoding="utf-8"))
        assert sl["shortlisted"] == 0          # 第二次（ii=99）的结果

    def test_aborted_run_writes_only_manifest_and_flag(self, tmp_path):
        """残缺清单比没有清单更危险 ⇒ 中止时不产出 shortlist/CSV。"""
        res = _result(ii=None)
        out = M.write_run(tmp_path, "20261014", M.build_shortlist(res, AS_OF),
                          M.build_run_manifest(res, AS_OF), M.build_excluded(res),
                          result=res)
        assert set(out) == {"manifest", "aborted"}
        d = M.run_dir(tmp_path, "20261014")
        assert not (d / "shortlist.json").exists()
        assert not (d / "screener_results.csv").exists()
        assert "rf_missing" in (d / "ABORTED.txt").read_text(encoding="utf-8")
