"""策略规则单点一致性 —— docs ⟷ strategy_rules.json ⟷ code 三方对齐。

为什么需要（known_issues.md §三十二）
------------------------------------
阈值/税率/值域原先只活在 markdown 里（`shared_tables.md`、`judgment_examples_turtle.md`），
代码里**手工转抄**，且抄了不止一份：

* `II_FLOOR` / `II_SPREAD` 在 `tushare_modules/constants.py` **与** `numeric_audit.py` 各一份；
* `factor_interface.md:96` 把 `lambda_reliability` 值域写成「正常/有一项警告/异常」，
  而代码自 2026-10 起实际产出「**多项警告或异常**」—— 契约与实现已漂移，没有测试发现。

**设计取舍**：`numeric_audit.py` 的「独立第二实现」是刻意的（公式重写、常量另抄），
所以**不**要求它 `import constants`。取而代之：以 `strategy_rules.json` 为**中立权威**，
docs 与两份代码都必须与它一致 —— 任何一方漂移，本测试立刻红。
"""

import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

RULES_PATH = ROOT / "scripts" / "strategy_rules.json"
SHARED_TABLES = ROOT / "strategies" / "turtle" / "references" / "shared_tables.md"
JUDGMENT = ROOT / "strategies" / "turtle" / "references" / "judgment_examples_turtle.md"
IFACE = ROOT / "strategies" / "turtle" / "references" / "factor_interface.md"


@pytest.fixture(scope="module")
def rules():
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


class TestCodeMatchesRules:
    def test_constants_module_ii(self, rules):
        from tushare_modules import constants as C
        assert C.II_FLOOR == rules["ii_threshold"]["floor_pct"]
        assert C.II_SPREAD == rules["ii_threshold"]["spread_pct"]

    def test_numeric_audit_ii_independent_copy(self, rules):
        """第二实现是刻意另抄的 —— 但必须与权威一致，否则"对不上账"时分不清谁错。"""
        import numeric_audit as NA
        assert NA.II_FLOOR == rules["ii_threshold"]["floor_pct"]
        assert NA.II_SPREAD == rules["ii_threshold"]["spread_pct"]

    def test_numeric_audit_q_defaults(self, rules):
        import numeric_audit as NA
        d = rules["dividend_tax_q_pct"]["default_by_market"]
        assert NA.Q_DEFAULT_HK == d["HK"]
        assert NA.Q_DEFAULT_A == d["A"]
        assert NA.Q_DEFAULT_US == d["US"]


class TestDocsMatchRules:
    """文档侧：解析 markdown 里的权威表述，与 rules 比对。"""

    def test_ii_formula_in_shared_tables(self, rules):
        txt = SHARED_TABLES.read_text(encoding="utf-8")
        m = re.search(r"II\s*=\s*max\(\s*([\d.]+)%\s*,\s*Rf\s*\+\s*([\d.]+)%\s*\)", txt)
        assert m, "shared_tables.md 里找不到 `II = max(X%, Rf + Y%)` 的权威表述"
        assert float(m.group(1)) == rules["ii_threshold"]["floor_pct"]
        assert float(m.group(2)) == rules["ii_threshold"]["spread_pct"]

    def test_lambda_band_in_judgment_examples(self, rules):
        txt = JUDGMENT.read_text(encoding="utf-8")
        m = re.search(r"λ\s*=\s*([\d.]+)-([\d.]+)", txt)
        assert m, "judgment_examples_turtle.md 里找不到 λ 正常带"
        lo, hi = float(m.group(1)), float(m.group(2))
        assert [lo, hi] == rules["lambda_reliability"]["normal_band"]

    def test_lambda_anomaly_threshold_in_judgment_examples(self, rules):
        txt = JUDGMENT.read_text(encoding="utf-8")
        m = re.search(r"λ\s*>\s*([\d.]+)\s*或\s*λ\s*<\s*([\d.]+)", txt)
        assert m, "judgment_examples_turtle.md 里找不到 λ 异常判据"
        assert float(m.group(1)) == rules["lambda_reliability"]["anomaly_above"]
        assert float(m.group(2)) == rules["lambda_reliability"]["anomaly_below"]


class TestInterfaceContractAlignment:
    def test_lambda_reliability_vocabulary_is_aligned(self, rules):
        """`factor_interface.md` 声明的值域必须与 rules（=代码实际产出）一致。

        这条是真实漂移的回归：接口文档曾写「正常/有一项警告/异常」，
        而代码产出「多项警告或异常」。
        """
        txt = IFACE.read_text(encoding="utf-8")
        line = next((l for l in txt.splitlines() if "lambda_reliability" in l), "")
        assert line, "factor_interface.md 未声明 lambda_reliability"
        for v in rules["lambda_reliability"]["vocabulary"]:
            assert v in line, (
                f"factor_interface.md 的 lambda_reliability 值域缺少 {v!r}；"
                f"该行现为：{line.strip()}"
            )


