"""Tests for scripts/dimension_slicer.py (包 2 阶段 1).

The slicer encodes hard-won empirical lessons, so the tests pin them:

- **TOC pages must be excluded** — a TOC hits nearly every dimension's keywords
  (a prototype picked the cover + TOC as "D5 MD&A").
- **Keyword-only page picking fails in both directions** — it either lands on the TOC or on
  the section title page alone (a prototype produced a 913-char D5 slice).
- **HK reports have no `第X节` and no dot leaders** — their TOC is bilingual plain text
  ("MANAGEMENT DISCUSSION AND ANALYSIS 管理層論析 19"), which the first implementation
  missed entirely (0 sections, 0 TOC entries).
- **Section matching must follow pattern priority, not document order** — otherwise D4
  anchors to the board-bios pages instead of the corporate-governance report.
- **Sections routinely exceed the budget** (a real MD&A was 31,351 chars vs 12,000), so
  in-section truncation plus a declared coverage note is mandatory, not optional.
"""

import pathlib

import pytest

from dimension_slicer import (
    DIMENSIONS,
    build_sections_from_toc,
    is_front_or_toc,
    load_pages,
    parse_toc,
    render_slice,
    slice_dimension,
    _match_section,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _pages(mapping):
    """Build a page-marked document from {page_no: text}."""
    return "".join(f"\n===== PDF PAGE {n} =====\n{t}\n" for n, t in mapping.items())


# ===== parsing =====


def test_load_pages_parses_markers():
    pages = load_pages(_pages({1: "封面", 2: "正文", 7: "附录"}))
    assert sorted(pages) == [1, 2, 7]
    assert pages[2].strip() == "正文"


def test_is_front_or_toc_detects_dot_leader_toc():
    toc = "第一节 重要提示 .......... 2\n第三节 管理层讨论与分析 .......... 10"
    assert is_front_or_toc(toc)


def test_is_front_or_toc_detects_contents_page():
    assert is_front_or_toc("CONTENTS 目錄\nMANAGEMENT DISCUSSION 19")


def test_is_front_or_toc_false_for_content_page():
    body = "本集团以轮胎制造为核心，产能与成本结构决定了资本密集度高。" * 5
    assert not is_front_or_toc(body)


# ===== TOC parsing: both real formats =====


def test_parse_toc_dot_leader_format():
    """A-share style: dot leaders."""
    doc = _pages({
        1: "封面",
        2: "第一节 重要提示、目录和释义 .......... 2\n第三节 管理层讨论与分析 .......... 10",
        10: "第三节 管理层讨论与分析\n正文……",
    })
    toc, ordered = parse_toc(load_pages(doc))
    assert toc["第三节 管理层讨论与分析"] == 10
    assert (10, "第三节 管理层讨论与分析") in ordered


def test_parse_toc_bilingual_plain_format():
    """HK style: no dot leaders at all — the case the first version missed."""
    doc = _pages({
        3: "CONTENTS 目錄\n"
           "MANAGEMENT DISCUSSION AND ANALYSIS 管理層論析 19\n"
           "CORPORATE GOVERNANCE REPORT 企業管治報告 43\n"
           "NOTES TO THE CONSOLIDATED FINANCIAL STATEMENTS 綜合財務報表附註 102",
        19: "MANAGEMENT DISCUSSION AND ANALYSIS 管理層論析\n正文……",
        43: "CORPORATE GOVERNANCE REPORT 企業管治報告\n正文……",
        102: "NOTES TO THE CONSOLIDATED FINANCIAL STATEMENTS 綜合財務報表附註\n正文……",
    })
    toc, _ = parse_toc(load_pages(doc))
    assert toc["MANAGEMENT DISCUSSION AND ANALYSIS 管理層論析"] == 19
    assert toc["CORPORATE GOVERNANCE REPORT 企業管治報告"] == 43
    assert toc["NOTES TO THE CONSOLIDATED FINANCIAL STATEMENTS 綜合財務報表附註"] == 102


def test_parse_toc_drops_targets_beyond_the_document():
    """A TOC entry pointing past the last page is bogus and must not create a section."""
    doc = _pages({3: "CONTENTS 目錄\nPHANTOM SECTION 幻影章節 999", 4: "正文"})
    toc, _ = parse_toc(load_pages(doc))
    assert "PHANTOM SECTION 幻影章節" not in toc


def test_parse_toc_ignores_body_pages():
    """A body page full of numbers must not become a TOC."""
    doc = _pages({
        5: "2025年营业收入 1,234\n归母净利润 567\n" * 6,
        6: "管理层讨论与分析\n正文……",
    })
    toc, ordered = parse_toc(load_pages(doc))
    assert toc == {}
    assert ordered == []


def test_build_sections_from_toc_ranges():
    toc = {"A": 2, "B": 10, "C": 40}
    ordered = sorted(((p, n) for n, p in toc.items()))
    sections = build_sections_from_toc(ordered, last_page=100)
    assert [(s["start"], s["end"]) for s in sections] == [(2, 9), (10, 39), (40, 100)]
    assert all(s["kind"] == "toc" for s in sections)


# ===== section matching priority =====


def test_match_section_prefers_pattern_priority_over_document_order():
    """D4 must reach the governance report, not the earlier board-bios pages."""
    sections = [
        {"start": 33, "end": 42, "title": "DIRECTORS AND SENIOR MANAGEMENT PROFILE 董事及高級管理層簡介", "kind": "toc"},
        {"start": 43, "end": 70, "title": "CORPORATE GOVERNANCE REPORT 企業管治報告", "kind": "toc"},
    ]
    hit = _match_section(sections, DIMENSIONS["D4"]["sections"])
    assert hit is not None
    assert hit["start"] == 43


def test_match_section_returns_none_when_nothing_matches():
    sections = [{"start": 1, "end": 2, "title": "封面", "kind": "toc"}]
    assert _match_section(sections, DIMENSIONS["D5"]["sections"]) is None


# ===== slicing behaviour =====


def _toy_report(budget_hint=200):
    """A small synthetic report with a TOC, front matter and a long MD&A section."""
    pages = {
        1: "封面 COVER",
        2: "CONTENTS 目錄\nMANAGEMENT DISCUSSION AND ANALYSIS 管理層論析 3\nCORPORATE GOVERNANCE REPORT 企業管治報告 6",
        3: "MANAGEMENT DISCUSSION AND ANALYSIS 管理層論析\n" + "经营回顾与展望。" * 20,
        4: "一、主要业务\n" + "收入与成本分析。" * 20,
        5: "二、竞争格局\n" + "市场份额与研发投入。" * 20,
        6: "CORPORATE GOVERNANCE REPORT 企業管治報告\n" + "董事会与审计委员会。" * 20,
        7: "關聯交易 disclosures\n" + "关联交易说明。" * 20,
    }
    return load_pages(_pages(pages))


def test_slice_respects_budget_and_records_truncation():
    pages = _toy_report()
    toc, ordered = parse_toc(pages)
    sections = build_sections_from_toc(ordered, max(pages))
    entry = slice_dimension(pages, sections, toc, "D5", budget=120)
    assert entry["used_chars"] <= 120
    assert entry["anchor_kind"] in {"toc_section", "toc_lookup"}
    assert entry["dropped_pages"] >= 0
    assert entry["section_chars"] >= entry["used_chars"]


def test_slice_excludes_front_and_toc_pages():
    pages = _toy_report()
    toc, ordered = parse_toc(pages)
    sections = build_sections_from_toc(ordered, max(pages))
    for dim in DIMENSIONS:
        entry = slice_dimension(pages, sections, toc, dim, budget=4000)
        assert 2 not in entry["provided_pages"], f"{dim} 选中了目录页"
        assert 1 not in entry["provided_pages"], f"{dim} 选中了封面页"


def test_slice_anchors_to_toc_section_for_each_dimension():
    pages = _toy_report()
    toc, ordered = parse_toc(pages)
    sections = build_sections_from_toc(ordered, max(pages))
    md = slice_dimension(pages, sections, toc, "D5", budget=4000)
    gov = slice_dimension(pages, sections, toc, "D4", budget=4000)
    assert md["anchor_kind"] == "toc_section" and "MANAGEMENT" in md["anchor"].upper()
    assert gov["anchor_kind"] == "toc_section" and "GOVERNANCE" in gov["anchor"].upper()


def test_slice_falls_back_to_keywords_without_anchors():
    pages = load_pages(_pages({
        1: "封面",
        2: "行业政策与周期讨论。" * 30,
        3: "汇率与关税影响。" * 30,
    }))
    entry = slice_dimension(pages, [], {}, "D3", budget=100)
    assert entry["anchor_kind"] == "keyword_fallback"
    assert entry["used_chars"] <= 100
    assert entry["provided_pages"], "兜底路径也必须产出切片"


def test_contiguous_strategy_yields_ascending_pages():
    """Narrative dimensions (D5) should read in page order, not by keyword scatter."""
    pages = _toy_report()
    toc, ordered = parse_toc(pages)
    sections = build_sections_from_toc(ordered, max(pages))
    entry = slice_dimension(pages, sections, toc, "D5", budget=4000)
    assert entry["strategy"] == "contiguous_fill"
    assert entry["provided_pages"] == sorted(entry["provided_pages"])


def test_narrative_dimension_covers_both_head_and_tail_of_the_section():
    """Live-run regression: head-only filling made D5 miss the outlook/risk chapters.

    The real D5 slice covered only pages 10–17 of a 23-page MD&A section, so the dimension
    agent had to declare material_status=不足 even though the material existed. Narrative
    dimensions must get both the opening (results) and the closing (outlook/risk) pages.
    """
    # a long MD&A section: pages 3..20, each page comfortably larger than a full head budget
    pages = load_pages(_pages({
        2: "CONTENTS 目錄\nMANAGEMENT DISCUSSION AND ANALYSIS 管理層論析 3",
        **{p: f"MANAGEMENT DISCUSSION AND ANALYSIS 管理層論析\n" + f"第{p}页经营讨论内容。" * 40
           for p in range(3, 21)},
    }))
    toc, ordered = parse_toc(pages)
    sections = build_sections_from_toc(ordered, max(pages))
    budget = 4000
    entry = slice_dimension(pages, sections, toc, "D5", budget=budget)
    chosen = entry["provided_pages"]
    window = list(range(3, 21))
    assert chosen, "叙事型维度不得产出空切片"
    assert entry["used_chars"] <= budget
    assert chosen[0] == window[0], "必须覆盖段落开头"
    assert chosen[-1] == window[-1], "必须覆盖段落结尾（展望/风险章节在尾部）"
    assert chosen == sorted(chosen)


def test_contiguous_slice_is_never_empty_even_with_a_tiny_budget():
    """A 1-page section larger than the budget must still yield that page (truncated)."""
    pages = load_pages(_pages({
        2: "CONTENTS 目錄\nMANAGEMENT DISCUSSION AND ANALYSIS 管理層論析 3",
        3: "MANAGEMENT DISCUSSION AND ANALYSIS 管理層論析\n" + "经营回顾。" * 60,
    }))
    toc, ordered = parse_toc(pages)
    sections = build_sections_from_toc(ordered, max(pages))
    entry = slice_dimension(pages, sections, toc, "D5", budget=50)
    assert entry["provided_pages"] == [3]
    assert entry["used_chars"] <= 50


def test_d3_keywords_include_risk_terms():
    """Live-run regression: without 风险/risk terms the D3 fallback missed the annual
    report's 「公司面临的风险和应对措施」 chapter, leaving regulatory_risk to web_search."""
    kw = DIMENSIONS["D3"]["keywords"]
    assert any("风险" in k for k in kw)
    assert any("关税" in k or "關稅" in k for k in kw)


def test_d6_keywords_cover_notes_that_carry_sotp_inputs():
    """Live-run regression: D6 needs 「在其他主体中的权益 / 分部报告」 pages for SOTP."""
    kw = DIMENSIONS["D6"]["keywords"]
    assert any("在其他主体中的权益" in k for k in kw)
    assert any("分部报告" in k for k in kw)


def test_render_slice_declares_coverage_and_forbids_inference():
    pages = _toy_report()
    toc, ordered = parse_toc(pages)
    sections = build_sections_from_toc(ordered, max(pages))
    entry = slice_dimension(pages, sections, toc, "D5", budget=120)
    text = render_slice(entry, pages, "TEST.HK", max(pages), 120, "slices/slice_manifest.json")
    assert "仅提供" in text
    assert "不得推断" in text
    assert "slice_manifest.json" in text
    assert f"## 页 {entry['provided_pages'][0]}" in text


def test_slice_header_reports_document_coverage_not_budget_fill():
    """「占全文 X%」必须是**文档覆盖率**，不是预算填充率（曾把 99% 误标为全文占比）。"""
    pages = _toy_report()
    toc, ordered = parse_toc(pages)
    sections = build_sections_from_toc(ordered, max(pages))
    entry = slice_dimension(pages, sections, toc, "D5", budget=120)
    total = max(pages)
    entry["used_chars"] = 120  # 预算填满 → 填充率 100%，若混淆就会写出"占全文 100%"
    text = render_slice(entry, pages, "TEST.HK", total, 120, "m.json")
    expected = len(entry["provided_pages"]) / total * 100
    assert f"占全文 {expected:.1f}%" in text
    assert "占全文 100.0%" not in text
    assert "填充 100.0%" in text


# ===== integration on the real reports in output/ =====


REAL_CASES = [
    ("002984.SZ", "output/002984_森麒麟"),
    ("00506.HK", "output/00506_中国食品"),
]


@pytest.mark.parametrize("code,out_dir", REAL_CASES)
def test_real_report_all_dimensions_fit_budget(code, out_dir):
    """On real annual reports every dimension slice must fit the budget."""
    directory = ROOT / out_dir
    txts = sorted(directory.glob("annual_report*.txt"))
    if not txts:
        pytest.skip(f"{out_dir} 无年报全文，跳过")
    pages = load_pages(txts[0].read_text(encoding="utf-8", errors="ignore"))
    toc, ordered = parse_toc(pages)
    sections = build_sections_from_toc(ordered, max(pages))
    budget = 12000
    for dim in DIMENSIONS:
        entry = slice_dimension(pages, sections, toc, dim, budget=budget)
        assert entry["used_chars"] <= budget, f"{code} {dim} 超预算"
        assert entry["provided_pages"], f"{code} {dim} 未产出切片"
        assert all(p >= 1 for p in entry["provided_pages"])
        assert not any(is_front_or_toc(pages[p]) for p in entry["provided_pages"])
        # 至少能填到预算的一半，否则说明定位失效
        assert entry["used_chars"] >= budget * 0.5, f"{code} {dim} 填充率过低"
