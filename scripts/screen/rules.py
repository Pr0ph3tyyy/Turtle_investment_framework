"""宽松规则层 —— A 股与港股两套，**阈值全部外置可调**。

设计意图（用户原话：用**比较宽松的指标**筛出 100 只，再自己挑）
----------------------------------------------------------------
- 规则只做「把明显不适合的剔掉」，不追求精确排名
- **粗筛（阶段一）** 与 **精筛（阶段二）** 分列，阈值/目标数量都在配置里
- 每个被剔除的都**计数并给原因** ⇒ 输出能回答"为什么只剩 100 只"
- 不做静默缺席：`全市场 N → 卫生后 N1 → 粗筛后 N2 → 精筛后 N3 → 取前 K`

港股的关键差异
--------------
港股**没有批量估值**（无 `hk_daily_basic`），所以：
- 阶段一只能用 **价格/成交额/上市年限/市场类别**（数据来自 Yahoo 或东财）
- 阶段二才用 `hk_fina_indicator` 的 **PE/PB/股息/ROE/负债率**（逐只，约 1.7 s/只）
- ⚠️ 阶段二的估值是**报告期口径**（实测滞后于当日价 11–13%）—— 宽松筛选够用，
  但**不要**把它当当日估值去用
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = ["Rules", "DEFAULT_A", "DEFAULT_HK", "load_rules", "screen_rows", "rank_rows"]


@dataclass
class Rules:
    """一套规则。所有阈值都能从 TOML/JSON 覆盖。"""

    market: str = "A"
    stage1_target: int = 500          # 粗筛保留数量（港股用户定为 500）
    limit: int = 100                  # 最终输出数量
    min_listing_years: int = 3
    max_per_industry_frac: float = 0.10

    # ── 阶段一：廉价字段（A 股来自 daily_basic；港股来自报价层） ──
    min_market_cap_yi: Optional[float] = 50.0     # 亿元
    min_turnover_pct: Optional[float] = 0.3       # %
    min_price: Optional[float] = None             # 本币（港股剔仙股用 0.5）
    min_amount: Optional[float] = None            # 成交额（本币）
    require_hkd_counter: bool = False             # 港股：只留 HKD 柜台
    main_board_only: bool = False                 # 港股：只留主板

    # ── 阶段二：估值/质量（A 股这些字段在阶段一就有；港股要逐只取） ──
    max_pe: Optional[float] = 40.0
    min_pe: float = 0.0                            # > 0 ⇒ 排除亏损
    require_positive_pb: bool = True
    require_dividend: bool = True                  # 龟龟关心分配能力（宽松地要 dv>0）
    min_roe: Optional[float] = None                # 港股用（%）
    max_debt_asset_ratio: Optional[float] = None   # 港股用（%）

    #: 阶段一排序键。⚠️ **必须与阶段二的可用字段匹配** ——
    #: 港股阶段一**没有**股息数据（要阶段二才逐只取），若此处用 `dividend_yield`，
    #: 排序会退化为任意顺序，"粗筛取 500"就失去意义。故港股用流动性代理 `amount`。
    #: 阶段一**只做必要条件筛除，不排序**（用户 2026-10-15 确认）。
    #: 这些条件都有**策略依据**（分配意愿 / AA 为正 / 可分析性），不是随手设的阈值。
    necessary_only: bool = True
    rank_by_stage1: str = "amount"                  # 仅在 necessary_only=False 时使用
    #: 阶段二（最终）排序键 —— 默认按**龟龟的决策变量**：安全边际 = GG_pess − II
    rank_by: str = "safety_margin"
    #: 可选门槛。``None`` = 不只留达标的，而是**全排名后取前 K**（粗筛的本意：
    #: 让"差一点"的也进名单，供人挑）。设成 0 则只留安全边际为正的。
    min_safety_margin: Optional[float] = None


#: A 股：`daily_basic` 一次调用即含全部阶段一+阶段二字段
DEFAULT_A = Rules(
    market="A", stage1_target=800, limit=100,
    min_market_cap_yi=50.0, min_turnover_pct=0.3, min_price=1.0,
    max_pe=40.0, require_positive_pb=True, require_dividend=True,
    rank_by="safety_margin",
)

#: 港股：阶段一无估值 ⇒ 先按价格/成交额/年限收窄到 500，再逐只取基本面
DEFAULT_HK = Rules(
    market="HK", stage1_target=500, limit=100,
    min_market_cap_yi=None, min_turnover_pct=None,
    min_price=0.5, min_amount=None,
    require_hkd_counter=False, main_board_only=True,
    max_pe=40.0, require_positive_pb=True, require_dividend=True,
    min_roe=None, max_debt_asset_ratio=None,
    rank_by_stage1="amount",        # ← 港股阶段一只有价/成交额，按成交额取前 500 才有意义
    rank_by="safety_margin",        # ← 阶段二用**龟龟的决策变量**排序
)


def load_rules(path: str | Path, market: str) -> Rules:
    """从 ``.toml`` / ``.json`` 加载并覆盖默认值。缺字段用默认。"""
    p = Path(path)
    if not p.is_file():
        return DEFAULT_A if market == "A" else DEFAULT_HK
    if p.suffix == ".toml":
        import tomllib
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    else:
        data = json.loads(p.read_text(encoding="utf-8"))
    base = asdict(DEFAULT_A if market == "A" else DEFAULT_HK)
    section = (data.get(market) or data.get(market.lower()) or {})
    base.update({k: v for k, v in section.items() if k in base})
    return Rules(**base)


@dataclass
class Exclusions:
    """剔除计数 —— 输出里要报，**不允静默丢弃**。"""

    counts: dict[str, int] = field(default_factory=dict)

    def add(self, reason: str) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1

    def merge(self, other: "Exclusions") -> None:
        for k, v in other.counts.items():
            self.counts[k] = self.counts.get(k, 0) + v

    def as_dict(self) -> dict[str, int]:
        return dict(sorted(self.counts.items(), key=lambda kv: -kv[1]))


def _g(row: Any, name: str) -> Optional[float]:
    v = getattr(row, name, None)
    return v if isinstance(v, (int, float)) else None


def screen_rows(rows: Iterable[Any], rules: Rules, *, stage: int = 1,
                fundamentals: Optional[dict] = None,
                extras: Optional[dict] = None) -> tuple[list[Any], Exclusions]:
    """按阶段过滤。``rows`` 是 ``Row``（阶段一）或其组合（阶段二）。

    ``stage=1``：只用廉价字段（市值/换手/价格/成交额/年限/市场类别）
    ``stage=2``：加估值与质量字段（A 股来自 Row 本身；港股来自 ``fundamentals``/``extras``）
    """
    ex = Exclusions()
    kept = []
    for r in rows:
        code = getattr(r, "code", "")
        f = (fundamentals or {}).get(code)
        x = (extras or {}).get(code) or {}

        price = _g(r, "close")
        if rules.min_price is not None and (price is None or price < rules.min_price):
            ex.add("price_too_low"); continue

        if stage >= 1:
            mv_yi = None
            mv = _g(r, "total_mv")
            if mv is not None:
                mv_yi = mv / 1e8
            elif x.get("total_mv"):
                mv_yi = x["total_mv"] / 1e8
            if rules.min_market_cap_yi is not None:
                if mv_yi is None or mv_yi < rules.min_market_cap_yi:
                    ex.add("market_cap_below_floor"); continue
            turn = _g(r, "turnover_rate")
            if turn is None and x.get("turnover_rate") is not None:
                turn = x["turnover_rate"]
            if rules.min_turnover_pct is not None:
                if turn is None or turn < rules.min_turnover_pct:
                    ex.add("illiquid"); continue
            amt = _g(r, "amount")
            if amt is None:
                amt = x.get("amount")
                if amt is None and getattr(r, "volume", None) and price:
                    amt = r.volume * price
            if rules.min_amount is not None:
                if amt is None or amt < rules.min_amount:
                    ex.add("amount_below_floor"); continue

        if stage >= 2:
            pe = _g(r, "pe_ttm")
            if pe is None and x.get("pe_ttm") is not None:
                pe = x["pe_ttm"]
            if pe is None and f is not None:
                pe = getattr(f, "pe_ttm", None)
            if rules.max_pe is not None and (pe is None or pe > rules.max_pe):
                ex.add("pe_out_of_band"); continue
            if rules.min_pe and (pe is None or pe <= rules.min_pe):
                ex.add("loss_making"); continue

            pb = _g(r, "pb")
            if pb is None and x.get("pb") is not None:
                pb = x["pb"]
            if pb is None and f is not None:
                pb = getattr(f, "pb_ttm", None)
            if rules.require_positive_pb and (pb is None or pb <= 0):
                ex.add("pb_non_positive"); continue

            if rules.require_dividend:
                dy = _dividend_yield(r, x, f, price)
                if dy is None or dy <= 0:
                    ex.add("no_dividend"); continue

            if rules.min_roe is not None and f is not None:
                roe = getattr(f, "roe_avg", None)
                if roe is None or roe < rules.min_roe:
                    ex.add("roe_below_floor"); continue
            if rules.max_debt_asset_ratio is not None and f is not None:
                dar = getattr(f, "debt_asset_ratio", None)
                if dar is not None and dar > rules.max_debt_asset_ratio:
                    ex.add("leverage_too_high"); continue

        kept.append(r)
    return kept, ex


def _dividend_yield(r: Any, extras: dict, fundamentals: Any,
                    price: Optional[float]) -> Optional[float]:
    """股息率 %。A 股直接有 `dv_ttm`；港股由 `dps_hkd / 价` 算。"""
    dv = _g(r, "dv_ttm")
    if dv is None and extras.get("dv_ttm") is not None:
        dv = extras["dv_ttm"]
    if dv is not None:
        return dv
    if fundamentals is not None:
        try:
            return fundamentals.dividend_yield(price)
        except AttributeError:
            dps = getattr(fundamentals, "dps_hkd", None)
            if dps and price:
                return dps / price * 100
    return None


def rank_rows(rows: list[Any], rules: Rules, *, fundamentals: Optional[dict] = None,
              extras: Optional[dict] = None, stage: int = 2,
              quick: Optional[dict] = None) -> list[Any]:
    """排序。``stage=1`` 用 ``rank_by_stage1``，否则用 ``rank_by``。

    默认最终按**股息率降序**（与龟龟的分配能力取向一致）。
    """
    rank_by = rules.rank_by_stage1 if stage == 1 else rules.rank_by

    def key(r: Any):
        code = getattr(r, "code", "")
        f = (fundamentals or {}).get(code)
        x = (extras or {}).get(code) or {}
        price = _g(r, "close")
        if rank_by == "safety_margin":
            qt = (quick or {}).get(code)
            m = getattr(qt, "safety_margin", None) if qt is not None else None
            # **降级股排到最后**（用户决定：M>100% 不封顶但要降级 —— 降级的意义就是
            # 不让它挤掉正常候选）。其次算不出的（None）也排最后。
            deg = 1 if getattr(qt, "degraded", False) else 0
            if m is None:
                return (2, 0.0)
            return (deg, -float(m))
        if rank_by == "market_cap":
            mv = _g(r, "total_mv") or x.get("total_mv") or 0
            return -float(mv)
        if rank_by == "amount":
            amt = _g(r, "amount") or x.get("amount") or 0
            return -float(amt)
        if rank_by == "dividend_yield":
            dy = _dividend_yield(r, x, f, price) or -1.0
            return -float(dy)
        # 未知键 ⇒ 退回成交额（**不静默退化成任意顺序**）
        amt = _g(r, "amount") or x.get("amount") or 0
        return -float(amt)

    return sorted(rows, key=lambda r: (key(r), getattr(r, "code", "")))
