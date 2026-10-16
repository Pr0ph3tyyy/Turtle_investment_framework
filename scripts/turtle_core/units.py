"""单位与币种规范化 —— turtle_core 的**唯一换算入口**。

背景（这两条都是本仓库真实事故，见 ``known_issues.md`` P0-2）
-----------------------------------------------------------
1. **金额单位**：Tushare 各接口混用 元 / 万元 / 百万元；本项目决策层的内部单位
   统一取 **百万元（mm）**。
2. **币种**：港股以 HKD 交易，但多数内地 A+H / 红筹发行人的报表币种是 **CNY**。
   ``factor_to_reporting`` 的语义是**「交易币种金额 × factor = 报表币种金额」**
   （``fx.json`` 的 option-A 契约）。

   - 市值/R/GG 的分母：必须用**报表币种**市值 ⇒ ``trading_to_reporting(cap, fx)``
   - 每股锚点（①净流动资产/股、②BVPS、⑤FCF资本化价）：报表币种每股量必须折成
     **交易币种**才能与价格比较/求均值 ⇒ ``per_share_to_trading(v, fx)``

   06862 实测：① 1.11 / ② 1.80 是 CNY/股却坐在「基准价（港元）」列，使综合价
   9.34 而非 9.46；同日 R 被低估 15.65%。**混币种是静默错，不会抛异常。**

设计纪律
--------
- 本模块**不猜**：``fx`` 为 ``None``/非正数时，``per_share_to_trading`` 与
  ``trading_to_reporting`` 一律返回 ``None``，由调用方按 fail-closed 处置
  （标记 ``fx_missing`` / ABORT），**绝不**按 1.0 兜底。
- 报表币种 == 交易币种时，调用方应显式传 ``1.0``（``fx.json`` 契约允许），
  而不是省略 —— 省略与"有汇率但为 1"在设计上是两回事。
"""

from __future__ import annotations

from typing import Optional

# --- 金额单位：以百万元（mm）为内部统一单位 ---

_YUAN_PER_MM = 1_000_000.0
_WAN_PER_MM = 100.0  # 1 百万元 = 100 万元


def yuan_to_mm(value: Optional[float]) -> Optional[float]:
    """元 → 百万元。"""
    if value is None:
        return None
    return value / _YUAN_PER_MM


def wan_to_mm(value: Optional[float]) -> Optional[float]:
    """万元 → 百万元。"""
    if value is None:
        return None
    return value / _WAN_PER_MM


def mm_to_yuan(value: Optional[float]) -> Optional[float]:
    """百万元 → 元。"""
    if value is None:
        return None
    return value * _YUAN_PER_MM


# --- 币种：交易币种 ⇄ 报表币种 ---


def _valid_factor(factor: Optional[float]) -> bool:
    return factor is not None and factor > 0


def trading_to_reporting(value: Optional[float],
                         factor_to_reporting: Optional[float]) -> Optional[float]:
    """交易币种金额 → 报表币种金额（``× factor``）。

    用于**市值**：港元市值 × 0.85753 = 人民币市值，使 ``R = 净利润(CNY) / 市值(CNY)``
    的分子分母同币种。

    返回 ``None`` 当 ``value`` 为 ``None`` 或 ``factor`` 缺失/非正（不按 1.0 兜底）。
    """
    if value is None or not _valid_factor(factor_to_reporting):
        return None
    return value * factor_to_reporting  # type: ignore[operator]


def per_share_to_trading(per_share_reporting: Optional[float],
                         factor_to_reporting: Optional[float]) -> Optional[float]:
    """报表币种**每股**金额 → 交易币种每股金额（``÷ factor``）。

    用于①②⑤等每股锚点：只有折成交易币种后才能与股价比较、才能与其他锚点求均值。
    例：0.85753 时 ``1.11 CNY/股 ÷ 0.85753 = 1.294 HKD/股``。

    返回 ``None`` 当入参缺失或 ``factor`` 缺失/非正。
    """
    if per_share_reporting is None or not _valid_factor(factor_to_reporting):
        return None
    return per_share_reporting / factor_to_reporting  # type: ignore[operator]


def needs_conversion(trading_currency: Optional[str],
                     reporting_currency: Optional[str]) -> bool:
    """是否需要币种换算。``None`` 币种视为"未确认" ⇒ 返回 True（要求调用方显式处理）。

    未确认币种时**不得**假定同币种 —— 这正是 `_unit_label()` 退回「币种未确认」
    的那条路径，静默假定会让整包数字失去意义。
    """
    if not trading_currency or not reporting_currency:
        return True
    return trading_currency.upper() != reporting_currency.upper()
