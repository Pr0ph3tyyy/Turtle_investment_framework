"""Precision regression tests for scripts/report_consistency.py.

The live end-to-end run (002984, 2026-10-13) showed the extractor produced 12 advisory
conflicts of which a numeric audit judged **8 to be pure artifacts** and only 4 to be real
caliber differences — i.e. 33% precision, which is why the audit recommended not using the
list as standalone gate evidence.

Each test below pins one artifact class that was fixed:

1. range notation   — ``8%-15%`` was parsed as the value ``-15``
2. thresholds       — ``<18%`` / ``30% 以上`` are limits, not reported observations
3. change vs level  — ``毛利率 21.13%、同比下降 11.65 个百分点`` mixed a delta with a level
4. keyword theft    — a metric name *after* the number stole a YoY series
5. table rows       — a row's main value inherited a year mentioned in its note column
6. series folding   — ``2021 年 14.66%、2024 年 23.63%`` collapsed into one period
7. change verbs     — ``由 2024 年 32.84% 回落至 21.28%`` made 21.28 inherit 2024

They also pin the recall side: genuine 母公司/合并 caliber differences on a single-year
prose line must still be surfaced.
"""

import pathlib

import pytest

import report_consistency as rc

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_REPORT = ROOT / "output" / "002984_森麒麟" / "qualitative_report.md"


def _values(text):
    """All normalized (category, year) -> values found in ``text``."""
    return {k: sorted(o.value for o in v) for k, v in rc.extract(text).items()}


def _all_values(text):
    return [o.value for v in rc.extract(text).values() for o in v]


# ===== 1. range notation =====


def test_range_is_not_parsed_as_a_negative():
    got = _all_values("毛利率区间 8%-15%，2025 年毛利率 21.28%")
    assert -15 not in got, "区间连接符 '-' 不得被当作负号"
    assert -15.0 not in got


def test_genuine_negative_money_value_is_still_parsed():
    """A negative money value must survive. (A YoY *rate* under a money metric is not
    comparable with 亿元 and is intentionally dropped — see test below.)"""
    got = _all_values("2025 年归母净利润 -1.25 亿元")
    assert -1.25 in got


def test_rate_under_a_money_metric_is_not_compared_with_money_values():
    """「归母净利润同比 -48.54%」与「归母净利润 11.25 亿元」不可比，不应聚成一类。"""
    got = _values("2025 年归母净利润 11.25 亿元、同比 -48.54% [src: CM§2]")
    assert got[("归母净利润", "2025")] == [11.25]


# ===== 2. thresholds =====


def test_threshold_prefix_is_excluded():
    assert 18 not in _all_values("毛利率<18% 连续两年，2025 年毛利率 21.28%")


def test_threshold_suffix_is_excluded():
    assert 30 not in _all_values("支付率能否维持 30% 以上，2025 年支付率 32.23%")


def test_nearby_real_observation_after_comparison_is_kept():
    """「高于 2024 年的 23.63%」是真实观测值，不能因"高于"被丢掉。"""
    assert 23.63 in _all_values("分红支付率 32.23%，高于 2024 年的 23.63%")


# ===== 3. change vs level =====


def test_percentage_point_change_does_not_conflict_with_the_level():
    text = ("2025 年毛利率 21.13%、同比下降 11.65 个百分点 [src: 年报P.18]。"
            "2025 年毛利率 21.13% [src: DP§12]。")
    assert rc.find_conflicts(rc.extract(text), tol=0.05) == []


# ===== 4. keyword theft =====


def test_trailing_metric_name_does_not_steal_a_series():
    text = "同比依次为 +6.31%、+70.88%、-48.54% [src: CM§2]；毛利率从 2024 年 32.84% 回落至 21.28%。"
    got = _values(text)
    assert ("毛利率", "2024") in got
    assert -48.54 not in got.get(("毛利率", "2024"), []), "同比序列不得被算成毛利率"


# ===== 5. table rows =====


def test_table_note_years_do_not_leak_into_the_row_value():
    text = "| 毛利率 | 21.28% | 2024 年 32.84%；5 年均值 24.63% | DP§12 / CM§3 |"
    got = _values(text)
    assert 21.28 not in got.get(("毛利率", "2024"), []), "主值不得继承备注列的年份"


# ===== 6. series folding =====


def test_year_series_is_assigned_per_value():
    text = "分红支付率从 2021 年 14.66%、2024 年 23.63% 提升至 2025 年 32.23% [src: CM§4]。"
    got = _values(text)
    assert got[("分红率", "2021")] == [14.66]
    assert got[("分红率", "2024")] == [23.63]
    assert got[("分红率", "2025")] == [32.23]
    assert rc.find_conflicts(rc.extract(text), tol=0.05) == []


# ===== 7. change verbs =====


def test_change_verb_blocks_year_inheritance():
    text = "毛利率由 2024 年 32.84% 回落至 21.28% [src: DP§12]。"
    got = _values(text)
    assert got[("毛利率", "2024")] == [32.84], "变化目标值不属于来源年份"
    assert rc.find_conflicts(rc.extract(text), tol=0.05) == []


# ===== recall: genuine caliber differences must survive =====


def test_genuine_caliber_difference_on_prose_line_is_surfaced():
    text = ("触发判定（2025 年度）：合并营业收入 86.12 亿元 [src: CM§1]，"
            "母公司口径营业收入仅 2,522.56 百万元 [src: DP§3P]。")
    conflicts = rc.find_conflicts(rc.extract(text), tol=0.05)
    assert [c["category"] for c in conflicts] == ["营业收入"]
    assert sorted(conflicts[0]["values"]) == [25.2256, 86.12]


# ===== integration on the real report =====


@pytest.mark.skipif(not REAL_REPORT.exists(), reason="002984 报告不存在，跳过")
def test_real_report_conflict_count_is_within_budget():
    """12 before the precision work; the audit judged only 4 to be genuine."""
    text = REAL_REPORT.read_text(encoding="utf-8")
    conflicts = rc.find_conflicts(rc.extract(text), tol=0.05)
    assert len(conflicts) <= 6, f"冲突数回升到 {len(conflicts)}，疑似精度退化"
    assert len(conflicts) >= 3, "不应把真实口径差异一并滤掉"


@pytest.mark.skipif(not REAL_REPORT.exists(), reason="002984 报告不存在，跳过")
def test_real_report_keeps_the_genuine_caliber_conflicts():
    text = REAL_REPORT.read_text(encoding="utf-8")
    conflicts = {c["category"]: c for c in rc.find_conflicts(rc.extract(text), tol=0.05)}
    # 母公司 vs 合并（营收 / 净利润）与 加权 vs 平均（ROE）是审计确认的口径差异，必须保留
    assert "营业收入" in conflicts
    assert "ROE" in conflicts
    assert {8.24, 8.41} <= set(conflicts["ROE"]["values"])
    assert 25.2256 in conflicts["营业收入"]["values"] and 86.12 in conflicts["营业收入"]["values"]
