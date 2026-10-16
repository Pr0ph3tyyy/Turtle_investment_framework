"""Tests for bare-number accounting (claim_ledger.bare_sections + coverage gate).

背景：`verify_report` 只遍历 `<claim>` 标记，**未绑定的裸数字既不失败也不计数** ——
而 AGENTS.md 声称 "a bare number is untrusted by default"。实测 06862 龟龟报告
绑定覆盖率仅 **1.9%**（绑定 48 / 共 2469 个数值 token）。本组测试锁定"未绑定可度量"。
"""

import json
import pathlib
import sys

sys.path.insert(0, "scripts")

import claim_ledger as cl  # noqa: E402
import report_consistency as rc  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
# ⚠️ hermetic：**不得**依赖 output/ 下的真实产物。
# 原先指向 `output/06862_海底捞/claims.json` —— 归档旧产物后该测试立刻集体失败，
# 暴露了"测试与运行环境耦合"（它测的是覆盖率逻辑，跟哪个标的一点关系都没有）。
CLAIMS = ROOT / "tests" / "fixtures" / "claims_min.json"

DOC = """# 报告

## 有绑定的区段

穿透回报率为 <claim id="cm17.14.k080.ggpess" policy="round:2">4.66%</claim>，
买点 <claim id="cm17.14.k080.tbp" policy="round:2">8.67</claim>。

## 大量裸数字的区段

门店 1,355 → 1,304 家；翻台率 4.1 → 3.9；同店 -6.73%；ROE 20.39%。
"""


class TestBareSections:
    def test_counts_only_unbound_numbers(self):
        r = cl.bare_sections(DOC)
        # 8.67 / 4.66% 在 claim 内，不应计入
        assert r["bare_tokens"] == 6, r
        samples = [s for s in r["sections"] if "裸数字" in s["section"]]
        assert samples and samples[0]["count"] == 6

    def test_groups_by_nearest_heading(self):
        r = cl.bare_sections(DOC)
        heads = {s["section"]: s["count"] for s in r["sections"]}
        assert heads.get("有绑定的区段", 0) == 0
        assert heads.get("大量裸数字的区段") == 6

    def test_sections_sorted_desc(self):
        r = cl.bare_sections("# A\n1,355 家 20.39%\n# B\n4.5%\n")
        assert [s["section"] for s in r["sections"]][0] == "A"

    def test_years_and_ordinals_are_not_quantities(self):
        """2026-10-14 收紧：旧实现把『5、2025、4』这类年份/序号也算作裸数字。

        实测 06862 报告 2,421 个"裸数字"里 214 个是四位年份、967 个是 1–2 位序号
        （合计 49%）。分母被噪声撑大后覆盖率既不真实、也永远无法达标。
        """
        r = cl.bare_sections("# T\n2025 年、2026 年；列表 1. 2. 3. 4.\n")
        assert r["bare_tokens"] == 0, r
        # 带单位/百分号/小数点/≥3 位的才算量
        r2 = cl.bare_sections("# T\n1,304 家；20.39%；4.5 倍；47%\n")
        assert r2["bare_tokens"] == 4, r2

    def test_verbatim_narrative_sections_are_excluded_from_denominator(self):
        """六个维度段是 dims 的**逐字嵌入**（I8 强制）：其数字既不能绑定也不能改，
        计入分母会与保真要求直接冲突。"""
        r = cl.bare_sections("## 维度一：商业模式与资本特征\n1,304 家、20.39%\n"
                             "## 穿透回报率\n4.66%\n")
        assert r["bare_tokens"] == 1, r           # 只数「穿透回报率」那 1 个
        assert r["bare_quantity_all"] == 3        # 明细里仍然看得到全部量级 token
        narr = [s for s in r["sections_all"] if s["section"].startswith("维度一")]
        assert narr and narr[0]["narrative"] is True

    def test_empty_doc(self):
        assert cl.bare_sections("")["bare_tokens"] == 0

    def test_verify_report_includes_bare(self):
        ledger = cl.load_claims(str(CLAIMS))
        res = cl.verify_report(DOC, ledger)
        assert res["tokens"] == 2 and not res["failed"]
        assert res["bare"]["bare_tokens"] == 6


