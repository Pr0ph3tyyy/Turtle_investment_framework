#!/usr/bin/env python3
"""Risk-free-rate plumbing shared by the collector and the assembly layer.

Why this module exists
----------------------
§17.9 / §17.11 / §17.13 (业绩下滑敏感性、穿透回报率网格、收入敏感性) all need a
risk-free rate. The rate only entered ``_store["risk_free_rate"]`` when the
``yc_cb`` endpoint returned data, so on an account without that permission the
three blocks silently disappeared — even when the coordinator had already
supplied the rate in §14 via WebSearch, because nothing ever read it back from
the data pack.

This module provides:
- :func:`parse_rf_from_data_pack` — read a usable rate back out of a data pack
- :func:`rf_text_is_failure` — tell "no permission / missing" text apart from
  a real value (a failed fetch returns a *string*, it does not raise, which is
  why the exception handler in ``refresh_market_sections`` never fired)
- :func:`make_rf_dataframe` — build the single-row frame ``_store`` expects
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

import pandas as pd

# Plausible range for a 10-year government bond yield (%)
_RF_MIN, _RF_MAX = 0.0, 15.0

_SECTION_RE = re.compile(r"^##\s+14\.\s.*$", re.MULTILINE)
_NEXT_SECTION_RE = re.compile(r"^##\s+\d+[A-Z]?\.\s", re.MULTILINE)
# | 20260509 | 1.7670 |  → (date, value)
_TABLE_ROW_RE = re.compile(r"\|\s*(\d{8})\s*\|\s*([0-9]+(?:\.[0-9]+)?)\s*\|")
_PCT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%")
_DATE_RE = re.compile(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})")

_FAILURE_MARKERS = ("数据缺失", "无权限", "暂不支持", "不支持")


def extract_section_14(text: str) -> str:
    """Return the text of the ``## 14.`` section (empty string when absent)."""
    if not text:
        return ""
    start_match = _SECTION_RE.search(text)
    if not start_match:
        return ""
    rest = text[start_match.end():]
    next_match = _NEXT_SECTION_RE.search(rest)
    section = rest[: next_match.start()] if next_match else rest
    return start_match.group(0) + "\n" + section


def _plausible(value: Optional[float]) -> bool:
    return value is not None and _RF_MIN < value < _RF_MAX


def parse_rf_from_section(section_text: str) -> Tuple[Optional[float], Optional[str]]:
    """Parse ``(yield_pct, trade_date)`` from a §14 section body.

    Preference order:
    1. An API table row ``| YYYYMMDD | <yield> |`` (authoritative).
    2. The first percentage in the text (manual/WebSearch supplement).
    Returns ``(None, None)`` when nothing usable is present.
    """
    if not section_text:
        return None, None

    rows = _TABLE_ROW_RE.findall(section_text)
    for date_str, value_str in rows:
        try:
            value = float(value_str)
        except ValueError:
            continue
        if _plausible(value):
            return value, date_str

    for pct_match in _PCT_RE.finditer(section_text):
        try:
            value = float(pct_match.group(1))
        except ValueError:
            continue
        if not _plausible(value):
            continue
        # Prefer the date sitting right before the figure ("... 2026-08-22 收盘
        # **1.688%**") over an earlier unrelated date such as the date on which
        # a coordinator wrote the supplement.
        window = section_text[max(0, pct_match.start() - 60):pct_match.start()]
        near_dates = _DATE_RE.findall(window)
        if near_dates:
            date_str = "".join(near_dates[-1])
        else:
            date_match = _DATE_RE.search(section_text)
            date_str = "".join(date_match.groups()) if date_match else None
        return value, date_str

    return None, None


def parse_rf_from_data_pack(text: str) -> Tuple[Optional[float], Optional[str]]:
    """Parse ``(yield_pct, trade_date)`` from a whole data-pack document."""
    return parse_rf_from_section(extract_section_14(text))


def rf_text_is_failure(text: str) -> bool:
    """True when §14 text carries no usable value (missing / no permission)."""
    value, _ = parse_rf_from_section(text)
    if value is not None:
        return False
    return any(marker in (text or "") for marker in _FAILURE_MARKERS)


def make_rf_dataframe(value: float, trade_date: Optional[str] = None) -> pd.DataFrame:
    """Build the single-row frame ``_store["risk_free_rate"]`` consumers expect.

    Consumers read ``df.iloc[0].get("yield")`` (newest first), so one row is
    sufficient; the schema matches the ``yc_cb`` response (trade_date, yield).
    """
    date_str = trade_date or pd.Timestamp.now().strftime("%Y%m%d")
    return pd.DataFrame([{"trade_date": str(date_str), "yield": float(value)}])
