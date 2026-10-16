"""Tests for scripts/instrument_audit.py —— 仪器体检（归属 / 稳定性 / 命名）。

判据是"能不能归约为一条对任意标的成立、且机器可检的规则"，不是"某份报告对不对"。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import instrument_audit as ia  # noqa: E402


PACK_MIN = """## 3. 合并利润表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 营业额 | 1,000.00 | 900.00 |
| 除税后溢利（合计） | 100.00 | 90.00 |
| 股东应占溢利 | 60.00 | 55.00 |
| 少数股东损益 | 40.00 | 35.00 |

## 4. 合并资产负债表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 现金及等价物 | 500.00 | 450.00 |
| 短期贷款 | — | — |
| 总资产 | 2,000.00 | 1,800.00 |

## 5. 现金流量表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 经营业务现金净额 (OCF) | 300.00 | 280.00 |
| 资本开支（购建固定＋无形及其他） | 100.00 | 90.00 |

### 17.2 因子2输入参数

| 变量 | 2025 | 2024 |
| --- | ---: | ---: |
| C 归母净利润 | 60.00 | 55.00 |
| 少数股东占比（%） | 40.00 | 38.89 |

### 17.5 因子3·步骤7 可支配现金结余（OCF 锚定）

- AA_2y（近2年均值，默认基准）= 200.00 百万元
- AA_all（全部年份均值）= 150.00 百万元
- 经营杠杆系数 λ = 1.50

### 17.10 支付率 M 三重校验

