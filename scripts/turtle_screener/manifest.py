"""交付清单：`shortlist.json` / `run_manifest.json` / `excluded.json`。

设计要点（`screener_daily_automation_design.md` §4 / §5 / §6）
----------------------------------------------------------
1. **`shortlist.json` 是 screener 与龟龟分析之间唯一的契约**，不是给人读的 CSV。
   每只股票带 `action`，协调器据此分派快路径/慢路径。
2. **按 `trade_date` 幂等**：重跑覆盖同一目录，绝不追加第二份。
   依据是 DSH schedule 的工具契约原文 —— "Delivery can repeat after a crash"。
3. **写盘原子**（`.tmp` + `os.replace`），杜绝半写被下游读到。
4. **门禁裁决走 `gate_policy` 单点**：只有全局输入缺口（`rf_missing` /
   `calendar_missing` / `market_data_not_ready`）是 blocking；
   逐股数据缺口是"合格地排除"，进 `excluded.json` 而不阻断交付。

刷新策略（已定参数）
------------------
财报期未变 **且** 价格变动 < 10% ⇒ 只刷新 §17 价格相关小节（`refresh_only`）；
否则重跑完整 Workflow 2（`rerun_turtle`）；没有定性报告 ⇒ 走慢路径（`needs_workflow1`）。
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import gate_policy

from turtle_core.reasons import describe, label_of, require

from .engine import RunResult, ScreenRow

__all__ = [
    "SCHEMA_VERSION",
    "PRICE_REFRESH_THRESHOLD_PCT",
    "run_dir",
    "decide_action",
    "build_shortlist",
    "build_excluded",
    "build_run_manifest",
    "verdict_for",
    "results_csv",
    "write_run",
]

SCHEMA_VERSION = "1.0"

#: 价格变动超过此百分比 ⇒ 报告过期，需重跑（已定参数）
PRICE_REFRESH_THRESHOLD_PCT = 10.0

#: 逐股原因码 → action 的三档
ACTION_REFRESH = "refresh_only"
ACTION_RERUN = "rerun_turtle"
ACTION_WORKFLOW1 = "needs_workflow1"


def run_dir(base: str | os.PathLike[str], trade_date: str) -> Path:
    """``{base}/screener/{trade_date}`` —— 按交易日一目录（幂等的单位）。"""
    d = str(trade_date).replace("-", "")
    return Path(base) / "screener" / d


def decide_action(existing: Optional[Mapping[str, Any]],
                  snapshot_fy_end: Optional[str],
                  close: Optional[float]) -> str:
    """决定该股要跑多深。``existing`` 为 ``{report_path, fy_end, last_price, last_analyzed_at}``。"""
    if not existing or not existing.get("report_path"):
        return ACTION_WORKFLOW1
    if (existing.get("fy_end") or "") != (snapshot_fy_end or ""):
        return ACTION_RERUN
    last = existing.get("last_price")
    if last and close:
        try:
            if abs(close / float(last) - 1.0) * 100.0 >= PRICE_REFRESH_THRESHOLD_PCT:
                return ACTION_RERUN
        except (TypeError, ValueError, ZeroDivisionError):
            return ACTION_RERUN
    return ACTION_REFRESH


def build_shortlist(result: RunResult,
                    as_of: str,
                    trade_dates: Optional[Mapping[str, str]] = None,
                    rf_pct: Optional[float] = None,
                    existing: Optional[Mapping[str, Mapping[str, Any]]] = None,
                    snapshot_fy_end: Optional[Mapping[str, str]] = None,
                    n_max: Optional[int] = None) -> dict:
    """构造交接契约。含 `action` 与 `needs_workflow1`，协调器据此分派。"""
    existing = existing or {}
    snapshot_fy_end = snapshot_fy_end or {}
    entries: list[dict] = []
    for r in result.shortlist:
        ex = existing.get(r.ts_code)
        action = decide_action(ex, snapshot_fy_end.get(r.ts_code), r.close)
        entries.append({
            "rank": r.rank,
            "ts_code": r.ts_code,
            "name": r.name,
            "market": r.market,
            "close": r.close,
            "trading_currency": r.currency,
            "gg_pess_pct": r.gg_pess,
            "ii_pct": r.ii,
            "margin_pct": r.margin,
            "score": r.score,
            "adopted_k": r.adopted_k,
            "market_cap_yuan": r.market_cap_yuan,
            "snapshot_fy_end": snapshot_fy_end.get(r.ts_code),
            "reason_codes": list(r.reasons),
            "action": action,
            "needs_workflow1": action == ACTION_WORKFLOW1,
            "existing_report": (ex or {}).get("report_path"),
            "last_analyzed_at": (ex or {}).get("last_analyzed_at"),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of,
        "trade_dates": dict(trade_dates or {}),
        "rf_pct": rf_pct,
        "rf_source": result.rf_source,
        "ii_pct": result.ii,
        "n_max": result.n_max if n_max is None else n_max,
        "eligible_count": result.eligible_count,
        "shortlisted": len(entries),
        "aborted": result.aborted,
        "notes": list(result.notes),
        "shortlist": entries,
    }


def build_excluded(result: RunResult) -> dict:
    """**因原因码未达标**的股票明细 + 原因分布（回答"为什么没选它"）。

    ⚠️ 三类别必须分清，混在一起会让日报失去诊断力：
    - ``eligible``                    —— 达标（进榜或合格但被 N 上限截掉）
    - ``qualified_not_selected``      —— 达标但未进 top-N（**不是**被排除）
    - 因原因码被排除的                 —— 本文件的内容
    """
    items = []
    for r in result.rows:
        if r.eligible:
            continue                     # 达标 ≠ 被排除
        for code in r.reasons:
            require(code)
        items.append({"ts_code": r.ts_code, "name": r.name, "market": r.market,
                      "reason_codes": list(r.reasons),
                      "reason_labels": [label_of(c) for c in r.reasons]})
    counts = result.reason_counts()
    return {
        "schema_version": SCHEMA_VERSION,
        "excluded_count": len(items),
        "qualified_not_selected": result.eligible_count - len(result.shortlist),
        "reason_counts": counts,
        "reason_summary": describe(counts),
        "excluded": items,
    }


def build_run_manifest(result: RunResult,
                       as_of: str,
                       trade_dates: Optional[Mapping[str, str]] = None,
                       universe_stats: Optional[Mapping[str, Any]] = None,
                       calendar_desc: Optional[str] = None,
                       price_stats: Optional[Mapping[str, Any]] = None,
                       snapshot_stats: Optional[Mapping[str, Any]] = None,
                       rf_source: Optional[str] = None,
                       allow_degraded: bool = False) -> dict:
    """运行清单：宇宙规模、各漏斗计数、门禁裁决、缓存/价格统计、耗时由调用方补。"""
    verdict = verdict_for(result, allow_degraded=allow_degraded)
    counts = result.reason_counts()
    total = max(len(result.rows), 1)
    data_gap = sum(v for k, v in counts.items()
                   if k not in ("below_threshold", "not_in_pool"))
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of,
        "trade_dates": dict(trade_dates or {}),
        "aborted": result.aborted,
        "rf_source": rf_source or result.rf_source,
        "ii_pct": result.ii,
        "universe": dict(universe_stats or {}),
        "calendar": calendar_desc,
        "prices": dict(price_stats or {}),
        "snapshots": dict(snapshot_stats or {}),
        "counts": {
            "screened": len(result.rows),
            "eligible": result.eligible_count,
            "shortlisted": len(result.shortlist),
            "excluded": len(result.rows) - result.eligible_count,
            "qualified_not_selected": result.eligible_count - len(result.shortlist),
            "data_gap": data_gap,
        },
        "data_gap_ratio": round(data_gap / total, 4),
        "reason_counts": counts,
        "reason_summary": describe(counts),
        "verdict": verdict,
        "deliverable": verdict["deliverable"],
    }


def verdict_for(result: RunResult, allow_degraded: bool = False) -> dict:
    """把本次运行折成 `gate_policy` 的交付裁决。

    - ``aborted`` ⇒ 一条 blocking finding（全局缺口）
    - 逐股数据缺口 ⇒ 一条 disclosure finding，附**占比**（占比过高是需要人看的信号，
      但不由本函数擅自升级为阻断 —— 那属于交付政策，要改就改 `gate_policy`）
    """
    findings: list[dict] = []
    if result.aborted:
        findings.append({"gate": "screener", "code": result.aborted,
                         "status": "FAIL", "title": "本次运行被中止",
                         "detail": "整批口令失真，不产出清单"})
    counts = result.reason_counts()
    gap = {k: v for k, v in counts.items()
           if k not in ("below_threshold", "not_in_pool")}
    if gap:
        findings.append({"gate": "screener", "code": "data_gaps", "status": "WARN",
                         "title": "存在逐股数据缺口（已带原因码排除）",
                         "detail": describe(gap)})
    return gate_policy.verdict(findings, allow_degraded=allow_degraded,
                               stock_code="screener")


def results_csv(result: RunResult) -> str:
    """全量结果 CSV（含不合格股，供人工排查）。"""
    cols = ["rank", "ts_code", "name", "market", "close", "currency",
            "market_cap_yuan", "ii", "gg_pess", "margin", "score",
            "eligible", "reasons"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in result.rows:
        w.writerow([r.rank, r.ts_code, r.name, r.market, r.close, r.currency,
                    r.market_cap_yuan, r.ii, r.gg_pess, r.margin, r.score,
                    int(r.eligible), "|".join(r.reasons)])
    return buf.getvalue()


def _atomic_write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path


def write_run(base: str | os.PathLike[str],
              trade_date: str,
              shortlist: Mapping[str, Any],
              manifest: Mapping[str, Any],
              excluded: Mapping[str, Any],
              result: Optional[RunResult] = None) -> dict[str, Path]:
    """原子写一个交易日的全部清单。**重跑覆盖同一目录**（幂等）。

    ⚠️ 若 ``shortlist['aborted']`` 非空，只写 ``run_manifest.json``（含中止原因），
    **不写 shortlist/excluded/CSV** —— 残缺清单比没有清单更危险。
    """
    d = run_dir(base, trade_date)
    out: dict[str, Path] = {}
    out["manifest"] = _atomic_write(
        d / "run_manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True))

    if shortlist.get("aborted"):
        out["aborted"] = _atomic_write(
            d / "ABORTED.txt", f"本次运行中止：{shortlist['aborted']}\n"
                               "按契约不产出 shortlist / excluded / CSV。\n")
        return out

    out["shortlist"] = _atomic_write(
        d / "shortlist.json",
        json.dumps(shortlist, ensure_ascii=False, indent=1, sort_keys=True))
    out["excluded"] = _atomic_write(
        d / "excluded.json",
        json.dumps(excluded, ensure_ascii=False, indent=1, sort_keys=True))
    if result is not None:
        out["csv"] = _atomic_write(d / "screener_results.csv", results_csv(result))
    return out
