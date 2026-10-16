"""Tests for scripts/check_report_fidelity.py.

Covers the two report-level invariants that the 06862 manual review found broken:

- **I8 faithfulness** — the turtle report claims the six qualitative narratives are embedded
  "逐字原样保留、未作任何改动"; mechanical comparison shows all six were rewritten
  (numbers added/dropped, a caliber renamed).
- **I9 rule precedence** — `phase3_valuation.md` step 4 makes the pessimistic target price the
  buy point, while step 5 labels "基准价溢价 ≤ 0%" as 「买入就是胜利」. If
  综合基准价 > 采纳档目标买入价 the framework signals "buy" inside a zone its own buy rule
  rejects. That is the set-inclusion invariant {P ≤ 基准价} ⊆ {P ≤ 买点}.
"""

import json
import pathlib

import pytest

import check_report_fidelity as crf

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_REPORT = ROOT / "output" / "06862_海底捞" / "海底捞_06862_分析报告.md"
REAL_DIMS = ROOT / "output" / "06862_海底捞" / "dims"


def _report(narrative: str, composite: float = 9.46, target: float = 8.92, k: float = 0.80,
            claim: bool = True) -> str:
    claim_line = "维度叙述按 **逐字原样保留、未作任何改动**。" if claim else ""
    return f"""# 报告

{claim_line}

### 1. 商业模式与资本特征

（material_status＝充分）

{narrative}

## 估值与定价

| 方法 | 基准价（港元） |
| --- | ---: |
| **综合基准价（算术平均）** | **{composite}** |

悲观档（决策口径）—— 选定 k = {k}

| k | AA_pess | GG_pess | vs 门槛 | 目标买入价（港元） | vs 当前 |
| --- | ---: | ---: | ---: | ---: | ---: |
| **{k:.2f} ★（选定）** | 3,288.04 | 4.79% | -0.21 | **{target}** | -4.1% |
"""


NARRATIVE = "2025 年营业收入 86.12 亿元，归母净利润 11.25 亿元 [src: DP§17.1]。"


class TestNarrativeFidelity:
    def test_verbatim_embedding_passes(self):
        f = crf.check_narrative_fidelity(_report(NARRATIVE), {"D1": NARRATIVE})
        assert f["status"] == crf.PASS

    def test_number_inserted_inside_the_narrative_fails_and_is_named(self):
        report = _report(NARRATIVE.replace("归母净利润 11.25 亿元",
                                           "归母净利润 11.25 亿元、净利率 9.37%"))
        f = crf.check_narrative_fidelity(report, {"D1": NARRATIVE})
        assert f["status"] == crf.FAIL
        assert any("9.37" in e for e in f["evidence"]), f["evidence"]

    def test_extra_numbers_beside_a_verbatim_narrative_warn(self):
        """叙述本身未被改动，但小节里多了数字 —— 记为 WARN 而非 FAIL。"""
        report = _report(NARRATIVE + "\n\n补充口径：净利率 9.37%。")
        f = crf.check_narrative_fidelity(report, {"D1": NARRATIVE})
        assert f["status"] == crf.WARN
        assert any("9.37" in e for e in f["evidence"])

    def test_dropped_number_fails(self):
        f = crf.check_narrative_fidelity(
            _report("2025 年营业收入 86.12 亿元。"),
            {"D1": NARRATIVE})
        assert f["status"] == crf.FAIL
        assert any("丢失" in e for e in f["evidence"])

    def test_renamed_caliber_fails(self):
        report = _report("2025 年营业收入 86.12 亿元，股东权益回报率 11.25 亿元。")
        assert crf.check_narrative_fidelity(report, {"D1": NARRATIVE})["status"] == crf.FAIL

    def test_missing_section_without_claim_is_unevaluated(self):
        """找不到小节 = 判不了，不是违规（避免对非龟龟报告产生假 FAIL）。"""
        f = crf.check_narrative_fidelity("# 报告\n\n没有任何维度小节\n", {"D1": NARRATIVE})
        assert f["status"] == crf.UNEVALUATED

    def test_missing_section_with_verbatim_claim_fails(self):
        report = "# 报告\n\n本报告六维叙述逐字原样保留、未作任何改动。\n"
        f = crf.check_narrative_fidelity(report, {"D1": NARRATIVE})
        assert f["status"] == crf.FAIL

    def test_qualitative_report_heading_style_is_supported(self):
        """定性报告用「## 维度一：…」，与龟龟的「### 1. …」并存支持。"""
        report = f"# 报告\n\n## 维度一：商业模式与资本特征\n\n{NARRATIVE}\n"
        assert crf.check_narrative_fidelity(report, {"D1": NARRATIVE})["status"] == crf.PASS

    def test_adapted_narrative_without_claim_is_only_a_warning(self):
        """综合报告本就是改写式综合 —— 未声明逐字时不得判违规。"""
        report = _report(NARRATIVE.replace("11.25 亿元", "11.25 亿元、净利率 9.37%"), claim=False)
        assert crf.check_narrative_fidelity(report, {"D1": NARRATIVE})["status"] == crf.WARN

    def test_incidental_phrase_does_not_count_as_a_verbatim_claim(self):
        """「按 D4 的 uncertainty 原样保留该风险」不是逐字声明（实测误触发）。"""
        text = "## 维度一：商业模式与资本特征\n\n本报告按 D4 的 uncertainty 原样保留该不对称风险。\n"
        assert not crf._VERBATIM_CLAIM_RE.search(text)

    def test_no_dims_is_unevaluated(self):
        assert crf.check_narrative_fidelity("# x", {})["status"] == crf.UNEVALUATED

    def test_src_tags_are_not_treated_as_differences(self):
        report = _report("2025 年营业收入 86.12 亿元，归母净利润 11.25 亿元。")
        src = "2025 年营业收入 86.12 亿元，归母净利润 11.25 亿元 [src: 年报P.24]。"
        assert crf.check_narrative_fidelity(report, {"D1": src})["status"] == crf.PASS


