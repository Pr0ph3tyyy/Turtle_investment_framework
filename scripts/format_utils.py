"""Markdown output formatting utilities for Turtle Investment Framework.

All financial amounts are in millions RMB (raw yuan / 1e6).
"""

from __future__ import annotations

from typing import List, Optional


def format_number(value, divider: float = 1e6, decimals: int = 2) -> str:
    """Format a number: divide by divider, then comma-separate with decimals.

    Args:
        value: Raw number (e.g., 96886000000 yuan).
        divider: Divisor (default 1e6 to convert to millions).
        decimals: Decimal places (default 2).

    Returns:
        Formatted string, e.g., '96,886.00'.
        Returns '—' for None/NaN values.
    """
    if value is None:
        return "—"
    try:
        num = float(value) / divider
    except (TypeError, ValueError):
        return "—"
    # Check for NaN
    if num != num:
        return "—"
    return f"{num:,.{decimals}f}"


def escape_md_cell(text) -> str:
    r"""把单元格文本里的**裸竖线**转义成 ``\|``。

    为什么必须在**建表处**做（真实缺陷，2026-10-15）：
    `format_table` 原先把单元格原样拼进 `| a | b |`，于是任何含竖线的文本
    （如 `FCF = OCF - |Capex|` 的绝对值符号）会被下游渲染器**当成列分隔符** ——
    该行凭空多出 2 列，表格结构静默错位（实测：表头 6 列、该行 8 列）。

    更糟的是 `format_table` 自己会**补齐**不足的列，这恰好**掩盖**了
    "这一行本来就不该有这么多格"的信号 —— 补齐看起来像在容错，实际是在藏错。
    所以：**进表前转义**。
    """
    s = "" if text is None else str(text)
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            out.append("\\|")          # 已转义：原样保留，不重复加反斜杠
            i += 2
            continue
        if ch == "|":
            out.append("\\|")          # 裸竖线：转义
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def format_table(headers: List[str], rows: List[List[str]],
                 alignments: Optional[List[str]] = None) -> str:
    """Generate a markdown table string.

    Args:
        headers: Column header strings.
        rows: List of rows, each a list of cell strings.
        alignments: Per-column alignment ('l', 'r', 'c'). Defaults to left.

    Returns:
        Markdown table string.

    所有单元格经 :func:`escape_md_cell` 处理：含竖线的文本（`|Capex|`）不会
    把表格拆坏。**行超出表头列数**是调用方的 bug（两个逻辑列挤进一行），
    按 fail-closed 显式提示而不是静默截断或静默补齐。
    """
    if not headers:
        return ""

    n_cols = len(headers)
    if alignments is None:
        alignments = ["l"] * n_cols

    # Build separator row
    sep_parts = []
    for a in alignments:
        if a == "r":
            sep_parts.append("---:")
        elif a == "c":
            sep_parts.append(":---:")
        else:
            sep_parts.append("---")

    lines = []
    lines.append("| " + " | ".join(escape_md_cell(h) for h in headers) + " |")
    lines.append("| " + " | ".join(sep_parts) + " |")
    for row in rows:
        if len(row) > n_cols:
            # 静默截断会丢数据；静默补齐会藏错。两者都不做，只显式提示。
            import sys as _sys
            print(f"  ⚠️ format_table: 行有 {len(row)} 格但表头只有 {n_cols} 列，"
                  f"已按表头截断（检查是否混入了未转义的分隔符）：{row[:3]}", file=_sys.stderr)
        padded = list(row) + [""] * (n_cols - len(row))
        lines.append("| " + " | ".join(
            escape_md_cell(c) if c is not None else "—" for c in padded[:n_cols]) + " |")

    return "\n".join(lines)


def format_header(level: int, text: str) -> str:
    """Generate a markdown header.

    Args:
        level: Header level (1-6).
        text: Header text.

    Returns:
        Markdown header string, e.g., '## Section Title'.
    """
    level = max(1, min(6, level))
    return "#" * level + " " + text
