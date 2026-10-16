"""底价锚点 ①②③④⑤ 与「综合底价 / 参考公允价」—— **纯函数，无取数、无渲染**。

为什么必须分语义（06862 事故，`derived_metrics.py:799-811` 的注释是原始记录）
------------------------------------------------------------------------
- ①②③ 是**底价型**锚点（净流动资产/股、每股净资产、10 年最低价）——回答"最差能值多少"；
- ④⑤ 是**收益率型公允价**锚点（股息资本化、FCF 资本化）——回答"按某收益率要求值多少"。

两者经济语义不同，④ 用的是 3% 毛股息收益率的公允价，天然远高于清算/账面底价。
旧实现把四项等权平均 ⇒ 06862 得 9.44，**既不是底价也不是公允价**，且高于悲观档买点 8.67，
使「低于底价 — 买入就是胜利」这一**买点语义**的读数在模型判定"不可买"的价位上触发。

因此：
- ``floor_composite`` = ①②③ 的算术平均 —— **只**用于「低于底价」判定（买点语义）
- ``fair_composite``  = ④⑤ 的算术平均 —— **只**作估值读数，**不得**用于买点判定
- 两者**永不混算**（`scripts/numeric_audit.py` D8 机械校验 `综合底价 == mean(①②③)`）

币种纪律（AGENTS.md 2026-10-13 扩围；`invariants.py` I2 机械校验）
--------------------------------------------------------------
**每一个将与价格比较或求均值的每股金额都必须折成交易币种。**
①②⑤ 来自报表币种报表 ⇒ 必须 ``÷ factor_to_reporting``；③④ 本身已是交易币种 ⇒ 不折。
``factor_to_reporting`` 的语义是「交易币种 × factor = 报表币种」（`scripts/fx_utils.py`）。

⚠️ ``invariants.py`` I2 **只**检查 §17.8 正文里是否出现字面串 `币种说明`，
**不校验任何数值**；且拿不到 ``--fx`` 时直接 PASS。所以数值正确性必须由本模块的单测兜住。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .units import per_share_to_trading

#: ④ 的折现率下限：``max(Rf, 3%)``，见 `derived_metrics.py:790`
DIVIDEND_DISCOUNT_FLOOR = 0.03

#: ⑤ 的默认窗口（文档与 screener、numeric_audit D8d 均为 5 年）
FCF_WINDOW_YEARS = 5


@dataclass(frozen=True)
class FloorAnchors:
    """五个锚点，单位一律为**交易币种/股**。``None`` 表示该锚点缺失（不参与均值）。"""

    nla: Optional[float] = None          # ① 净流动资产/股（底价型）
    bvps: Optional[float] = None         # ② 每股净资产（底价型）
    low_10y: Optional[float] = None      # ③ 10 年最低价（底价型）
    div_implied: Optional[float] = None  # ④ 股息隐含价（收益率型）
    fcf_cap: Optional[float] = None      # ⑤ 悲观 FCF 资本化价（收益率型）
    notes: tuple[str, ...] = field(default_factory=tuple)

    # --- 语义分组：靠**显式字段**而非行名前缀（canonical 用 startswith("①②③")，脆弱） ---

    @property
    def floor_prices(self) -> list[float]:
        """底价型锚点（①②③）中已具备的那些。"""
        return [v for v in (self.nla, self.bvps, self.low_10y) if v is not None]

    @property
    def fair_prices(self) -> list[float]:
        """收益率型锚点（④⑤）中已具备的那些。"""
        return [v for v in (self.div_implied, self.fcf_cap) if v is not None]

    @property
    def all_prices(self) -> list[float]:
        return self.floor_prices + self.fair_prices

    @property
    def floor_composite(self) -> Optional[float]:
        """综合底价 = ①②③ 算术平均。**只**用于「低于底价」判定。"""
        return _mean(self.floor_prices)

    @property
    def fair_composite(self) -> Optional[float]:
        """参考公允价 = ④⑤ 算术平均。仅作估值读数，**不得**用于买点判定。"""
        return _mean(self.fair_prices)


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


# ---------------------------------------------------------------------------
# 各锚点
# ---------------------------------------------------------------------------


def anchor_nla(cash: Optional[float],
               trad_assets: Optional[float],
               interest_bearing_debt: Optional[float],
               total_shares: Optional[float],
               factor_to_reporting: Optional[float]) -> Optional[float]:
    """① 净流动资产/股 = ``(现金 + 交易性金融资产 − 有息负债) / 总股本 / fx``。

    输入：``cash`` / ``trad_assets`` / ``interest_bearing_debt`` 为**报表币种原始元**；
    ``total_shares`` 为**股**；``factor_to_reporting`` 为 fx.json 契约的系数。

    输出：**交易币种 元/股**。任一必需项缺失（含 fx）⇒ ``None``（不按 0 兜底）。

    ⚠️ ``interest_bearing_debt`` 必须由 ``constants.interest_bearing_debt(row, include_lease=True)``
    提供（含租赁负债），不得在本模块内联组件表 —— 那正是 `screener_core.py:819,955` 被修正前的问题。
    """
    if cash is None or interest_bearing_debt is None or not total_shares or total_shares <= 0:
        return None
    trad = 0.0 if trad_assets is None else trad_assets
    per_share_reporting = (cash + trad - interest_bearing_debt) / total_shares
    return per_share_to_trading(per_share_reporting, factor_to_reporting)


def anchor_bvps(equity_parent: Optional[float],
                total_shares: Optional[float],
                factor_to_reporting: Optional[float]) -> Optional[float]:
    """② 每股净资产 = ``归母权益 / 总股本 / fx``。输入报表币种原始元，输出交易币种 元/股。"""
    if equity_parent is None or not total_shares or total_shares <= 0:
        return None
    return per_share_to_trading(equity_parent / total_shares, factor_to_reporting)


def anchor_low_10y(weekly_closes: Iterable[Optional[float]]) -> Optional[float]:
    """③ 近 N 年最低周收盘价（交易币种，**不折汇**）。

    输出交易币种 元/股；无有效值 ⇒ ``None``。

    窗口由**调用方**负责截取；本函数只做「取最小」—— 纯计算层不做日期解析。
    （刻意不提供 ``years`` 参数：它只能"标注"而无法过滤，空转参数会误导读者以为窗口在此生效。）

    ✅ **窗口本身已由采集器保证**（2026-10-14 核实，此前我的判断有误）：
    `financials.get_weekly_prices` 三市场都传了 10 年窗口 ——

    - A 股：``weekly(ts_code, start_date=十年前, end_date=今天)``（`financials.py:1301-1306`）
    - 港股 / 美股：``_yahoo_chart_df(ts_code, "10y", "1wk")``（`yfinance_integration._yf_weekly_history`）

    实测印证：`000651` 产物 §11 的「10年最低 21.68 (**20161021**)」—— 日期恰在 10 年前，
    若取全量历史会落到更早的年份。

    ⚠️ **撤回一条我先前的错误结论**：我曾据「Tushare ``weekly`` 不带 ``start_date`` 返回全量历史
    （``600036.SH`` 1238 根 = 24 年）」推断 canonical 的 ③ 是「上市以来最低价」。
    该推断**无效** —— 它描述的是 **API 的默认行为**，而采集器**显式传了窗口**。
    教训：核实**代码实际存了什么**，不要拿 API 默认值推断调用方行为。
    """
    vals = [v for v in weekly_closes if v is not None]
    if not vals:
        return None
    return float(min(vals))


def anchor_div_implied(recent_dps: Sequence[Optional[float]],
                       rf_pct: Optional[float]) -> Optional[float]:
    """④ 股息隐含价 = ``近3次分红均值 / max(Rf, 3%)``。

    输入 ``recent_dps`` 为**交易币种 元/股**（Tushare ``cash_div_tax``，已是交易币种，
    **不折汇**）；``rf_pct`` 为**百分数**（如 1.67 表示 1.67%）。

    输出交易币种 元/股；无有效 DPS 或 ``rf_pct`` 缺失 ⇒ ``None``。
    ``rf_pct`` 允许 ≤0（此时折现率取 3% 下限），与 canonical 一致。

    ⚠️ canonical 取的是「最近 3 次**分红事件**」（按 ``end_date`` 倒序的行），
    而非「最近 3 个**财年**」。对中期分红发行人，二者差异可达 20%+
    （`numeric_audit.py` D8c 会把它标为口径冲突）。窗口选择由调用方决定。
    """
    if rf_pct is None:
        return None
    dps = [v for v in recent_dps if v is not None]
    if not dps:
        return None
    discount = max(rf_pct / 100.0, DIVIDEND_DISCOUNT_FLOOR)
    return (sum(dps) / len(dps)) / discount


def anchor_fcf_capitalized(fcf_series: Sequence[Optional[float]],
                           rf_pct: Optional[float],
                           total_shares: Optional[float],
                           factor_to_reporting: Optional[float]
                           ) -> tuple[Optional[float], Optional[str]]:
    """⑤ 悲观 FCF 资本化价 = ``min(FCF序列) / (Rf/100) / 总股本 / fx``。

    ``fcf_series`` 为**报表币种原始元**的逐年 FCF（调用方决定窗口，默认近 5 年）；
    输出交易币种 元/股。

    返回 ``(值, 跳过原因)``：
    - ``rf_pct`` 缺失或 ≤0 ⇒ ``(None, "no_rf")``
    - 无有效 FCF ⇒ ``(None, "no_fcf")``
    - **存在负 FCF 年份** ⇒ ``(None, "negative_fcf")`` —— 文档要求「仅当窗口内 FCF 全为正时有效」
    - 否则 ``(值, None)``

    ⚠️ canonical 在「跳过」时会打印一行说明（`derived_metrics.py:804`），而 screener
    是**静默**省略 —— 静默正是本仓库反复出事的形态，故这里把原因显式返回。
    ⚠️ canonical 的 FCF 用 ``ocf − capex``（**无 abs**），`numeric_audit.py:797-802` 用
    ``ocf − abs(capex)``。在资本开支为正流出口径下二者等价；本函数只接收已算好的 FCF。
    """
    if rf_pct is None or rf_pct <= 0:
        return None, "no_rf"
    if not total_shares or total_shares <= 0:
        return None, "no_shares"
    vals = [v for v in fcf_series if v is not None]
    if not vals:
        return None, "no_fcf"
    lo = min(vals)
    if lo <= 0:
        return None, "negative_fcf"
    per_share_reporting = lo / (rf_pct / 100.0) / total_shares
    return per_share_to_trading(per_share_reporting, factor_to_reporting), None


# ---------------------------------------------------------------------------
# 溢价率：两个读数，用途不同
# ---------------------------------------------------------------------------


def premium_pct(close: Optional[float], composite: Optional[float]) -> Optional[float]:
    """溢价率（%）= ``(close / composite − 1) × 100``。

    ``composite`` 缺失或 **≤0** ⇒ ``None``。

    ⚠️ canonical **没有**这个保护：``close / floor_composite`` 在 composite 为 0 时
    抛 ``ZeroDivisionError``，为负时静默翻转符号（`derived_metrics.py:905,916`）。
    screener 则有 ``> 0`` 保护（`screener_core.py:1015`）。本模块统一取**保护**版本。
    """
    if close is None or composite is None or composite <= 0:
        return None
    return (close / composite - 1.0) * 100.0


def is_below_floor(close: Optional[float], anchors: FloorAnchors) -> bool:
    """「低于底价 — 买入就是胜利」**买点语义**判定。

    只有存在**底价型**综合值时才可判定（底价型锚点缺失 ⇒ 恒 ``False``，不得据此发信号）。
    该读数的合规性由 `viewpoint_audit.py` V4a（综合底价 ≤ 采纳档买点）进一步约束。
    """
    p = premium_pct(close, anchors.floor_composite)
    return p is not None and p <= 0


#: 参考公允价的估值档位（canonical 代码为 5 档；`phase3_valuation.md:194-198` 只写了 4 档，属文档滞后）
FAIR_TIERS: tuple[tuple[float, str], ...] = (
    (0.0, "低于公允价 — 安全边际充足（估值读数，买点另由悲观档决定）"),
    (30.0, "略低于/接近公允价 — 安全边际尚可"),
    (80.0, "合理溢价 — 需确认成长性"),
    (150.0, "较高溢价 — 依赖持续成长"),
)


def fair_tier_verdict(premium: Optional[float]) -> Optional[str]:
    """按参考公允价溢价率给出估值档位（**不是**买点判定）。"""
    if premium is None:
        return None
    for upper, label in FAIR_TIERS:
        if premium <= upper:
            return label
    return "显著溢价 — 高成长预期已定价"
