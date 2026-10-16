#!/usr/bin/env python3
"""HK FX plumbing: trading currency → reporting currency.

Why this module exists
----------------------
The collector labels a stock's financial statements by **ticker suffix**
(``_detect_currency``: ``.HK`` → HKD). But Tushare's ``hk_income`` /
``hk_balancesheet`` / ``hk_cashflow`` return values in the issuer's **reporting
currency** — for mainland A+H / red-chip issuers that is **CNY**, and their
annual reports say so explicitly ("The presentation currency … was Renminbi").

Consequence: ``R = 净利润(CNY) / 市值(HKD)`` mixes two currencies. Measured on
06862.HK (FY2024): pack 7.225% vs correct 8.357% — an understatement of
``1/0.86466 − 1 = 15.65%``. See ``known_issues.md`` P0-2.

Resolution order (mirrors ``rf_utils`` / ``--rf`` so the two behave alike):

1. ``--fx-hkd-cny`` on the command line
2. ``TUSHARE_FX_HKD_CNY`` environment variable
3. ``{output_dir}/fx.json`` — the option-A contract, written by the
   coordinator/agent from the annual report or the CFETS central parity::

       {"factor_to_reporting": 0.86466,          # trading → reporting (preferred)
        "date": "2026-08-21",
        "source": "CFETS中间价(新华社)",
        "reporting_currency": "CNY",
        "trading_currency": "HKD"}

   ``hkd_cny`` is accepted as a convenience alias for ``factor_to_reporting``.
4. The ``*[协调器注 …]*`` note already embedded in an existing data pack
   (several packs record ``汇率 HKD/CNY = 0.86476``), so a hand-supplied rate
   is not lost when the pack is regenerated.

Nothing is guessed: when no rate can be resolved the caller must degrade
**audibly** rather than silently mixing currencies.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

# Plausible band for HKD→CNY (roughly 0.75–1.05 in recent history; kept wide).
_FX_MIN, _FX_MAX = 0.5, 1.5

# 汇率 HKD/CNY = 0.86476  /  汇率 HKD/CNY = 0.8648
_NOTE_RE = re.compile(r"汇率\s*HKD\s*/\s*CNY\s*=\s*([0-9]+(?:\.[0-9]+)?)")
_NOTE_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def is_plausible(rate: Optional[float]) -> bool:
    """True when ``rate`` looks like a real HKD→CNY factor."""
    return rate is not None and _FX_MIN < rate < _FX_MAX


def load_fx_json(output_dir: str | Path
                 ) -> Tuple[Optional[float], Optional[str], Optional[str], Optional[str]]:
    """Read ``fx.json`` from ``output_dir``.

    Returns ``(factor, date, source, reporting_currency)``; all-``None`` when the
    file is absent or unusable (unreadable / malformed / implausible value).

    ``reporting_currency`` must be propagated, not dropped: it is what lets the
    pack label statement amounts correctly instead of guessing HKD from the
    ticker suffix (known_issues.md P0-2 / §3.20 层1).
    """
    path = Path(output_dir) / "fx.json"
    if not path.exists():
        return None, None, None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"  ⚠️ fx.json 无法解析（{e}），忽略", file=sys.stderr)
        return None, None, None, None

    raw = data.get("factor_to_reporting", data.get("hkd_cny"))
    try:
        factor = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        factor = None
    if not is_plausible(factor):
        print(f"  ⚠️ fx.json 的 factor_to_reporting={raw!r} 不在合理区间 "
              f"({_FX_MIN}, {_FX_MAX})，忽略", file=sys.stderr)
        return None, None, None, None

    date = str(data.get("date") or "") or None
    source = str(data.get("source") or "") or None
    reporting = data.get("reporting_currency")
    reporting = str(reporting).upper() if reporting else None
    if reporting == "HKD":
        # 真正的港元报表发行人：无需换算
        return 1.0, date, (source or "") + "（reporting=HKD，无需换算）", "HKD"
    return factor, date, source, reporting


def parse_fx_from_data_pack(text: str) -> Tuple[Optional[float], Optional[str]]:
    """Parse ``(rate, date)`` from a coordinator note inside a data pack.

    Example line (2026-08 packs)::

        ④ 汇率 HKD/CNY = 0.86476（2026-08-20 央行中间价）

    Returns ``(None, None)`` when no plausible rate is present.
    """
    if not text:
        return None, None
    for match in _NOTE_RE.finditer(text):
        try:
            rate = float(match.group(1))
        except ValueError:
            continue
        if not is_plausible(rate):
            continue
        window = text[match.end():match.end() + 80]
        date_match = _NOTE_DATE_RE.search(window)
        date_str = "-".join(date_match.groups()) if date_match else None
        return rate, date_str
    return None, None


def _reporting_currency_from_fx_json(data_pack_path) -> Optional[str]:
    """Read only the *reporting-currency declaration* from ``fx.json``.

    报表币种是**发行人属性**，与用哪条路径拿到汇率无关。CLI/env 显式给汇率时若一并丢掉
    这个声明，`_unit_label()` 会退回「币种未确认」，整份数据包的单位标签随之失去意义
    （实测 2026-10-13：`--fx-hkd-cny 0.85849` + fx.json 已声明 CNY，产物却写「币种未确认」）。
    """
    if not data_pack_path:
        return None
    try:
        _factor, _date, _source, reporting = load_fx_json(Path(data_pack_path).parent)
        return reporting
    except Exception:
        return None


def resolve_fx_hkd_cny(cli_value: Optional[float],
                       data_pack_path: str | Path | None = None
                       ) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """Resolve the trading→reporting factor.

    Returns ``(factor, source_label, reporting_currency)``. The reporting currency
    is ``None`` when it was not explicitly confirmed (CLI/env/pack-note paths carry
    no currency), which callers must treat as "unknown" rather than defaulting to
    the ticker suffix.

    CLI / env win over files so a manual value is never lost to a stale artefact.
    """
    if cli_value is not None:
        if not is_plausible(cli_value):
            print(f"Error: --fx-hkd-cny {cli_value} 不在合理区间 "
                  f"({_FX_MIN}, {_FX_MAX})", file=sys.stderr)
            sys.exit(1)
        return cli_value, "--fx-hkd-cny", _reporting_currency_from_fx_json(data_pack_path)

    env_val = None
    try:
        import os
        env_val = os.environ.get("TUSHARE_FX_HKD_CNY")
    except Exception:  # pragma: no cover - os is always importable
        env_val = None
    if env_val:
        try:
            value = float(env_val)
        except ValueError:
            print(f"Error: TUSHARE_FX_HKD_CNY='{env_val}' 不是数字", file=sys.stderr)
            sys.exit(1)
        if not is_plausible(value):
            print(f"Error: TUSHARE_FX_HKD_CNY={value} 不在合理区间 "
                  f"({_FX_MIN}, {_FX_MAX})", file=sys.stderr)
            sys.exit(1)
        return (value, "TUSHARE_FX_HKD_CNY env",
                _reporting_currency_from_fx_json(data_pack_path))

    if data_pack_path:
        path = Path(data_pack_path)
        # 4a: fx.json sitting next to the data pack
        factor, date, source, reporting = load_fx_json(path.parent)
        if factor is not None:
            label = f"fx.json ({date})" if date else "fx.json"
            if source:
                label += f" — {source}"
            return factor, label, reporting
        # 4b: rate already recorded in the pack's coordinator note
        if path.exists():
            try:
                rate, date_str = parse_fx_from_data_pack(path.read_text(encoding="utf-8"))
            except OSError as e:
                print(f"  ⚠️ 无法读取数据包以复用汇率: {e}", file=sys.stderr)
                rate, date_str = None, None
            if rate is not None:
                label = f"数据包协调器注 ({date_str})" if date_str else "数据包协调器注"
                return rate, label, None

    return None, None, None


def make_fx_store_entry(factor: float, date: Optional[str] = None,
                        source: Optional[str] = None,
                        reporting_currency: Optional[str] = None) -> dict:
    """Build the ``_store["fx"]`` payload consumers read.

    ``factor_to_reporting`` means: **multiply a trading-currency amount by this
    to obtain the reporting currency**.

    ``reporting_currency`` MUST be carried through when known: it is what
    ``InfrastructureMixin._unit_label()`` uses to decide between "百万元" and
    "百万（币种未确认）". Dropping it here silently reverted the label to the
    ticker-suffix guess — observed on a live run 2026-10-13.
    """
    entry = {
        "factor_to_reporting": float(factor),
        "date": date,
        "source": source,
    }
    if reporting_currency:
        entry["reporting_currency"] = reporting_currency
    return entry