- 三种方法3年均值两两相对偏差最大 5.0%（说明）
"""


class TestParserWasRemoved:
    """P1b 迁移的**完成标志**：本模块不再自带任何 markdown 解析器。

    原先它有 `_heading_index` / `_sec` / `num` / `_row` / `_grab` 五套实现，
    其中 `_sec` 踩过两个实测坑（`### 17.3` 词边界、`### 17.10` 截断）。
    现在这些能力全部由 `scripts/pack_metrics.py` 单点提供
    （其回归见 `tests/test_pack_metrics.py::TestSectionSlicing`）。
    """

    def test_no_local_parser_functions_remain(self):
        for gone in ("_sec", "_heading_index", "_row", "_grab", "_HEAD_RE"):
            assert not hasattr(ia, gone), f"{gone} 应已删除（单点在 pack_metrics）"

    def test_uses_the_single_point_reader(self):
        import pack_metrics
        assert ia.num is pack_metrics.num
        assert ia.section_text is pack_metrics.section_text


class TestEntityBasis:
    def test_minority_exposure_measured_from_17_2(self):
        fs = ia.check_entity_basis(PACK_MIN, "TEST.HK")
        imp = [f for f in fs if f["metric"] == "impact"]
        assert imp and imp[0]["minority_pct"] == pytest.approx(40.0)


class TestStability:
    def test_aa_window_sign_safe(self):
        """分母跨零/为负时不得产生无意义百分比（民生银行 −204,060 vs −8,321.6 → 2352%）。"""
        pack = PACK_MIN.replace(
            "- AA_2y（近2年均值，默认基准）= 200.00 百万元",
            "- AA_2y（近2年均值，默认基准）= -204,060.00 百万元").replace(
            "- AA_all（全部年份均值）= 150.00 百万元",
            "- AA_all（全部年份均值）= -8,321.60 百万元")
        f = [x for x in ia.check_stability(pack, "T") if x["metric"] == "AA_window"][0]
        assert f["status"] == ia.FAIL
        assert f["sign_unstable"] is True
        assert f["value"] < 200.0        # 不得是 2352% 那种量级

    def test_lambda_anomaly_gate(self):
        f = [x for x in ia.check_stability(PACK_MIN, "T") if x["metric"] == "lambda"][0]
        assert f["status"] == ia.PASS
        bad = PACK_MIN.replace("经营杠杆系数 λ = 1.50", "经营杠杆系数 λ = 3.17")
        f2 = [x for x in ia.check_stability(bad, "T") if x["metric"] == "lambda"][0]
        assert f2["status"] == ia.FAIL

    def test_M_method_spread_gate(self):
        bad = PACK_MIN.replace("两两相对偏差最大 5.0%", "两两相对偏差最大 69.2%")
        f = [x for x in ia.check_stability(bad, "T") if x["metric"] == "M_method"][0]
        assert f["status"] == ia.FAIL


class TestMeasureNaming:
    def test_return_named_metric_needs_declaration(self):
        pack = PACK_MIN + "\n| 穿透回报率 | 7.19% |\n"
        fs = ia.check_measure_naming(pack, "T")
        assert fs and fs[0]["status"] == ia.FAIL

    def test_declared_metric_passes(self):
        pack = PACK_MIN + "\n| 税后股息收益率 | 2.81% |\n"
        fs = ia.check_measure_naming(pack, "T")
        assert fs and fs[0]["status"] == ia.PASS


class TestExitCodes:
    def test_readonly_and_exit_codes(self, tmp_path):
        d = tmp_path / "001_测试"
        d.mkdir()
        (d / "data_pack_market.md").write_text(PACK_MIN, encoding="utf-8")
        out = tmp_path / "audit.json"
        rc = ia.main(["--root", str(tmp_path), "--output", str(out), "--quiet"])
        assert rc == 1                                  # 存在 FAIL（AA 未标归属）
        assert out.is_file()

    def test_empty_root_is_usage_error(self, tmp_path):
        assert ia.main(["--root", str(tmp_path), "--quiet"]) == 2


class TestEntityBasisContract:
    """实体归属体检（2026-10-15 **订正**过的判定）。

    初版把 §17.5 AA 的语义硬编码为"归母可得"，判它"声明归母却实现合并"⇒
    对 **107/107** 个产物报 blocked。**那个判定是错的**：
    框架从未要求 AA 归母（它由合并报表构造），且 `factor3_穿透利润精算.md:281`
    明确说因子3 的 AA 与因子2 的 C 基准不同"是粗算/精算方法论差异的正常结果"。
    现在契约只有一处（`strategy_rules.json` 的 `entity_basis`），检查做两件可判的事：
      ① 各量的实体归属与契约一致；② 契约要求披露时产物里**有没有那段披露**。
    """

    @staticmethod
    def _pack(minority="39.64", disclose=True) -> str:
        disc = ("> ⚠️ **口径混用披露**：少数股东占净利润 **39.64%**。"
                "GG 的分子 AA 是**合并口径**、支付率 M 是**归母口径**。\n\n") if disclose else ""
        return (
            "## 3. 合并利润表\n\n| 项目 (百万元) | 2025 |\n| --- | ---: |\n"
            "| 归母净利润 | 861.97 |\n| 少数股东损益 | 566.08 |\n\n"
            "## 17. 衍生指标\n\n### 17.2 因子2输入参数\n\n"
            "| 变量 | 2025 |\n| --- | ---: |\n"
            f"| 少数股东占比（%） | {minority} |\n\n"
            "### 17.11 穿透回报率网格（税后）\n\n" + disc +
            "| 项目 | 值 |\n| --- | ---: |\n| GG | 18.48% |\n"
        )

    def test_contract_quantities_are_all_pass(self):
        fs = [f for f in ia.check_entity_basis(self._pack(), "T")
              if f["metric"] != "disclosure"]
        assert fs and all(f["status"] == ia.PASS for f in fs), \
            [(f["metric"], f["status"]) for f in fs]

    def test_missing_disclosure_fails(self):
        """少数股东 >5% 却缺披露 ⇒ FAIL（这是**真**缺陷：读者会把 GG 当归母可得）。"""
        fs = ia.check_entity_basis(self._pack(disclose=False), "T")
        disc = [f for f in fs if f["metric"] == "disclosure"][0]
        assert disc["status"] == ia.FAIL
        assert "披露" in disc["detail"]

    def test_present_disclosure_passes(self):
        fs = ia.check_entity_basis(self._pack(disclose=True), "T")
        disc = [f for f in fs if f["metric"] == "disclosure"][0]
        assert disc["status"] == ia.PASS

    def test_small_minority_needs_no_disclosure(self):
        """少数股东 ≤5% 不触发披露要求（契约里的阈值）。"""
        fs = ia.check_entity_basis(self._pack(minority="2.12", disclose=False), "T")
        assert not [f for f in fs if f["metric"] == "disclosure"]

    def test_missing_1711_is_unknown_not_pass(self):
        """§17.11 未产出 ⇒ 无从检查披露 ⇒ UNKNOWN（**不是**通过）。"""
        pack = self._pack(disclose=False).replace("### 17.11 穿透回报率网格（税后）", "### 17.12 G 系数网格")
        fs = ia.check_entity_basis(pack, "T")
        disc = [f for f in fs if f["metric"] == "disclosure"]
        assert disc and disc[0]["status"] == ia.UNKNOWN

    def test_impact_is_a_disclosed_property_not_a_defect(self):
        """少数股东暴露本身是**已声明的口径属性**，不再被算成 blocked。"""
        fs = ia.check_entity_basis(self._pack(), "T")
        imp = [f for f in fs if f["metric"] == "impact"][0]
        assert imp["status"] == ia.PASS
        # disposition 由 audit_one 注入；直调检查函数时按规则判定
        assert ia.disposition_of(imp["rule"], imp["metric"], imp["status"]) == ia.DISCLOSE_ONLY

    def test_aa_basis_is_consolidated_per_contract(self):
        """AA 在契约里是**合并口径**（事实声明）—— 不得再被判为"应归母"。"""
        fs = ia.check_entity_basis(self._pack(), "T")
        aa = [f for f in fs if f["metric"] == "17.5.AA"][0]
        assert aa["status"] == ia.PASS
        assert "consolidated" in aa["detail"]
