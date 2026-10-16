"""精华长图渲染器的机械不变量（不含 Chrome 渲染）。

这一组盯的是「装上以后才会暴露」的那类问题：claim 标记没剥干净、中文标点前多了
空格、分P 把一行字切断、成果类型被正文关键词误导、结论词取到了百分比。

渲染本身（Chrome 无头 + 截图）不在单测范围内 —— 它慢且依赖本机浏览器；
`plugins/dsh-share-longimage/tests/host.test.mjs` 用假解释器覆盖了编排与结果投影。
"""

import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
RENDERER = ROOT / "plugins" / "dsh-share-longimage" / "assets" / "render_longimage.py"


def _load():
    spec = importlib.util.spec_from_file_location("share_longimage", RENDERER)
    assert spec and spec.loader, f"cannot load {RENDERER}"
    module = importlib.util.module_from_spec(spec)
    # dataclasses 会去 sys.modules[cls.__module__] 查命名空间，必须先登记再执行
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rl = _load()


# --------------------------------------------------------------- markdown layer


def test_claim_tags_are_stripped_to_plain_values():
    """报告里到处是 <claim id="…" policy="…">4.66%</claim>，长图上只能是 4.66%。"""
    raw = '悲观档 <claim id="cm17.14.k080.ggpess" policy="round:2">4.66%</claim>，买点 8.67'
    assert rl.strip_claims(raw) == "悲观档 4.66%，买点 8.67"
    assert "<" not in rl.inline(raw) and "claim" not in rl.inline(raw)


def test_inline_markdown_keeps_cjk_and_drops_links():
    # 链接只留文字；抽掉链接后汉字之间多出来的空格一并收紧
    assert rl.inline("见 [年报](http://x) 第 24 页") == "见年报第 24 页"
    assert "<b>" in rl.inline("**加粗**")


def test_cjk_spacing_and_punctuation_are_tightened():
    """行内换行折叠成空格后，「亿元 ，2022」这种空格必须被消掉。"""
    assert rl.norm("广义净现金 82.99 亿元\n，2022 年") == "广义净现金 82.99 亿元，2022 年"
    assert rl.norm("广 义 净 现 金") == "广义净现金", "汉字之间的空格是抽取痕迹"
    assert rl.norm("（ 测试）") == "（测试）"
    # 汉字↔数字/拉丁的边界必须原样保留
    assert rl.norm("5 年 CAGR 与 A 股") == "5 年 CAGR 与 A 股"


def test_short_value_drops_long_parenthetical():
    assert rl.short_value("KK = −0.34 pct（决策口径 = GG_pess − II）") == "KK = −0.34 pct"
    assert rl.short_value("高（5 项判据中 2 项「存在」）") == "高"
    assert rl.short_value("低（1 项高 / 1 项中 / 3 项低）") == "低"
    assert rl.short_value("4,049.82 百万元") == "4,049.82 百万元"


def test_first_sentence_does_not_dangle_a_semicolon():
    out = rl.first_sentence("甲；乙；丙", 60)
    assert out.endswith("。"), out


def test_split_sections_ignores_headings_inside_code_fences():
    md = "# 标题\n\n```\n## 这是代码里的假标题\n```\n\n## 真标题\n正文\n"
    keys = [s.key for s in rl.split_sections(md) if s.level]
    assert keys == ["标题", "真标题"]


def test_kv_from_box_reads_the_ascii_summary_block():
    box = """
═══════════════
  精算 GG = 5.82%（中性对照）
  门槛 II = 5.00%
  仓位建议：不建仓
═══════════════
"""
    kv = rl.kv_from_box(box)
    assert kv["仓位建议"] == "不建仓"
    assert kv["门槛 II"] == "5.00%"


# ------------------------------------------------------------------ report kind


def test_detect_kind_prefers_filename_over_body_keywords():
    """回归：正文里引用「定性分析」会把整份分析报告误判成定性档而掉进降级路径。"""
    md = "# 龟龟投资策略 · 分析报告：招商银行（600036.SH）\n\n> 承接 qualitative_report.md 的定性分析结论\n"
    kind, _ = rl.detect_kind(pathlib.Path("招商银行_600036.SH_分析报告.md"), md)
    assert kind == "analysis"


def test_detect_kind_recognises_each_flavour():
    cases = {
        "海底捞_06862_分析报告.md": "analysis",
        "某公司_300001_估值报告.md": "valuation",
        "qualitative_report.md": "qualitative",
    }
    for filename, expected in cases.items():
        kind, _ = rl.detect_kind(pathlib.Path(filename), "# 标题\n")
        assert kind == expected, filename


def test_derive_short_name_uses_the_directory_not_the_full_legal_name():
    doc = rl.Doc(company="海底捞国际控股有限公司")
    assert rl.derive_short_name(pathlib.Path("/x/output/06862_海底捞"), doc) == "海底捞"
    assert rl.derive_short_name(pathlib.Path("/x/output/600941_中国移动"), doc) == "中国移动"


# ------------------------------------------------------------------ panel split


