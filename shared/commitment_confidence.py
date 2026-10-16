#!/usr/bin/env python3
"""commitment_confidence.py —— 「回购/分红承诺」的**执行置信度**与取信额（单点实现）。

为什么需要
----------
龟龟的 §17.10 有一条「承诺优先」路径：只要公开文件写了最低支付率/最低金额，就一票
采用它、覆盖三法交叉校验。但对**回购类**承诺，这条路径有两个没被处理的问题：

1. **承诺是意向，不是已实现的分配。** 公告的「每年约 5 亿元」是**预算上限**，而公式
   直接把它当现金回报，等于假设足额执行。
2. **「会不会做」与「会做多少」是两件事。** 只看公告文本无法区分"真在做"与"说说而已"。

用户（2026-10-16）提出的方法正是解法：**先查已实际发生的执行，再据此评估承诺置信度，
而不是静态取信公告金额。** 本模块把该方法固化成可复算、可审计的函数。

三条设计取舍（都有理由，改动前请先读）
--------------------------------------
1. **置信度是「上限可靠程度」的标签，不是对已实现金额的折减系数。**
   已执行的额度是**既成事实**，不能因为"整体进度还不高"就把它打折（那会违反"不得臆造
   比事实更低的数"）。故 `effective_annual_mm` **不含**置信度乘数。
2. **有效额是「单调不减」的**：`effective = max(年化已执行, 既有基线)`。
   基线 = 已证的存量能力（例如上一年度实际注销额）。这条保证"取信"只会让我们**更保守地
   下调**、绝不会让我们比"什么都不信"时更乐观 —— 否则"评估承诺"反而会放大结论。
3. **只按已执行年化取信，承诺总额永远只当上限。** 因此本模块**不会**把足额承诺灌进模型。

置信度分档（比例 = 已执行 / 该承诺额度，按最近一个「最近截止」的承诺算）
------------------------------------------------------------------------
    >= 60%: 高    —— 执行到位，额度可作上限使用
    >= 30%: 中    —— 执行中但未过半，额度须打折看待
    <  30%: 低    —— 主要为意向；只采已执行年化，不做任何外推

用法::

    from commitment_confidence import assess, classify

    a = assess(commitment_amount_mm=300.0, executed_shares=14_899_800,
               avg_price=10.0, fx_to_reporting=0.85702,
               elapsed_months=10.5, baseline_annual_mm=22.83)
    a.confidence      # "中"
    a.effective_annual_mm   # 取信的年化额（供 O 或 M 使用）
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "CommitmentAssessment",
    "assess",
    "classify",
    "confidence_of",
    "load_execution", "periods_of",
    "HIGH", "MEDIUM", "LOW",
    "HIGH_MIN", "MEDIUM_MIN",
]

HIGH, MEDIUM, LOW = "高", "中", "低"

#: 分档阈值（执行额 / 承诺额）。改动等于改动交付政策，须同步文档与测试。
HIGH_MIN = 0.60
MEDIUM_MIN = 0.30

#: 年化所需的最少覆盖月数。不足则**不外推**（10 个月能年化，2 个月不能）。
MIN_MONTHS_TO_ANNUALIZE = 6.0


def confidence_of(executed_mm: Optional[float],
                  commitment_mm: Optional[float]) -> Optional[str]:
    """按「已执行 / 承诺额」分档。任一不可用 ⇒ ``None``（未知，不要猜）。"""
    if not executed_mm or not commitment_mm or commitment_mm <= 0:
        return None
    ratio = executed_mm / commitment_mm
    if ratio >= HIGH_MIN:
        return HIGH
    if ratio >= MEDIUM_MIN:
        return MEDIUM
    return LOW


#: 兼容别名（对外文档里两种写法都出现过）
classify = confidence_of


@dataclass(frozen=True)
class CommitmentAssessment:
    """一次承诺取信评估的结果。所有金额单位为**报表币种百万元**。"""

    commitment_mm: Optional[float]
    commitment_label: str
    executed_shares: Optional[int]
    avg_price: Optional[float]
    executed_mm: Optional[float]
    elapsed_months: Optional[float]
    annualized_executed_mm: Optional[float]
    execution_ratio: Optional[float]
    confidence: Optional[str]
    baseline_annual_mm: float
    effective_annual_mm: float
    notes: List[str] = field(default_factory=list)

    def describe(self) -> str:
        c = self.confidence or "未知"
        ratio = "—" if self.execution_ratio is None else f"{self.execution_ratio * 100:.1f}%"
        return (f"承诺 {self.commitment_label}｜已执行 {_f(self.executed_mm)} "
                f"（进度 {ratio}）⇒ 置信度 {c}｜年化 {_f(self.annualized_executed_mm)} "
                f"｜基线 {self.baseline_annual_mm:.2f} ⇒ **取信 {self.effective_annual_mm:.2f}**")


def _f(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.2f}"


def assess(*, commitment_amount_mm: Optional[float],
           executed_shares: Optional[int],
           avg_price: Optional[float],
           fx_to_reporting: float = 1.0,
           elapsed_months: Optional[float] = None,
           baseline_annual_mm: float = 0.0,
           commitment_label: str = "",
           executed_amount_mm: Optional[float] = None,
           ) -> CommitmentAssessment:
    """把「公告额度 + 实际执行」折成一个**取信的年化额**与置信度标签。

    参数
    ----
    commitment_amount_mm : 公告额度（报表币种百万元）。``None`` ⇒ 无金额承诺。
    executed_shares      : 评估期内**已实际执行**的回购股数（注销型 + 库存股）。
    avg_price            : 执行均价（交易币种/股）。``None`` ⇒ 无法折算金额，只给进度。
    fx_to_reporting      : 交易币种 → 报表币种。
    elapsed_months       : 评估期已过月数。``None`` 或 < ``MIN_MONTHS_TO_ANNUALIZE``
                           ⇒ **不外推**，年化额 = 已执行额（保守）。
    baseline_annual_mm   : 已证的存量能力（如上年度实际注销额）。作为**下限**。
    executed_amount_mm   : **已披露的执行金额**（报表币种百万元）。给了就用它，
                           **不再**由 ``executed_shares × avg_price × fx`` 推导 ——
                           发行人自披露的总额优先于买卖双方各自折算的乘积
                           （06078：中期业绩公告直接给了「约1.2亿元」，比按均价折算更可核）。
    """
    notes: List[str] = []
    executed_mm: Optional[float] = None
    if executed_amount_mm is not None:
        executed_mm = float(executed_amount_mm)
        notes.append("执行金额取**发行人披露值**（不由股数×均价推导）")
    elif executed_shares is not None and avg_price is not None:
        executed_mm = executed_shares * avg_price * fx_to_reporting / 1e6
    elif executed_shares is not None:
        notes.append("无执行均价且无披露金额 ⇒ 只给进度、不给金额；取信额退回基线")

    annualized: Optional[float] = executed_mm
    if executed_mm is not None:
        if elapsed_months and elapsed_months >= MIN_MONTHS_TO_ANNUALIZE:
            annualized = executed_mm * 12.0 / elapsed_months
            notes.append(f"按已过 {elapsed_months:.1f} 个月年化")
        else:
            notes.append(f"覆盖不足 {MIN_MONTHS_TO_ANNUALIZE:.0f} 个月（{_f(elapsed_months)}）"
                         "⇒ 不外推，年化额 = 已执行额")

    ratio = None
    if executed_mm is not None and commitment_amount_mm:
        ratio = executed_mm / commitment_amount_mm
    conf = confidence_of(executed_mm, commitment_amount_mm)
    if conf is None:
        notes.append("缺额度或缺执行金额 ⇒ 置信度未知（不得按『高』处理）")

    # ⚠️ 单调不减：取信额**只可能 ≥ 基线**。设计意图见模块 docstring 取舍 2。
    effective = max(annualized if annualized is not None else 0.0, float(baseline_annual_mm))
    if annualized is not None and annualized < baseline_annual_mm:
        notes.append(f"年化执行 {annualized:.2f} < 基线 {baseline_annual_mm:.2f} "
                     "⇒ 取基线（不对已证能力做向下修正）")

    return CommitmentAssessment(
        commitment_mm=commitment_amount_mm,
        commitment_label=commitment_label or "（未命名）",
        executed_shares=executed_shares,
        avg_price=avg_price,
        executed_mm=executed_mm,
        elapsed_months=elapsed_months,
        annualized_executed_mm=annualized,
        execution_ratio=ratio,
        confidence=conf,
        baseline_annual_mm=float(baseline_annual_mm),
        effective_annual_mm=effective,
        notes=notes,
    )


def load_execution(product_dir: str | Path) -> Dict[str, Any]:
    """读 ``{product_dir}/commitment_execution.json``；缺失/损坏 ⇒ ``{}``（不猜）。"""
    p = Path(product_dir) / "commitment_execution.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def periods_of(execution: Dict[str, Any]) -> List[Dict[str, Any]]:
    """取执行期序列。兼容两种写法：顶层 ``periods`` 或 ``execution.periods``。"""
    ex = execution or {}
    if ex.get("periods"):
        return list(ex["periods"])
    nested = ex.get("execution")
    if isinstance(nested, dict) and nested.get("periods"):
        return list(nested["periods"])
    return []


def total_executed_shares(execution: Dict[str, Any], *,
                          upto_month: Optional[str] = None) -> int:
    """把执行期的股数加总（可选截到某月，含该月）。"""
    periods = periods_of(execution)
    total = 0
    for row in periods:
        m = str(row.get("month") or "")
        if upto_month is not None and m and m > upto_month:
            continue
        try:
            total += int(row.get("shares") or 0)
        except (TypeError, ValueError):
            continue
    return total
