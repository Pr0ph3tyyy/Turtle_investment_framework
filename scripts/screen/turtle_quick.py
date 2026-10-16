"""最小龟龟计算器 —— 只取龟龟精髓所需的基础数据，**复用 `turtle_core` 的公式**。

为什么要它（用户原话：粗筛不该只比 PE/股息这些直接数值，要用基础数据做计算）
------------------------------------------------------------------------
龟龟的精髓是四个量，粗筛就应该算这四个，而不是比估值倍数：

1. **AA = 经营现金流 − |资本开支| − 扣减项①** —— *股东真正能拿到的现金*（非会计利润）
2. **M_pess = min(法1, 法2, 法3)** —— *分配意愿*，多法取最低
   - 法1 = 逐年 `DPS / EPS`；法2 = 逐年 `已付股息 / 归母净利润`（含利息与少数股东股利，是上界）；
     法3 = §17.1 同币种派息率（A 股与法1 同口径；港股为年报宣告口径）
3. **GG_pess = (AA × k × M_pess/100 × (1−Q) + O) / 市值 × 100** —— *穿透回报率*
4. **安全边际 = GG_pess − II**，`II = max(5%, Rf+3%)` —— *决策变量是差值，不是绝对水平*

**公式全部来自 `turtle_core`**（纯计算层，20/20 等价护栏）⇒ 选股器算出的数**与 analysis 同源**，
不是另写一套。本模块只负责**取最小字段集 + 组装输入**。

港股的行转列格式
----------------
`hk_cashflow` / `hk_income` 返回的是 `ind_name`/`ind_value` 的**行转列**格式，需要透视。
下面 `_HK_*` 常量**镜像** `tushare_modules/constants.py` 的 `HK_CASHFLOW_MAP`/`HK_INCOME_MAP`
（这里刻意只抄需要的几项，避免 import 采集器整包 —— 那会触发 `tushare_modules/__init__.py`
的循环导入，且违背"选股器独立"的架构目标）。

⚠️ 精度声明：这是**粗筛**。M 用三法均值（与 analysis 的完整阶梯/承诺锚定略有差别），
底价/公允价与仓位矩阵**不在此算**。**权威数字一律以 analysis 模块为准。**
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from turtle_core import decision as D                            # noqa: E402

__all__ = ["QuickTurtle", "compute_a", "compute_hk", "DEFAULT_K", "DEFAULT_Q_PCT_A",
           "DEFAULT_Q_PCT_HK"]

DEFAULT_K = 0.8              # 悲观折扣（用户确认：先用默认）
DEFAULT_Q_PCT_A = 0.0        # 股息税：A 股档位（用户确认：先用默认）
DEFAULT_Q_PCT_HK = 0.0       # 股息税：港股档位
_LOOKBACK_YEARS = 5          # AA/M 的回看年数（龟龟要求 ≥2 年可用）

# ── 港股行转列映射（镜像 constants.HK_*_MAP，只抄需要的） ──
_HK_CF = {
    "n_cashflow_act": ["经营业务现金净额"],
    "c_pay_acq_const_fiolta": ["购建固定资产", "购建无形资产及其他资产"],   # **求和**
    "c_pay_dist_dpcp_int_exp": ["已付股息"],
    "c_pay_acq_sub": ["收购附属公司"],
    "c_pay_fin_lease": ["偿还融资租赁"],
    "c_pay_minority": ["购买子公司少数股权"],
    "c_pay_int_finance": ["已付利息"],
}
_HK_INC = {
    "n_income_attr_p": ["股东应占溢利"],
    "basic_eps": ["每股基本盈利", "基本每股收益"],
    "revenue": ["营业额"],
}
#: 文档扣减项①（与 `derived_metrics.DEDUCT_FIELDS` 同源）
_DEDUCT_FIELDS = ("c_pay_acq_sub", "c_pay_fin_lease", "c_pay_minority", "c_pay_int_finance")


@dataclass
class QuickTurtle:
    """粗筛用的龟龟量。任一为 ``None`` = **算不出**（不是 0）。"""

    code: str
    aa: Optional[float] = None                  # 元
    m_pess: Optional[float] = None              # %
    gg_pess: Optional[float] = None             # %
    ii: Optional[float] = None                  # %
    safety_margin: Optional[float] = None       # pct 点 = gg_pess − ii
    mkt_cap: Optional[float] = None             # 元（报表币种）
    close: Optional[float] = None
    dividend_yield_pct: Optional[float] = None  # 股息率 %（A 股取 dv_ttm；港股取 dividend_rate）
    fy_end: str = ""
    years_used: int = 0
    reasons: list[str] = field(default_factory=list)
    #: **降级标记**（用户 2026-10-15 决定：M 不封顶，但必须告警并降级）。
    #: 触发条件：``M_pess > 100`` —— 即 `AA × M/100 > AA`，等于"把可支配现金的 N 倍分掉"，
    #: 会让 GG 出现经济上不可能的读数（实测大悦城 M=705.74% ⇒ GG=213.70%）。
    #: **不封顶**是为了与 analysis 保持同一语义；**降级**是为了不把它当正常候选。
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.safety_margin is not None


def _num(v: Any) -> Optional[float]:
    """NaN 是真值 —— 必须显式判（本仓库已因 `v or 0` 之类的写法吃过亏）。"""
    if v is None or v == "-" or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _year(end_date: Any) -> str:
    return str(end_date or "")[:4]


def _dedupe_annual(records: Sequence[dict]) -> list[dict]:
    """按 ``end_date`` 去重，**每年只留第一行**。

    ⚠️ **必须去重**：上游对同一 `end_date` 会返回多行（`report_type` 变体，如合并报表的
    更正前后；实测 000039.SZ 的 20251231 出现 2 次、20231231 出现 2 次）。
    不去重会让"近 2 年均值"实际取成**同一年两次**，并把重复年份混入多年均值。
    """
    seen, out = set(), []
    for r in records:
        e = str(r.get("end_date") or "")
        if not e or e in seen:
            continue
        seen.add(e)
        out.append(r)
    return out


def _is_annual(end_date: Any) -> bool:
    """``end_date`` 是否为**年度**报告（1231）。

    ⚠️ **必须过滤**：`pro.cashflow()` / `pro.income()` 返回**季度与年度混合**行
    （实测 600887 的 cashflow 有 100 行）。若不过滤，`rows[:5]` 取到的是**5 个季度**，
    而季度 OCF ≈ 年度的 1/4 ⇒ AA 会被系统性低估（实测 000651：180.79 亿 vs analysis 353.68 亿，差 1.96×）。
    """
    e = str(end_date or "")
    return len(e) >= 8 and e[4:8] == "1231"


def _pivot(records: Sequence[dict], mapping: dict[str, list[str]]) -> dict[str, dict[str, float]]:
    """行转列 → ``{canonical_field: {year: value}}``。同名多项**求和**（如 capex 两项）。"""
    out: dict[str, dict[str, float]] = {}
    seen: set[tuple[str, str]] = set()
    for rec in records:
        if not _is_annual(rec.get("end_date")):    # ← 港股同样是年度/中期混合
            continue
        ind = str(rec.get("ind_name") or "").replace(" ", "")
        val = _num(rec.get("ind_value"))
        y = _year(rec.get("end_date"))
        if not ind or val is None or not y:
            continue
        # ⚠️ **去重键必须是 `(end_date, ind_name)`**，不是 `end_date`。
        # 行转列格式里**一行 = 一个行项目**，同一个 `end_date` 有几百行；
        # 若按 `end_date` 去重（`_dedupe_annual`），每年只剩 1 行 ⇒ **数据被毁**
        # （实测 06862：pivot 只抽出 2 个字段、OCF 只剩 2018/2016 ⇒ 全池 0 只能算）。
        key = (str(rec.get("end_date") or ""), ind)
        if key in seen:
            continue
        seen.add(key)
        for canon, names in mapping.items():
            if any(n in ind for n in names):
                out.setdefault(canon, {})
                out[canon][y] = out[canon].get(y, 0.0) + val
    return out


def _m_methods(dps_by_year: dict[str, float], eps_by_year: dict[str, float],
               paid_by_year: dict[str, float], ni_by_year: dict[str, float],
               years: list[str]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """三法各自的近 N 年均值（百分数）。**任一法算不出就是 None，绝不补 0。**"""
    def mean(vals: list[float]) -> Optional[float]:
        return sum(vals) / len(vals) if vals else None

    # ⚠️ **最近 3 年**，与 canonical 的 `_mean_first3` 一致（此前我用全部回看年数 ⇒ 偏离）
    y3 = list(years)[:3]
    m1 = mean([dps_by_year[y] / eps_by_year[y] * 100
               for y in y3 if dps_by_year.get(y) is not None
               and eps_by_year.get(y) not in (None, 0)])
    m2 = mean([paid_by_year[y] / ni_by_year[y] * 100
               for y in y3 if paid_by_year.get(y) is not None
               and ni_by_year.get(y) not in (None, 0)])
    # 法3：A 股与法1 同口径（§17.1 派息率即 DPS/EPS）；港股需年报宣告口径，粗筛沿用 DPS/EPS
    m3 = m1
    return m1, m2, m3


def _merge_dividends(records: Sequence[dict]) -> dict[str, float]:
    """年度每股股息（含税）。**只取已实施**（``div_proc`` 含"实施"），预案/未通过一律不算。

    ⚠️ 实测坑：`div_proc='预案'` 时 `cash_div_tax=0.0`，`'股东大会通过'` 时是 **NaN**。
    若不过滤进度、或把 NaN 当 0，会把"尚未分红"误算成"分红为零"。
    """
    out: dict[str, float] = {}
    for r in records:
        proc = str(r.get("div_proc") or "")
        if "实施" not in proc:
            continue
        v = _num(r.get("cash_div_tax"))
        y = _year(r.get("end_date"))
        if v is None or v <= 0 or not y:
            continue
        out[y] = out.get(y, 0.0) + v
    return out


def _assemble(aa: Optional[float], m1, m2, m3, mkt_cap: Optional[float],
              close: Optional[float], ii: Optional[float], code: str,
              years_used: int, q_pct: float, o: float, fy_end: str,
              reasons: list[str], *, net_profit_yuan: Optional[float] = None,
              identical_dps: bool = False) -> QuickTurtle:
    """组装并**用 `turtle_core` 算 GG 与安全边际**（与 analysis 同源）。"""
    qt = QuickTurtle(code=code, aa=aa, mkt_cap=mkt_cap, close=close, ii=ii,
                     fy_end=fy_end, years_used=years_used, reasons=list(reasons))
    if aa is None:
        return qt
    # ⚠️ **AA ≤ 0 必须排除**：经营现金流减资本开支后不是正的，意味着公司**不产生自由现金**
    # ⇒ 龟龟的"可支配现金"为零或为负，穿透回报率无从谈起。
    # 不做这一层守卫会出现**两个负数相乘抵消**的荒谬结果：
    # 实测 002208 合肥城建 AA=−19 亿、M_pess=−246.89% ⇒ GG_pess 50.28%、
    # 安全边际 45.28，被排到全市场第 1 —— 而它现金流为负且不分红。
    if aa <= 0:
        qt.reasons.append("aa_non_positive")
        return qt
    # 与 canonical 同样的入参（否则 `m_rec` 的阶梯会不同）
    anchor = D.payout_anchor(m1, m2, m3, net_profit_yuan=net_profit_yuan,
                             identical_dps=identical_dps)
    m_pess = getattr(anchor, "m_pess", None)
    qt.m_pess = m_pess
    if m_pess is None:
        qt.reasons.append("no_payout")
        return qt
    # ⚠️ **M_pess ≤ 0 同样排除**（不分红 / 支付率为负）—— 理由同上：负值会与负 AA 抵消
    if m_pess <= 0:
        qt.reasons.append("payout_non_positive")
        return qt
    # ── M 溢出：不封顶（与 analysis 同语义），但**必须告警并降级** ──
    if m_pess is not None and m_pess > 100.0:
        qt.degraded = True
        qt.warnings.append(
            f"M_pess={m_pess:.1f}% > 100% ⇒ 相当于分配可支配现金的 {m_pess/100:.2f} 倍"
            f"（AA={aa/1e8:,.1f} 亿）—— 该 GG 读数是公式外推的结果，**经济上不可持续**，"
            f"请以 analysis 模块的完整口径复核")
    gg = D.penetration_return(aa * DEFAULT_K, m_pess, q_pct, o, mkt_cap)
    qt.gg_pess = gg
    if gg is None:
        return qt
    qt.safety_margin = D.margin(gg, ii)
    return qt


# ─────────────────────────── A 股 ───────────────────────────

def compute_a(code: str, *, pro, mkt_cap: Optional[float], close: Optional[float],
              ii: Optional[float], trade_date: str = "", dv_ttm: Optional[float] = None,
              q_pct: float = DEFAULT_Q_PCT_A, o: float = 0.0) -> QuickTurtle:
    """A 股：`cashflow` + `income` + `dividend`（3 次调用）→ AA / M_pess / GG_pess / 安全边际。

    ``mkt_cap``（元）与 ``close`` 由调用方**批量**传入（`daily_basic` 一次覆盖全市场）。
    """
    reasons: list[str] = []
    try:
        cf = pro.cashflow(ts_code=code, fields="end_date,n_cashflow_act,"
                          "c_pay_acq_const_fiolta,c_pay_dist_dpcp_int_exp,"
                          "c_pay_acq_sub,c_pay_fin_lease,c_pay_minority,c_pay_int_finance")
    except Exception:                                          # noqa: BLE001
        return QuickTurtle(code=code, reasons=["cashflow_fetch_failed"])
    if cf is None or getattr(cf, "empty", True):
        return QuickTurtle(code=code, reasons=["no_cashflow"])
    cols = set(cf.columns)
    # ⚠️ **扣减项字段可能不存在**（A 股多为空）—— 不得当 0 静默代入，而是按实际列取
    deduct_cols = [c for c in _DEDUCT_FIELDS if c in cols]
    cf_annual = _dedupe_annual([r for r in cf.to_dict("records")
                                if _is_annual(r.get("end_date"))])
    rows = []
    for r in cf_annual:
        ocf, capex = _num(r.get("n_cashflow_act")), _num(r.get("c_pay_acq_const_fiolta"))
        if ocf is None or capex is None:
            continue
        deduct = 0.0
        for c in deduct_cols:
            v = _num(r.get(c))
            if v is not None and v > 0:        # 净流入不扣（06049 实测 −5.94 是流入）
                deduct += v
        rows.append((_year(r.get("end_date")), ocf, abs(capex), deduct))
    rows.sort(key=lambda t: t[0], reverse=True)
    rows = rows[:_LOOKBACK_YEARS]
    prof, why = D.aa_profile(rows)
    if prof is None:
        return QuickTurtle(code=code, reasons=[why or "no_cashflow"])
    years = [y for y, *_ in rows]

    try:
        inc = pro.income(ts_code=code, fields="end_date,basic_eps,n_income_attr_p")
    except Exception:                                          # noqa: BLE001
        inc = None
    ni_by_year, eps_by_year = {}, {}
    if inc is not None and not getattr(inc, "empty", True):
        for r in _dedupe_annual([x for x in inc.to_dict("records")
                                 if _is_annual(x.get("end_date"))]):
            y = _year(r.get("end_date"))
            ni = _num(r.get("n_income_attr_p"))
            if ni is not None:
                ni_by_year[y] = ni
            e = _num(r.get("basic_eps"))
            if e is not None:
                eps_by_year[y] = e
    else:
        reasons.append("no_income")

    try:
        dv = pro.dividend(ts_code=code, fields="end_date,cash_div_tax,div_proc")
    except Exception:                                          # noqa: BLE001
        dv = None
    dps_by_year = _merge_dividends(dv.to_dict("records")) if (
        dv is not None and not getattr(dv, "empty", True)) else {}
    if not dps_by_year:
        reasons.append("no_dividend")

    paid_by_year = {}
    if "c_pay_dist_dpcp_int_exp" in cols:
        for r in cf_annual:
            v, y = _num(r.get("c_pay_dist_dpcp_int_exp")), _year(r.get("end_date"))
            if v is not None and y:
                paid_by_year[y] = v

    m1, m2, m3 = _m_methods(dps_by_year, eps_by_year, paid_by_year, ni_by_year, years)
    ni_latest = ni_by_year.get(years[0]) if years else None
    # A 股判据（canonical）：逐年 DPS 在 ≥3 年完全相同 ⇒ 填充错误，须走法2
    dps_seq = [dps_by_year[y] for y in years[:3] if dps_by_year.get(y) is not None]
    identical = len(dps_seq) >= 3 and len(set(round(v, 6) for v in dps_seq)) == 1
    return _assemble(prof.aa_selected, m1, m2, m3, mkt_cap, close, ii, code,
                     len(rows), q_pct, o, years[0] if years else "", reasons,
                     net_profit_yuan=ni_latest, identical_dps=identical)


# ─────────────────────────── 港股 ───────────────────────────

def compute_hk(code: str, *, pro, mkt_cap: Optional[float] = None, close: Optional[float],
               ii: Optional[float], dps_hkd: Optional[float] = None,
               fund=None, em_total_mv: Optional[float] = None,
               q_pct: float = DEFAULT_Q_PCT_HK, o: float = 0.0) -> QuickTurtle:
    """港股：`hk_cashflow` + `hk_income`（**行转列需透视**）+ `dps_hkd` → 四个量。

    ⚠️ 港股**没有** `hk_dividend` 接口（实测"请指定正确的接口名"）⇒ 每股股息只能取
    `hk_fina_indicator.dps_hkd`，由调用方传入（那只本来就为阶段二拉过，不额外花调用）。
    """
    reasons: list[str] = []
    warns: list[str] = []
    # ── 市值口径（用户 2026-10-15 确认）：**报表币种 × 当日价** ──
    # 关键：港股报表金额是**发行人报告币种**（内地发行人多为 CNY），而
    # `hk_fina_indicator.total_market_cap` 是**原始港元** ⇒ 直接相除跨币种（约 18.5% 偏差）。
    # 解法：Tushare 的 `pb_ttm` 内部已做换算 ⇒ `市值(报表币种) = pb_ttm × 股东权益`，
    # 再用 `价_当日/价_报告期` 折到当日。
    if mkt_cap is None and fund is not None:
        try:
            from . import hk as _HK
            _eq = _HK.fetch_hk_equity(code, pro=pro, end_date=getattr(fund, "end_date", ""))
            _cap = _HK.hk_current_cap_reporting(
                close=close, total_market_cap=getattr(fund, "total_market_cap", None),
                pb_ttm=getattr(fund, "pb_ttm", None), bps=getattr(fund, "bps", None),
                equity=_eq, em_total_mv=em_total_mv)
            mkt_cap = _cap.mkt_cap
            if _cap.mkt_cap is not None:
                warns.append(f"市值口径：{_cap.basis}")
            if _cap.why:
                reasons.append(f"cap_basis:{_cap.why}")
        except Exception as exc:                              # noqa: BLE001
            reasons.append(f"cap_failed:{type(exc).__name__}")
    try:
        cf = pro.hk_cashflow(ts_code=code)
        inc = pro.hk_income(ts_code=code)
    except Exception:                                          # noqa: BLE001
        return QuickTurtle(code=code, reasons=["hk_fetch_failed"])
    # ⚠️ **不要**对港股行转列数据套 `_dedupe_annual`（那是给 A 股"一行一报表"用的）
    cfp = _pivot(cf.to_dict("records"), _HK_CF) if (cf is not None
              and not getattr(cf, "empty", True)) else {}
    incp = _pivot(inc.to_dict("records"), _HK_INC) if (inc is not None
               and not getattr(inc, "empty", True)) else {}
    if not cfp.get("n_cashflow_act"):
        return QuickTurtle(code=code, reasons=["no_cashflow"])

    ocf_y = cfp["n_cashflow_act"]
    capex_y = cfp.get("c_pay_acq_const_fiolta", {})
    rows = []
    for y, ocf in ocf_y.items():
        capex = capex_y.get(y)
        if ocf is None or capex is None:
            continue
        deduct = sum(v for f in _DEDUCT_FIELDS
                     for v in [cfp.get(f, {}).get(y)] if v is not None and v > 0)
        rows.append((y, ocf, abs(capex), deduct))
    rows.sort(key=lambda t: t[0], reverse=True)
    rows = rows[:_LOOKBACK_YEARS]
    prof, why = D.aa_profile(rows)
    if prof is None:
        return QuickTurtle(code=code, reasons=[why or "no_cashflow"])
    years = [y for y, *_ in rows]

    ni_by_year = incp.get("n_income_attr_p", {})
    eps_by_year = incp.get("basic_eps", {})
    if not ni_by_year:
        reasons.append("no_income")
    paid_by_year = cfp.get("c_pay_dist_dpcp_int_exp", {})
    # 股息只有单一来源（dps_hkd，报告期口径）⇒ 摊到最近 3 年，法3 与法1 同
    dps_by_year = {}
    if dps_hkd:
        for y in years[:3]:
            dps_by_year[y] = dps_hkd
    else:
        reasons.append("no_dividend")

    m1, m2, m3 = _m_methods(dps_by_year, eps_by_year, paid_by_year, ni_by_year, years)
    ni_latest = ni_by_year.get(years[0]) if years else None
    # A 股判据（canonical）：逐年 DPS 在 ≥3 年完全相同 ⇒ 填充错误，须走法2
    dps_seq = [dps_by_year[y] for y in years[:3] if dps_by_year.get(y) is not None]
    identical = len(dps_seq) >= 3 and len(set(round(v, 6) for v in dps_seq)) == 1
    qt = _assemble(prof.aa_selected, m1, m2, m3, mkt_cap, close, ii, code,
                   len(rows), q_pct, o, years[0] if years else "", reasons,
                   net_profit_yuan=(ni_by_year.get(years[0]) if years else None))
    # 股息率：港股**直接用 Tushare 的 `dividend_rate`**（避免自己算的舍入与口径差）
    qt.dividend_yield_pct = getattr(fund, "dividend_rate", None) if fund is not None else None
    if warns:
        qt.warnings = warns + list(qt.warnings)
    return qt
