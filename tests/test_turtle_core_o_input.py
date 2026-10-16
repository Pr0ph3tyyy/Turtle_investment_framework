"""`turtle_core.o_input` —— O（注销型回购）单点解析与 fail-closed 校验（findings D-3）。

两条核心不变量：
1. **无声明 ≡ O=0**，且与现状逐字节相同（纯增量，不动既有数值）；
2. **非零 O 必须有来源** —— 一个凭空填的非零 O 会静默抬高穿透回报率，比 0 危害大得多。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from turtle_core import o_input as O  # noqa: E402
from turtle_core import reasons  # noqa: E402


def _write(tmp_path, payload, name=O.O_FILENAME):
    p = tmp_path / name
    if isinstance(payload, str):
        p.write_text(payload, encoding="utf-8")
    else:
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return tmp_path


GOOD = {"o_annual_mm": 123.4, "years": 5,
        "source": "2025年报 p.56「注销式回购」", "confirmed_by": "human",
        "as_of": "2026-10-14"}


class TestNoDeclaration:
    def test_missing_file_is_default_zero(self, tmp_path):
        o, why = O.resolve_o(tmp_path)
        assert o.o_annual_mm == 0.0 and o.is_default and why is None

    def test_missing_dir_is_default_zero(self, tmp_path):
        o, why = O.resolve_o(tmp_path / "nope")
        assert o.is_default and why is None

    def test_default_describe_says_unconfirmed(self, tmp_path):
        o, _ = O.resolve_o(tmp_path)
        assert "默认 0" in o.describe() and "待年报确认" in o.describe()


class TestDeclaredZero:
    def test_zero_with_source_is_accepted_and_not_default(self, tmp_path):
        o, why = O.resolve_o(_write(tmp_path, {"o_annual_mm": 0, "source": "2025年报 无注销式回购"}))
        assert why is None and o.o_annual_mm == 0.0 and not o.is_default

    def test_zero_without_source_is_rejected(self, tmp_path):
        """「确认过是 0」与「根本没确认」必须能区分 ⇒ 声明 0 也要来源。"""
        o, why = O.resolve_o(_write(tmp_path, {"o_annual_mm": 0}))
        assert why == "o_unsourced" and o.is_default

    def test_declared_zero_describe_differs_from_default(self, tmp_path):
        declared, _ = O.resolve_o(_write(tmp_path, {"o_annual_mm": 0, "source": "年报 p.1"}))
        default, _ = O.resolve_o(tmp_path / "elsewhere")
        assert declared.describe() != default.describe()


class TestNonzero:
    def test_good_payload_accepted(self, tmp_path):
        o, why = O.resolve_o(_write(tmp_path, GOOD))
        assert why is None
        assert o.o_annual_mm == pytest.approx(123.4)
        assert o.confirmed_by == "human" and o.years == 5
        assert not o.is_default and o.is_nonzero

    def test_llm_confirmer_allowed(self, tmp_path):
        o, why = O.resolve_o(_write(tmp_path, {**GOOD, "confirmed_by": "llm"}))
        assert why is None and o.is_nonzero

    def test_confirmer_case_insensitive(self, tmp_path):
        o, why = O.resolve_o(_write(tmp_path, {**GOOD, "confirmed_by": "HUMAN"}))
        assert why is None and o.confirmed_by == "human"

    def test_missing_source_is_rejected(self, tmp_path):
        """**最重要的一条**：非零 O 没有来源 ⇒ 拒绝，避免凭空补数。"""
        payload = {k: v for k, v in GOOD.items() if k != "source"}
        o, why = O.resolve_o(_write(tmp_path, payload))
        assert why == "o_unsourced"
        assert o.o_annual_mm == 0.0 and o.is_default

    def test_missing_confirmer_is_rejected(self, tmp_path):
        payload = {k: v for k, v in GOOD.items() if k != "confirmed_by"}
        o, why = O.resolve_o(_write(tmp_path, payload))
        assert why == "o_unsourced" and o.is_default

    def test_unknown_confirmer_is_rejected(self, tmp_path):
        o, why = O.resolve_o(_write(tmp_path, {**GOOD, "confirmed_by": "api"}))
        assert why == "o_unsourced" and o.is_default

    def test_blank_source_is_rejected(self, tmp_path):
        o, why = O.resolve_o(_write(tmp_path, {**GOOD, "source": "   "}))
        assert why == "o_unsourced"

    def test_describe_names_the_source_and_confirmer(self, tmp_path):
        o, _ = O.resolve_o(_write(tmp_path, GOOD))
        text = o.describe()
        assert "123.40" in text and "p.56" in text and "人工" in text


class TestInvalid:
    @pytest.mark.parametrize("bad", [-1, -0.01, "123", None, [], {}, True, False])
    def test_bad_value_rejected(self, tmp_path, bad):
        o, why = O.resolve_o(_write(tmp_path, {**GOOD, "o_annual_mm": bad}))
        assert why == "o_invalid", f"{bad!r} 应被拒"
        assert o.o_annual_mm == 0.0 and o.is_default

    def test_missing_value_key_rejected(self, tmp_path):
        payload = {k: v for k, v in GOOD.items() if k != "o_annual_mm"}
        assert O.resolve_o(_write(tmp_path, payload))[1] == "o_invalid"

    def test_malformed_json_rejected(self, tmp_path):
        assert O.resolve_o(_write(tmp_path, "{not json"))[1] == "o_invalid"

    def test_json_array_rejected(self, tmp_path):
        assert O.resolve_o(_write(tmp_path, "[1,2,3]"))[1] == "o_invalid"

    def test_nan_and_inf_rejected(self, tmp_path):
        assert O.resolve_o(_write(tmp_path, '{"o_annual_mm": NaN, "source": "x", '
                                          '"confirmed_by": "human"}'))[1] == "o_invalid"
        assert O.resolve_o(_write(tmp_path, '{"o_annual_mm": Infinity, "source": "x", '
                                          '"confirmed_by": "human"}'))[1] == "o_invalid"

    def test_no_abs_of_negative(self, tmp_path):
        """负值不得取绝对值 —— 那是静默改写输入。"""
        o, why = O.resolve_o(_write(tmp_path, {**GOOD, "o_annual_mm": -50}))
        assert why == "o_invalid" and o.o_annual_mm == 0.0

    def test_bad_years_is_dropped_not_fatal(self, tmp_path):
        o, why = O.resolve_o(_write(tmp_path, {**GOOD, "years": -3}))
        assert why is None and o.years is None


class TestReasonVocabulary:
    def test_new_codes_are_registered(self):
        for code in ("o_unsourced", "o_invalid"):
            reasons.require(code)
            assert reasons.label_of(code)
            assert reasons.remedy_of(code)

    def test_rem_e_dy_is_actionable(self):
        assert "o.json" in reasons.remedy_of("o_unsourced")

    def test_explain_rejection_mentions_remedy(self):
        text = O.explain_rejection("o_unsourced")
        assert "o_unsourced" in text and "o.json" in text


class TestOIsWiredIntoDecisions:
    """O 必须真的进结论（不是死参数）。

    背景：`o_input.py` 的模块文档长期写着「`o_val` 在 §17.9/§17.11/§17.14
    三处各自硬编码 0.0」——**该问题早已接线完毕**，文档未同步。
    本测试把"已接线"这件事钉住，防止将来又退回硬编码 0。
    """

    def test_three_sections_read_the_single_point(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent / "scripts"
               / "tushare_modules" / "derived_metrics.py").read_text(encoding="utf-8")
        # 三处都必须是 _o_input()，不得出现"o_val = 0.0"这类硬编码
        assert "_o_input().o_annual_mm" in src
        assert src.count("self._o_input().o_annual_mm") >= 3, (
            "§17.9/§17.11/§17.14 应各自引用 O 的单点；数量不足说明有一处回退成硬编码"
        )
        assert "o_val = 0.0" not in src

    def test_o_has_a_registered_claim(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent / "scripts"
               / "tushare_modules" / "derived_metrics.py").read_text(encoding="utf-8")
        assert '"cm17.11.o"' in src, "O 必须有 claim，否则报告引用它时无法机械核验"