def test_pack_panels_never_splits_a_block():
    blocks = [{"i": i, "t": i * 500, "h": 500} for i in range(6)]
    panels = rl.pack_panels(blocks, total=3000, panel_h=1200)
    assert panels, "应当切出分P"
    # 每个分P的高度都是整块之和，且首尾相接、完整覆盖
    assert panels[0][0] == 0
    assert sum(height for _, height in panels) == 3000
    for offset, height in panels:
        assert height % 500 == 0, f"分P 高度 {height} 不是区块高度的整数倍 ⇒ 切断了内容"
        assert height <= 1200 or height == 500 * 2, f"分P 高度 {height} 超出目标且不是单块"


def test_pack_panels_gives_an_oversized_block_its_own_panel():
    blocks = [
        {"i": 0, "t": 0, "h": 400},
        {"i": 1, "t": 400, "h": 3000},  # 单块就超过 panel_h，只能独占
        {"i": 2, "t": 3400, "h": 400},
    ]
    panels = rl.pack_panels(blocks, total=3800, panel_h=1440)
    assert (400, 3000) in panels, panels
    assert sum(height for _, height in panels) == 3800


def test_pack_panels_handles_empty_geometry():
    assert rl.pack_panels([], total=0, panel_h=1440) == []
    assert rl.pack_panels([], total=99, panel_h=1440) == [(0, 99)]


# ------------------------------------------------------------------- typography


def test_longimage_type_scale_stays_readable_on_a_phone():
    """B 站长图在手机上按屏宽缩放：1080 宽下正文必须够大，否则读者要放大。"""
    css = rl.render_css("paper")
    body = rl.re.search(r"\nbody\{(.*?)\n\}", css, rl.re.S)
    assert body, "找不到 body 规则"
    size = int(rl.re.search(r"font-size:(\d+)px", body.group(1)).group(1))
    assert size >= 28, f"正文字号 {size}px 在 1080 宽的长图里手机上会太小"
    assert "line-height:1.8" in body.group(1).replace(" ", ""), "中文正文需要 1.8 以上的行距"
    assert "inter-ideograph" in css, "中文长句应当两端对齐"


def test_both_themes_define_every_token_the_css_uses():
    for theme in rl.THEMES:
        css = rl.render_css(theme)
        for token in ("paper", "card", "ink", "hair", "clay", "muted"):
            assert f"--{token}:" in css, f"{theme} 缺少 --{token}"


# --------------------------------------------------------------------- assembly


MINIMAL_REPORT = """# 龟龟投资策略 · 分析报告：示例公司（000001.SZ）

## 报告元信息

| 项目 | 内容 |
|:--|:--|
| 分析日期 | 2026-01-01 |
| 最新股价 | 10.00 元 |

## Executive Summary

**一句话结论**：示例公司 — **不建仓**。

| 指标 | 数值 | 说明 |
|:--|:--|:--|
| 门槛值 II | **5.00%** | 下限生效 |
| 安全边际 | **KK = -0.34 pct**（决策口径） | GG_pess = <claim id="x" policy="round:2">4.66%</claim> |
| 仓位建议 | **不建仓** | KK < 0 |

## 风险提示

### 特定风险

- 风险一
- 风险二
"""


def test_building_blocks_from_a_thin_report_never_crashes(tmp_path):
    """缺 sidecar、缺六维、缺估值表时应当整块略去，而不是抛异常或留空壳。"""
    report = tmp_path / "示例公司_000001.SZ_分析报告.md"
    report.write_text(MINIMAL_REPORT, encoding="utf-8")
    doc = rl.Doc(company="示例公司", code="000001.SZ", short_name="示例公司")
    sections = rl.split_sections(MINIMAL_REPORT, 3)
    rl.build_analysis(MINIMAL_REPORT, doc, tmp_path, sections)
    blocks = rl.build_blocks(doc)
    kinds = [b.kind for b in blocks]
    assert kinds[0] == "hero"
    assert "footer" in kinds
    assert doc.verdict_word == "不建仓"
    assert any(k["value"] == "5.00%" for k in doc.kpis), doc.kpis
    assert all(b.html.strip() for b in blocks), "不允许渲染出空区块"


def test_verdict_word_falls_back_to_the_bold_action_in_the_one_liner(tmp_path):
    """回归：「仓位建议」列只给百分比时，结论词要从一句话结论的加粗片段里取动作词。"""
    md = MINIMAL_REPORT.replace("| 仓位建议 | **不建仓** | KK < 0 |", "| 仓位建议 | **70%（标准仓位上限）** | KK≥1.5pct |")
    md = md.replace("**一句话结论**：示例公司 — **不建仓**。", "**一句话结论**：示例公司 — **买入（70% 标准仓位）**")
    doc = rl.Doc(company="示例公司", code="000001.SZ", short_name="示例公司")
    rl.build_analysis(md, doc, tmp_path, rl.split_sections(md, 3))
    assert doc.verdict_word == "买入", doc.verdict_word
    assert doc.verdict_extra == "70%", doc.verdict_extra


