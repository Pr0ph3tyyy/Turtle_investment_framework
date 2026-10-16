"""两套受控原因码词表的**跨模块一致性**（tests for the reason vocabularies）。

为什么要有这个测试
------------------
`analysis` 与 `screener` 各自维护一套受控原因码：

* `scripts/section_contract.py::REASONS` —— §17 小节状态的原因码（采集器/analysis 产出）；
* `scripts/turtle_core/reasons.py::SCREEN_REASONS` —— 选股器的排除原因码。

`turtle_screener/slow_layer.py` 把前者**直接搬进**后者的命名空间：小节状态里的 `reason`
若不在 `SCREEN_REASONS` 里，只会打一条「不在选股器受控词表内，未登记」的警告 ⇒
`excluded.json` 的排除理由**不可诊断**。

这条缺口是真实发生过的（2026-10-15）：给 §17.14 新增 `negative_basis` 后，
选股器立刻报「未登记」。两套词表当时没有任何一致性约束 —— 本文件把它补上。
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from section_contract import REASONS  # noqa: E402
from turtle_core.reasons import SCREEN_REASONS  # noqa: E402

#: 有意**不**映射到选股器词表的码。新增豁免必须写明理由 —— 否则本测试会挡住它。
INTENTIONAL_EXEMPTIONS: dict[str, str] = {}


class TestVocabularyContainment:
    def test_every_analysis_reason_is_known_to_screener(self):
        """analysis 的每个码都必须被选股器认识（否则排除理由不可诊断）。"""
        missing = [r for r in REASONS if r not in SCREEN_REASONS
                   and r not in INTENTIONAL_EXEMPTIONS]
        assert not missing, (
            f"这些码在 section_contract.REASONS 里但不在 SCREEN_REASONS 里：{missing}；"
            "请在 turtle_core/reasons.py 补 label/remedy，或登记进 INTENTIONAL_EXEMPTIONS 并写明理由"
        )

    def test_exemptions_are_real_and_documented(self):
        """豁免必须是真存在的码，且不能同时又被定义了。"""
        for code, why in INTENTIONAL_EXEMPTIONS.items():
            assert code in REASONS, f"豁免 {code!r} 不在 section_contract.REASONS 里"
            assert code not in SCREEN_REASONS, f"{code!r} 已定义，不该同时豁免"
            assert why.strip(), f"豁免 {code!r} 没写理由"

    def test_screener_vocab_entries_are_complete(self):
        """每个码都要有非空 label 与 remedy（否则 `describe()` 输出空标签）。"""
        for code, spec in SCREEN_REASONS.items():
            assert spec.get("label", "").strip(), f"{code} 缺 label"
            assert spec.get("remedy", "").strip(), f"{code} 缺 remedy"

    def test_analysis_vocab_entries_are_complete(self):
        for code, spec in REASONS.items():
            assert spec.get("label", "").strip(), f"{code} 缺 label"
            assert spec.get("remedy", "").strip(), f"{code} 缺 remedy"


class TestGlobalCodes:
    """全局缺口 ⇒ 整批 ABORT（`gate_policy` 依此裁决）。"""

    def test_no_rf_is_global(self):
        """`no_rf`（section_contract 名）与 `rf_missing`（选股器名）是同一件事，都必须是全局。"""
        from turtle_core.reasons import is_global
        assert is_global("rf_missing")
        assert is_global("no_rf")

    def test_per_stock_gaps_are_not_global(self):
        """逐股缺口是「合格地排除」，不得升级为整批中止。"""
        from turtle_core.reasons import is_global
        for code in ("negative_basis", "no_data", "not_applicable", "below_threshold"):
            assert not is_global(code), code

    def test_unknown_code_fails_closed(self):
        from turtle_core.reasons import require
        with pytest.raises(KeyError):
            require("definitely_not_a_real_reason")
