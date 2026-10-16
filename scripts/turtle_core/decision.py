"""穿透回报率决策层：II / R / GG / HH / GG−II / 目标买入价 / 门槛价格。

公式（`derived_metrics.py:2043,2072`；文档 `shared_tables.md:46-64`）
--------------------------------------------------------------
::

    穿透回报率 = [基准值 × M × (1 − Q) + O] / 市值 × 100        [%]

    因子2 粗算 R  ：基准值 = C  归母净利润（报表口径）
    因子3 精算 GG ：基准值 = AA 真实可支配现金结余

    HH    = R − GG        （粗算/精算方法论差异，通常在 1–3 pct）
    GG−II = GG − 门槛
    目标买入价 = 现价 × GG / II      （现价与结果同为**交易币种**）

单位纪律
--------
``base`` / ``o`` / ``mkt_cap`` **必须同币种**，且 canonical 内部一律用
**报表币种原始元**（`derived_metrics.py:1544` 已把港股港元市值 ×fx 折成报表币种）。
本模块只做算术，**不做任何币种换算** —— 换算由 `turtle_core.units` 负责，
避免"算一半折一半"的静默错。

门槛 II 的单一来源是 `constants.ii_threshold`，本模块只**转发**，绝不重写
``max(5%, Rf+3%)``（`known_issues.md` P1-12：这条公式曾在三处各写一遍）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

__all__ = [
    "ii_threshold",
    "market_kind",
    "Q_SCENARIOS",
    "Q_DEFAULT",
    "tax_rates",
    "penetration_return",
    "hh",
    "margin",
    "target_buy_price",
    "threshold_price",
    "o_increment_pct",
    "AAProfile",
    "aa_profile",
    "PayoutAnchor",
    "payout_anchor",
]

# ---------------------------------------------------------------------------
# 股息税率 Q（shared_tables.md「股息税率表」）
# ---------------------------------------------------------------------------

#: 每个市场的 (情景标签, 税率%) —— 顺序即渲染顺序
Q_SCENARIOS: dict[str, tuple[tuple[str, float], ...]] = {
    "HK": (("H股直接", 28.0), ("港股通", 20.0), ("红筹/开曼", 20.0)),
    "US": (("直接持有", 30.0), ("W-8BEN", 10.0)),
    "A": (("持有>1年", 0.0), ("持有1月-1年", 10.0), ("持有<1月", 20.0)),
}

#: 默认渠道（`shared_tables.md:41`：港股用港股通、A股用长期持有、美股用 W-8BEN）
Q_DEFAULT: dict[str, float] = {"HK": 20.0, "US": 10.0, "A": 0.0}

#: 文档提及但**未实现**的情景。保留为显式缺口，避免被误认为已覆盖。
UNIMPLEMENTED_Q_SCENARIOS: tuple[str, ...] = (
    "港股涉美国来源收入（加权计算，美国来源部分 44%）",
)


def ii_threshold(ts_code: str, rf_pct: float) -> float:
    """门槛 II（%）= ``max(5%, Rf+3%)`` —— 转发 `tushare_modules.constants` 的**单点**实现。

    ⚠️ **延迟导入不是偷懒**：`turtle_core` 是纯计算层，若在 import 期就
    ``from tushare_modules.constants import …``，会触发 `tushare_modules/__init__.py`，
    而它又会 import `derived_metrics`，后者 import `turtle_core.pessimistic`，
    后者再 import 本模块 —— **成环**（实测 ImportError: partially initialized module）。
    实测教训：全量测试**没抓到**这个环（测试的导入顺序恰好避开），
    是 `turtle_core_selfcheck.py` 独立跑时炸的 ⇒ 已补 subprocess 级导入守卫测试。
    """
    from tushare_modules.constants import ii_threshold as _impl   # 单点仍在 constants.py
    return _impl(ts_code, rf_pct)


def market_kind(ts_code: str) -> str:
    """按**代码后缀**判市场（与 canonical `_is_hk`/`_is_us` 一致）。"""
    upper = str(ts_code or "").upper()
    if upper.endswith(".HK"):
        return "HK"
    if upper.endswith(".US"):
        return "US"
    return "A"


def tax_rates(ts_code: str) -> tuple[list[str], list[float], float]:
    """返回 ``(标签列表, 去重后的税率列表, 默认税率)``。

    去重按**值**保序：港股 ``[28, 20]``、美股 ``[30, 10]``、A股 ``[0, 10, 20]``
    （`derived_metrics.py:1677-1684`）。默认税率**不**因去重而改变。
    """
    kind = market_kind(ts_code)
    pairs = Q_SCENARIOS[kind]
    labels = [label for label, _ in pairs]
    rates: list[float] = []
    for _, rate in pairs:
        if rate not in rates:
            rates.append(rate)
    return labels, rates, Q_DEFAULT[kind]


# ---------------------------------------------------------------------------
# 核心算术
# ---------------------------------------------------------------------------


def penetration_return(base: Optional[float],
                       m_pct: Optional[float],
                       q_pct: Optional[float],
                       o: Optional[float],
                       mkt_cap: Optional[float]) -> Optional[float]:
    """穿透回报率（%）= ``[基准值 × M × (1 − Q) + O] / 市值 × 100``。

    ``base`` / ``o`` / ``mkt_cap`` 同币种（canonical 为报表币种**原始元**）；
    ``m_pct`` / ``q_pct`` 为**百分数**（20.0 表示 20%）。

    R 与 GG 共用本函数，区别只在 ``base``：R 传 ``C``，GG 传 ``AA``。
    **这正是 screener 此前把 ``AA×M/市值`` 叫作 R 的错误所在** —— 它不是 R。

    任一必需项为 ``None`` 或 ``mkt_cap <= 0`` ⇒ ``None``（不兜底）。
    ``O`` 允许为 ``None``（视为 0，与 canonical `o_val = 0.0` 的默认口径一致）。
    """
    if base is None or m_pct is None or q_pct is None:
        return None
    if mkt_cap is None or mkt_cap <= 0:
        return None
    o_val = 0.0 if o is None else o
    keep = 1.0 - q_pct / 100.0
    return (base * m_pct / 100.0 * keep + o_val) / mkt_cap * 100.0


def hh(r: Optional[float], gg: Optional[float]) -> Optional[float]:
    """``HH = R − GG``（pct 点）。canonical 对缺 GG 按 0 处理（`derived_metrics.py:2077`）。"""
    if r is None:
        return None
    return r - (gg if gg is not None else 0.0)


def margin(gg: Optional[float], ii: Optional[float]) -> Optional[float]:
    """``安全边际 = GG − II``（pct 点）。

    ⚠️ 这是**审计口径**（`viewpoint_audit.py` V1a，容差 0.02）。
    `phase3_valuation.md:98-103` 另要求强周期标的做 ``II∓1/2 pct`` 的周期性修正，
    而审计**假定未修正** —— 文档与审计在此矛盾（见 findings D-1 邻近条目）。
    本函数只给未修正口径；周期性修正若要做，必须在报告里显式登记为 claim。
    """
    if gg is None or ii is None:
        return None
    return gg - ii


def target_buy_price(close: Optional[float],
                     gg: Optional[float],
                     ii: Optional[float]) -> Optional[float]:
    """``目标买入价 = 现价 × GG / II``（**交易币种/股**）。

    守卫与 canonical 完全一致（`derived_metrics.py:2101,2501`）：
    ``close`` 为真值、``ii > 0``、``gg > 0`` 才给值，否则 ``None``。
    ``GG ≤ 0`` 的含义是**无解**（任何价格都达不到门槛），不是"价格为零"。
    """
    if not close or ii is None or ii <= 0 or gg is None or gg <= 0:
        return None
    return close * gg / ii


def threshold_price(base: Optional[float],
                    m_pct: Optional[float],
                    q_pct: Optional[float],
                    o: Optional[float],
                    ii: Optional[float],
                    total_shares: Optional[float]) -> Optional[float]:
    """§17.9「门槛价格」= ``[基准值 × M × (1−Q) + O] / (II/100 × 总股本)``。

    ⚠️ **币种缺陷（known_issues D1 / findings D-2）**：本式输出的是**报表币种/股**，
    而 canonical 把它渲染在 ``门槛价格（{交易币种}）`` 的表头下（`derived_metrics.py:1011,1036`）。
    跨币种标的（报表 CNY / 交易 HKD）会差 ``1/fx``，canonical 因此在
    ``|fx−1| > 1e-9`` 时把该 claim **强制置 None**（`derived_metrics.py:1057-1090`）。

    本函数**只做算术**。要不要置 None 由调用方按 fx 决定 —— 但**推荐**：
    凡跨币种一律用 :func:`target_buy_price`（它天然是交易币种），
    不要在报告里引用门槛价格。
    """
    if base is None or m_pct is None or q_pct is None:
        return None
    if ii is None or ii <= 0 or total_shares is None or total_shares <= 0:
        return None
    o_val = 0.0 if o is None else o
    numerator = base * m_pct / 100.0 * (1.0 - q_pct / 100.0) + o_val
    return numerator / (ii / 100.0 * total_shares)


def o_increment_pct(mkt_cap_mm: Optional[float]) -> float:
    """每确认 **100 百万元（报表币种）** 年均注销型回购，穿透回报率 +value pct。

    = ``100 / 市值(百万) × 100``（`derived_metrics.py:2010-2011`）。
    市值缺失或非正 ⇒ ``0.0``（canonical 同）。
    """
    if not mkt_cap_mm or mkt_cap_mm <= 0:
        return 0.0
    return 100.0 / mkt_cap_mm * 100.0


# ---------------------------------------------------------------------------
# AA（真实可支配现金结余）聚合
# ---------------------------------------------------------------------------

#: 文档扣减项①（`derived_metrics.py:1331`）。注意 `c_pay_int_operating` **不**扣
#: （它已在 OCF 内，重复扣会低估）。
DEDUCT_FIELDS: tuple[str, ...] = (
    "c_pay_acq_sub", "c_pay_fin_lease", "c_pay_minority", "c_pay_int_finance",
)


@dataclass(frozen=True)
class AAProfile:
    """AA 的各变体，单位与输入一致（canonical 为报表币种**原始元**）。"""

    series: tuple[tuple[str, float], ...]   # (year, AA)，按年降序
    aa_2y: float
    aa_all: float
    aa_latest: float
    aa_excl: float
    aa_selected: float                      # §17.9/§17.13/§17.14 用这个

    @property
    def n_years(self) -> int:
        return len(self.series)


def deduct_total(values: Sequence[Optional[float]]) -> float:
    """扣减项合计 = **只累加正值**（canonical `v > 0` 守卫，`derived_metrics.py:1339-1343`）。"""
    return sum(v for v in values if v is not None and v > 0)


def aa_profile(rows: Sequence[tuple[str, Optional[float], Optional[float], Optional[float]]]
               ) -> tuple[Optional[AAProfile], Optional[str]]:
    """由逐年 ``(year, ocf, capex, deduct)`` 算 AA 各变体。

    - ``AA(year) = OCF − |capex| − deduct``（元，报表币种）
    - **硬门禁**：至少 **2 年**同时有 OCF 与 capex，否则返回 ``(None, "no_cashflow")``
      —— canonical 在此 `mark("17.5","missing","no_cashflow")` 并 return None，
      **缺失 ≠ 零值，不得手算替代**（`derived_metrics.py:1319-1328`）。
    - ``rows`` 必须**按年份降序**（canonical `_get_annual_df` 保证）。
    - 四个变体：
      ``aa_2y`` = 前 2 年均值；``aa_all`` = 全部均值；``aa_latest`` = 第 1 年；
      ``aa_excl`` = 非负年份均值（无非负年则退化为 ``aa_all``）；
      ``aa_selected`` = ``aa_2y``（年数 ≥2 时，即恒成立）。

    ⚠️ ``aa_excl`` 的实现是"**剔除负值年份**"，而文档写的是"剔除非经营性主导年份"
    （findings D-7）—— 口径差异已登记在 canonical 的 claim basis 里。
    """
    usable: list[tuple[str, float, float, float]] = []
    for year, ocf, capex, deduct in rows:
        if ocf is None or capex is None:
            continue
        usable.append((str(year), float(ocf), abs(float(capex)),
                       0.0 if deduct is None else float(deduct)))
    if len(usable) < 2:
        return None, "no_cashflow"

    series = tuple((y, ocf - cp - dd) for y, ocf, cp, dd in usable)
    vals = [v for _, v in series]

    aa_2y = sum(vals[:2]) / min(2, len(vals))
    aa_all = sum(vals) / len(vals)
    aa_latest = vals[0]
    positives = [v for v in vals if v >= 0]
    aa_excl = (sum(positives) / len(positives)) if positives else aa_all
    aa_selected = aa_2y if len(vals) >= 2 else aa_all

    return AAProfile(series=series, aa_2y=aa_2y, aa_all=aa_all,
                     aa_latest=aa_latest, aa_excl=aa_excl,
                     aa_selected=aa_selected), None


# ---------------------------------------------------------------------------
# M（支付率锚定值）
# ---------------------------------------------------------------------------

#: `dividend_commitment.json` 的 kind 取值
COMMITMENT_KINDS: tuple[str, ...] = ("公告", "口头")

M_LABELS: dict[str, str] = {"法1": "DPS÷EPS", "法2": "现金流口径", "法3": "同币种口径"}


@dataclass(frozen=True)
class PayoutAnchor:
    m1: Optional[float]
    m2: Optional[float]
    m3: Optional[float]
    m_rec: Optional[float]
    m_rec_label: str
    m_pess: Optional[float]
    m_rec_reason: str


def payout_anchor(m1: Optional[float],
                  m2: Optional[float],
                  m3: Optional[float],
                  commitment: Optional[dict] = None,
                  net_profit_yuan: Optional[float] = None,
                  identical_dps: bool = False) -> PayoutAnchor:
    """按 canonical 阶梯定 ``M_rec`` / ``M_pess``（`derived_metrics.py:1848-1889`）。

    ``m1/m2/m3`` 为**百分数**（三法各自的近 3 年均值）。

    承诺锚定（优先于三法）：
    - ``kind`` ∈ :data:`COMMITMENT_KINDS` 且（``kind == "公告"`` 或 ``occurrences >= 2``）
      才**有效**（口头承诺需至少出现 2 次）；
    - ``min_payout_pct`` 直接作 ``m_rec``，label ``承诺(支付率)``；
    - 否则 ``min_amount_mm``（**百万元**）÷ 最新归母净利润（**元**，需 ÷1e6 对齐单位）
      × 100，label ``承诺(金额折算)``。

    三法阶梯（首个命中即止）：
    1. 承诺已生效 → 承诺值
    2. 三法最大两两相对偏差 > 15% 且 m2 可用 → 法2
    3. DPS 序列恒定（``identical_dps``）且 m2 可用 → 法2
    4. m3 可用 → **法3 ★（默认）**
    5. m2 可用 → 法2
    6. 否则法1

    ``m_pess = min(m1, m2, m3 中可用者)``，若三法皆缺则退化为 ``m_rec``。
    ⚠️ **``m_pess`` 完全忽略承诺值**（findings D-2/D-3）：即使存在公开承诺，
    §17.14 悲观档仍按三法最低者定价。
    """
    commit = commitment or {}
    kind = str(commit.get("kind") or "无")
    try:
        occurrences = int(commit.get("occurrences") or 1)
    except (TypeError, ValueError):
        occurrences = 1
    commit_valid = kind in COMMITMENT_KINDS and (kind == "公告" or occurrences >= 2)

    m_rec: Optional[float] = None
    label = ""
    reason = ""

    if commit_valid:
        pct = commit.get("min_payout_pct")
        amt = commit.get("min_amount_mm")
        if pct is not None:
            m_rec = float(pct)
            label = "承诺(支付率)"
            reason = "使用公告承诺的最低支付率"
        elif amt is not None and net_profit_yuan:
            m_rec = float(amt) / (float(net_profit_yuan) / 1e6) * 100.0
            label = "承诺(金额折算)"
            reason = "按公告承诺的最低分红金额 ÷ 最新归母净利润折算"
        if m_rec is not None:
            if kind == "口头":
                reason += "；**口头承诺**，需在后续公告中反复确认"
            period = commit.get("period")
            if period:
                reason += f"；承诺期间 {period}"

    if m_rec is None:
        values = [v for v in (m1, m2, m3) if v is not None]
        max_dev = _max_pairwise_dev(m1, m2, m3)
        if max_dev is not None and max_dev > 0.15 and m2 is not None:
            m_rec, label = m2, "法2"
            reason = f"三法最大相对偏差 {max_dev*100:.1f}% > 15%，取现金流口径（上界）"
        elif identical_dps and m2 is not None:
            m_rec, label = m2, "法2"
            reason = "DPS 序列跨年恒定（疑上游按公告快照回填）⇒ 不用 DPS/EPS 口径"
        elif m3 is not None:
            m_rec, label = m3, "法3"
            reason = "同币种口径三法交叉校验通过（默认档）"
        elif m2 is not None:
            m_rec, label = m2, "法2"
            reason = "同币种口径不可用，退回现金流口径"
        elif m1 is not None:
            m_rec, label = m1, "法1"
            reason = "仅 DPS/EPS 口径可用"
        else:
            m_rec, label = None, ""
            reason = "三法皆不可用（分红数据全缺）"

    pess_candidates = [v for v in (m1, m2, m3) if v is not None]
    m_pess = min(pess_candidates) if pess_candidates else m_rec

    return PayoutAnchor(m1=m1, m2=m2, m3=m3, m_rec=m_rec, m_rec_label=label,
                        m_pess=m_pess, m_rec_reason=reason)


def _max_pairwise_dev(*values: Optional[float]) -> Optional[float]:
    """可用值之间的最大两两相对偏差 ``|a−b| / ((|a|+|b|)/2)``（canonical 口径）。"""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    worst = 0.0
    for i in range(len(vals)):
        for j in range(i + 1, len(vals)):
            a, b = vals[i], vals[j]
            denom = (abs(a) + abs(b)) / 2.0
            if denom == 0:
                continue
            worst = max(worst, abs(a - b) / denom)
    return worst
