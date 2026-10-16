"""Tests for scripts/report_consistency.py — 杠杆6 cross-passage audit + 杠杆7 gates."""

import os
import pathlib
import sys

import pytest

import report_consistency as rc
from report_consistency import extract, find_conflicts, check_gates, main

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "sample_report_with_conflicts.md")


@pytest.fixture(scope="module")
def report_text():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return f.read()


# ── extraction & masking ────────────────────────────────────────────

class TestExtraction:
    def test_src_number_skipped(self, report_text):
        """The 999.99 亿 inside [src: ...] must never be extracted."""
        clusters = extract(report_text)
        vals = [o.value for obs in clusters.values() for o in obs]
        assert 999.99 not in vals

    def test_bare_year_not_a_metric(self, report_text):
        """A bare '2024' (no unit) must not be extracted as a value."""
        clusters = extract(report_text)
        vals = [o.value for obs in clusters.values() for o in obs]
        assert 2024 not in vals and 2024.0 not in vals

    def test_revenue_cluster_built(self, report_text):
        clusters = extract(report_text)
        assert ("营业收入", "2024") in clusters
        vals = sorted({round(o.value, 2) for o in clusters[("营业收入", "2024")]})
        assert 500.0 in vals and 560.0 in vals

    def test_margin_cluster_built(self, report_text):
        clusters = extract(report_text)
        assert ("毛利率", "2024") in clusters
        vals = sorted({round(o.value, 2) for o in clusters[("毛利率", "2024")]})
        assert 47.6 in vals and 49.2 in vals

    def test_nearest_keyword_classification(self):
        """营业利润 next to the number wins over a farther 归母 keyword."""
        txt = "2024 年营业利润 122.51 亿，归母 89.54 亿。"
        clusters = extract(txt)
        assert ("营业利润", "2024") in clusters
        assert ("归母净利润", "2024") in clusters
        prof = [round(o.value, 2) for o in clusters[("营业利润", "2024")]]
        assert prof == [122.51]


# ── conflict detection ──────────────────────────────────────────────

class TestConflicts:
    def test_default_tolerance_flags_revenue(self, report_text):
        conflicts = find_conflicts(extract(report_text), 0.05)
        cats = {(c["category"], c["year"]) for c in conflicts}
        assert ("营业收入", "2024") in cats            # 500 vs 560 = 10.7% > 5%
        assert ("毛利率", "2024") not in cats          # 3.3% < 5% → within tol

    def test_tight_tolerance_flags_margin(self, report_text):
        conflicts = find_conflicts(extract(report_text), 0.02)
        cats = {(c["category"], c["year"]) for c in conflicts}
        assert ("营业收入", "2024") in cats
        assert ("毛利率", "2024") in cats              # 3.3% > 2%

    def test_identical_values_deduped(self):
        """Same value repeated is not a conflict."""
        txt = "2024 年营业收入 500.00 亿。后文重申 2024 年营业收入 500.00 亿。"
        conflicts = find_conflicts(extract(txt), 0.05)
        assert conflicts == []


# ── hard gates (杠杆7) ───────────────────────────────────────────────

class TestGates:
    def test_short_dimensions_fail(self, report_text):
        failures = check_gates(report_text)
        assert any("维度一" in f for f in failures)
        assert any("维度二" in f for f in failures)

    def test_conditional_dim6_skipped_when_degraded(self, report_text):
        failures = check_gates(report_text)
        assert not any("维度六" in f for f in failures)   # 不适用 → skipped

    def test_required_sections_present(self, report_text):
        failures = check_gates(report_text)
        assert not any("数字溯源汇总" in f for f in failures)
        assert not any("结构化参数" in f for f in failures)

    def test_missing_required_section_flagged(self):
        txt = "## 维度一：商业模式\n" + ("字" * 700) + "\n## 维度二：护城河\n" + ("字" * 900)
        failures = check_gates(txt)
        assert any("数字溯源汇总" in f for f in failures)
        assert any("结构化参数" in f for f in failures)


# ── CLI ─────────────────────────────────────────────────────────────