def test_gates_read_sidecars_instead_of_guessing(tmp_path):
    (tmp_path / "invariants.json").write_text(json.dumps({
        "pack": "x", "findings": [
            {"code": "I1", "status": "PASS"}, {"code": "I4", "status": "FAIL"},
        ],
    }), encoding="utf-8")
    (tmp_path / "claims.json").write_text(json.dumps({"claims": [{"id": "a"}, {"id": "b"}]}), encoding="utf-8")
    (tmp_path / "section17_status.json").write_text(json.dumps({
        "sections": {"17.1": {"status": "ok"}, "17.13": {"status": "missing"}},
    }), encoding="utf-8")
    gates = {g["label"]: g for g in rl.collect_gates(tmp_path, "")}
    assert gates["机构不变量"]["value"] == "1 / 2 PASS"
    assert "I4" in gates["机构不变量"]["note"]
    assert gates["机构不变量"]["tone"] == "amber"
    assert gates["claim 账本"]["value"] == "2 条已登记"
    assert gates["§17 小节状态"]["value"] == "1 / 2 ok"


def test_brief_overrides_only_the_fields_it_declares(tmp_path):
    report = tmp_path / "示例公司_000001.SZ_分析报告.md"
    report.write_text(MINIMAL_REPORT, encoding="utf-8")
    doc = rl.Doc(company="示例公司", code="000001.SZ", short_name="示例公司")
    rl.build_analysis(MINIMAL_REPORT, doc, tmp_path, rl.split_sections(MINIMAL_REPORT, 3))
    before_kpis = list(doc.kpis)
    touched = rl.apply_brief(doc, {"title": "自定义标题", "verdict_line": "自定义结论解释。"})
    assert doc.company == "自定义标题"
    assert doc.verdict_line == "自定义结论解释。"
    assert doc.kpis == before_kpis, "没被 brief 覆盖的字段不应被清空"
    assert set(touched) == {"title", "verdict_line"}


@pytest.mark.parametrize("theme", sorted(rl.THEMES))
def test_build_html_is_self_contained(theme):
    doc = rl.Doc(company="示例公司", code="000001.SZ", kind_label="投资分析")
    html = rl.build_html(doc, rl.build_blocks(doc), theme, 1080)
    assert html.startswith("<!DOCTYPE html>")
    assert "http://" not in html and "https://" not in html, "长图必须自包含（不拉外部资源）"
    assert "data-h" in html or "MEASURE" in html or "scrollHeight" in html, "缺少高度测量脚本"
    assert f'width:1080px' in html


class TestClaimScrubIsBacktickSafe:
    """洗 claim 标记的正则**不得**把正文里「引用 token 形态本身」的字面提及当成真标签。

    实测事故（2026-10-16，06078）：报告用反引号引用 token 形态做说明，例如
    `` `<claim id="…">` ``、`` `<claim>` ``。旧模式 `<claim\\b[^>]*>(.*?)</claim>` 会匹配这些
    **没有闭标签**的提及，于是非贪婪匹配一路吃到**十几行之后**某个真标签的 `</claim>`：
      · 06078 实测跨行配对 `行841→行860`、`行884→行931`；
      · 被吞噬区间内的**正文被整体替换**，且产物里留下未闭合的 `<claim …>`；
      · 数字守恒检查通不出问题（标签内数字被剥离属预期），**只能靠标签配对/跨行检查发现**。
    修复：模式要求 `id="…"`（真 claim 必有 id，字面提及没有），并清理正文里的标签字面量。
    """

    def test_pattern_requires_id(self):
        import pathlib, re
        for f in ("report_to_html_reading.py", "report_to_docx.py"):
            src = (ROOT / "scripts" / f).read_text(encoding="utf-8")
            m = re.search(r"^CLAIM_RE\s*=\s*re\.compile\((.*)$", src, re.M)
            assert m, f"{f} 找不到 CLAIM_RE"
            assert r'\bid="' in m.group(1), (
                f"{f} 的 CLAIM_RE 未要求 id= ⇒ 会把正文里的字面提及当标签，跨行吞噬正文")

    def test_scrub_leaves_no_tag_residue_and_no_cross_line_pairs(self):
        import re, sys, pathlib
        sys.path.insert(0, str(ROOT / "scripts"))
        from report_to_html_reading import CLAIM_RE, strip_claims
        report = ROOT / "output" / "06078_海吉亚医疗" / "海吉亚医疗_06078_分析报告.md"
        if not report.is_file():
            import pytest
            pytest.skip("未找到 06078 报告")
        text = report.read_text(encoding="utf-8")
        cross = [m for m in CLAIM_RE.finditer(text)
                 if text[:m.start()].count("\n") != text[:m.end()].count("\n")]
        assert not cross, f"存在跨行配对（会吞噬正文）：{[(text[:m.start()].count(chr(10))+1, text[:m.end()].count(chr(10))+1) for m in cross]}"
        out = strip_claims(text)
        assert "<claim" not in out and "</claim>" not in out, "洗标记后仍有残留标签"
