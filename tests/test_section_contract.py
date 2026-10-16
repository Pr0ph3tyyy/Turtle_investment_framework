"""Tests for the §17 section status contract (scripts/section_contract.py).

Locks down known_issues.md P2-20 改动 4: §17 sections are optional, so absence
used to be inferable only from a heading *not* appearing — which left
"not computed" indistinguishable from "computed as zero" and pushed the prompt
into hand-calculation fallbacks. The contract makes absence explicit and
fail-closed.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from section_contract import (  # noqa: E402
    REASONS,
    SCHEMA_VERSION,
    SECTION_TITLES,
    STATUS_DEGRADED,
    STATUS_MISSING,
    STATUS_OK,
    SectionMarkError,
    SectionRegistry,
    check_pack_consistency,
    headings_in_pack,
    missing_summary,
    write_status,
)


class TestCatalogue:
    def test_sixteen_sections(self):
        """The catalogue must match the sections assembly actually produces."""
        assert len(SECTION_TITLES) == 16
        assert set(SECTION_TITLES) == {f"17.{i}" for i in range(1, 17)}

    def test_titles_are_nonempty(self):
        for number, title in SECTION_TITLES.items():
            assert title.strip(), number

    def test_reason_codes_are_controlled(self):
        """Every reason carries a human label AND an actionable remedy."""
        for code, entry in REASONS.items():
            assert entry["label"].strip(), code
            assert entry["remedy"].strip(), code


class TestMarkValidation:
    def test_accepts_ok(self):
        reg = SectionRegistry()
        reg.mark("17.1", STATUS_OK)
        assert reg.status_of("17.1") == STATUS_OK
        assert reg.is_marked("17.1")

    def test_rejects_unknown_section(self):
        reg = SectionRegistry()
        with pytest.raises(SectionMarkError, match="未知的 §17 小节编号"):
            reg.mark("17.99")

    def test_rejects_unknown_status(self):
        reg = SectionRegistry()
        with pytest.raises(SectionMarkError, match="不在词表"):
            reg.mark("17.1", "maybe")

    def test_missing_requires_reason(self):
        """Fail-closed at the point of production: no unlabelled absence."""
        reg = SectionRegistry()
        with pytest.raises(SectionMarkError, match="必须给出 reason"):
            reg.mark("17.5", STATUS_MISSING)

    def test_rejects_reason_outside_vocabulary(self):
        reg = SectionRegistry()
        with pytest.raises(SectionMarkError, match="不在受控词表"):
            reg.mark("17.5", STATUS_MISSING, "made_up")

    def test_ok_must_not_carry_reason(self):
        reg = SectionRegistry()
        with pytest.raises(SectionMarkError, match="不应给出 reason"):
            reg.mark("17.5", STATUS_OK, "no_rf")

    def test_degraded_carries_note(self):
        reg = SectionRegistry()
        reg.mark("17.13", STATUS_DEGRADED, "no_data", note="λ ≈ 0")
        payload = reg.finalize("600887.SH")
        assert payload["sections"]["17.13"]["status"] == STATUS_DEGRADED
        assert payload["sections"]["17.13"]["note"] == "λ ≈ 0"


class TestFailClosedFinalize:
    def test_unmarked_becomes_missing(self):
        """A section that silently stopped being produced must not look present."""
        reg = SectionRegistry()
        reg.mark("17.1", STATUS_OK)
        payload = reg.finalize("600887.SH")
        assert payload["sections"]["17.1"]["status"] == STATUS_OK
        assert payload["sections"]["17.11"]["status"] == STATUS_MISSING
        assert payload["sections"]["17.11"]["reason"] == "not_generated"

    def test_every_catalogue_section_present(self):
        payload = SectionRegistry().finalize()
        assert set(payload["sections"]) == set(SECTION_TITLES)

    def test_schema_version_and_units(self):
        payload = SectionRegistry().finalize("06862.HK", {"reporting_currency": "CNY"})
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["stock_code"] == "06862.HK"
        assert payload["units"]["reporting_currency"] == "CNY"

    def test_warnings_are_included(self):
        reg = SectionRegistry()
        reg.warn("币种未确认")
        assert reg.finalize()["warnings"] == ["币种未确认"]


class TestConsistencyCheck:
    def test_heading_regex_matches_only_real_headings(self):
        pack = (
            "### 17.1 财务趋势速览\n"
            "| §17.11 | 穿透回报率网格 | ❌ missing | 无Rf |\n"
            "### 17.11 穿透回报率网格（税后）\n"
        )
        assert headings_in_pack(pack) == {"17.1", "17.11"}

    def test_status_table_reference_is_not_a_heading(self):
        """`§17.N` in the status table must not be read as a section heading."""
        pack = "| §17.9 | 因子4·业绩下滑敏感性 | ❌ missing | — |"
        assert headings_in_pack(pack) == set()

    def test_ok_without_heading_is_a_violation(self):
        payload = {"sections": {"17.5": {"status": STATUS_OK}}}
        problems = check_pack_consistency("no headings here", payload)
        assert len(problems) == 1
        assert "找不到 `### 17.5` 标题" in problems[0]

    def test_missing_with_heading_is_a_violation(self):
        payload = {"sections": {"17.5": {"status": STATUS_MISSING}}}
        problems = check_pack_consistency("### 17.5 因子3·步骤7\n", payload)
        assert len(problems) == 1
        assert "存在 `### 17.5` 标题" in problems[0]

    def test_consistent_pack_passes(self):
        payload = {"sections": {
            "17.5": {"status": STATUS_OK},
            "17.11": {"status": STATUS_MISSING},
        }}
        assert check_pack_consistency("### 17.5 x\n| §17.11 | y |", payload) == []


class TestMissingSummary:
    def test_lists_only_non_ok_sorted(self):
        payload = {"sections": {
            "17.11": {"status": STATUS_MISSING, "title": "网格", "reason_label": "无Rf", "remedy": "补 --rf"},
            "17.1": {"status": STATUS_OK},
            "17.9": {"status": STATUS_DEGRADED, "title": "敏感性", "reason_label": "部分缺失"},
        }}
        out = missing_summary(payload)
        assert [m["section"] for m in out] == ["17.9", "17.11"]
        assert out[1]["reason"] == "无Rf"
        assert out[1]["remedy"] == "补 --rf"

    def test_empty_when_all_ok(self):
        payload = {"sections": {"17.1": {"status": STATUS_OK}}}
        assert missing_summary(payload) == []


class TestWriteStatus:
    def test_writes_json(self, tmp_path):
        reg = SectionRegistry()
        reg.mark("17.1", STATUS_OK)
        out = write_status(tmp_path / "section17_status.json", reg, "600887.SH")
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["stock_code"] == "600887.SH"
        assert payload["sections"]["17.1"]["status"] == STATUS_OK

    def test_creates_parent_dirs(self, tmp_path):
        out = write_status(tmp_path / "nested" / "dir" / "s.json", SectionRegistry())
        assert out.exists()


# ===== 缺 Rf 时的小节原因码（findings D-9）=====
class TestRfDependentSections:
    """缺 Rf ⇒ 缺 II ⇒ 四节都算不出来，**原因码必须都是 `no_rf`**。

    实测（00696.HK，不传 `--rf`，2026-10-14）：§17.14 曾落到 `no_data`。
    `no_rf` 的补救是「传 `--rf`」，`no_data` 的补救是「去找上游数据」——
    标错会把读者引向完全错误的修法。
    """

    RF_DEPENDENT = ("因子4·业绩下滑敏感性", "穿透回报率网格", "收入敏感性", "悲观基准网格")

    @pytest.mark.parametrize("label", ("因子4·业绩下滑敏感性", "穿透回报率网格",
                                       "收入敏感性", "悲观基准网格"))
    def test_label_is_declared_rf_dependent(self, label):
        from tushare_modules.assembly import AssemblyMixin
        assert label in AssemblyMixin._RF_DEPENDENT_LABELS, (
            f"{label} 依赖 II（=max(5%, Rf+3%)），缺 Rf 时必然算不出来；"
            f"漏登记会让它的原因码落到 no_data 而不是 no_rf"
        )

    def test_all_four_sections_share_the_same_reason(self):
        """四节的依赖完全相同 ⇒ 分组必须一致（防止将来只改其中几个）。"""
        from tushare_modules.assembly import AssemblyMixin
        assert set(self.RF_DEPENDENT) <= set(AssemblyMixin._RF_DEPENDENT_LABELS)

    def test_no_rf_is_in_the_controlled_vocabulary(self):
        """原因码必须来自受控词表（契约要求 `status != ok` 时给出词表内的 reason）。

        ⚠️ **两套词表不要混**：§17 小节原因码在 `section_contract.REASONS`，
        screener 的排除码在 `turtle_core.reasons.SCREEN_REASONS`。
        `no_rf` 只属于前者（我第一版错写成后者，测试立刻抓到）。
        """
        import section_contract as SC
        assert "no_rf" in SC.REASONS
        label = (SC.REASONS["no_rf"] or {}).get("label", "")
        assert "Rf" in label or "利率" in label, label

    def test_seventeen_fourteen_is_mapped_for_reason_lookup(self):
        """§17.14 必须在 label→section 表里，否则原因码查不到它。"""
        from tushare_modules.assembly import AssemblyMixin
        assert AssemblyMixin._LABEL_TO_SECTION.get("悲观基准网格") == "17.14"


class TestSafeMark:
    """`_safe_mark`：错误处理器不得比它处理的错误更脆弱（known_issues.md §三十）。

    `SectionRegistry.mark()` 在 number 未知或 reason 不在受控词表时会 raise。
    它被调用的位置是 `except` 分支 —— 一旦它自己抛，受控缺失会升级成整包失败。
    """

    def test_unknown_section_number_does_not_raise(self):
        from tushare_modules.assembly import AssemblyMixin

        class _C(AssemblyMixin):
            def __init__(self):
                self._store = {}

        c = _C()
        reg = c._section_registry()
        c._safe_mark(reg, "99.99", "missing", "exception", note="boom")   # 不得抛
        fails = c._store.get("_mark_failures") or []
        assert fails and "99.99" in fails[0]

    def test_out_of_vocab_reason_does_not_raise(self):
        from tushare_modules.assembly import AssemblyMixin

        class _C(AssemblyMixin):
            def __init__(self):
                self._store = {}

        c = _C()
        reg = c._section_registry()
        c._safe_mark(reg, "17.14", "missing", "not_a_real_reason")        # 不得抛
        assert c._store.get("_mark_failures")

    def test_valid_mark_still_works_and_records_nothing(self):
        from tushare_modules.assembly import AssemblyMixin

        class _C(AssemblyMixin):
            def __init__(self):
                self._store = {}

        c = _C()
        reg = c._section_registry()
        c._safe_mark(reg, "17.14", "missing", "negative_basis", note="AA<0")
        assert not c._store.get("_mark_failures")
        entry = reg.finalize("TEST")["sections"]["17.14"]
        assert entry["status"] == "missing" and entry["reason"] == "negative_basis"
