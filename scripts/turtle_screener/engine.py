"""每日快层：慢层快照 × 当日价格 × turtle_core → 按**安全边际**排序的选股结果。

为什么排序键是 `GG_pess − II` 而不是自造评分（设计 §C1）
-----------------------------------------------------
个股报告的结论由 `GG_pess − II → 目标买入价 → 仓位` 决定。若 screener 用另一套
评分排名，自动化每天都会产出"screener 说第一、报告说不可买"的自相矛盾产物，
`viewpoint_audit` 还会判报告"结论与档位不相容"。同源是**结构性**要求。

市值口径：本模块**自己算**（不读产物市值）
----------------------------------------
``市值(报表币种) = 总股本 × 现价 × factor_to_reporting``

这正是 `turtle_core_extraction_findings.md` **D-10** 指出的恒等式。
产物里 §17.9 用"股本"、§17.11/§17.14 用"市值"，二者不同源时会差 10%+（00696 实测）。
选股器走"由股本与现价推市值"的一条路，天然自洽；若与产物的市值不一致，
说明产物的股本口径有降级，应登记 ``no_shares``/``input_inconsistent`` 而不是照抄。

资格是**门槛制 + N 上限**，不是硬取前 N（设计 §C4）
------------------------------------------------
资格 = ``安全边际 > 0`` **且** ``GG_pess > 0``（否则目标买入价无定义）。
全市场都不便宜的日子应输出"今日 0 只达标"——这是**正确结果**，不是故障。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from turtle_core import decision as D
from turtle_core import pessimistic as P
from turtle_core.reasons import require
from turtle_core.snapshot import FinancialSnapshot

from .prices import PriceQuote

__all__ = ["ScreenRow", "RunResult", "screen_one", "run_screen", "resolve_market_cap",
           "check_market_cap_consistency", "MARKET_CAP_TOLERANCE"]

#: 采纳档默认 k（因子2「打八折」）
ADOPTED_K = P.PESS_K_DEFAULT

#: 产物市值与「股本×现价×fx」允许的最大相对偏差（`findings` D-10 建议值）
MARKET_CAP_TOLERANCE = 0.005


def check_market_cap_consistency(pack_market_cap_yuan: Optional[float],
                                 total_shares: Optional[float],
                                 close: Optional[float],
                                 factor_to_reporting: Optional[float],
                                 tol: float = MARKET_CAP_TOLERANCE) -> tuple[bool, str]:
    """校验 ``市值 ≡ 股本 × 现价 × fx``（D-10 的机械检查）。

    为什么必须有：产物里 §17.9 的门槛价格用**股本**、§17.11/§17.14 的目标买入价用**市值**。
    二者不同源时同一物理量在两节里不同 —— 00696 实测偏差 **+10.32%**，
    导致 `numeric_audit` X1/X2 报 critical 并**阻断交付**。

    返回 ``(ok, detail)``；任一侧缺失 ⇒ ``(False, ...)``（判不了不等于通过）。

    ⚠️ **本函数是验收标准第 1 条的前置**：只要产物市值与自洽市值不等，
    screener（用自洽市值）与报告 §17（用产物市值）的 GG 必然不等，
    同源一致性检查会失败。修法见 findings D-10（改股本反推口径 + 新增不变式 I11）。
    """
    if pack_market_cap_yuan is None or not total_shares or not close:
        return False, "输入不全（市值/股本/现价至少缺一）—— 判不了不等于通过"
    fx = 1.0 if factor_to_reporting is None else factor_to_reporting
    if fx <= 0:
        return False, f"fx={fx} 非正，无法折算"
    implied = total_shares * close * fx
    if pack_market_cap_yuan <= 0:
        return False, f"产物市值 {pack_market_cap_yuan} 非正"
    gap = abs(implied - pack_market_cap_yuan) / pack_market_cap_yuan
    ok = gap <= tol
    detail = (f"产物市值 {pack_market_cap_yuan:,.0f} vs 自洽市值 {implied:,.0f}"
              f"（偏差 {gap*100:+.2f}%，容差 ±{tol*100:.2f}%）"
              + ("" if ok else " ⇒ 产物股本口径降级，screener 与报告 §17 的 GG 将不相等"))
    return ok, detail


def resolve_market_cap(total_shares: Optional[float],
                       close: Optional[float],
                       factor_to_reporting: Optional[float],
                       trading_currency: Optional[str] = None,
                       reporting_currency: Optional[str] = None,
                       ) -> tuple[Optional[float], Optional[str]]:
    """由股本与现价推**报表币种**市值（元）。返回 ``(市值, reason)``。

    同币种时可省略 ``factor_to_reporting``（视为 1.0）；跨币种必须有 fx，
    否则返回 ``(None, "fx_missing")`` —— 绝不按 1.0 兜底（会静默低估约 15.65%）。
    """
    if not total_shares or total_shares <= 0 or not close or close <= 0:
        return None, "no_market_cap"
    same = (trading_currency and reporting_currency
            and trading_currency.upper() == reporting_currency.upper())
    fx = 1.0 if same else factor_to_reporting
    if fx is None or fx <= 0:
        return None, "fx_missing"
    return total_shares * close * fx, None


@dataclass
class ScreenRow:
    ts_code: str
    name: str = ""
    market: str = ""
    close: Optional[float] = None
    currency: Optional[str] = None
    market_cap_yuan: Optional[float] = None
    ii: Optional[float] = None
    gg_pess: Optional[float] = None          # 采纳档 k=0.80
    margin: Optional[float] = None           # 安全边际 = GG_pess − II
    #: 排名积分 = margin / II —— 对 margin 的**单调变换**，不引入新信息。
    #: 为什么这样定义：① §C1 要求排序键与个股报告同源，加新信息就会脱钩；
    #: ② `margin/II` 是"相对门槛的倍数"，天然归一化掉 A/港/美三市场的
    #: Rf 差异（II = max(5%, Rf+3%)），可直接跨市场比较。
    #: 它**不是**买点，也**不可**当作买点使用。
    score: Optional[float] = None
    #: 目标买入价**只在内部用于资格判定与账本核验，不再进入任何输出契约**。
    #: 选股器给"买点"会被当成个股建议；排名只需积分。
    target_buy_price: Optional[float] = None
    r_pess: Optional[float] = None
    adopted_k: float = ADOPTED_K
    eligible: bool = False
    reasons: list[str] = field(default_factory=list)
    rank: Optional[int] = None

    @property
    def position_vs_target(self) -> Optional[float]:
        """现价相对目标买入价的溢价（%）；负值 = 已低于买点。"""
        if self.close is None or not self.target_buy_price or self.target_buy_price <= 0:
            return None
        return (self.close / self.target_buy_price - 1.0) * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_code": self.ts_code, "name": self.name, "market": self.market,
            "close": self.close, "currency": self.currency,
            "market_cap_yuan": self.market_cap_yuan,
            "ii_pct": self.ii, "gg_pess_pct": self.gg_pess, "margin_pct": self.margin,
            "score": self.score, "r_pess_pct": self.r_pess,
            "adopted_k": self.adopted_k,
            "eligible": self.eligible, "reasons": list(self.reasons), "rank": self.rank,
        }


def screen_one(snapshot: FinancialSnapshot,
               quote: Optional[PriceQuote],
               ii: Optional[float],
               name: str = "",
               market: str = "",
               o_yuan: Optional[float] = None,
               adopted_k: float = ADOPTED_K,
               allow_upstream_degraded: bool = False) -> ScreenRow:
    """对单只股票算决策行。**任何缺口都登记原因码，绝不兜底。**

    ``allow_upstream_degraded=False``（默认）时，上游产物门禁未通过
    （``gate_status.json`` 的 ``deliverable=False``）的股票**不纳入排名** ——
    采集器已判定该包不得作为分析输入，screener 不得绕过它。
    置 ``True``（对应 CLI 的 ``--allow-degraded``）则纳入，但原因码保留、随清单披露。
    """
    row = ScreenRow(ts_code=snapshot.ts_code, name=name, market=market,
                    adopted_k=adopted_k, ii=ii)
    reasons: list[str] = []
    for code in snapshot.reasons.values():
        require(code)
        reasons.append(code)

    # 上游门禁：**始终披露**；仅在未显式降级时排除。
    # （采集器对 deliverable=False 的包明说"不得作为分析输入"，screener 不得静默绕过）
    if snapshot.gate_deliverable is False:
        reasons.append("upstream_gate_failed")
        if not allow_upstream_degraded:
            row.reasons = sorted(set(reasons))
            return row

    #: **阻断计算**的原因（算不出 GG）与**仅披露**的原因必须分开。
    #: 首版用 `if reasons:` 一刀切，于是快照级原因（如 §17 某小节的
    #: `input_inconsistent`）会让**所有带任何原因码的股票都算不出 GG** —— 实测
    #: 池内 4 只 A 股全被这样挡掉，screener 实际上什么都没算。
    blockers: list[str] = []

    if ii is None:
        blockers.append("rf_missing")
        row.reasons = sorted(set(reasons + blockers))
        return row

    close = quote.price if quote is not None else None
    row.currency = quote.currency if quote is not None else None
    if close is None:
        blockers.append("no_price")
        row.reasons = sorted(set(reasons + blockers))
        return row
    row.close = close

    cap, cap_reason = resolve_market_cap(
        snapshot.total_shares, close, snapshot.factor_to_reporting,
        trading_currency=quote.currency if quote else None,
        reporting_currency=snapshot.reporting_currency)
    if cap is None:
        blockers.append(cap_reason or "no_market_cap")
        row.reasons = sorted(set(reasons + blockers))
        return row
    row.market_cap_yuan = cap

    aa_selected = snapshot.aa.get("aa_selected")
    m_pess = snapshot.payout.get("m_pess")
    c_yuan_mm = snapshot.c_yuan
    # 读 `snapshot.o`（元）。**不要用 `snapshot.o_yuan`** —— 那是陈旧字段，默认恒为 0.0
    o = (snapshot.o or 0.0) if o_yuan is None else o_yuan

    if aa_selected is None:
        blockers.append("no_cashflow")
    if m_pess is None:
        blockers.append("no_payout")

    if blockers:
        row.reasons = sorted(set(reasons + blockers))
        return row

    # O 优先取快照（报告同源）；显式传入的 o_yuan 仅作覆盖（测试用）
    o = o_yuan if o_yuan is not None else (snapshot.o or 0.0)

    aa_pess = aa_selected * adopted_k
    gg = D.penetration_return(aa_pess, m_pess, snapshot.q_default, o, cap)
    row.gg_pess = gg
    row.margin = D.margin(gg, ii)
    # 积分 = margin / II（II 缺失/为 0 ⇒ None ⇒ 不参与排名）
    row.score = (row.margin / ii) if (row.margin is not None and ii) else None
    row.target_buy_price = D.target_buy_price(close, gg, ii)
    row.r_pess = D.penetration_return(
        None if c_yuan_mm is None else c_yuan_mm * adopted_k,
        m_pess, snapshot.q_default, o, cap)

    # 资格：门槛制（不是"取前 N"）。注意 `reasons` 里的快照级原因**只作披露**，
    # 不参与资格判定 —— 阻断计算的原因已在上面 `blockers` 里提前返回。
    if gg is not None and gg > 0 and row.margin is not None and row.margin > 0:
        row.eligible = True
    else:
        reasons.append("below_threshold")
    row.reasons = sorted(set(reasons))
    return row


@dataclass
class RunResult:
    rows: list[ScreenRow] = field(default_factory=list)
    shortlist: list[ScreenRow] = field(default_factory=list)
    n_max: int = 8
    ii: Optional[float] = None
    rf_source: Optional[str] = None
    aborted: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def eligible_count(self) -> int:
        return sum(1 for r in self.rows if r.eligible)

    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.rows:
            for code in r.reasons:
                counts[code] = counts.get(code, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def run_screen(snapshots: Iterable[FinancialSnapshot],
               quotes: dict[str, PriceQuote],
               ii: Optional[float],
               n_max: int = 8,
               names: Optional[dict[str, str]] = None,
               markets: Optional[dict[str, str]] = None,
               o_by_code: Optional[dict[str, float]] = None,
               adopted_k: float = ADOPTED_K,
               rf_source: Optional[str] = None,
               allow_upstream_degraded: bool = False) -> RunResult:
    """跑一遍每日快层。**ii 缺失 ⇒ 整批 ABORT**（II 错则全市场口径全错）。"""
    res = RunResult(n_max=n_max, ii=ii, rf_source=rf_source)
    if ii is None:
        res.aborted = "rf_missing"
        return res

    names = names or {}
    markets = markets or {}
    o_by_code = o_by_code or {}

    for snap in snapshots:
        row = screen_one(snap, quotes.get(snap.ts_code), ii,
                         name=names.get(snap.ts_code, ""),
                         market=markets.get(snap.ts_code, ""),
                         o_yuan=o_by_code.get(snap.ts_code),
                         adopted_k=adopted_k,
                         allow_upstream_degraded=allow_upstream_degraded)
        res.rows.append(row)

    # 排序：主键 = 安全边际降序（与个股报告同源，§C1）。
    # 次键**不再用"现价相对买点"** —— 买点已退出输出契约，排名不应依赖它；
    # 同分按代码稳定排序，保证同一输入两次运行结果一致（可复现）。
    def _key(r: ScreenRow):
        return (-(r.margin if r.margin is not None else -1e9), r.ts_code)

    eligible = sorted([r for r in res.rows if r.eligible], key=_key)
    res.shortlist = eligible[:max(0, int(n_max))]
    for i, r in enumerate(res.shortlist, start=1):
        r.rank = i

    if not res.shortlist:
        res.notes.append("今日 0 只达标（门槛制，不硬凑 N）—— 这是正确结果，不是故障")
    return res
