"""港股专用：静态信息 / 逐只基本面 / 交易日历（全部走 Tushare）。

本模块解决的三个具体问题
------------------------
1. **静态信息**（`hk_basic`，1 次调用）：`market`（主板/GEM）· **`curr_type`（货币！）** ·
   `list_date` · `trade_unit`（每手股数）· `isin`。
   ⚠️ `curr_type` 很重要：港股有以 **CNY/USD** 计价的柜台，**币种不能只看 `.HK` 后缀**。
2. **逐只基本面**（`hk_fina_indicator`，须逐只）：每股（eps/bps/**dps_hkd**）、
   盈利（roe/roa/毛利率/净利率）、增长、负债、**估值（pe_ttm/pb_ttm/total_market_cap）**。
   实测：单只 96 行 × 18 列；连续 15 次调用 15/15 成功，**1.73s/次**，无可见限频。
   ⚠️ 这些估值是**报告期口径**，不是当日口径（实测 00700：反推价 374.88 vs 当日 423.40）
   —— 对**宽松筛选**够用；**当日价另走 quotes.py（Yahoo，不占 Tushare 配额）**。
3. **交易日历**（`hk_tradecal`）：**现成接口**，不需自建。
   （我早先查 `trade_cal[HKEX]` 得 0 行是**用错了接口** —— 这条结论已作废。）
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

__all__ = ["HkStatic", "HkFundamental", "HkCap", "fetch_hk_static", "fetch_hk_fundamentals",
           "fetch_hk_equity", "hk_current_cap_reporting", "hk_calendar", "hk_trading_days",
           "FUNDAMENTAL_FIELDS", "EQUITY_NAMES"]

#: 逐只拉取的字段。**不要省略 `dps_hkd`** —— 港股唯一的分红来源，股息率 = dps_hkd / 价。
FUNDAMENTAL_FIELDS = ("ts_code,end_date,basic_eps,diluted_eps,eps_ttm,bps,dps_hkd,"
                      "roe_avg,roa,gross_profit_ratio,net_profit_ratio,"
                      "operate_income_yoy,holder_profit_yoy,debt_asset_ratio,current_ratio,"
                      # ↓ 手册新发现：`dividend_rate`=股息率(%)、`divi_ratio`=分红率、
                      #   `hksk_market_cap`=港股通市值。股息率**直接用**，不必自己算。
                      "pe_ttm,pb_ttm,total_market_cap,hksk_market_cap,divi_ratio,dividend_rate")

#: `hk_balancesheet` 里权益合计的 `ind_name`（**实测确认**，见 2026-10-15 核对：
#: 06862 `股东权益`=9,765,180,000 / `总权益`=9,773,262,000，两者隐含 fx 均 ≈0.868，
#: 与手册「实测 5/5 落在 0.865」一致）。按优先级排列。
EQUITY_NAMES = ("股东权益", "总权益")


def _num(v: Any) -> Optional[float]:
    """NaN 是真值 —— 必须显式判，不能用 `v or 0`（本仓库已因这个陷阱吃过一次大亏）。"""
    if v is None or v == "-" or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _is_annual(end_date: Any) -> bool:
    """``end_date`` 是否为年度报告（1231）。"""
    e = str(end_date or "")
    return len(e) >= 8 and e[4:8] == "1231"


def _text(v: Any) -> str:
    if v is None:
        return ""
    t = str(v).strip()
    return "" if t.lower() in ("nan", "none", "-") else t


def _pro(token: Optional[str] = None):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import get_token
    import tushare as ts
    return ts.pro_api(token or get_token())


@dataclass
class HkStatic:
    code: str
    name: str = ""
    fullname: str = ""
    market: str = ""            # 主板 / GEM
    curr_type: str = ""         # ⚠️ HKD / CNY / USD
    list_date: str = ""
    delist_date: str = ""
    trade_unit: Optional[float] = None    # 每手股数
    isin: str = ""
    list_status: str = ""


@dataclass
class HkFundamental:
    code: str
    end_date: str = ""
    eps_ttm: Optional[float] = None
    bps: Optional[float] = None
    dps_hkd: Optional[float] = None       # 每股股息（HKD）
    roe_avg: Optional[float] = None
    gross_profit_ratio: Optional[float] = None
    net_profit_ratio: Optional[float] = None
    debt_asset_ratio: Optional[float] = None
    current_ratio: Optional[float] = None
    pe_ttm: Optional[float] = None        # **报告期**口径
    pb_ttm: Optional[float] = None        # **报告期**口径（Tushare 内部已做 HKD→报表币种换算）
    total_market_cap: Optional[float] = None   # **原始港元**，报告期口径
    hksk_market_cap: Optional[float] = None    # 港股通市值（原始港元）
    divi_ratio: Optional[float] = None         # 分红率（Tushare 直接给，可交叉校验 M_pess）
    dividend_rate: Optional[float] = None      # 股息率 %（**直接用**，不必自己算）

    def dividend_yield(self, price: Optional[float]) -> Optional[float]:
        """股息率 % = dps_hkd / 当日价 × 100。

        分子是**去年/当期已宣派**的每股股息，分母是**当日价** —— 这是有意的：
        分红是已发生的事实，收益率要用当前买入成本衡量。
        """
        if self.dps_hkd is None or not price or price <= 0:
            return None
        return self.dps_hkd / price * 100


def fetch_hk_static(*, token: Optional[str] = None, pro=None) -> list[HkStatic]:
    """`hk_basic` 一次调用拿全部可交易港股（实测 2,788 行）。"""
    pro = pro or _pro(token)
    df = pro.hk_basic(fields="ts_code,name,fullname,market,list_status,list_date,"
                             "delist_date,trade_unit,isin,curr_type")
    if df is None or getattr(df, "empty", True):
        raise RuntimeError("hk_basic 返回空 —— 检查权限（需 2000 积分）")
    out = []
    for r in df.to_dict("records"):
        code = _text(r.get("ts_code"))
        if not code:
            continue
        out.append(HkStatic(
            code=code, name=_text(r.get("name")), fullname=_text(r.get("fullname")),
            market=_text(r.get("market")), curr_type=_text(r.get("curr_type")),
            list_date=_text(r.get("list_date")), delist_date=_text(r.get("delist_date")),
            trade_unit=_num(r.get("trade_unit")), isin=_text(r.get("isin")),
            list_status=_text(r.get("list_status")),
        ))
    return out


def fetch_hk_fundamentals(codes: Iterable[str], *, token: Optional[str] = None, pro=None,
                          workers: int = 4, sleep: float = 0.25,
                          progress: Optional[Callable[[int, int, str], None]] = None,
                          ) -> tuple[dict[str, HkFundamental], list[str]]:
    """逐只拉 `hk_fina_indicator` 的**最新报告期**。

    返回 ``(ok_map, failed_codes)`` —— 失败**不静默**，由调用方决定是否 fail-closed。

    并发度默认 4 + 每次间隔 0.25s：实测串行 1.73s/次（含 0.3s 间隔）。并发是为了缩短
    500 只的墙上时间，但**保留节流**以免触发未记录的上游限频。
    """
    pro = pro or _pro(token)
    codes = list(codes)
    ok: dict[str, HkFundamental] = {}
    failed: list[str] = []
    done = {"n": 0}

    def one(code: str) -> Optional[HkFundamental]:
        try:
            df = pro.hk_fina_indicator(ts_code=code, fields=FUNDAMENTAL_FIELDS)
        except Exception:                                     # noqa: BLE001
            return None
        if df is None or getattr(df, "empty", True):
            return None
        # **必须按 end_date 降序取最新** —— 上游返回顺序不保证（实测 96 行含多年）
        if "end_date" in df.columns:
            df = df.sort_values("end_date", ascending=False)
        r = df.iloc[0]
        time.sleep(sleep)
        return HkFundamental(
            code=code, end_date=_text(r.get("end_date")),
            eps_ttm=_num(r.get("eps_ttm")), bps=_num(r.get("bps")),
            dps_hkd=_num(r.get("dps_hkd")), roe_avg=_num(r.get("roe_avg")),
            gross_profit_ratio=_num(r.get("gross_profit_ratio")),
            net_profit_ratio=_num(r.get("net_profit_ratio")),
            debt_asset_ratio=_num(r.get("debt_asset_ratio")),
            current_ratio=_num(r.get("current_ratio")),
            pe_ttm=_num(r.get("pe_ttm")), pb_ttm=_num(r.get("pb_ttm")),
            total_market_cap=_num(r.get("total_market_cap")),
            # ↓ 手册新发现的三项：先前只加进了 `FUNDAMENTAL_FIELDS` 与 dataclass，
            #   但**没在这里装进对象** ⇒ 产物里股息率列为空（"字段拉了却没用上"）
            hksk_market_cap=_num(r.get("hksk_market_cap")),
            divi_ratio=_num(r.get("divi_ratio")),
            dividend_rate=_num(r.get("dividend_rate")),
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for code, res in zip(codes, ex.map(one, codes)):
            done["n"] += 1
            if res is None:
                failed.append(code)
            else:
                ok[code] = res
            if progress:
                progress(done["n"], len(codes), code)
    return ok, failed


def hk_calendar(start_date: str, end_date: str, *, token: Optional[str] = None,
                pro=None) -> list[dict]:
    """`hk_tradecal` —— 港股交易日历（单次最多 2000 条）。

    ⚠️ 不要用 `trade_cal(exchange='HKEX')`：实测返回 **0 行**（那是 A 股日历接口）。
    """
    pro = pro or _pro(token)
    df = pro.hk_tradecal(start_date=start_date, end_date=end_date)
    if df is None or getattr(df, "empty", True):
        raise RuntimeError(f"hk_tradecal({start_date},{end_date}) 返回空")
    return df.to_dict("records")


def hk_trading_days(start_date: str, end_date: str, *, token: Optional[str] = None,
                    pro=None) -> list[str]:
    """区间内的港股交易日（升序 ``YYYYMMDD``）。"""
    rows = hk_calendar(start_date, end_date, token=token, pro=pro)
    days = [_text(r.get("cal_date")) for r in rows
            if _num(r.get("is_open")) == 1 and _text(r.get("cal_date"))]
    return sorted(days)


@dataclass
class HkCap:
    """港股市值（**报表币种、当日口径**）及其推导依据。"""

    mkt_cap: Optional[float] = None        # 报表币种
    implied_fx: Optional[float] = None     # pb×权益/市值(HKD) ⇒ HKD→报表币种的隐含汇率
    reporting_ccy: str = ""                # "CNY" | "HKD" | "?"
    basis: str = ""                        # 口径说明（写进披露）
    why: str = ""


def fetch_hk_equity(code: str, *, token: Optional[str] = None, pro=None,
                    end_date: str = "") -> Optional[float]:
    """从 `hk_balancesheet`（行转列）取**归母权益**。``股东权益`` 优先，回落 ``总权益``。

    ⚠️ **必须与 `pb_ttm`/`bps` 同报告期**：`hk_fina_indicator` 的最新行可能是**中期**
    （实测 06862 最新为 20260630），若这里只取年度（1231）就会用 2025 年报的权益
    与 2026H1 的 pb 相除 ⇒ `implied_fx` 从 0.8685 偏到 0.8907（**2.6% 误差**）。
    故：给了 ``end_date`` 就精确取该期；否则取**最新可用期**（含中期）。
    """
    pro = pro or _pro(token)
    try:
        df = pro.hk_balancesheet(ts_code=code)
    except Exception:                                          # noqa: BLE001
        return None
    if df is None or getattr(df, "empty", True):
        return None
    recs = df.to_dict("records")
    target = str(end_date or "")[:8]
    if target:
        pool = [r for r in recs if str(r.get("end_date") or "") == target]
    else:
        latest = max((str(r.get("end_date") or "") for r in recs), default="")
        pool = [r for r in recs if str(r.get("end_date") or "") == latest]
    for name in EQUITY_NAMES:
        for r in pool:
            if str(r.get("ind_name") or "").replace(" ", "") == name:
                v = _num(r.get("ind_value"))
                if v:
                    return v
    return None


def hk_current_cap_reporting(*, close: Optional[float], total_market_cap: Optional[float],
                             pb_ttm: Optional[float], bps: Optional[float],
                             equity: Optional[float],
                             em_total_mv: Optional[float] = None) -> HkCap:
    """港股市值 → **报表币种 × 当日口径**（用户 2026-10-15 确认的 D-10 口径）。

    为什么不能直接用 ``total_market_cap``（原始港元）：
      港股报表金额是**发行人报告币种**（内地发行人多为人民币），而 `hk_cashflow` 算出的
      AA 也是该币种 ⇒ ``AA / total_market_cap`` **跨币种相除**，约 **18.5%** 的系统性偏差
      （分析侧靠 `fx.json` 解决，选股器对未分析过的港股没有该来源）。

    解法（只用 Tushare + 一个现价，**不需要股数、不需要 fx 来源**）：
      Tushare 的 `pb_ttm` **内部已做 HKD→报表币种换算**（手册实测 5/5 落在 0.865）。故::

          implied_fx        = pb_ttm × 股东权益 / total_market_cap     # HKD → 报表币种
          cap_报告期_报表币  = pb_ttm × 股东权益
          价_报告期_报表币   = pb_ttm × bps
          价_当日_报表币     = close × implied_fx
          cap_当日_报表币    = cap_报告期 × (价_当日 / 价_报告期)

    附带收益：``implied_fx`` 顺便判定**报表币种**（≈0.86–0.87 ⇒ 人民币；≈1.0 ⇒ 港币），
    比猜或维护映射表更可靠。

    ⚠️ 局限：`pb_ttm` 是**报告期**比率，其隐含汇率也是报告期的；若与今日汇率差异大，
    `implied_fx` 会有偏差。故把它写进披露，并优先采用东财的当日市值（可用时）。
    """
    if not all([total_market_cap, pb_ttm, equity]):
        return HkCap(why="缺 total_market_cap / pb_ttm / 股东权益，无法定口径")
    if total_market_cap <= 0 or pb_ttm <= 0 or equity == 0:
        return HkCap(why="输入非正，拒绝计算")
    implied_fx = pb_ttm * equity / total_market_cap
    cap_rep = pb_ttm * equity
    if implied_fx > 1.5 or implied_fx < 0.3:
        return HkCap(implied_fx=implied_fx, why=f"隐含 fx={implied_fx:.3f} 不合理，拒绝采用")
    ccy = "HKD" if abs(implied_fx - 1.0) < 0.03 else "CNY"
    # ① **东财优先**：它的 `total_mv` 是**当日口径**（Tushare 的 total_market_cap 是期末口径），
    #    配合本股隐含 fx 折到报表币种 ⇒ 同时满足「当日」与「报表币种」两个要求。
    #    用户要求：使用东财时必须报告 ⇒ 调用方在 SourceReport 里记，`basis` 里也写明。
    if em_total_mv and em_total_mv > 0:
        return HkCap(mkt_cap=em_total_mv * implied_fx, implied_fx=implied_fx,
                     reporting_ccy=ccy,
                     basis=f"报表币种({ccy})·**当日**口径 —— 东财当日市值 × 隐含fx "
                           f"{implied_fx:.4f}（东财源，见来源报告）")
    if not close or not bps or bps <= 0:
        return HkCap(mkt_cap=cap_rep, implied_fx=implied_fx, reporting_ccy=ccy,
                     basis="报表币种·**报告期**口径（缺现价/bps，未折到当日）",
                     why="缺 close 或 bps")
    price_rep_period = pb_ttm * bps
    price_rep_now = close * implied_fx
    if price_rep_period <= 0:
        return HkCap(mkt_cap=cap_rep, implied_fx=implied_fx, reporting_ccy=ccy,
                     basis="报表币种·报告期口径", why="报告期价非正")
    cap_now = cap_rep * (price_rep_now / price_rep_period)
    return HkCap(mkt_cap=cap_now, implied_fx=implied_fx, reporting_ccy=ccy,
                 basis=f"报表币种({ccy})·**当日**口径（隐含fx {implied_fx:.4f}；"
                       f"由 pb_ttm×权益 反推，非市场汇率）")