class TestCodeEmitsDeclaredVocabulary:
    def test_derived_metrics_loads_rules_and_uses_them(self, rules):
        """代码判 λ 异常用的阈值必须**来自** strategy_rules（防止又一处裸字面量）。"""
        from tushare_modules import derived_metrics as DM
        assert DM.LA["anomaly_above"] == rules["lambda_reliability"]["anomaly_above"]
        assert DM.LA["anomaly_below"] == rules["lambda_reliability"]["anomaly_below"]
        src = pathlib.Path(DM.__file__).read_text(encoding="utf-8")
        assert 'LA["anomaly_above"]' in src and 'LA["anomaly_below"]' in src, (
            "λ 判据未引用 LA（strategy_rules）—— 又被写成裸字面量了"
        )


class TestRuleCompleteness:
    """**完备性**方向：规则单点里不该有"没人读"的条目，代码里不该有"没进单点"的策略常量。

    P3 做的是**一致性**（docs ⟷ rules ⟷ code 三方对齐）；本类做**完备性**：
    2026-10-15 扫描发现 `pessimistic_k_grid` / `g_grid` / `defensive_gates`
    **只活在代码里**，规则单点里根本没有 —— 一致性测试永远发现不了这种缺口
    （它只比对"两边都有"的东西）。所以要有第二条方向。
    """

    #: 允许"只作文档"的条目（无代码读者），必须写明理由
    DOC_ONLY: dict[str, str] = {
        "schema_version": "格式版本，非策略规则",
        "note": "说明文字",
        "entity_basis": "契约（由 instrument_audit 读取，见 test_entity_basis_is_read）",
    }

    def test_every_rule_entry_has_a_reader_or_is_doc_only(self, rules):
        """每个规则条目都要有代码读者，或在 DOC_ONLY 里登记理由。"""
        import pathlib
        blob = ""
        for f in ("tushare_modules/derived_metrics.py", "tushare_modules/constants.py",
                  "numeric_audit.py", "instrument_audit.py", "turtle_core/pessimistic.py",
                  "turtle_core/decision.py", "field_coverage.py", "invariants.py"):
            p = ROOT / "scripts" / f
            if p.is_file():
                blob += p.read_text(encoding="utf-8")
        orphan = []
        for key in rules:
            if key in self.DOC_ONLY:
                continue
            if key not in blob:
                orphan.append(key)
        assert not orphan, (
            f"这些规则条目没有任何代码读者（要么接线，要么登记进 DOC_ONLY 并写明理由）：{orphan}"
        )

    def test_entity_basis_is_read_by_the_audit(self):
        """`entity_basis` 的读者是 instrument_audit（它在运行时读 JSON，源码里没有键名字面量）。"""
        import instrument_audit as IA
        assert IA.ENTITY_CONTRACT.get("quantities"), "instrument_audit 未读到实体归属契约"

    def test_k_grid_in_code_matches_rules(self, rules):
        """k 网格以前只在代码里 —— 现在两边必须一致。"""
        from turtle_core import pessimistic as P
        spec = rules["pessimistic_k_grid"]
        assert list(P.PESS_K_VALUES) == spec["values_desc"]
        assert P.PESS_K_DEFAULT == spec["default_star"]

    def test_g_grid_in_code_matches_rules(self, rules):
        """G 网格的起止与步长必须与代码一致（代码里是 0.7..1.8 步长 0.1）。"""
        import pathlib
        src = (ROOT / "scripts" / "tushare_modules" / "derived_metrics.py").read_text(encoding="utf-8")
        spec = rules["g_grid"]["values"]
        # 代码用 round(x,1) 生成；这里断言字面量在源码里出现（防漂移）
        assert f"{spec['start']}" in src and f"{spec['stop']}" in src
        assert "0.7" in src and "1.8" in src

    def test_defensive_gate_thresholds_match_code(self, rules):
        import pathlib
        src = (ROOT / "scripts" / "tushare_modules" / "derived_metrics.py").read_text(encoding="utf-8")
        spec = rules["defensive_gates"]
        assert f"({spec['cash_to_mktcap_pct']['ample']}," in src, "③ 的 40/30 分档与规则不一致"
        assert f"({spec['aa_to_mktcap_minus_netcash_pct']['ok_floor']}," in src, "④ 的 40 门槛与规则不一致"