class TestCLI:
    def test_main_conflicts_are_advisory_exit_0(self, tmp_path):
        """2026-10-14 语义变更：advisory 冲突**不**计入交付退出码（默认）。"""
        out = tmp_path / "cons.md"
        rc_code = main(["--report", FIXTURE, "--output", str(out)])
        assert rc_code == 0
        assert out.exists()
        # 冲突清单仍必须被渲染出来，否则信号会静默消失
        assert "冲突" in out.read_text(encoding="utf-8")

    def test_conflicts_are_failures_flag_restores_exit_1(self, tmp_path):
        """逃生口：需要旧语义时可显式恢复。"""
        out = tmp_path / "cons.md"
        rc_code = main(["--report", FIXTURE, "--output", str(out),
                        "--conflicts-are-failures"])
        assert rc_code == 1

    def test_main_missing_file_exit_2(self, tmp_path):
        rc_code = main(["--report", str(tmp_path / "nope.md")])
        assert rc_code == 2

    def test_main_clean_report_exit_0(self, tmp_path):
        clean = tmp_path / "clean.md"
        clean.write_text("# 报告\n\n## 维度一\n2024 年营业收入 500.00 亿，稳健增长。\n",
                         encoding="utf-8")
        rc_code = main(["--report", str(clean)])
        assert rc_code == 0

    def test_main_gates_exit_1(self, tmp_path):
        out = tmp_path / "g.md"
        rc_code = main(["--report", FIXTURE, "--output", str(out), "--gates"])
        assert rc_code == 1
        assert "硬门槛" in out.read_text(encoding="utf-8")


# ===== §17 claim verification (--claims · known_issues.md P2-20 改动 2) =====


class TestClaimGate:
    """`--claims` must mechanically verify claim-bound tokens, fail-closed."""

    def _ledger_file(self, tmp_path):
        import json
        payload = {
            "schema_version": "1.0", "stock_code": "600887.SH",
            "claims": [
                {"claim_id": "cm17.11.aa2y.m3.gg", "indicator": "GG",
                 "value": 2.89, "unit": "%", "time": "2024",
                 "basis": "AA_2y=8710", "policy": "round:2", "section": "17.11"},
            ],
        }
        path = tmp_path / "claims.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_valid_token_passes(self, tmp_path):
        from report_consistency import check_claims
        report = '穿透回报率 <claim id="cm17.11.aa2y.m3.gg">2.89%</claim>'
        result = check_claims(report, str(self._ledger_file(tmp_path)))
        assert result["tokens"] == 1
        assert result["failed"] == []

    def test_tampered_value_fails(self, tmp_path):
        from report_consistency import check_claims
        report = '穿透回报率 <claim id="cm17.11.aa2y.m3.gg">7.20%</claim>'
        result = check_claims(report, str(self._ledger_file(tmp_path)))
        assert len(result["failed"]) == 1

    def test_unknown_claim_id_fails(self, tmp_path):
        from report_consistency import check_claims
        report = '<claim id="cm17.99.nope">1.00%</claim>'
        result = check_claims(report, str(self._ledger_file(tmp_path)))
        assert len(result["failed"]) == 1

    def test_missing_ledger_returns_none(self, tmp_path):
        from report_consistency import check_claims
        assert check_claims("x", str(tmp_path / "nope.json")) is None

    def test_render_flags_bare_numbers(self, tmp_path):
        from report_consistency import check_claims, render_claims
        result = check_claims("全是裸数字 1.0 2.0", str(self._ledger_file(tmp_path)))
        out = render_claims(result)
        assert "没有任何" in out
        assert "默认不可信" in out

    def test_render_lists_failures(self, tmp_path):
        from report_consistency import check_claims, render_claims
        report = '<claim id="cm17.11.aa2y.m3.gg">7.20%</claim>'
        out = render_claims(check_claims(report, str(self._ledger_file(tmp_path))))
        assert "未能通过机械校验" in out
        assert "cm17.11.aa2y.m3.gg" in out

    def test_cli_exit_code_is_1_on_claim_failure(self, tmp_path):
        import subprocess
        ledger = self._ledger_file(tmp_path)
        report = tmp_path / "r.md"
        report.write_text('<claim id="cm17.11.aa2y.m3.gg">7.20%</claim>\n'
                          "## 七、风险\n内容足够长以通过字数门槛检查，重复若干次。"
                          "内容足够长以通过字数门槛检查，重复若干次。\n",
                          encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "report_consistency.py"),
             "--report", str(report), "--claims", str(ledger), "--gates"],
            capture_output=True, text=True)
        assert proc.returncode == 1
        assert "声明校验失败" in proc.stderr

    def test_cli_requires_gates(self, tmp_path):
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "report_consistency.py"),
             "--report", __file__, "--claims", str(self._ledger_file(tmp_path))],
            capture_output=True, text=True)
        assert proc.returncode == 2
        assert "--gates" in proc.stderr