class TestRulePrecedence:
    def test_baseline_below_buy_point_passes(self):
        f = crf.check_rule_precedence(_report(NARRATIVE, composite=8.50, target=8.92))
        assert f["status"] == crf.PASS

    def test_baseline_above_buy_point_fails(self):
        f = crf.check_rule_precedence(_report(NARRATIVE, composite=9.34, target=8.92))
        assert f["status"] == crf.FAIL
        assert "不可买" in f["detail"]

    def test_claim_token_form_is_parsed(self):
        report = _report(NARRATIVE, composite=9.34, target=8.92).replace(
            "**8.92**", '<claim id="cm17.14.k080.tbp" policy="round:2">8.92</claim>')
        f = crf.check_rule_precedence(report)
        assert f["status"] == crf.FAIL and "8.92" in f["evidence"][1]

    def test_prefers_floor_composite_over_legacy_composite(self):
        """语义分组后，I9 必须取**综合底价**（底价型均值），而不是旧的跨语义「综合基准价」。"""
        report = _report(NARRATIVE, composite=9.34, target=8.92).replace(
            "| **综合基准价（算术平均）** | **9.34** |",
            "**综合底价（底价型锚点算术平均，3 项）= 4.12 港元**")
        f = crf.check_rule_precedence(report)
        assert f["status"] == crf.PASS, "底价 4.12 ≤ 买点 8.92 应通过"
        assert "综合底价 = 4.12" in f["evidence"][0]

    def test_unparsable_is_unevaluated_not_pass(self):
        assert crf.check_rule_precedence("# 什么都没有")["status"] == crf.UNEVALUATED


class TestCli:
    def test_exit_1_on_failure_and_0_on_clean(self, tmp_path):
        dims = tmp_path / "dims"
        dims.mkdir()
        (dims / "D1.json").write_text(json.dumps({"narrative": NARRATIVE}, ensure_ascii=False),
                                      encoding="utf-8")
        clean = tmp_path / "clean.md"
        clean.write_text(_report(NARRATIVE, composite=8.50), encoding="utf-8")
        assert crf.main(["--report", str(clean), "--dims", str(dims), "--quiet"]) == 0

        bad = tmp_path / "bad.md"
        bad.write_text(_report(NARRATIVE + " 9.37%", composite=9.34), encoding="utf-8")
        assert crf.main(["--report", str(bad), "--dims", str(dims), "--quiet"]) == 1

    def test_missing_report_is_exit_2(self, tmp_path):
        assert crf.main(["--report", str(tmp_path / "nope.md"), "--quiet"]) == 2

    def test_strict_counts_unevaluated(self, tmp_path):
        thin = tmp_path / "thin.md"
        thin.write_text("# 只有标题", encoding="utf-8")
        assert crf.main(["--report", str(thin), "--quiet"]) == 0
        assert crf.main(["--report", str(thin), "--quiet", "--strict"]) == 1


@pytest.mark.skipif(not REAL_REPORT.exists(), reason="龟龟报告不存在")
class TestRealReportStatus:
    """实时龟龟报告的不变式状态（断言里写明"为什么"，使未来的变化必须是刻意的）。

    历史缺陷（六个维度叙述被改写却声明"逐字原样保留"）不再用真实产物做回归 —— 那份产物
    已被修复并归档到 `output/_baselines/`；该缺陷类别现由 `TestNarrativeFidelity` 的
    合成夹具覆盖（夹具比真实产物稳定，不会随重跑漂移）。
    """

    def test_live_status(self):
        """语义断言（不钉具体状态，钉**要求**）——真实产物会随每轮重跑变化。"""
        findings = {f["code"]: f for f in crf.run_all(
            REAL_REPORT.read_text(encoding="utf-8"), crf._load_dims(str(REAL_DIMS)))}

        # 要求一：不得"已改写却声称逐字保留"（历史缺陷 N6；上一轮就是 FAIL 态）
        assert findings["I8"]["status"] != crf.FAIL, \
            "报告不得在改写六维叙述的情况下声明逐字保留"

        # 要求二：规则序不变式必须成立；若被违反，报告必须**如实披露**而非文字化解
        i9 = findings["I9"]["status"]
        assert i9 in (crf.PASS, crf.FAIL), f"I9 不应为 {i9}"
        if i9 == crf.FAIL:
            txt = REAL_REPORT.read_text(encoding="utf-8")
            assert ("不构成" in txt) or ("冲突" in txt), \
                "I9 FAIL 时报告必须披露基准价读数与买点的冲突"
