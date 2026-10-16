"""Tests for the slice-contract hard gate in scripts/validate_dim_result.py.

背景：`dropped_pages` / `slice_truncated` 过去**不被任何门禁消费**（实测 0 引用），
`material_status` 又由读了那份不完整切片的同一个代理自评 ⇒ 可合法地判「充分」而没看到
能证伪它的页（06862：D6 切片不含资产负债表/借款附注页 → 漏计 20.29 亿债券）。

**设计取舍**：门禁**不判断"几页够不够"**（同一个 6 页切片，D1/D2 判充分是合理的 —— 那 6 页
正是商业模式/MD&A 内容），只做三件可机械判定的事：截断必须显式、预算截断必须显式、
引用页码必须落在切片内。（`dropped_pages` 的语义是"锚点范围内被丢弃的页数"，
不是 `total − provided`，故不做记账一致性校验。）
"""

import sys

sys.path.insert(0, "scripts")

import validate_dim_result as vdr  # noqa: E402


def _inst(dim="D6", dropped=279, pages=(161, 166, 228, 271, 272, 273, 283), total=286,
          flags=("slice_truncated",), used=1000, budget=12000, evidence=None):
    ev = evidence if evidence is not None else [
        {"claim": "x", "source": {"kind": "annual_report", "page": 161}}]
    return {
        "dimension": dim,
        "slice": {"provided_pages": list(pages), "total_pages": total,
                  "dropped_pages": dropped, "used_chars": used, "budget_chars": budget},
        "verdict": {"material_status": "不足"},
        "flags": [{"code": c, "severity": "warn", "detail": "x"} for c in flags],
        "evidence": ev,
    }


class TestSliceContractGate:
    def test_well_formed_instance_passes(self):
        assert vdr.check_slice_coverage([_inst()]) == []

    def test_unflagged_dropped_pages_fails(self):
        errs = vdr.check_slice_coverage([_inst(flags=())])
        assert errs and "slice_truncated" in errs[0]

    def test_budget_saturation_requires_flag(self):
        """预算用满 ⇒ 切片被截断，必须有 flag（与 dropped_pages 无关的独立触发路径）。"""
        errs = vdr.check_slice_coverage(
            [_inst(dropped=0, pages=(1, 2, 3, 4, 5, 6, 7), total=286, used=12000, budget=12000,
                   flags=())])
        assert errs and "预算" in errs[0]

    def test_out_of_slice_citation_fails(self):
        """核心回归：06862 的 D6 切片没有 242 页（债券附注），引用它即违规。"""
        ev = [{"claim": "附注29 长期债券", "source": {"kind": "annual_report", "page": 242}}]
        errs = vdr.check_slice_coverage([_inst(evidence=ev)])
        assert errs and "越界引用" in errs[0]

    def test_off_by_one_page_offset_is_tolerated(self):
        """切片页签与印刷页存在 +1 偏移（D6 已登记），故允许 ±1。"""
        ev = [{"claim": "x", "source": {"kind": "annual_report", "page": 284}}]
        assert vdr.check_slice_coverage([_inst(evidence=ev)]) == []

    def test_data_pack_sources_are_not_page_checked(self):
        ev = [{"claim": "x", "source": {"kind": "data_pack", "page": 9999}}]
        assert vdr.check_slice_coverage([_inst(evidence=ev)]) == []

    def test_missing_slice_block_is_tolerated(self):
        assert vdr.check_slice_coverage([{"dimension": "D1"}]) == []
