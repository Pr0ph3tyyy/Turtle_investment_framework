"""渲染层的**结构**校验（P7）—— 替代"肉眼比版式"。

为什么需要
----------
本环境里 LibreOffice 栅格化预览输出空白页 ⇒ **版式无法用截图验证**。
但"表格列数错位"这类缺陷不需要看图就能查：它表现为
**同一表格里某一行的单元格数 ≠ 表头列数**。

真实缺陷（2026-10-15）：报告里有含竖线的文本（`|HH|`），写入时转义成 `\\|HH\\|`，
而渲染器用朴素的 `strip("|").split("|")` 拆行 ⇒ 该行变 3 个单元格 ⇒
HTML 表格结构错位，且**渲染器不校验列数，所以是静默的**。
"""

import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from report_to_html_reading import _split_md_row  # noqa: E402


class TestEscapeAwareSplitting:
    def test_plain_row(self):
        assert _split_md_row("| a | b | c |") == ["a", "b", "c"]

    def test_escaped_pipe_stays_one_cell(self):
        """核心回归：`\\|HH\\|` 必须是**一个**单元格。"""
        assert _split_md_row(r"| x | \|HH\| | y |") == ["x", "|HH|", "y"]

    def test_ragged_row_detected_not_padded(self):
        """不等长行必须**如实**保留长度差（渲染层据此判缺陷，而不是补齐掩盖）。"""
        assert len(_split_md_row("| a | b |")) == 2
        assert len(_split_md_row(r"| a | \|x\| | b |")) == 3

    def test_no_leading_or_trailing_empty_cells(self):
        assert _split_md_row("| a | b |") == ["a", "b"]


def _md_tables(text: str):
    """把 markdown 表格切成 (header_cells, [row_cells...])，转义感知。"""
    out, block = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            if set(s.replace("|", "").replace(" ", "")) <= set("-:"):
                continue
            block.append(_split_md_row(s))
        elif block:
            if len(block) >= 2:
                out.append((block[0], block[1:]))
            block = []
    if len(block) >= 2:
        out.append((block[0], block[1:]))
    return out


class TestReportTablesAreRectangular:
    """报告与数据包的每张表都必须"矩形"（除有意合并的表头）。"""

    @pytest.mark.parametrize("rel", [
        "output/_legacy/00506_中国食品/data_pack_market.md",
    ])
    def test_pack_tables_have_uniform_columns(self, rel):
        p = ROOT / rel
        if not p.is_file():
            pytest.skip(f"{rel} 不在工作区")
        bad = []
        for header, rows in _md_tables(p.read_text(encoding="utf-8")):
            for r in rows:
                if len(r) != len(header):
                    bad.append(f"表头 {len(header)} 列 vs 行 {len(r)} 列：{r[:3]}")
        assert not bad, "表格行宽不一致（渲染会错位）：\n  " + "\n  ".join(bad[:10])


class TestRenderersDoNotSilentlyAbsorbRaggedness:
    def test_reading_renderer_uses_escape_aware_split(self):
        import io as _io
        import tokenize
        import report_to_html_reading as R
        src = pathlib.Path(R.__file__).read_text(encoding="utf-8")
        assert "_split_md_row" in src

        # 只查**真实代码**里的朴素写法。注意两点（都是实测踩出来的）：
        #   ① 用 tokenizer 而非"行首是不是引号" —— docstring 续行不以引号开头；
        #   ② 不能用 tokens 拼字符串去找模式 —— `strip("|").split("|")` 会被切成
        #      多个 token，`tok.string` 上永远匹配不到（该错误使断言形同虚设）。
        # 正确做法：在**原始源码**里找模式，再判断该位置是否落在代码区。
        covered = []
        for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                (srow, scol), (erow, ecol) = tok.start, tok.end
                covered.append(((srow, scol), (erow, ecol)))
        needle = 'strip("|").split("|")'
        offenders = []
        for ln, line in enumerate(src.splitlines(), start=1):
            col = line.find(needle)
            if col < 0:
                continue
            pos = (ln, col)
            if any(a <= pos < b for a, b in covered):
                continue                       # 在 docstring/注释里（反例引用）⇒ 不算违规
            offenders.append(line.strip())
        assert not offenders, (
            "仍在用朴素拆行 —— `\\|` 转义会被拆成多列，导致表格静默错位："
            + " | ".join(offenders)
        )