class TestCoverageGate:
    def _run(self, tmp_path, threshold):
        rep = tmp_path / "r.md"
        rep.write_text(DOC, encoding="utf-8")
        return rc.main(["--report", str(rep), "--claims", str(CLAIMS), "--gates",
                        "--min-claim-coverage", str(threshold),
                        "--output", str(tmp_path / "cr.md")])

    def test_low_coverage_fails_gate(self, tmp_path):
        assert self._run(tmp_path, 50) == 1
        txt = (tmp_path / "cr.md").read_text(encoding="utf-8")
        assert "绑定覆盖率" in txt and "门槛 50.0%" in txt

    def test_threshold_below_actual_does_not_add_a_coverage_failure(self, tmp_path):
        """实测覆盖率 25%（2 绑定 / 8 量级 token）⇒ 门槛 1% 不应产生覆盖率门槛失败。

        注：`--gates` 另有章节门槛，本用例只断言**覆盖率门槛本身**不触发。
        """
        self._run(tmp_path, 1)
        txt = (tmp_path / "cr.md").read_text(encoding="utf-8")
        assert "门槛 1.0%" not in txt
        assert "决策区段覆盖率 **25.0%**" in txt

    def test_strict_enables_a_default_floor(self, tmp_path):
        """--strict 下不显式给门槛，也用默认下限（旧行为是"不启用"）。

        用一份覆盖率明显低于下限的报告（2 绑定 / 22 量级 ≈ 9%）。
        """
        rep = tmp_path / "r.md"
        body = "、".join(f"{100 + i * 7}.{i}%" for i in range(20))
        rep.write_text(
            "# 报告\n\n## 穿透回报率\n\n"
            '穿透回报率为 <claim id="cm17.14.k080.ggpess" policy="round:2">4.66%</claim>，'
            '买点 <claim id="cm17.14.k080.tbp" policy="round:2">8.67</claim>。\n\n'
            f"其余数据：{body}。\n", encoding="utf-8")
        rc.main(["--report", str(rep), "--claims", str(CLAIMS), "--gates", "--strict",
                 "--output", str(tmp_path / "cr.md")])
        txt = (tmp_path / "cr.md").read_text(encoding="utf-8")
        assert f"门槛 {rc.DEFAULT_MIN_CLAIM_COVERAGE:.1f}%" in txt

    def test_zero_bound_tokens_still_prints_coverage(self, tmp_path):
        """绑定数为 0 时旧实现提前 return，连裸数字明细都不打印 ——
        静默恰好发生在最需要它发声的时候。"""
        rep = tmp_path / "r.md"
        rep.write_text("# 报告\n\n## 穿透回报率\n\n穿透回报率 4.66%。\n", encoding="utf-8")
        rc.main(["--report", str(rep), "--claims", str(CLAIMS), "--gates",
                 "--output", str(tmp_path / "cr.md")])
        txt = (tmp_path / "cr.md").read_text(encoding="utf-8")
        assert "没有任何" in txt            # 仍然说明没有绑定
        assert "决策区段覆盖率" in txt      # **且**打印覆盖率（旧实现在此提前返回）
        assert "未绑定数值 token" in txt

    def test_gate_off_by_default(self, tmp_path):
        rep = tmp_path / "r.md"
        rep.write_text(DOC, encoding="utf-8")
        rc.main(["--report", str(rep), "--claims", str(CLAIMS), "--gates",
                 "--output", str(tmp_path / "cr.md")])
        txt = (tmp_path / "cr.md").read_text(encoding="utf-8")
        assert "< 门槛" not in txt
