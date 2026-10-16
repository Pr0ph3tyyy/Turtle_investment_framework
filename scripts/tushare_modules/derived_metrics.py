"""Turtle Investment Framework - DerivedMetricsMixin.

Section 17 derived metrics: financial trends, Factor 2/3/4 computations.
"""

from pathlib import Path

import pandas as pd

from format_utils import format_number, format_table, format_header
import re

from claim_ledger import UNIT_ENUM
from tushare_modules.constants import (II_FLOOR, II_SPREAD, ii_label, ii_threshold,
                                       interest_bearing_debt)

# ---------------------------------------------------------------------------
# 策略规则的**中立权威**（scripts/strategy_rules.json）
#
# 为什么要有这个文件：阈值原先只活在 markdown（shared_tables.md /
# judgment_examples_turtle.md）里，代码手工转抄，且抄了不止一份 ——
# `II_FLOOR` 在 constants.py 与 numeric_audit.py 各一份，λ 判据则是裸字面量 `3`。
# 现在规则集中在那个 JSON，docs 与代码都必须与它一致（tests/test_strategy_rules.py）。
# ---------------------------------------------------------------------------
#: 规则单点加载失败时的原因（None = 正常）。**必须被交付门禁读到**。
#: 为什么要有这个全局标记（known_issues §四十九）：规则文件缺失时，
#: 本模块会回落到内嵌默认值继续跑 —— 那意味着**产物可能与策略规则矛盾而无人知道**，
#: 而且"回落到旧常量"是最难察觉的失败方向。原先 `return {}` 连一句警告都没有，
#: 注释却写着"降级值必须可见"（**注释与实现不符**）。
STRATEGY_RULES_ERROR = None      # str | None（本模块未 import typing.Optional）


class StrategyRulesUnavailable(RuntimeError):
    """规则单点（`strategy_rules.json`）不可用 —— 不得声称"策略一致"。"""


def _load_strategy_rules() -> dict:
    global STRATEGY_RULES_ERROR
    import json
    p = Path(__file__).resolve().parents[1] / "strategy_rules.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        STRATEGY_RULES_ERROR = None
        return data
    except Exception as e:  # noqa: BLE001 —— 不崩，但**必须可见且阻断交付**
        STRATEGY_RULES_ERROR = f"{type(e).__name__}: {e}"
        import sys as _sys
        print("=" * 72, file=_sys.stderr)
        print(f"⛔ 策略规则单点不可用：{p}", file=_sys.stderr)
        print(f"   {STRATEGY_RULES_ERROR}", file=_sys.stderr)
        print("   ⇒ 本次运行回落到**内嵌默认值**。产物可能与 strategy_rules.json 矛盾，"
              "不得被视为策略一致。", file=_sys.stderr)
        print("=" * 72, file=_sys.stderr)
        return {}


_STRATEGY_RULES = _load_strategy_rules()
LA = _STRATEGY_RULES.get("lambda_reliability",
                         {"anomaly_above": 3.0, "anomaly_below": 0.0,
                          "normal_band": [0.3, 2.0]})
# 公式单点：本模块的决策算术改走 `scripts/turtle_core`（纯计算层，无取数无渲染）。
# 等价性由 `scripts/turtle_core_selfcheck.py` 机械验证（账本 ↔ turtle_core 逐字段相等）。
from turtle_core import decision as _tc_dec
from turtle_core import floors as _tc_floors
from turtle_core import o_input as _tc_o
from turtle_core import pessimistic as _tc_pess


class DerivedMetricsMixin:
    """Mixin providing derived metrics computation for TushareClient."""

    def _record_stability(self, param: str, value, threshold: float, detail: str,
                          below: float = None) -> bool:
        """登记一个**参数稳定性**判定，返回是否"不稳定"。

        为什么要有这个单点（known_issues §四十七）：
        原先 §17.5 的 AA 窗口守卫写成 `if _aa_gap > 30:`（**硬编码 30**），
        而告警文案用的是从 `strategy_rules` 读来的 `_aa_thr` ⇒ 改规则只改文案、
        不改判据（T3 的接线只接了一半）。现在判据与文案共用同一个阈值。
        同时把结论落到 `_store["stability"]`，让**产物自己带着这份事实**，
        而不是只活在 `instrument_audit` 的一次性扫描里。
        """
        unstable = False
        if value is not None:
            try:
                v = float(value)
                # ⚠️ 判据可能是**双边**的：λ 的异常是「>3 **或** <0」。
                # 初版只测上界 ⇒ λ=-0.317 被记成 stable（实测）。
                upper = threshold is not None and v > float(threshold)
                lower = below is not None and v < float(below)
                unstable = bool(upper or lower)
            except (TypeError, ValueError):
                unstable = False
        self._store.setdefault("stability", {})[param] = {
            "value": None if value is None else round(float(value), 3),
            "threshold": threshold,
            "unstable": unstable,
            "disposition": "disclose_only" if unstable else "ok",
            "detail": detail,
        }
        return unstable

    def _commitment_assessment(self, product_dir):
        """读 `commitment_execution.json` → 承诺执行置信度评估（`shared/commitment_confidence.py`）。

        为什么要有这一步（用户 2026-10-16 提出的方法）：对**回购类**承诺，静态取信公告额
        会严重高估——公告是"预算上限"，不是"已实现的分配"。06078 实测：若把「每年约5亿元」
        当年度现金回报，GG 由 0.58% 跳到 10.00%、买点 19.12 > 现价 9.56，**直接翻转结论**。
        正确做法是先查已实际发生的执行、评出置信度，再**只按已执行年化**取信。

        缺文件 ⇒ 返回 `None`（纯增量，不影响既有产物）。
        """
        try:
            import sys as _sys
            _shared = str(Path(__file__).resolve().parents[2] / "shared")
            if _shared not in _sys.path:
                _sys.path.insert(0, _shared)
            import commitment_confidence as _cc
        except Exception:  # noqa: BLE001 — 可选能力，缺了不能拖垮采集
            return None
        ex = _cc.load_execution(product_dir)
        if not ex:
            return None
        periods = _cc.periods_of(ex)
        if not periods:
            return None
        blk = ex.get("execution") or {}
        # 额度取**最近一个**金额承诺（越近越有约束力）；多个承诺不可相加
        commits = [c for c in (ex.get("commitments") or []) if c.get("amount_mm")]
        commits.sort(key=lambda c: str(c.get("announced_date") or ""))
        near = commits[-1] if commits else {}
        # 评估期 = 已记录月份数（含最后一段的半月按 0.5 计）
        months = 0.0
        for p in periods:
            m = str(p.get("month") or "")
            months += 0.5 if "-01~" in m or "~" in m else 1.0
        try:
            fx = float(self._fx_to_reporting(self._store.get("dividends_ts_code", "")) or 1.0)
        except Exception:  # noqa: BLE001
            fx = 1.0
        px = blk.get("avg_price_hkd")
        try:
            px = float(px) if px is not None else None
        except (TypeError, ValueError):
            px = None
        base = float((self._o_input_raw().o_annual_mm
                      if not self._o_input_raw().is_default else 0.0) or 0.0)
        a = _cc.assess(commitment_amount_mm=(near.get("amount_mm") or None),
                       executed_shares=_cc.total_executed_shares(ex),
                       avg_price=px, fx_to_reporting=fx,
                       elapsed_months=months, baseline_annual_mm=base,
                       commitment_label=str(near.get("commitment_id") or ""),
                       # 发行人自披露的执行总额优先（06078：中期业绩公告「已动用约1.2亿元」）
                       executed_amount_mm=blk.get("executed_cny_mm_to_20261015"))
        self._store["_commitment_assessment"] = a
        return a

    def _o_input_raw(self):
        """只读 `o.json`（不做承诺评估），供 `_commitment_assessment` 取基线，避免递归。"""
        cached = self._store.get("_o_input_raw_cache")
        if cached is not None:
            return cached
        path = self._store.get("_output_path")
        product_dir = Path(path).parent if path else Path(".")
        resolved, why = _tc_o.resolve_o(product_dir)
        if why:
            warn = getattr(self, "_warn", None) or getattr(self, "warn", None)
            msg = (f"o.json 被拒（{why}）⇒ O 按 0 处理（**非零 O 必须声明来源**）："
                   f"{_tc_o.explain_rejection(why)}")
            if callable(warn):
                warn(msg)
            else:
                self._store.setdefault("_o_input_warnings", []).append(msg)
        self._store["_o_input_raw_cache"] = resolved
        return resolved

    def _o_input(self):
        """O（注销型回购）的**单点**取值 —— 读 `{产物目录}/o.json`（`turtle_core.o_input`）。

        为什么走 sidecar 而不是直接读 `repurchase`（findings **D-3**）：Tushare 无法区分
        **注销型**与库存股回购，而龟龟的 O 只认真正减少股本的那类 ⇒ 必须由人/LLM 从年报确认。
        缺文件 ⇒ O=0（与旧行为逐字节相同，纯增量）；文件被拒 ⇒ **登记警告并同样按 0**，
        绝不静默忽略（有人以为非零 O 生效了而实际没有，是最坏的失败模式）。
        结果缓存在 `_store`，供 §15/§17.9/§17.11/§17.14 共用**同一个**值。

        **2026-10-16 增补**：若存在 `commitment_execution.json`，先用
        `_commitment_assessment` 评出「执行置信度 + 已执行年化额」。`o.json` **显式给出**时
        仍然优先（人/LLM 显式值 > 派生值），此时把两者是否一致登记为披露；`o.json` 为
        默认值（无文件）时改用评估出的**已执行年化额**。
        """
        cached = self._store.get("_o_input_cache")
        if cached is not None:
            return cached
        path = self._store.get("_output_path")
        product_dir = Path(path).parent if path else Path(".")
        raw = self._o_input_raw()
        a = self._commitment_assessment(product_dir)
        if a is None:
            self._store["_o_input_cache"] = raw
            return raw
        if not raw.is_default:
            # 显式 o.json 优先；与之不一致只作披露，不静默改数
            if abs(raw.o_annual_mm - a.effective_annual_mm) > max(1.0, a.effective_annual_mm * 0.05):
                self._store.setdefault("_data_warnings", []).append(
                    f"o.json 的 O={raw.o_annual_mm:.2f} 与「承诺执行评估」的取信额 "
                    f"{a.effective_annual_mm:.2f} 不一致（{a.describe()}）；"
                    "已按**显式 o.json** 取值，请核对两者口径是否重复或遗漏")
            self._store["_o_input_cache"] = raw
            return raw
        # o.json 缺省 ⇒ 用评估值（此时 O 有了可核来源，不再是"待年报确认"的 0）
        self._store.setdefault("_data_warnings", []).append(
            "O 由「承诺执行评估」派生（无显式 o.json）：" + a.describe())
        resolved = _tc_o.OInput(o_annual_mm=float(a.effective_annual_mm),
                                years=1, is_default=False,
                                source=f"commitment_execution.json ⇒ {a.describe()}",
                                confirmed_by="llm")
        self._store["_o_input_cache"] = resolved
        return resolved

    # ------------------------------------------------------------------
    # claim 账本辅助（2026-10-14：账本从「只有 §17.10/11/14 的 28 条」扩到覆盖
    # 全部决策单元格。为什么必须扩：门禁把「覆盖率」做成门槛后，28 条声明意味着
    # 报告里 96%+ 的数字永远无法获得机械信任 —— 门槛与账本必须同时长大，否则
    # 收紧门禁只会把交付路径锁死。）
    # ------------------------------------------------------------------
    @staticmethod
    def _ledger_time(income_df) -> str | None:
        try:
            return str(income_df.iloc[0].get("end_date") or "")[:4] or None
        except Exception:  # noqa: BLE001 — 时间戳缺失不应阻断登记
            return None

    def _amount_unit(self) -> str:
        """UNIT_ENUM-safe 金额单位。

        `_unit_label()` 在港股报表币种未确认时会渲染「百万（币种未确认）」——
        那是给**人读**的可恢复标注，不落在契约单位枚举里。声明账本的单位必须在
        枚举内，故此时退化为纯量级「百万」，币种由 basis 文本承担。
        """
        u = self._unit_label()
        return u if u in UNIT_ENUM else "百万"

    @staticmethod
    def _to_mm(value):
        """元 → 百万。

        §17 内部金额多为**原始元**（`format_number` 默认除以 1e6 才渲染成百万），
        而声明账本声明的单位是「百万」。登记前不换算就会写出 10^6 倍的错误声明
        —— 一个"机器可校验"的错误数字比裸数字更危险，因为它会带着 ✅ 通过校验。
        """
        try:
            return float(value) / 1e6
        except (TypeError, ValueError):
            return None

    def _price_per_share_unit(self) -> str:
        u = f"{self._price_unit()}/股"
        return u if u in UNIT_ENUM else "元/股"

    def _register_claim(self, ts_code: str, claim_id: str, indicator: str, value,
                        unit: str, *, basis: str = "", policy: str = "round:2",
                        note: str | None = None, section: str = "",
                        time: str | None = None):
        """登记一条声明；值不可得时**必须**给 note（null ≠ 0，契约硬校验）。

        **入库精度必须与声明的 policy 一致**：旧实现一律 `round(value, 2)`，而
        `cm17.2.rf` 声明的是 `round:4`、包内渲染 4 位小数（实测 600036/600941/601398
        的 Rf = 1.7670%）。于是账本存 1.77、包内写 1.7670 ⇒ 报告**照抄数据包自己的数字**
        反而被 fail-closed 的 claim 门禁拒绝，而抄错的 1.77% 反倒通过（独立审计 F6）。
        """
        if value is None and not note:
            note = "该值不可得（上游数据缺失或计算被 fail-closed 拒绝）"
        digits = 2
        m = re.search(r"round:(\d+)", str(policy or ""))
        if m:
            digits = max(2, int(m.group(1)))
        return self._claim_ledger(ts_code).add(
            claim_id, indicator,
            (round(value, digits) if isinstance(value, (int, float)) else None),
            unit, time=time, basis=basis, policy=policy, note=note, section=section)

    def _compute_financial_trends(self) -> str | None:
        """Compute §17.1: Financial trend summary (CAGR, debt ratios, net cash, payout)."""
        income_df = self._get_annual_df("income")
        bs_df = self._get_annual_df("balance_sheet")

        if income_df.empty or len(income_df) < 2:
            return None

        years_labels = [str(r["end_date"])[:4] for _, r in income_df.iterrows()]
        n_years = len(years_labels)

        lines = [format_header(3, "17.1 财务趋势速览"), ""]

        # --- Revenue & Net Profit series ---
        rev_series = [(y, self._safe_float(r.get("revenue"))) for y, (_, r) in zip(years_labels, income_df.iterrows())]
        np_series = [(y, self._safe_float(r.get("n_income_attr_p"))) for y, (_, r) in zip(years_labels, income_df.iterrows())]

        # CAGR calculation
        _cagr_notes: list[str] = []

        def _cagr(label: str, series: list[tuple[str, float | None]]) -> str:
            """CAGR with an explicit period, and **no silent base substitution**.

            旧实现把序列中的非正值整段过滤掉再算 CAGR，后果有二（06862 实测）：
            ① 期数被静默改变——营收用 4 期、净利润用 3 期，却共用「5年CAGR」列头，
               43.36% 实际是 2022–2025 的 3 期增长率，被当成 5 年增长率使用；
            ② 该列不再是它自称的那个窗口的指标，跨行不可比。
            正确行为：基年为正 ⇒ 渲染期数与基年；基年为负 ⇒ CAGR **无定义**，输出 N/A
            并注明（可另附明确标注期数的替代口径，但不得与其它行并列）。
            """
            pairs = [(y, v) for y, v in series if v is not None]
            if len(pairs) < 2:
                return "—"
            latest_year, latest = pairs[0]
            base_year, base = pairs[-1]
            if base > 0:
                n = len(pairs) - 1
                value = (latest / base) ** (1 / n) - 1
                _cagr_notes.append(
                    f"{label}：基年 {base_year}，**{n} 期**（{base_year}–{latest_year}）")
                return f"{value * 100:.2f}%"
            alt = ""
            positives = [(y, v) for y, v in pairs if v > 0]
            if len(positives) >= 2:
                alt_year, alt_base = positives[-1]
                n2 = len(positives) - 1
                alt_value = (latest / alt_base) ** (1 / n2) - 1
                alt = (f"；若改用首个盈利年 {alt_year} 为基年，{n2} 期 CAGR = "
                       f"{alt_value * 100:.2f}%（**口径不同，不得与其它行并列或相加**）")
            _cagr_notes.append(
                f"⚠️ {label}：基年 {base_year} 为负（{base:,.2f}），**CAGR 在该窗口无定义** "
                f"⇒ 本列记 N/A，不换基年、不静默丢弃{alt}")
            return "N/A（基年负）"

        rev_cagr = _cagr("营业收入", rev_series)
        np_cagr = _cagr("归母净利润", np_series)

        # --- Interest-bearing debt per year ---
        # 口径单点在 constants.interest_bearing_debt；**含租赁负债**（HKFRS 16 已资本化，
        # 是真实合同义务）。旧口径漏掉它，使广义净现金被系统性高估。
        def _interest_bearing_debt(row) -> float | None:
            # "科目存在但为 0" 与 "科目缺失" **必须区分**：前者要渲染 0.00，后者才是 "—"。
            # 旧写法 `total if (total or lease_only)` 把显式零负债也变成 "—"，连带
            # 有息负债率与广义净现金一起消失（无负债公司反而看不到净现金）——独立审计 P1。
            total = interest_bearing_debt(row, include_lease=True)
            any_present = False
            for _c in ("st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y",
                       "lease_liab", "lease_liab_noncur"):
                try:
                    _v = row.get(_c)
                except AttributeError:
                    _v = None
                if _v is not None and _v == _v:
                    any_present = True
                    break
            return total if any_present else None

        debt_series = []  # (year, debt_raw)
        debt_ratio_series = []  # (year, ratio_pct)
        net_cash_series = []  # (year, net_cash_raw)
        if not bs_df.empty:
            for _, r in bs_df.iterrows():
                year = str(r["end_date"])[:4]
                debt = _interest_bearing_debt(r)
                ta = self._safe_float(r.get("total_assets"))
                cash = self._safe_float(r.get("money_cap"))
                debt_series.append((year, debt))
                if debt is not None and ta and ta > 0:
                    debt_ratio_series.append((year, debt / ta * 100))
                else:
                    debt_ratio_series.append((year, None))
                if cash is not None and debt is not None:
                    net_cash_series.append((year, cash - debt))
                else:
                    net_cash_series.append((year, None))

        # --- Payout ratio per year ---
        payout_lookup = self._get_payout_by_year()
        payout_series = [(y, payout_lookup.get(y)) for y, _ in np_series]

        # --- Build table ---
        # Use income years as primary (most complete)
        def _fmt_val(val: float | None, divider: float = 1e6, is_pct: bool = False) -> str:
            if val is None:
                return "—"
            if is_pct:
                return f"{val:.2f}"
            return format_number(val, divider=divider)

        def _lookup(series: list[tuple[str, float | None]], year: str) -> float | None:
            for y, v in series:
                if y == year:
                    return v
            return None

        headers = ["指标"] + years_labels + ["5年CAGR"]
        rows = []

        # Revenue
        row = [f"营业收入（{self._unit_label()}）"]
        for y, v in rev_series:
            row.append(_fmt_val(v))
        row.append(rev_cagr)
        rows.append(row)

        # Net profit
        row = [f"归母净利润（{self._unit_label()}）"]
        for y, v in np_series:
            row.append(_fmt_val(v))
        row.append(np_cagr)
        rows.append(row)

        # Interest-bearing debt
        row = [f"有息负债（{self._unit_label()}）"]
        for y in years_labels:
            row.append(_fmt_val(_lookup(debt_series, y)))
        row.append("—")
        rows.append(row)

        # 租赁负债明细：口径必须**可见**。HKFRS 16 已把租赁负债全额资本化，
        # 它现在是"有息负债"的一部分；不单独列行的话，读者无法判断净现金里含不含它。
        lease_series = []
        if not bs_df.empty:
            for _, r in bs_df.iterrows():
                lease_series.append((str(r["end_date"])[:4],
                                     interest_bearing_debt(
                                         {k: r.get(k) for k in ("lease_liab",
                                                                "lease_liab_noncur")})))
        _lease_rendered = bool(any(v for _, v in lease_series))
        self._store["_lease_series_rendered"] = _lease_rendered
        if _lease_rendered:
            row = [f"其中：租赁负债（{self._unit_label()}）"]
            for y in years_labels:
                row.append(_fmt_val(_lookup(lease_series, y)))
            row.append("—")
            rows.append(row)

        # Debt/total_assets ratio
        row = ["有息负债/总资产（%）"]
        for y in years_labels:
            row.append(_fmt_val(_lookup(debt_ratio_series, y), is_pct=True))
        row.append("—")
        rows.append(row)

        # Net cash
        row = [f"广义净现金（{self._unit_label()}）"]
        for y in years_labels:
            row.append(_fmt_val(_lookup(net_cash_series, y)))
        row.append("—")
        rows.append(row)

        # Payout ratio
        row = ["股息支付率（%）"]
        for y in years_labels:
            row.append(_fmt_val(_lookup(payout_series, y), is_pct=True))
        row.append("—")
        rows.append(row)

        table = format_table(headers, rows, alignments=["l"] + ["r"] * (n_years + 1))
        lines.append(table)
        if _cagr_notes:
            lines.append("")
            for note in _cagr_notes:
                lines.append(f"> {note}")
        return "\n".join(lines)

    def _compute_factor2_inputs(self, ts_code: str) -> str | None:
        """Compute §17.2: Factor 2 input parameters (OE components, payout, threshold)."""
        income_df = self._get_annual_df("income")
        cf_df = self._get_annual_df("cashflow")

        if income_df.empty:
            return None

        years_labels = [str(r["end_date"])[:4] for _, r in income_df.iterrows()]
        n_years = len(years_labels)
        lines = [format_header(3, "17.2 因子2输入参数"), ""]

        # --- Per-year table: C, B, minority%, D&A, Capex, Capex/D&A, FCF ---
        headers = ["变量"] + years_labels
        rows = []

        # C = n_income_attr_p (already in millions after format_number)
        c_row = ["C 归母净利润"]
        b_row = ["B 少数股东损益"]
        min_pct_row = ["少数股东占比（%）"]
        for _, r in income_df.iterrows():
            c = self._safe_float(r.get("n_income_attr_p"))
            b = self._safe_float(r.get("minority_gain"))
            ni = self._safe_float(r.get("n_income"))
            c_row.append(format_number(c))
            b_row.append(format_number(b))
            if b is not None and ni and ni != 0:
                min_pct_row.append(f"{b / ni * 100:.2f}")
            else:
                min_pct_row.append("—")
        rows.extend([c_row, b_row, min_pct_row])

        # D&A and Capex from cashflow
        da_row = ["D 折旧与摊销"]
        capex_row = ["E 资本开支"]
        capex_da_row = ["Capex/D&A"]
        fcf_row = ["FCF = OCF - |Capex|"]
        da_vals = []  # for median calculation
        capex_vals = []  # for median calculation
        capex_da_vals = []

        if not cf_df.empty:
            # Align cashflow by year
            cf_by_year = {}
            for _, r in cf_df.iterrows():
                y = str(r["end_date"])[:4]
                cf_by_year[y] = r

            for y in years_labels:
                r = cf_by_year.get(y)
                if r is None:
                    da_row.append("—"); capex_row.append("—")
                    capex_da_row.append("—"); fcf_row.append("—")
                    continue

                depr = self._safe_float(r.get("depr_fa_coga_dpba"))
                amort_i = self._safe_float(r.get("amort_intang_assets"))
                amort_d = self._safe_float(r.get("lt_amort_deferred_exp"))
                da_components = [v for v in [depr, amort_i, amort_d] if v is not None]
                da = sum(da_components) if da_components else None

                capex = self._safe_float(r.get("c_pay_acq_const_fiolta"))
                ocf = self._safe_float(r.get("n_cashflow_act"))

                da_row.append(format_number(da) if da is not None else "—")
                capex_row.append(format_number(capex))
                if da and da > 0 and capex is not None:
                    ratio = abs(capex) / da
                    capex_da_row.append(f"{ratio:.2f}")
                    da_vals.append(da)
                    capex_vals.append(abs(capex))
                    capex_da_vals.append(ratio)
                else:
                    capex_da_row.append("—")
                if ocf is not None and capex is not None:
                    fcf_row.append(format_number(ocf - abs(capex)))
                else:
                    fcf_row.append("—")
        else:
            for _ in years_labels:
                da_row.append("—"); capex_row.append("—")
                capex_da_row.append("—"); fcf_row.append("—")

        rows.extend([da_row, capex_row, capex_da_row, fcf_row])

        table = format_table(headers, rows, alignments=["l"] + ["r"] * n_years)
        lines.append(table)
        lines.append("")

        # --- Summary variables ---
        summary_rows = []

        # F = Capex/D&A 5-year median
        if capex_da_vals:
            sorted_vals = sorted(capex_da_vals)
            mid = len(sorted_vals) // 2
            f_median = sorted_vals[mid] if len(sorted_vals) % 2 else (sorted_vals[mid - 1] + sorted_vals[mid]) / 2
            summary_rows.append(["F（Capex/D&A 5年中位数）", f"{f_median:.2f}", "—"])
        else:
            summary_rows.append(["F（Capex/D&A 5年中位数）", "—", "数据不足"])

        # Payout ratio: M, N
        payout_lookup = self._get_payout_by_year()
        payout_ratios = [payout_lookup[y] for y in years_labels[:3] if y in payout_lookup]

        if payout_ratios:
            m_mean = sum(payout_ratios) / len(payout_ratios)
            if len(payout_ratios) > 1:
                variance = sum((x - m_mean) ** 2 for x in payout_ratios) / (len(payout_ratios) - 1)
                n_std = variance ** 0.5
            else:
                n_std = 0
            summary_rows.append(["M（支付率3年均值）", f"{m_mean:.2f}%", f"基于 {len(payout_ratios)} 年"])
            summary_rows.append(["N（支付率3年标准差）", f"{n_std:.2f}%", "—"])
        else:
            summary_rows.append(["M（支付率3年均值）", "—", "分红数据不足"])
            summary_rows.append(["N（支付率3年标准差）", "—", "—"])

        # O = buyback annual average (cancellation-type only)
        # Tushare does not provide repurchase purpose; default to 0.
        # Phase 3 should determine cancellation amount from annual report.
        _rep_df = self._store.get("repurchase")
        _o = self._o_input()
        # 如实区分四种状态：未声明(默认0) / 声明为0 / 确认是0 / 非零并注明来源与确认者。
        # 旧写法无论有无回购记录都印 "0.00 百万"，读者无法分辨"确认过"与"没确认"。
        _o_note = _o.describe()
        if _o.is_default and _rep_df is not None and not _rep_df.empty:
            _o_note += "；上游有回购记录但未确认注销型（须查年报）"
        elif _o.is_default:
            _o_note += "；上游无回购记录"
        summary_rows.append(["O（年均回购金额）", f"{_o.o_annual_mm:.2f} 百万", _o_note])

        # Rf and II (threshold)
        rf_df = self._store.get("risk_free_rate")
        rf_val = None
        if rf_df is not None and not rf_df.empty:
            rf_val = self._safe_float(rf_df.iloc[0].get("yield"))

        if rf_val is not None:
            summary_rows.append(["Rf（无风险利率）", f"{rf_val:.4f}%", "来自 §14"])
            # 单点口径：见 constants.II_THRESHOLDS（known_issues.md P1-12）
            summary_rows.append(["II（门槛值）", f"{ii_threshold(ts_code, rf_val):.2f}%",
                                 ii_label(ts_code, rf_val)])
        else:
            summary_rows.append(["Rf（无风险利率）", "—", "数据缺失"])
            summary_rows.append(["II（门槛值）", "—", "需Rf"])

        # OE base case (G=1.0)
        latest_c = self._safe_float(income_df.iloc[0].get("n_income_attr_p"))
        if latest_c is not None:
            summary_rows.append(["OE_base（G=1.0）", f"{format_number(latest_c)} 百万",
                                 "OE = C + D×(1-G); LLM 选 G 后代入"])

        # --- claim 账本：§17.2 的决策输入 ---
        # II 此前**没有登记任何声明**，是最终判定里唯一完全裸奔的关键量：
        # `KK = GG_pess − II` 只有一个操作数受机械校验，另一个可以随便写
        # （600941/002027 实测就写了已废止的 v1 公式 3.67%/3.5%）。
        _y = self._ledger_time(income_df)
        _amt = self._amount_unit()
        _ii_val = ii_threshold(ts_code, rf_val) if rf_val is not None else None
        self._register_claim(
            ts_code, "cm17.2.rf", "无风险利率 Rf", rf_val, "%",
            basis="§14 无风险利率（yc_cb 或 --rf 显式提供）", policy="round:4",
            section="17.2", time=_y,
            note=None if rf_val is not None else "§14 无风险利率不可用（接口无权限且未提供替代值）")
        self._register_claim(
            ts_code, "cm17.2.ii", "门槛值 II", _ii_val, "%",
            basis=(f"II = max({II_FLOOR}%, Rf+{II_SPREAD}%)，Rf={rf_val:.4f}%" if rf_val is not None
                   else "II 需要 Rf；数据包该行渲染为「—/需Rf」"),
            policy="round:2", section="17.2", time=_y,
            note=None if _ii_val is not None else "Rf 不可用 ⇒ II 无定义；不得以任何地板值替代")
        self._register_claim(ts_code, "cm17.2.c", "C 归母净利润（最新年）", self._to_mm(latest_c), _amt,
                             basis="§3 归母净利润，最新年报列", section="17.2", time=_y)
        self._register_claim(ts_code, "cm17.2.f", "F（Capex/D&A 5年中位数）",
                             f_median if capex_da_vals else None, "倍",
                             basis="§17.2 Capex/D&A 的 5 年中位数", section="17.2", time=_y,
                             note=None if capex_da_vals else "Capex 或 D&A 数据不足")
        self._register_claim(ts_code, "cm17.2.m", "M（支付率3年均值）",
                             m_mean if payout_ratios else None, "%",
                             basis=f"§6 DPS÷§3 EPS 的最近 {len(payout_ratios)} 年均值"
                                   "（注意：这是 §17.1 口径，与 §17.10 的 M_rec 可能不同）",
                             section="17.2", time=_y,
                             note=None if payout_ratios else "分红数据不足")

        summary_table = format_table(["汇总变量", "值", "说明"], summary_rows,
                                     alignments=["l", "r", "l"])
        lines.append(summary_table)
        return "\n".join(lines)

    def _compute_factor4_inputs(self, ts_code: str = "") -> str | None:
        """Compute §17.6: Price percentiles from 10yr weekly data."""
        wp_df = self._store.get("weekly_prices")
        basic_df = self._store.get("basic_info")

        if wp_df is None or wp_df.empty:
            return None

        lines = [format_header(3, "17.6 因子4·股价分位"), ""]

        closes = wp_df["close"].dropna().tolist()
        if not closes:
            return None

        nn = len(closes)
        current_price = closes[-1] if closes else None  # latest (sorted ascending)

        # Also try from basic_info
        if basic_df is not None and not basic_df.empty:
            bp = self._safe_float(basic_df.iloc[0].get("close"))
            if bp is not None:
                current_price = bp

        if current_price is None:
            return None

        # Current price percentile
        below_count = sum(1 for c in closes if c < current_price)
        current_percentile = below_count / nn * 100

        # Key percentile prices
        sorted_closes = sorted(closes)

        def _percentile_price(pct: float) -> float:
            idx = int(pct / 100 * (nn - 1))
            return sorted_closes[min(idx, nn - 1)]

        rows = [
            ["10年数据点数", str(nn)],
            ["当前股价", f"{current_price:.2f}"],
            ["当前股价历史分位", f"{current_percentile:.1f}%"],
            ["10%分位价格", f"{_percentile_price(10):.2f}"],
            ["25%分位价格", f"{_percentile_price(25):.2f}"],
            ["50%分位价格（中位数）", f"{_percentile_price(50):.2f}"],
            ["75%分位价格", f"{_percentile_price(75):.2f}"],
            ["90%分位价格", f"{_percentile_price(90):.2f}"],
        ]

        table = format_table(["指标", "值"], rows, alignments=["l", "r"])
        lines.append(table)

        # --- claim 账本：§17.6 分位（买点触发概率判定的输入） ---
        _pu = self._price_per_share_unit()
        self._register_claim(ts_code, "cm17.6.price", "当前股价", current_price, _pu,
                             basis=f"§2 最新价；分位窗口 {nn} 个周线点", section="17.6")
        self._register_claim(ts_code, "cm17.6.pct", "当前股价历史分位", current_percentile, "%",
                             basis=f"低于当前价的数据点 {below_count}/{nn}", policy="round:1",
                             section="17.6")
        for _p in (10, 25, 50, 75, 90):
            self._register_claim(ts_code, f"cm17.6.q{_p:02d}", f"{_p}% 分位价格",
                                 _percentile_price(_p), _pu,
                                 basis=f"{nn} 个周线点的最近秩分位（无插值）", section="17.6")
        return "\n".join(lines)

    def _compute_sotp_inputs(self) -> str | None:
        """Compute §17.7: SOTP holding company structure inputs from parent/consolidated BS."""
        bs_df = self._get_annual_df("balance_sheet")
        bs_parent_df = self._get_annual_df("balance_sheet_parent")

        if bs_df.empty or bs_parent_df.empty:
            return None

        lines = [format_header(3, "17.7 控股结构辅助"), ""]

        latest_consol = bs_df.iloc[0]
        latest_parent = bs_parent_df.iloc[0]

        def _debt(row):
            # 含租赁负债（口径单点，known_issues 2026-10-14）
            return interest_bearing_debt(row, include_lease=True)

        consol_debt = _debt(latest_consol)
        parent_debt = _debt(latest_parent)
        consol_cash = self._safe_float(latest_consol.get("money_cap")) or 0
        parent_cash = self._safe_float(latest_parent.get("money_cap")) or 0

        rows = [
            ["有息负债", format_number(consol_debt), format_number(parent_debt)],
            # ⚠️ 标签必须**消歧**（known_issues §四十四）：本节的「现金」只取 `money_cap`，
            # 而 §17.15/§17.16 的「现金」是 `money_cap + trad_asset`（它们自己声明为「狭义」）。
            # 同一个词在同一份包里指两个不同的量 ⇒ 读者/子代理会把 §17.7 的净现金
            # 当成 §17.16 的净现金引用（实测 61/99 个产物两者不等，差额恰好是交易性金融资产）。
            ["现金（仅货币资金）", format_number(consol_cash), format_number(parent_cash)],
            ["净现金（仅货币资金口径）",
             format_number(consol_cash - consol_debt), format_number(parent_cash - parent_debt)],
        ]

        if consol_debt > 0:
            sub_ratio = (consol_debt - parent_debt) / consol_debt * 100
            rows.append(["子公司层面负债占比", f"{sub_ratio:.1f}%", "—"])

        table = format_table(["指标", "合并口径", "母公司口径"], rows,
                             alignments=["l", "r", "r"])
        lines.append(table)
        return "\n".join(lines)

    # --- Feature #94: §17.8 EV baseline + "买入就是胜利"基准价 ---

    def _compute_factor4_ev_baseline(self, ts_code: str) -> str | None:
        """Compute §17.8: Valuation dashboard + floor-price baseline.

        Requires basic_info in _store (provides close, total_mv, total_share).
        All amounts in 百万元 unless stated otherwise.
        """
        basic_df = self._store.get("basic_info")
        if basic_df is None or basic_df.empty:
            return None

        bi = basic_df.iloc[0]
        close = self._safe_float(bi.get("close"))

        # 市值 / 股本 **统一经 `_get_mktcap_shares`**（单一来源）。
        # 旧实现让 §17.8 自己再写一遍 A/HK/US 分支 ⇒ 与 §17.9/§17.11 各算各的：
        # 一旦股本改用年报申报值（known_issues #25），港股会出现
        # 「§1/§17.8 用上游市值、§17.9/§17.11 用申报股本重算的市值」这种**同一份报告两个市值**。
        # 现在只有一个入口，§17.8 的 ①② 也自动吃到申报股本。
        ms = self._get_mktcap_shares(ts_code)
        if ms is None:
            return None
        mkt_cap_yuan, total_shares, close = ms
        if not mkt_cap_yuan or not total_shares or not close:
            # 本表的每股锚点与比率都需要三者
            return None
        mkt_cap = mkt_cap_yuan / 1e6  # 报表币种、百万

        # --- Gather data from _store ---
        income_df = self._get_annual_df("income")
        bs_df = self._get_annual_df("balance_sheet")
        cf_df = self._get_annual_df("cashflow")

        if income_df.empty or bs_df.empty or cf_df.empty:
            return None

        latest_inc = income_df.iloc[0]
        latest_bs = bs_df.iloc[0]
        latest_cf = cf_df.iloc[0]

        # Helper: interest-bearing debt components (yuan)
        def _ibd_yuan(row):
            total = 0.0
            # 含租赁负债（口径单点）；下面仍逐项累加以保留明细
            for c in ["st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y",
                      "lease_liab", "lease_liab_noncur"]:
                v = self._safe_float(row.get(c))
                if v:
                    total += v
            return total

        ibd_yuan = _ibd_yuan(latest_bs)
        cash_yuan = self._safe_float(latest_bs.get("money_cap")) or 0
        trad_yuan = self._safe_float(latest_bs.get("trad_asset")) or 0
        goodwill_yuan = self._safe_float(latest_bs.get("goodwill")) or 0
        total_assets_yuan = self._safe_float(latest_bs.get("total_assets")) or 0
        equity_yuan = self._safe_float(latest_bs.get("total_hldr_eqy_exc_min_int")) or 0

        oper_profit_yuan = self._safe_float(latest_inc.get("operate_profit")) or 0
        finance_exp_yuan = self._safe_float(latest_inc.get("finance_exp")) or 0
        np_parent_yuan = self._safe_float(latest_inc.get("n_income_attr_p")) or 0

        da_yuan = 0.0
        for c in ["depr_fa_coga_dpba", "amort_intang_assets", "lt_amort_deferred_exp"]:
            v = self._safe_float(latest_cf.get(c))
            if v:
                da_yuan += v

        ocf_yuan = self._safe_float(latest_cf.get("n_cashflow_act")) or 0
        capex_yuan = self._safe_float(latest_cf.get("c_pay_acq_const_fiolta")) or 0
        fcf_yuan = ocf_yuan - capex_yuan

        # Convert to 百万元
        ibd = ibd_yuan / 1e6
        cash = cash_yuan / 1e6
        trad = trad_yuan / 1e6
        goodwill = goodwill_yuan / 1e6
        ta = total_assets_yuan / 1e6
        equity = equity_yuan / 1e6
        oper_profit = oper_profit_yuan / 1e6
        fin_exp = finance_exp_yuan / 1e6
        np_parent = np_parent_yuan / 1e6
        da = da_yuan / 1e6
        fcf = fcf_yuan / 1e6

        # ===== Part A: Valuation indicators =====
        # 权威口径（本表所有比率一律用它，且与 §17.7 控股结构辅助保持一致）：
        #   现金   = 货币资金（money_cap）
        #   净负债 = 有息负债 − 货币资金        （负值 = 净现金）
        #   EBITDA = 营业利润 + 财务费用 + D&A
        #   FCF    = 经营现金流 − 资本开支        （**不是** FCFF / 企业自由现金流）
        ebitda = oper_profit + fin_exp + da
        net_debt = ibd - cash  # positive = net debt, negative = net cash

        # Tushare fina_indicator 的同名字段口径不同，**不得覆盖上表口径**：
        #   netdebt 并非"有息负债−货币资金"；fcff 是"企业自由现金流"而非 FCF。
        # 实测后果（002984，2026-10-13）：覆盖后 §17.8 与 §17.7 出现第三种现金口径
        # （EV 反解现金 2,101.73 ≠ 货币资金 2,242.01），FCF 收益率被写成 +2.88%
        # 而真实 FCF 为 −231.85 百万元（−1.59%）——符号相反。故此处仅作**对照记录**。
        fi_ref: dict[str, float] = {}
        fi_df = self._store.get("fina_indicators")
        if fi_df is not None and not fi_df.empty:
            fy_month_str = f"{self._fy_end_month:02d}"
            fi_annual = fi_df[fi_df["end_date"].str[4:6] == fy_month_str].sort_values(
                "end_date", ascending=False)
            if not fi_annual.empty:
                fi_row = fi_annual.iloc[0]
                for _key in ("ebitda", "netdebt", "fcff"):
                    _v = self._safe_float(fi_row.get(_key))
                    if _v is not None:
                        fi_ref[_key] = _v / 1e6

        ev = mkt_cap + net_debt
        net_cash = -net_debt

        ev_ebitda = f"{ev / ebitda:.2f}x" if ebitda > 0 else "—"
        cash_pe = f"{(mkt_cap - net_cash) / np_parent:.2f}x" if np_parent > 0 else "—"
        fcf_yield = f"{fcf / mkt_cap * 100:.2f}%" if mkt_cap > 0 else "—"
        pb = f"{mkt_cap / equity:.2f}x" if equity > 0 else "—"
        net_debt_ebitda = f"{net_debt / ebitda:.2f}x" if ebitda > 0 else "—"
        goodwill_ratio = f"{goodwill / ta * 100:.2f}%" if ta > 0 else "—"
        ibd_ratio = f"{ibd / ta * 100:.2f}%" if ta > 0 else "—"

        # Dividend yield: latest DPS / close
        div_yield_str = "—"
        div_df = self._store.get("dividends")
        latest_dps = None
        if div_df is not None and not div_df.empty:
            sorted_div = div_df.sort_values("end_date", ascending=False)
            latest_dps = self._safe_float(sorted_div.iloc[0].get("cash_div_tax"))
            if latest_dps is not None and close > 0:
                div_yield_str = f"{latest_dps / close * 100:.2f}%"

        lines = [format_header(3, '17.8 因子4·绝对估值与"买入就是胜利"基准价'), ""]

        # Valuation table
        lines.append("#### 估值指标")
        lines.append("")
        fmt = lambda v: format_number(v, divider=1)
        val_rows = [
            [f"总市值（{self._unit_label()}）", fmt(mkt_cap), "—"],
            [f"企业价值 EV（{self._unit_label()}）", fmt(ev), "市值+有息负债−货币资金"],
            [f"EBITDA（{self._unit_label()}）", fmt(ebitda), "营业利润+财务费用+D&A"],
            ["EV/EBITDA", ev_ebitda, "—"],
            ["扣除现金PE", cash_pe, "EV/归母净利润"],
            ["FCF收益率", fcf_yield, "（经营现金流−资本开支）/市值"],
            ["P/B", pb, "市值/归母权益"],
            ["净负债/EBITDA", net_debt_ebitda, "（有息负债−货币资金）/EBITDA，负值=净现金"],
            ["商誉/总资产", goodwill_ratio, "—"],
            ["有息负债率", ibd_ratio, "有息负债/总资产"],
            ["股息率", div_yield_str, "最新DPS/当前股价"],
        ]
        # Tushare fina_indicator 对照值：口径不同，单列且标注，不参与任何比率
        if "ebitda" in fi_ref:
            val_rows.append(["· 对照 EBITDA（Tushare fina_indicator）", fmt(fi_ref["ebitda"]),
                             "Tushare 口径 ≠ 营业利润+财务费用+D&A"])
        if "netdebt" in fi_ref:
            val_rows.append(["· 对照 净负债（Tushare netdebt）", fmt(fi_ref["netdebt"]),
                             "Tushare 口径 ≠ 有息负债−货币资金"])
        if "fcff" in fi_ref:
            val_rows.append(["· 对照 FCFF（Tushare fina_indicator）", fmt(fi_ref["fcff"]),
                             "企业自由现金流，**不是** FCF（经营现金流−资本开支）"])
        lines.append(format_table(["指标", "值", "说明"], val_rows,
                                  alignments=["l", "r", "l"]))
        lines.append("")
        _has_lease_row = bool((self._store.get("_lease_series_rendered") or False))
        lines.append("> **口径说明（含租赁）**：有息负债 = 短期/长期借款 + 应付债券 + 一年内到期"
                     "＋**租赁负债（流动+非流动）** —— HKFRS 16 已把租赁负债全额资本化，属真实"
                     "合同义务，不并入会系统性高估净现金（06862 实测：含租赁后净现金 6,203.16 → "
                     "2,696.65 百万元）。"
                     + ("明细见 §17.1「其中：租赁负债」。" if _has_lease_row
                        else "本标的各年度均无租赁负债明细行（金额为 0 或科目缺失）。"))
        lines.append("> **口径说明**：现金 = 货币资金；净负债 = 有息负债 − 货币资金（与 §17.7 一致）；"
                     "EBITDA = 营业利润 + 财务费用 + D&A；FCF = 经营现金流 − 资本开支（**非** FCFF）。"
                     "带「· 对照」的行来自 Tushare `fina_indicator`，口径与本表不同，**不参与任何比率计算**。")
        lines.append("")

        # ===== 股本口径（known_issues.md #25）=====
        # **必须放在这里**：`_get_mktcap_shares` 已解析出权威股本，而 §1 渲染得更早
        # ——那时 `fina_indicators` / `weekly_prices` 还没进 store，在 §1 落笔只会在
        # **降级值**上落笔（实测：§1 写 5,735.83、§17 用 5,574.00，两处打架）。
        # 放这里还能让数字、来源、是否降级三者同时可见。
        _sc = self._store.get("share_count")
        if _sc is not None:
            lines.append(_sc.as_note_line())
            for _w in (self._store.get("warnings_extra") or []):
                lines.append(f"> ⚠️ {_w}")
            lines.append("")

        # ===== Part B: "买入就是胜利" baselines =====
        lines.append('#### "买入就是胜利"基准价')
        lines.append("")

        baselines = []  # (name, value_in_price_unit, logic)

        # 每股价的币种：① ② ⑤ 由**报表币种**金额 ÷ 股数得到（即 `_per_share_unit()` 口径），
        # 而本表列头与 ③ ④ 是**交易币种**（`_price_unit()`）。两者不等价时必须折算，
        # 否则把 CNY/股 与 HKD/股 一起求算术平均 = 混币种（06862 实测：综合基准价
        # 9.34 里 ① 1.11、② 1.80 实为元/股，折算后应为 1.2963、2.0927，综合值 9.46）。
        fx_tr = self._fx_to_reporting(ts_code)
        if not fx_tr or fx_tr <= 0:
            fx_tr = 1.0
        fx_note = (f"；已按 1/{fx_tr:.5f} 由报表币种折为{self._price_unit()}"
                   if abs(fx_tr - 1.0) > 1e-12 else "")

        # **算术单点**：①②③④⑤ 与「综合底价 / 参考公允价」的语义分组全部由
        # `turtle_core.floors` 提供 —— screener 用的是同一份实现。
        # ⚠️ 分组改用 `FloorAnchors` 的**显式字段**，不再靠行名前缀
        # `startswith(("①","②","③"))`：前缀一改（或新增带圈数字）就**静默错分组**，
        # 而错分组的后果是把收益率型公允价混进底价（06862 的 9.44 事故）。
        low10y = div_implied = fcf_cap = None

        # ① Net liquid assets / share
        nla = _tc_floors.anchor_nla(cash_yuan, trad_yuan, ibd_yuan, total_shares, fx_tr)
        baselines.append(("① 净流动资产/股", nla,
                          "(现金+交易性金融资产−有息负债)/总股本" + fx_note))

        # ② BVPS
        bvps = _tc_floors.anchor_bvps(equity_yuan, total_shares, fx_tr)
        baselines.append(("② 每股净资产", bvps, "归母权益/总股本" + fx_note))

        # ③ 10-year low from weekly prices
        wp_df = self._store.get("weekly_prices")
        if wp_df is not None and not wp_df.empty:
            # 窗口（近 10 年）由 §11 取数保证：A 股传 start_date、港/美用 range=10y
            low10y = _tc_floors.anchor_low_10y(wp_df["close"].dropna().tolist())
            if low10y is not None:
                baselines.append(("③ 10年最低价", low10y, "周线最低收盘价"))

        # ④ Dividend yield implied price: 3yr avg DPS / max(Rf, 3%)
        rf_df = self._store.get("risk_free_rate")
        rf_pct = None
        if rf_df is not None and not rf_df.empty:
            rf_pct = self._safe_float(rf_df.iloc[0].get("yield"))

        if div_df is not None and not div_df.empty and rf_pct is not None:
            sorted_div = div_df.sort_values("end_date", ascending=False)
            recent_dps = []
            for _, row in sorted_div.head(3).iterrows():
                v = self._safe_float(row.get("cash_div_tax"))
                if v is not None:
                    recent_dps.append(v)
            if recent_dps:
                div_implied = _tc_floors.anchor_div_implied(recent_dps, rf_pct)
                if div_implied is not None:
                    baselines.append(("④ 股息隐含价", div_implied,
                                      f"3年均DPS÷max(Rf,3%)"))

        # ⑤ Pessimistic FCF capitalization: min(5yr FCF) / Rf / total_shares
        if rf_pct is not None and rf_pct > 0:
            fcf_list = []
            for _, row in cf_df.iterrows():
                ocf_v = self._safe_float(row.get("n_cashflow_act"))
                cap_v = self._safe_float(row.get("c_pay_acq_const_fiolta"))
                if ocf_v is not None and cap_v is not None:
                    fcf_list.append(ocf_v - cap_v)
            if fcf_list and min(fcf_list) <= 0:
                lines.append("> ⑤ 悲观FCF资本化：跳过（存在负FCF年份）")
                lines.append("")
            if fcf_list and min(fcf_list) > 0:
                fcf_cap, _skip = _tc_floors.anchor_fcf_capitalized(
                    fcf_list, rf_pct, total_shares, fx_tr)
                if fcf_cap is not None:
                    baselines.append(("⑤ 悲观FCF资本化", fcf_cap,
                                      "min(5年FCF)÷Rf÷总股本" + fx_note))

        # Build baseline table
        bl_rows = []
        valid_prices = []
        for name, val, logic in baselines:
            bl_rows.append([name, f"{val:.2f}", logic])
            valid_prices.append(val)

        lines.append(format_table(["方法", f"基准价（{self._price_unit()}）", "计算逻辑"], bl_rows,
                                  alignments=["l", "r", "l"]))
        lines.append("")
        if abs(fx_tr - 1.0) > 1e-12:
            lines.append(f"> **币种说明**：本表各行一律为**交易币种（{self._price_unit()}）**。"
                         f"① ② ⑤ 由报表币种（{self._per_share_unit()}/股）按 1/{fx_tr:.5f} 折算后参与"
                         f"算术平均；未经折算的报表币种每股量**不得**与本表混用或求均值。")
            lines.append("")

        # ===== Part C: Composite baseline（**按语义分组**，不跨语义求均值）=====
        # ① ② ③ 是**底价型**锚点（净流动资产/股、每股净资产、10 年最低价）——回答"最差能值多少"；
        # ④ ⑤ 是**收益率型公允价**锚点（股息资本化、FCF 资本化）——回答"按某个收益率要求值多少"。
        # 两者经济语义不同：④ 用的是 3% 毛股息收益率的**公允价**，天然远高于清算/账面底价。
        # 旧实现把四项等权平均 ⇒ 06862 得到 9.44，**既不是底价也不是公允价**，且高于悲观档买点 8.67，
        # 使"低于基准线 — 买入就是胜利"这一**买点语义**的读数在本模型判定"不可买"的价位上触发。
        # 现改为分列两个读数，溢价与"低于底价"判定**只用底价型**。
        # 分组由 `FloorAnchors` 的**显式字段**给出（见上），不再依赖行名前缀
        _anchors = _tc_floors.FloorAnchors(nla=nla, bvps=bvps, low_10y=low10y,
                                           div_implied=div_implied, fcf_cap=fcf_cap)
        floor_prices = _anchors.floor_prices
        fair_prices = _anchors.fair_prices

        if valid_prices:
            floor_composite = (sum(floor_prices) / len(floor_prices)) if floor_prices else None
            fair_composite = (sum(fair_prices) / len(fair_prices)) if fair_prices else None

            # --- claim 账本：§17.8 ---
            # `cm17.8.floor` 是 I9 规则序不变式（综合底价 ≤ 采纳档买点）的**左操作数**；
            # 此前 §17.8 一条声明都没有，于是报告只能裸写底价，而裸数字永不失败 ——
            # I9 也就只能从正文正则抓，可被"两处同时改写"洗白。
            _pu = self._price_per_share_unit()
            _au = self._amount_unit()
            self._register_claim(ts_code, "cm17.8.mktcap", "总市值", mkt_cap, _au,
                                 basis="§1 市值；跨币种时按报表币种折算", section="17.8")
            self._register_claim(ts_code, "cm17.8.ev", "企业价值 EV", ev, _au,
                                 basis="市值 + 有息负债 − 货币资金", section="17.8")
            self._register_claim(ts_code, "cm17.8.ebitda", "EBITDA", ebitda, _au,
                                 basis="营业利润 + 财务费用 + D&A", section="17.8")
            for _cid, _label, _val, _unit in (
                ("cm17.8.ev_ebitda", "EV/EBITDA", (ev / ebitda if ebitda > 0 else None), "倍"),
                ("cm17.8.cash_pe", "扣除现金PE",
                 ((mkt_cap - net_cash) / np_parent if np_parent > 0 else None), "倍"),
                ("cm17.8.fcf_yield", "FCF收益率",
                 (fcf / mkt_cap * 100 if mkt_cap > 0 else None), "%"),
                ("cm17.8.pb", "P/B", (mkt_cap / equity if equity > 0 else None), "倍"),
                ("cm17.8.net_debt_ebitda", "净负债/EBITDA",
                 (net_debt / ebitda if ebitda > 0 else None), "倍"),
                ("cm17.8.goodwill_ratio", "商誉/总资产",
                 (goodwill / ta * 100 if ta > 0 else None), "%"),
                ("cm17.8.ibd_ratio", "有息负债率", (ibd / ta * 100 if ta > 0 else None), "%"),
            ):
                self._register_claim(ts_code, _cid, _label, _val, _unit,
                                     basis="§17.8 权威口径（与 §17.7 一致）", section="17.8")
            _slug = {"①": "nla", "②": "bvps", "③": "low10y", "④": "div_implied", "⑤": "fcf_cap"}
            for _name, _val, _logic in baselines:
                _k = _slug.get(_name[:1])
                if _k:
                    self._register_claim(ts_code, f"cm17.8.anchor_{_k}", _name, _val, _pu,
                                         basis=f"§17.8 基准价锚点：{_logic}", section="17.8")
            if floor_composite is not None:
                self._register_claim(ts_code, "cm17.8.floor", "综合底价（底价型锚点算术平均）",
                                     floor_composite, _pu,
                                     basis="①②③ 算术平均；**只**用于「低于底价」判定（买点语义）",
                                     section="17.8")
            if fair_composite is not None:
                self._register_claim(ts_code, "cm17.8.fair", "参考公允价（收益率型锚点算术平均）",
                                     fair_composite, _pu,
                                     basis="④⑤ 算术平均；仅作估值读数，**不得**用于买点判定",
                                     section="17.8")

            if floor_composite is not None:
                lines.append(f"**综合底价（底价型锚点算术平均，{len(floor_prices)} 项）"
                             f"= {floor_composite:.2f} {self._price_unit()}**")
            if fair_composite is not None:
                lines.append(f"**参考公允价（收益率型锚点算术平均，{len(fair_prices)} 项；"
                             f"**不得**与底价混算、**不得**单独用于买点判定）"
                             f"= {fair_composite:.2f} {self._price_unit()}**")
            if not floor_prices:
                lines.append("> ⚠️ **底价型锚点缺失**：本次仅有收益率型锚点 ⇒ **无法给出底价读数**，"
                             "亦不得据此发出「低于底价」信号（买点仍只由悲观档目标买入价决定）。")

            composite = floor_composite if floor_composite is not None else fair_composite

            if len(valid_prices) < 3:
                lines.append("*数据不足（有效方法<3），仅供参考*")

            # ===== Part D: Premium analysis（两个读数各司其职）=====
            # (a) 底价型 → 只回答"是否低于底价"（买点语义；**买点仍只由悲观档目标买入价决定**）
            if floor_composite is not None:
                p_floor = (close / floor_composite - 1) * 100
                lines.append(f"当前股价 {close:.2f} {self._price_unit()}，较**综合底价**"
                             f"（{floor_composite:.2f}）溢价 **{p_floor:+.1f}%**")
                if p_floor <= 0:
                    lines.append("→ **低于底价 — 买入就是胜利**（清算/账面/价格底三类锚点同时被跌破）")
                else:
                    lines.append(f"→ 高于底价 {p_floor:.1f}%：**不构成**「低于底价」读数"
                                 "（该读数是买点语义，买点仍只由悲观档目标买入价决定）")

            # (b) 收益率型 → 估值读数（安全边际/溢价档位）。语义上"公允价"才是估值的参照。
            if fair_composite is not None:
                premium = (close / fair_composite - 1) * 100
                lines.append(f"当前股价较**参考公允价**（{fair_composite:.2f}）溢价 "
                             f"**{premium:+.1f}%**（估值读数，非买点）")
            elif floor_composite is not None:
                premium = (close / floor_composite - 1) * 100
            else:
                premium = None

            # 档位只挂在**收益率型公允价**读数上（"安全边际/合理溢价"本就是相对公允价的说法）
            if premium is None:
                lines.append("→ **无收益率型锚点**，本次不给估值档位读数（底价读数见上）")
            else:
                if premium <= 0:
                    verdict = "低于公允价 — 安全边际充足（估值读数，买点另由悲观档决定）"
                elif premium <= 30:
                    verdict = "略低于/接近公允价 — 安全边际尚可"
                elif premium <= 80:
                    verdict = "合理溢价 — 需确认成长性"
                elif premium <= 150:
                    verdict = "较高溢价 — 依赖持续成长"
                else:
                    verdict = "显著溢价 — 高成长预期已定价"
                lines.append(f"→ {verdict}")

        return "\n".join(lines)

    # --- Feature #96: §17.9 Factor 4 earnings decline sensitivity ---

    def _compute_factor4_sensitivity(self, ts_code: str) -> str | None:
        """Compute §17.9: Earnings decline sensitivity tables.

        Shows how 穿透回报率 and 门槛价格 change under AA decline scenarios.
        Requires factor3_sensitivity (AA), basic_info (market cap, shares),
        risk_free_rate (II), dividends+income (M payout ratio).
        """
        # Read AA from factor3_sensitivity stored by _compute_factor3_sensitivity_base
        f3s = self._store.get("factor3_sensitivity")
        if not f3s:
            return None
        aa = f3s.get("aa_selected")
        if aa is None or aa == 0:
            return None

        # 市值 / 股本 / 现价 **统一经 `_get_mktcap_shares`**（单一来源）。
        # 旧实现把 §17.9 与 §17.11 的市值/股数分支各写一遍，于是在 #25 上漂移：
        # 两表都声称"同一 M_rec、可直接比较"，却可能用不同股数。现在只有一个入口，
        # 申报股本（shares.json / --shares）在两条链上同时生效。
        ms = self._get_mktcap_shares(ts_code)
        if ms is None:
            return None
        mkt_cap, total_shares, close = ms
        if not close or not total_shares:
            # 门槛价格需要现价与股数；缺任一则本表无法给出价格列
            return None

        # Read II (threshold) — single source of truth (known_issues.md P1-12)
        rf_val, ii = self._get_rf_ii(ts_code)
        if ii is None:
            return None

        # M：与 §17.11 同源（§17.10 的 M_rec），不再单独用 §17.1 的 3 年均值。
        # 旧实现用 m_pct(§17.1) 且不含 (1−Q)，与 §17.11 并存于同一份报告里，
        # 港股下两者相差 25%（1/0.8），而 phase3_valuation 又指示 Agent 引用本表
        # 的「门槛价格」—— 必须统一（known_issues.md P1-10）。
        income_df = self._get_annual_df("income")
        years_labels = [str(r["end_date"])[:4] for _, r in income_df.iterrows()] if not income_df.empty else []
        pc = self._store.get("payout_crosscheck")
        m_pct = pc.get("m_rec") if pc else None
        m_source = (pc.get("m_rec_label") if pc else None) or "§17.1 3年均值"
        if m_pct is None:
            payout_lookup = self._get_payout_by_year()
            payout_ratios = [payout_lookup[y] for y in years_labels[:3] if y in payout_lookup]
            m_pct = sum(payout_ratios) / len(payout_ratios) if payout_ratios else None
            m_source = "§17.1 3年均值（§17.10 不可用）"
        if m_pct is None:
            return None

        # 默认税率档：与 §17.11 表B 的默认 Q 列一致，使两表可直接比较
        _, q_default = self._unique_tax_rates(ts_code)
        keep = 1.0 - q_default / 100.0

        # O = 注销型回购年均（**单点**：与 §15/§17.11/§17.14 同值，见 `_o_input`）
        # ⚠️ **必须 ×1e6**：`o.json` / `cm17.11.o` 的单位是**百万**（与 §15 摘要、
        # `claim_ledger.UNIT_ENUM` 一致），而本节内部量纲是**元** ——
        # 实测插桩：`aa=2,126,605,006.91`（元）· `mkt_cap=22,533,626,329.52`（元）·
        # `o_val=123.4`（百万）。不加换算时 O 的贡献
        # = 123.4/22,533,626,329×100 = **0.00000055 pct**，效果等同"没接通"，
        # 而 claim 却如实写着 123.4 ⇒ 产物内部自相矛盾（findings D-3b）。
        # 这正是 known_issues P0-2 那类静默单位错，只不过量级是 10^6。
        o_val = self._o_input().o_annual_mm * 1e6

        # Base 穿透回报率（默认 Q 口径）
        gg_base = (aa * m_pct / 100 * keep + o_val) / mkt_cap * 100  # percent

        # 跨币种判定必须在**渲染单元格之前**得出：`tp = numerator/(ii×总股本)` 得到的是
        # **报表币种**每股金额，而表头写的是 `_price_unit()`（交易币种）。港股下差 1/fx
        # （06862 实测 10.28 实为人民币，同口径港元应为 11.98），且 `vs当前股价` 是拿它
        # 去比港元价 —— 6 行里 3 行符号翻转（known_issues D1）。
        # 此前只把该列的 **claim** 置 null，**单元格照印** ⇒ 报告与审计读的是单元格，
        # 于是「已封锁」在纸面上成立、在产物里不成立（findings D-2）。
        _fx_tr = self._fx_to_reporting(ts_code)
        _cross_ccy = abs((_fx_tr or 1.0) - 1.0) > 1e-9

        def _row(label: str, factor: float):
            aa_new = aa * factor
            # **算术单点**：GG 与门槛价格都由 `turtle_core.decision` 给出
            gg = _tc_dec.penetration_return(aa_new, m_pct, q_default, o_val, mkt_cap)
            vs_threshold = _tc_dec.margin(gg, ii)
            # 门槛价格 = 股东现金回报 / (II × 总股本)，仅在回报为正时有定义。
            # AA ≤ 0（如 600941 中国移动）时不存在解，输出 "—" 而非负价格。
            # `numerator` 仍用于**符号判定**（>0 才有解）；除法本身走单点。
            numerator = aa_new * m_pct / 100 * keep + o_val
            if numerator > 0 and ii > 0 and total_shares > 0 and not _cross_ccy:
                tp = _tc_dec.threshold_price(aa_new, m_pct, q_default, o_val,
                                             ii, total_shares)
                vs_price = (tp / close - 1) * 100 if close and close > 0 else 0
                tp_str, vs_str = f"{tp:.2f}", f"{vs_price:+.1f}%"
            else:
                # 跨币种时**单元格也置 "—"**，与 claim 的 fail-closed 一致：
                # 不可引用的数不得出现在产物里，否则读者/审计照样会引用它。
                tp_str, vs_str = "—", "—"
            return [
                label,
                format_number(aa_new),
                f"{gg:.2f}%",
                f"{vs_threshold:+.2f} pct",
                tp_str,
                vs_str,
            ]

        lines = [format_header(3, "17.9 因子4·业绩下滑敏感性"), ""]
        lines.append(f"> AA（真实可支配现金结余）= {format_number(aa)} {self._unit_label()}，"
                     f"M = {m_pct:.2f}%（{m_source}），O = {format_number(o_val)}，"
                     f"默认 Q = {q_default:g}%（到手 {keep * 100:.0f}%），"
                     f"II = {ii:.2f}%，市值 = {format_number(mkt_cap)} {self._unit_label()}")
        if _cross_ccy:
            lines.append("> ⚠️ **门槛价格列已整体置 `—`**：该列以报表币种计算却按交易币种"
                         f"（{self._price_unit()}）渲染，跨币种下相差 1/{_fx_tr:.5f}"
                         " ⇒ 不可引用。买点请用 §17.11 目标买入价或 §17.14 悲观档目标买入价。"
                         "（known_issues D1 / findings D-2）")
        else:
            lines.append("> 口径与 §17.11 表B 一致（同一 M_rec、同一默认 Q），两表数值可直接比较。")
        lines.append("")

        # Table 1: cumulative 10%/year decline over 1-3 years
        lines.append("#### 表1：逐年累积下滑（每年-10%）")
        lines.append("")
        headers1 = ["情景", "真实可支配现金结余", "穿透回报率", "vs 门槛", f"门槛价格（{self._price_unit()}）", "vs当前股价"]
        rows1 = [
            _row("基准", 1.0),
            _row("下滑1年 (×0.9)", 0.9),
            _row("下滑2年 (×0.9²)", 0.81),
            _row("下滑3年 (×0.9³)", 0.729),
        ]
        lines.append(format_table(headers1, rows1, alignments=["l", "r", "r", "r", "r", "r"]))
        lines.append("")

        # Table 2: single-year different decline magnitudes
        lines.append("#### 表2：单年不同下滑幅度")
        lines.append("")
        headers2 = ["下滑幅度", "真实可支配现金结余", "穿透回报率", f"门槛价格（{self._price_unit()}）", "vs当前股价"]
        rows2 = []
        for pct, factor in [("-10%", 0.9), ("-20%", 0.8), ("-30%", 0.7)]:
            r = _row(pct, factor)
            rows2.append([r[0], r[1], r[2], r[4], r[5]])
        lines.append(format_table(headers2, rows2, alignments=["l", "r", "r", "r", "r"]))

        # --- claim 账本：§17.9 情景行 ---
        # 门槛价格列是**已知缺陷**：`tp = numerator/(ii×total_shares)` 得到的是**报表币种**
        # 每股金额，而表头用 `_price_unit()`（交易币种）——港股下差 1/fx（06862 实测
        # 10.28 实为人民币，同口径港元应为 11.98），且 `vs当前股价` 拿它去比港元价，
        # 6 行里 3 行符号翻转（known_issues D1）。修复前该列**不得被引用**，故在跨币种
        # 标的上一律登记为 null（fail-closed：不可引用 ≠ 可以随便写），A 股币种一致时正常登记。
        _pu = self._price_per_share_unit()
        _fx = _fx_tr                     # 与上面**同一**判定，避免两处各算一次而漂移
        _claim_year = years_labels[0] if years_labels else None
        _scen = [("base", 1.0), ("y1", 0.9), ("y2", 0.81), ("y3", 0.729),
                 ("d10", 0.9), ("d20", 0.8), ("d30", 0.7)]
        for _tag, _k in _scen:
            _num = aa * _k * m_pct / 100 * keep + o_val
            _gg = _num / mkt_cap * 100
            self._register_claim(
                ts_code, f"cm17.9.{_tag}.gg", f"穿透回报率（{_tag}）", _gg, "%",
                basis=(f"AA×{_k:g}={format_number(aa * _k)}, M={m_pct:.2f}%（{m_source}）, "
                       f"Q={q_default:g}%, 市值={format_number(mkt_cap)}"),
                section="17.9", time=_claim_year)
            if _cross_ccy:
                self._register_claim(
                    ts_code, f"cm17.9.{_tag}.tpp", f"门槛价格（{_tag}）", None, _pu,
                    basis="该列当前以报表币种计算却按交易币种渲染（known_issues D1）",
                    section="17.9",
                    note="已按 fail-closed 封锁：门槛价格列在跨币种标的上不可引用；"
                         "请改用 §17.11 目标买入价或 §17.14 悲观档目标买入价")
            else:
                self._register_claim(
                    ts_code, f"cm17.9.{_tag}.tpp", f"门槛价格（{_tag}）",
                    (_num / (ii / 100 * total_shares) if (_num > 0 and ii > 0 and total_shares > 0)
                     else None), _pu,
                    basis=f"= AA×{_k:g}×M×(1−Q) ÷ (II×总股本)", section="17.9",
                    note=None if (_num > 0 and ii > 0 and total_shares > 0)
                    else "回报为负 ⇒ 门槛价格无解（不是 0，也不可当作负价格）")

        return "\n".join(lines)

    # --- Feature #92: §17.3-17.5 Factor 3 base case computations ---

    def _compute_factor3_step1(self) -> str | None:
        """Compute §17.3: True cash revenue (步骤1).

        Conservative base case:
        - Deduct AR increases (revenue not yet collected as cash)
        - Deduct contract liability decreases (consumed pre-collected cash)
        - Do NOT add back AR decreases or CL increases (conservative)
        Stores results in self._store["_true_cash_rev"] for §17.5.
        """
        income_df = self._get_annual_df("income")
        bs_df = self._get_annual_df("balance_sheet")

        if income_df.empty or bs_df.empty or len(income_df) < 2:
            return None

        lines = [format_header(3, "17.3 因子3·步骤1 真实现金收入（保守基准）"), ""]
        lines.append("> AR增加扣除，CL增加不加回。LLM 可根据例外规则（如白酒预收）调整。")
        lines.append("")

        rows = []
        true_cash_rev = {}
        cl_warnings = []
        # 逐年需要**上一年**的资产负债表值 ⇒ 至少要有 2 年（C3 硬门禁）
        for i in range(len(bs_df) - 1):
            cur = bs_df.iloc[i]
            prev = bs_df.iloc[i + 1]
            year = str(cur.get("end_date"))[:4]

            inc_match = income_df[income_df["end_date"] == cur.get("end_date")]
            if inc_match.empty:
                continue
            revenue = self._safe_float(inc_match.iloc[0].get("revenue"))
            if revenue is None:
                continue

            ar_cur_raw = cur.get("accounts_receiv")
            ar_prev_raw = prev.get("accounts_receiv")
            ar_cur = self._safe_float(ar_cur_raw) or 0
            ar_prev = self._safe_float(ar_prev_raw) or 0
            t = ar_cur - ar_prev
            if ar_cur_raw is None or ar_prev_raw is None:
                # 缺 AR 时 t=0 ⇒ 不扣应收增加 ⇒ 系统性**高估**现金收入，必须说出来
                cl_warnings.append(f"{year}: accounts_receiv 为空，AR变动=0 可能高估现金收入")

            cl_cur_raw = cur.get("contract_liab")
            cl_prev_raw = prev.get("contract_liab")
            cl_cur = self._safe_float(cl_cur_raw)
            cl_prev = self._safe_float(cl_prev_raw)
            u = (cl_cur - cl_prev) if (cl_cur is not None and cl_prev is not None) else 0.0
            if cl_cur_raw is None or cl_prev_raw is None:
                cl_warnings.append(f"{year}: contract_liab 为空，CL变动=0 可能影响现金收入")

            # 保守规则：AR 增加扣除、**AR 减少不加回**；CL 增加不加回、CL 减少扣除。
            # 与 §17.3 的 Python 实现（known_issues.md P1-11）一致。
            cash_rev = revenue - max(0.0, t) - max(0.0, -u)
            # 收款比率在收入 ≤0 时**无定义** ⇒ 渲染 "—"，不是 0.00%（0% 会被读成"一分钱没收到"）
            ratio = (cash_rev / revenue) if revenue else None
            true_cash_rev[year] = cash_rev
            rows.append([year, format_number(revenue), format_number(t), format_number(u),
                         format_number(cash_rev),
                         f"{ratio * 100:.2f}%" if ratio is not None else "—"])

        if not rows:
            return None

        lines.append(format_table(
            ["年份", "S 营业收入", "T 应收变动", "U 合同负债变动", "真实现金收入", "收款比率"],
            rows, alignments=["l", "r", "r", "r", "r", "r"]))
        if cl_warnings:
            lines.append("")
            for _w in cl_warnings:            # 引用块：与其余小节的告警形态一致
                lines.append(f"> ⚠️ {_w}")

        self._store["_true_cash_rev"] = true_cash_rev
        return "\n".join(lines)

    def _compute_factor3_step4(self) -> str | None:
        """Compute §17.4: Operating cash outflows W1–W4 (步骤4).

        W1 = 营业成本 + max(0, 应付净减少)
        W2 = 员工现金支出（利润表薪金福利 → 现金流量表 → SGA 兜底†，三级链）
        W3 = 所得税 − (Δ递延税资产 − Δ递延税负债)
        W4 = 财务费用（现金利息）
        Stores per-year totals in self._store["_w_total"].
        """
        income_df = self._get_annual_df("income")
        bs_df = self._get_annual_df("balance_sheet")
        cf_df = self._get_annual_df("cashflow")

        # W1/W3 都要用资产负债表（ΔAP、ΔDTA/ΔDTL）；没有它就只能拿 oper_cost/income_tax
        # 充数 ⇒ 表看起来完整却是错的。HEAD 在此返回 None，重建时丢了这条门禁（独立审计）。
        if income_df.empty or cf_df.empty or len(income_df) < 2 or bs_df.empty:
            return None

        lines = [format_header(3, "17.4 因子3·步骤4 经营性现金支出"), ""]

        rows = []
        w_total = {}
        fallback_used = False
        w_warnings: list[str] = []
        for i in range(len(income_df)):
            inc = income_df.iloc[i]
            year = str(inc.get("end_date"))[:4]
            cf_match = cf_df[cf_df["end_date"] == inc.get("end_date")]
            cf = cf_match.iloc[0] if not cf_match.empty else None

            # --- W1 供应商 ---
            _oper_cost_raw = inc.get("oper_cost")
            oper_cost = self._safe_float(_oper_cost_raw) or 0.0
            if _oper_cost_raw is None:
                w_warnings.append(f"{year}: oper_cost 为空，W1 可能偏低")
            ap_delta = 0.0
            if not bs_df.empty:
                bs_match = bs_df[bs_df["end_date"] == inc.get("end_date")]
                if not bs_match.empty:
                    j = bs_df.index.get_loc(bs_match.index[0])
                    if j + 1 < len(bs_df):
                        ap_cur = self._safe_float(bs_df.iloc[j].get("acct_payable")) or 0.0
                        ap_prev = self._safe_float(bs_df.iloc[j + 1].get("acct_payable")) or 0.0
                        ap_delta = ap_cur - ap_prev
            w1 = oper_cost + max(0.0, -ap_delta)

            # --- W2 员工（三级链：利润表 → 现金流量表 → SGA 兜底）---
            w2 = self._safe_float(inc.get("c_pay_to_staff"))
            w2_src = "利润表薪金福利支出"
            if w2 is None and cf is not None:
                w2 = self._safe_float(cf.get("c_pay_to_staff"))
                w2_src = "现金流量表支付给职工现金"
            if w2 is None:
                sga = sum(x for x in (self._safe_float(inc.get("sell_exp")),
                                      self._safe_float(inc.get("admin_exp")),
                                      self._safe_float(inc.get("rd_exp"))) if x is not None)
                w2 = sga or 0.0
                w2_src = "SGA 兜底†"
                # 只有**真的**取到 SGA 才算降级；三项全缺时 w2 仍是 0，
                # 此时打 † 脚注等于宣称做了一个并未发生的替代
                fallback_used = fallback_used or bool(sga)

            # --- W3 现金税款 ---
            _tax_raw = inc.get("income_tax")
            income_tax = self._safe_float(_tax_raw) or 0.0
            if _tax_raw is None and (self._safe_float(inc.get("total_profit")) or 0) > 0:
                w_warnings.append(f"{year}: income_tax 为空但利润总额>0，W3 可能偏低")
            dta_delta = dtl_delta = 0.0
            if not bs_df.empty:
                bs_match = bs_df[bs_df["end_date"] == inc.get("end_date")]
                if not bs_match.empty:
                    j = bs_df.index.get_loc(bs_match.index[0])
                    if j + 1 < len(bs_df):
                        dta_cur = self._safe_float(bs_df.iloc[j].get("defer_tax_assets")) or 0.0
                        dta_prev = self._safe_float(bs_df.iloc[j + 1].get("defer_tax_assets")) or 0.0
                        dtl_cur = self._safe_float(bs_df.iloc[j].get("defer_tax_liab")) or 0.0
                        dtl_prev = self._safe_float(bs_df.iloc[j + 1].get("defer_tax_liab")) or 0.0
                        dta_delta = dta_cur - dta_prev
                        dtl_delta = dtl_cur - dtl_prev
            w3 = income_tax - (dta_delta - dtl_delta)

            # --- W4 利息 ---
            w4 = self._safe_float(inc.get("finance_exp")) or 0.0

            total = w1 + w2 + w3 + w4
            w_total[year] = total
            rows.append([year, format_number(w1), format_number(w2), format_number(w3),
                         format_number(w4), format_number(total)])

        if not rows:
            return None

        lines.append(format_table(
            ["年份", "W1 供应商", "W2 员工", "W3 现金税", "W4 利息", "W 合计"],
            rows, alignments=["l", "r", "r", "r", "r", "r"]))
        if w_warnings:
            lines.append("")
            for _w in w_warnings:
                lines.append(f"> ⚠️ {_w}")
        if fallback_used:
            lines.append("")
            # 方向必须是「低估」：港股按性质列报时「营运支出」已含员工成本却不单列，
            # SGA 兜底会**少算**一个数量级（实测 06862：1,475.5 vs 年报 14,073.0 百万）。
            # 旧的"高估"写法会把 LLM 引向相反结论（独立审计 P1）。
            lines.append("> W2 取值链：① 利润表「薪金福利支出」→ ② 现金流量表「支付给职工现金」"
                         "→ ③ SGA 兜底。")
            lines.append("> † 表示已降级到 ③（销售+管理+研发费用）。港股按性质列报时"
                         "「营运支出」已包含员工成本而不单列，此兜底会显著**低估**"
                         "（实测 06862：1,475.5 vs 年报 14,073.0 百万）。")
        self._store["_w_total"] = w_total
        return "\n".join(lines)

    def _compute_factor3_sensitivity_base(self, ts_code: str = "") -> str | None:
        """Compute §17.5: AA（真实可支配现金结余），**OCF 锚定口径**。

        为什么必须 OCF 锚定（known_issues.md P0-1）：旧实现从营业收入倒推
        （真实现金收入 − W − 资本开支），于是重资产公司因为 W1 含 D&A 却未加回而系统性地
        算出**负的**可支配现金（中国移动 4/4 年为负，与 §5 的 OCF 直接矛盾）。
        现口径与文档因子3 一致：
            AA = 经营现金流 − |资本开支| − 文档扣减项①
        并保留倒推口径仅作**审计列**（逐行给出偏差%）。

        硬门禁（C3）：OCF 与资本开支至少 2 年可得，否则输出"本节未生成"并**不**写
        ``factor3_sensitivity`` —— 缺失 ≠ 零值，调用方据此停止而不是手算。
        """
        cf_df = self._get_annual_df("cashflow")
        if cf_df is None or cf_df.empty:
            return None

        lines = [format_header(3, "17.5 因子3·步骤7 可支配现金结余（OCF 锚定）"), "",
                 # 实体归属声明（known_issues §四十五）：这是**事实声明**，不是数值修正。
                 # 框架因子2 用归母净利润（C），因子3 的 AA 由合并报表构造 —— 两者口径
                 # 不同是**刻意**的（factor3 精算.md:281 明说"粗算/精算方法论差异的正常结果"）。
                 # 写下来的目的：下游不必去猜自己在混用什么口径。
                 "> **实体归属**：AA 的输入（真实现金收入 / 经营性现金支出 / 资本开支）"
                 "全部取自**合并报表** ⇒ AA 是**合并口径**，并非全部归母可得。"
                 "见 `strategy_rules.json` 的 `entity_basis` 契约。",
                 ""]
        lines.append("> **AA = 经营现金流 − |资本开支| − 文档扣减项①**（文档因子3 口径）。")
        lines.append("> ① 收购附属公司 ＋ 偿还融资租赁 ＋ 购买子公司少数股权 ＋ 已付利息(融资)，"
                     "各项只取 `max(0, v)`（流出为正、净流入为负）。")
        lines.append("> `已付利息(经营)` 已在 OCF 内，不重复扣除。**A 股不支持①**：标准字段集"
                     "没有这几项独立条目（混在 `c_pay_invest` 等科目里），故 A 股 AA 仍是上界 "
                     "—— 见 known_issues.md §3.21 21e。")
        lines.append("> 「倒推结余」列 = 旧口径 `真实现金收入 − W − 资本开支`，**仅作审计参考**，"
                     "不参与 AA。")
        lines.append("")

        # --- 硬门禁：OCF 与 capex 至少 2 年 ---
        usable = []
        for _, r in cf_df.iterrows():
            ocf_v = self._safe_float(r.get("n_cashflow_act"))
            cap_v = self._safe_float(r.get("c_pay_acq_const_fiolta"))
            if ocf_v is not None and cap_v is not None:
                usable.append((str(r.get("end_date"))[:4], ocf_v, abs(cap_v), r))
        if len(usable) < 2:
            # **必须登记为 missing 并返回 None**：旧写法把"本节未生成"渲染进正文后仍返回
            # 一个非空字符串，于是 assembly 按"返回了内容"把 §17.5 记成 `ok`（§17 状态契约
            # 承诺 `heading ⟺ ok`，而这里 heading 与 ok 同时成立却什么都没算出来）。
            # 结果是下游把"未生成"当成"已生成且可引用"（独立审计 P1）。
            self._section_registry().mark(
                "17.5", "missing", "no_cashflow",
                note="缺少可用的经营现金流 / 资本开支（至少需 2 年）；缺失 ≠ 零值，"
                     "不得手算替代")
            return None

        # 文档扣减项①（港股可得；A 股无独立科目）
        DEDUCT_FIELDS = ("c_pay_acq_sub", "c_pay_fin_lease", "c_pay_minority", "c_pay_int_finance")
        has_deduct_field = any(f in cf_df.columns for f in DEDUCT_FIELDS)

        # **算术单点**：逐年 AA（`ocf − |capex| − 扣减项①`）与四个变体
        # （aa_2y / aa_all / aa_latest / aa_excl / aa_selected）全部由
        # `turtle_core.decision.aa_profile` 计算 —— screener 用的是同一份实现，
        # 连「至少 2 年可用」的硬门禁也在那里统一表达。
        _deduct_rows = []
        for year, ocf, capex, r in usable:
            deduct = 0.0
            for f in DEDUCT_FIELDS:
                v = self._safe_float(r.get(f))
                if v is not None and v > 0:      # 净流入不扣（06049 实测 −5.94 是流入）
                    deduct += v
            _deduct_rows.append((year, ocf, capex, deduct))
        _prof, _prof_why = _tc_dec.aa_profile(_deduct_rows)
        if _prof is None:                        # 上面的 len(usable)<2 已拦过；这里是二道闸
            raise RuntimeError(f"aa_profile 拒绝了已通过门禁的输入：{_prof_why}")
        aa_by_year = dict(_prof.series)
        _deduct_by_year = {y: d for y, _o, _c, d in _deduct_rows}

        audit_by_year = {}
        audit_dev = []
        rows = []
        for year, ocf, capex, r in usable:
            deduct = _deduct_by_year[year]
            aa = aa_by_year[year]

            # 审计列：旧倒推口径（需要 §17.3 与 §17.4 的中间量）
            tcr = (self._store.get("_true_cash_rev") or {}).get(year)
            wt = (self._store.get("_w_total") or {}).get(year)
            if tcr is not None and wt is not None:
                recon = tcr - wt - capex
                audit_by_year[year] = recon
                if aa:
                    dev = (recon - aa) / abs(aa) * 100
                    audit_dev.append((year, dev))
                    rows.append([year, format_number(ocf), format_number(capex),
                                 format_number(deduct), format_number(aa),
                                 format_number(recon), f"{dev:+.1f}%"])
                else:
                    rows.append([year, format_number(ocf), format_number(capex),
                                 format_number(deduct), format_number(aa), format_number(recon), "—"])
            else:
                rows.append([year, format_number(ocf), format_number(capex),
                             format_number(deduct), format_number(aa), "—", "—"])

        lines.append(format_table(
            ["年份", "OCF（经营现金流）", "- 资本开支", "- 文档扣减项①", "= AA（OCF 锚定）",
             "倒推结余（审计）", "偏差%"],
            rows, alignments=["l", "r", "r", "r", "r", "r", "r"]))
        lines.append("")

        vals = [aa_by_year[y] for y, _, _, _ in usable]
        aa_2y, aa_all = _prof.aa_2y, _prof.aa_all
        aa_latest, aa_excl = _prof.aa_latest, _prof.aa_excl
        aa_selected = _prof.aa_selected

        self._store["factor3_sensitivity"] = {
            "aa_incl": aa_all,  # legacy key for backward compatibility
            "aa_all": aa_all,
            "aa_2y": aa_2y,
            "aa_excl": aa_excl,
            "aa_latest": aa_latest,
            "aa_selected": aa_selected,
            "aa_series": aa_by_year,
            "aa_basis": "ocf_minus_capex",
        }

        lines.append(f"- AA_2y（近2年均值，默认基准）= {format_number(aa_2y)} {self._unit_label()}")
        lines.append(f"- AA_all（全部年份均值）= {format_number(aa_all)} {self._unit_label()}")
        lines.append(f"- AA_excl（剔除负值年份均值）= {format_number(aa_excl)} {self._unit_label()}")
        lines.append(f"- AA_latest（最近一年，短视主义口径）= {format_number(aa_latest)} {self._unit_label()}")
        # AA_2y 是 §17.11/§17.14 的**默认基准**；它与全期均值差得离谱时，说明近两年
        # 异常（一次性损益/口径变化），必须让人看到 —— 重建时丢了这条守卫（独立审计 P1）。
        _aa_thr = (_STRATEGY_RULES.get("stability_gates") or {}).get(
            "aa_window_spread_pct", 30.0)
        if aa_2y is not None and aa_all is not None:
            # ⚠️ 分母必须 sign-safe：AA 可为负（负的可支配现金结余），
            # `÷ abs(AA_all)` 在跨零/趋零时给出无意义的百分比（instrument_audit 实测过
            # 民生银行 −204,060 vs −8,321.6 输出 2352%）。改用「绝对差 ÷ 两者绝对值均值」。
            _den = (abs(aa_2y) + abs(aa_all)) / 2
            _aa_gap = (abs(aa_2y - aa_all) / _den * 100) if _den > 0 else None
            _unstable = self._record_stability(
                "AA_window", _aa_gap, _aa_thr,
                f"AA_2y={format_number(aa_2y)} vs AA_all={format_number(aa_all)} {self._unit_label()}"
                + ("（两者异号或为负 ⇒ 相对差不可解释）"
                   if (aa_2y < 0) != (aa_all < 0) or aa_2y < 0 else ""))
            if _unstable and _aa_gap is not None:
                lines.append(f"  ⚠️ AA_2y 与 AA_all 差异 {_aa_gap:.1f}% > {_aa_thr:g}%"
                             f"（阈值来自 strategy_rules.stability_gates）"
                             f"⇒ **该参数降级为披露项，不得单独绑定决策**；"
                             f"请审核近2年是否存在非经常性高峰")

        # --- CV 与 λ ---
        income_df = self._get_annual_df("income")
        rev_by_year = {}
        for _, r in income_df.iterrows():
            year = str(r["end_date"])[:4]
            rev_by_year[year] = self._safe_float(r.get("revenue")) or 0

        import statistics

        all_revenues = [rev_by_year[y] for y in sorted(rev_by_year.keys()) if rev_by_year[y] > 0]
        cv = None
        if len(all_revenues) >= 2:
            rev_mean = statistics.mean(all_revenues)
            rev_stdev = statistics.pstdev(all_revenues)  # population stdev
            cv = rev_stdev / rev_mean if rev_mean > 0 else None
        if cv is not None:
            lines.append(f"- 收入波动率 CV = {cv * 100:.2f}%")

        lambda_vals = []
        aa_years = sorted(aa_by_year)
        for i in range(1, len(aa_years)):
            y_cur, y_prev = aa_years[i], aa_years[i - 1]
            delta_s = rev_by_year.get(y_cur, 0) - rev_by_year.get(y_prev, 0)
            if delta_s:
                lambda_vals.append((aa_by_year[y_cur] - aa_by_year[y_prev]) / delta_s)
        lambda_vals = lambda_vals[-3:]
        lambda_median = statistics.median(lambda_vals) if lambda_vals else None

        lambda_warnings = []
        if len(all_revenues) >= 3:
            lambda_revs = [rev_by_year.get(y, 0) for y in aa_years if rev_by_year.get(y, 0) > 0]
            if lambda_revs and min(lambda_revs) > 0:
                amplitude = max(lambda_revs) / min(lambda_revs) - 1
                if amplitude < 0.10:
                    lambda_warnings.append("历史收入波幅不足10%，λ外推可靠性低")
        if lambda_vals:
            signs = [1 if v >= 0 else -1 for v in lambda_vals]
            if len(set(signs)) > 1:
                lambda_warnings.append("ΔAA/ΔRevenue符号不一致，成本结构可能变化")
        if lambda_median is not None and (
                lambda_median > LA["anomaly_above"] or lambda_median < LA["anomaly_below"]):
            lambda_warnings.append(f"λ={lambda_median:.2f}异常，建议人工核查")

        # λ 登记：值 = |λ| 是否越界（用 LA，与规则单点一致）
        _lam_thr = LA["anomaly_above"]
        if lambda_median is not None:
            self._record_stability(
                "lambda", lambda_median, LA["anomaly_above"],
                f"λ={lambda_median:.3f}；异常判据 λ>{LA['anomaly_above']:g} 或 <{LA['anomaly_below']:g}"
                + (f"；另有 {len(lambda_warnings)} 条告警" if lambda_warnings else ""),
                below=LA["anomaly_below"])
        lambda_reliability = "正常"
        if len(lambda_warnings) >= 2 or (lambda_median is not None
                                        and (lambda_median > LA["anomaly_above"]
                                             or lambda_median < LA["anomaly_below"])):
            lambda_reliability = "多项警告或异常"
        elif len(lambda_warnings) == 1:
            lambda_reliability = "有一项警告"

        if lambda_median is not None:
            lines.append(f"- 经营杠杆系数 λ = {lambda_median:.4f}")
        lines.append(f"- λ可靠性 = {lambda_reliability}")
        for w in lambda_warnings:
            lines.append(f"  ⚠️ {w}")

        self._store["factor3_sensitivity"].update({
            "lambda_median": lambda_median,
            "lambda_reliability": lambda_reliability,
            "lambda_warnings": lambda_warnings,
            "cv": cv,
        })

        if not has_deduct_field:
            lines.append("")
            lines.append("> ⚠️ **A 股不支持文档扣减项①**：标准字段集没有收购/融资租赁/"
                         "少数股东股权/已付利息(融资)的独立条目，故本表的 AA 仍是"
                         "**可支配现金的上界**（known_issues.md §3.21 21e）。")
        if audit_dev and any(abs(d) > 20 for _, d in audit_dev):
            worst = max(audit_dev, key=lambda x: abs(x[1]))
            lines.append("")
            lines.append(f"> ⚠️ **倒推口径与 OCF 锚定口径偏差过大**"
                         f"（{len(audit_dev)}/{len(usable)} 年 > 20%，最大 {worst[0]} 年 "
                         f"{worst[1]:+.1f}%）。已知成因：`W1 = 利润表营业成本 + ΔAP` 含 D&A "
                         f"却未加回；港股按性质列报时 `oper_cost` / 员工成本可能整体缺失。"
                         f"**以 OCF 锚定口径为准**，倒推列仅作审计（known_issues.md P0-1）。")
        elif not audit_by_year:
            lines.append("")
            lines.append("> 倒推口径不可用（缺 §17.3/§17.4 的中间量）⇒ 本表无审计列；"
                         "AA（OCF 锚定）不受影响。")

        # --- claim 账本：§17.5 的 AA 变体（报告要"选用哪一个基准"） ---
        _y = self._ledger_time(income_df)
        _amt = self._amount_unit()
        _aa_basis = ("AA = 经营现金流 − |资本开支| − 文档扣减项①（OCF 锚定口径）"
                     + ("" if not aa_years else f"；窗口 {min(aa_years)}–{max(aa_years)}"))
        self._register_claim(ts_code, "cm17.5.aa2y", "AA_2y（近2年均值，默认基准）",
                             self._to_mm(aa_2y), _amt,
                             basis=_aa_basis, section="17.5", time=_y)
        self._register_claim(ts_code, "cm17.5.aaall", "AA_all（全部年份均值）",
                             self._to_mm(aa_all), _amt,
                             basis=_aa_basis, section="17.5", time=_y)
        self._register_claim(ts_code, "cm17.5.aaexcl", "AA_excl（剔除负值年份均值）",
                             self._to_mm(aa_excl), _amt,
                             basis=_aa_basis + "；注意：实现口径是『剔除负值年』，"
                                   "与文档『剔除非经营主导型年』不同（known_issues D5）",
                             section="17.5", time=_y)
        self._register_claim(ts_code, "cm17.5.aalatest", "AA_latest（最近一年）",
                             self._to_mm(aa_latest), _amt,
                             basis=_aa_basis, section="17.5", time=_y)
        # ⚠️ `cv` 是**比值**（0.2119），而 unit 声明为 `%` ⇒ 必须 ×100 再登记，
        # 否则账本值比数据包正文小 100 倍（06078 实测：账本 0.21% vs §17.5 正文 21.19%）。
        # 下游按"账本即真值"引用时会把它当 0.21% 的极低波动率，从而高估收入的稳定性与
        # 外推可信度 —— 与同一节 `λ` 的单位问题同源（该节正文一律 ×100 渲染）。
        self._register_claim(ts_code, "cm17.5.cv", "收入波动率 CV",
                             None if cv is None else cv * 100, "%",
                             basis="全部可得年度的收入总体标准差 ÷ 均值", section="17.5", time=_y,
                             note=None if cv is not None else "可用年度不足 2 年")
        self._register_claim(ts_code, "cm17.5.lambda", "经营杠杆系数 λ", lambda_median, "倍",
                             basis=f"ΔAA/ΔRevenue 最近 3 个年度对的中位数；可靠性={lambda_reliability}",
                             section="17.5", time=_y,
                             note=None if lambda_median is not None else "无有效年度对（ΔRevenue=0）")

        return "\n".join(lines)

    def _balance_sheet_share_raw(self):
        """`balancesheet.total_share`（原始**股**，期末申报值）；取不到返回 None。

        选最新 `end_date` 那一行 —— 与 `fina_indicators`（BPS/EPS 的来源）同期。
        实测单位是**股**（格力 5,601,406,000；兴业 21,163,000,000）。
        """
        bs = self._store.get("balance_sheet")
        if bs is None or getattr(bs, "empty", True):
            return None
        if "total_share" not in getattr(bs, "columns", []):
            return None
        df = bs
        if "end_date" in bs.columns:
            df = bs.sort_values("end_date", ascending=False)
        for _, row in df.iterrows():
            v = self._safe_float(row.get("total_share"))
            if v and v > 0:
                return v
        return None

    def _get_mktcap_shares(self, ts_code: str):
        """Return (mkt_cap_yuan, total_shares, close), replicating §17.9's market-cap
        branching (A股 total_mv 万元, HK total_market_cap **原始港元**, US total_mv raw).

        mkt_cap is in raw currency units (matching financial statements / AA / C).
        Returns None if basic_info or required fields are missing.
        """
        basic_df = self._store.get("basic_info")
        if basic_df is None or basic_df.empty:
            return None
        bi = basic_df.iloc[0]
        # `close` 只用于反推股数（进而算目标买入价）。市值本身来自
        # `total_market_cap`(HK) / `total_mv`(A/US)，**与收盘价无关**。
        #
        # 旧实现先 `if not close: return None`，把两者绑死 —— 于是收盘价缺失时
        # （实测 2026-10-13：`hk_daily` 配额用尽 → §1 当前价格 "—"、§2/§11 数据缺失）
        # 整个 §17.11/§17.13/§17.14 网格连带消失，而 GG/R 根本不需要 close。
        # 现改为：市值取不到才返回 None；close/股数缺失只降级目标买入价为 "—"。
        close = self._safe_float(bi.get("close"))
        _pe_cap, period_end_close, _pe_date = self._period_end_cap_and_close()

        if self._is_us(ts_code):
            total_mv_raw = self._safe_float(bi.get("total_mv"))  # raw USD
            if not total_mv_raw:
                return None
            mkt_cap = total_mv_raw
            _cap_current = total_mv_raw / 1e6
        elif self._is_hk(ts_code):
            trading_cap = self._hk_market_cap_raw(bi)  # 原始港元
            if not trading_cap:
                return None
            _cap_current = trading_cap / 1e6            # 百万港元
            mkt_cap = trading_cap * self._fx_to_reporting(ts_code)  # 报表币种
        else:
            total_mv_wan = self._safe_float(bi.get("total_mv"))  # 万元
            if not total_mv_wan:
                return None
            mkt_cap = total_mv_wan * 10000  # 元
            _cap_current = total_mv_wan / 100.0         # 百万元

        # 股本口径（known_issues.md #25，2026-10-14 用户拍板）：
        # **以年报的已发行股数为准**；反推值只在没有任何申报值时使用，并带降级标记。
        # 旧实现一律用「总市值 ÷ 当前价」，把期末口径的市值与当前价混用
        # （06862 实测 5,735.83 vs 年报 5,574.00 百万股，差 2.79%）。
        from shares_utils import resolve_shares
        upstream_wan = self._safe_float(bi.get("total_share"))     # A 股：万股
        sc = resolve_shares(
            # 资产负债表申报股数（期末口径，与 BPS/EPS 同期）—— 优先级高于 daily_basic
            bs_share_raw=self._balance_sheet_share_raw(),
            cli_value=self._store.get("shares_mm"),
            output_path=self._store.get("_output_path"),
            upstream_share_wan=upstream_wan,
            period_end_cap_mm=_pe_cap,
            period_end_close=period_end_close,
            period_end_date=_pe_date,
            current_cap_mm=_cap_current,
            current_close=close,
        )
        self._store["share_count"] = sc
        total_shares = sc.shares_mm * 1e6 if (sc.shares_mm and sc.shares_mm > 0) else None

        # ── 市值口径政策（用户 2026-10-14 拍板）──────────────────────────────
        # **市值 = 股数 × 当日价 × fx**。必须**逐市场**执行，不能一刀切：
        #
        # · **A 股**：`daily_basic.total_mv` 实测**精确等于** `total_share × close`
        #   （抽查 6 只，差 0.000%）⇒ 上游市值**本来就是当日口径**，且与股数自洽。
        #   若在这里再乘一遍，只会引入舍入噪声，故**不动**。
        # · **港股**：上游来自 `hk_fina_indicator.total_market_cap`，是**期末**口径。
        #   它与 §17.9（用股数）/§17.11/§17.14（用市值）的**当日决策**语义不同，
        #   混用会让同一份报告里两节对"同一个市值"给出不同的数
        #   （findings **D-10**：00696 实测 22,533.63 vs 25,329.59，差 **+12.41%**）。
        #   故港股**必须**按 股数 × 当日价 重算。
        #
        # 旧实现的缺陷：只在 `sc.trusted`（有**申报**股本）时才重算 ⇒ 绝大多数港股
        # （无 `shares.json`）仍带期末口径，D-10 因此在全池范围内存在。
        # 现改为：**只要有可用股数就重算**，并如实披露股数的来源（申报 / 反推降级）。
        if self._is_hk(ts_code) and sc.shares_mm and sc.shares_mm > 0 and close:
            recomputed_raw = sc.shares_mm * 1e6 * close          # 原始港元
            gap = abs(recomputed_raw - trading_cap) / trading_cap if trading_cap else 0.0
            if gap > 0.005:
                _prov = "申报股本" if sc.trusted else "**反推股本（降级值）**"
                self._store.setdefault("warnings_extra", []).append(
                    f"港股总市值已按**当日口径**重算：上游期末口径 {trading_cap/1e6:,.2f} 百万港元 "
                    f"vs {_prov}×当日价 {recomputed_raw/1e6:,.2f}（差 {gap:.2%}）"
                    f" ⇒ 采用后者（口径政策：市值=股数×当日价×fx，findings D-10）。"
                    f"该差来自期末到今日的价格变动，不是数据错误；"
                    f"但若股数为反推降级值，市值亦继承该降级")
            trading_cap = recomputed_raw
            mkt_cap = trading_cap * self._fx_to_reporting(ts_code)
        return mkt_cap, total_shares, close

    # -- 期末价 / 期末市值：**成对**取用，口径一致的反推基准 ---------------------
    def _annual_period_ends(self):
        """年度报告期（YYYYMMDD）集合 —— 取自 §3/§5 的年度序列，用来排除中期快照。

        为什么不能用"最新一行"：`fina_indicators` 经 `_prepare_display_periods` 保留了
        **比最新年报更新的中期列**（06862 包 §12 表头实测为 `2026H1 | 2025 | …`）。
        取最新一行就取到了中期快照。
        """
        ends = set()
        for key in ("income", "balance_sheet"):
            df = self._store.get(key)
            if df is None or getattr(df, "empty", True):
                continue
            cols = getattr(df, "columns", [])
            if "end_date" not in cols:
                continue
            ends |= {str(x) for x in df["end_date"].dropna().tolist()}
        return ends

    def _period_end_cap_and_close(self):
        """``(期末市值百万, 期末价, 期末日期)`` —— **必须成对**；取不到返回 ``(None, None, None)``。

        为什么成对：旧实现拿「最新 fina 快照」÷「最新周线」= 中期市值 ÷ 当前价，两者不同期，
        却标成「期末市值 ÷ 期末价（反推，口径一致）」—— 于是 #25 的原始错值 5,735.83
        换了个**可信标签**回来了（独立审计 F2 实测 06862）。宁可取不到、如实降级到
        `implied_current`（标签写明"口径混用"），也不要一个自称口径一致的错值。
        """
        fi = self._store.get("fina_indicators")
        if fi is None or getattr(fi, "empty", True) or "end_date" not in getattr(fi, "columns", []):
            return None, None, None
        annual = self._annual_period_ends()
        if not annual:
            return None, None, None
        try:
            cand = fi[fi["end_date"].astype(str).isin(annual)]
        except Exception:  # noqa: BLE001
            return None, None, None
        if cand.empty:
            return None, None, None
        row = cand.sort_values("end_date").iloc[-1]
        v = self._safe_float(row.get("total_market_cap"))
        if v is None or v <= 0:
            return None, None, None
        if v < getattr(self, "_HK_MKT_CAP_RAW_FLOOR", 0):
            v *= 1e6
        end = str(row.get("end_date"))

        # 期末价 = **该报告期当日或之前**的最后一根周线收盘，而不是"最新一根"
        close = None
        wp = self._store.get("weekly_prices")
        if wp is not None and not getattr(wp, "empty", True) and "trade_date" in getattr(wp, "columns", []):
            try:
                upto = wp[wp["trade_date"].astype(str) <= end]
                if not upto.empty:
                    close = self._safe_float(upto.sort_values("trade_date").iloc[-1].get("close"))
            except Exception:  # noqa: BLE001
                close = None
        if not close:
            return None, None, None
        return v / 1e6, close, end

    def _get_rf_ii(self, ts_code: str):
        """Return (rf_val_pct, ii_pct) — the single source of truth for 门槛 II.

        The max(floor, Rf+spread) table lives in ``constants.II_THRESHOLDS``
        (known_issues.md P1-12); §17.2 / §17.9 / §17.11 all route through here.
        Returns (None, None) if the risk-free rate is unavailable.
        """
        rf_df = self._store.get("risk_free_rate")
        if rf_df is None or rf_df.empty:
            return None, None
        rf_val = self._safe_float(rf_df.iloc[0].get("yield"))
        if rf_val is None:
            return None, None
        return rf_val, ii_threshold(ts_code, rf_val)

    def _tax_scenarios(self, ts_code: str):
        """Return (scenarios, default_rate) encoding shared_tables.md 股息税率表.

        scenarios: list of (label, rate_pct). default_rate: the default channel's rate.
        A股默认持有>1年 0%；港股默认港股通 20%；美股默认 W-8BEN 10%。
        """
        if self._is_hk(ts_code):
            # 档表读 strategy_rules（单点）；读不到则回落原字面量（逐字节同旧行为）
            _tq = _STRATEGY_RULES.get("dividend_tax_q_pct") or {}
            scenarios = [("H股直接", _tq.get("hk_h_share_direct", 28.0)),
                         ("港股通", _tq.get("hk_h_share_connect", 20.0)),
                         ("红筹/开曼", _tq.get("hk_red_chip_connect", 20.0))]
            default_rate = scenarios[1][1]
        elif self._is_us(ts_code):
            _tq = _STRATEGY_RULES.get("dividend_tax_q_pct") or {}
            scenarios = [("直接持有", _tq.get("us_direct_no_treaty", 30.0)),
                         ("W-8BEN", _tq.get("us_w8ben", 10.0))]
            default_rate = scenarios[1][1]
        else:
            _tq = _STRATEGY_RULES.get("dividend_tax_q_pct") or {}
            scenarios = [("持有>1年", _tq.get("a_share_gt_1y", 0.0)),
                         ("持有1月-1年", _tq.get("a_share_1m_to_1y", 10.0)),
                         ("持有<1月", _tq.get("a_share_lt_1m", 20.0))]
            default_rate = scenarios[0][1]
        return scenarios, default_rate

    def _unique_tax_rates(self, ts_code: str):
        """Return (unique_rates, default_rate) — dedup scenario rates preserving order."""
        scenarios, default_rate = self._tax_scenarios(ts_code)
        seen = []
        for _, r in scenarios:
            if r not in seen:
                seen.append(r)
        return seen, default_rate

    @staticmethod
    def _mean_first3(lookup: dict, years: list) -> float | None:
        """Mean of a {year: value} lookup over the first up-to-3 of the given years."""
        vals = [lookup[y] for y in years[:3] if y in lookup]
        return sum(vals) / len(vals) if vals else None

    def _compute_payout_crosscheck(self) -> str | None:
        """Compute §17.10: M (payout ratio) three-method cross-check + recommendation.

        法1 §6 逐年 DPS/EPS（港股加静态币种不一致注释，不换算）
        法2 §5 逐年 c_pay_dist_dpcp_int_exp / 归母净利润（含利息及少数股东股利，系上界）
        法3 §17.1 payout 口径（_get_payout_by_year）

        Recommendation (Python):
          三种方法 3 年均值两两相对偏差 > M_method 阈值（strategy_rules.stability_gates）→ M_rec=法2 + warning
          A股逐年 DPS 在 ≥3 年完全相同 → 填充错误 warning + M_rec=法2
          否则 → M_rec=法3

        Stores self._store["payout_crosscheck"]. Returns None only if all methods lack data.
        """
        income_df = self._get_annual_df("income")
        if income_df.empty:
            return None
        years = [str(r["end_date"])[:4] for _, r in income_df.iterrows()]  # desc

        np_by_year = {}
        eps_by_year = {}
        for _, r in income_df.iterrows():
            y = str(r["end_date"])[:4]
            np_by_year[y] = self._safe_float(r.get("n_income_attr_p"))
            eps_by_year[y] = self._safe_float(r.get("basic_eps"))

        # --- 法1: DPS / EPS ---
        m1_by_year = {}
        dps_by_year = {}
        currency_note = False
        hk_dps_note = False
        div_df = self._store.get("dividends")
        hk_df = self._store.get("dividends_hk")
        if hk_df is not None and not hk_df.empty:
            # 港股法1 **不出数**（auditor 路径 B）。
            #
            # 00506 数值审核（附年报页码）实测：九个期间的 `dps_hkd`/`cash_div_tax` 全部是
            # 0.177 —— 它是"按公告快照"的单值，而年报的每股股息是 0.148→0.153→0.154 的
            # 逐年序列；`divi_ratio` 同样与年报不符（FY2024 0.5197 vs 49.73%）。
            # `invariants I5` 已点名该列**不得用于任何趋势/分位/派息率判断**。
            #
            # 旧实现把不可用的 DPS 与不可信的 divi_ratio 做 20% 交叉校验，只会**稳定地**
            # 选中一个错值；而且 DPS(港元) ÷ EPS(人民币) 未折汇，再高估 15.65%
            # （渲染 57.10%，年报口径 49.4%）。
            # 逐年派息率改由"法3"承担（年报宣告口径，见 `_declared_hk_payout`）。
            hk_dps_note = True
            dps_by_year = {}
        elif div_df is not None and not div_df.empty:
            # A 股：中期与末期是不同的现金分派事件，逐年累加是正确的。
            for _, r in div_df.iterrows():
                y = str(r.get("end_date", ""))[:4]
                v = self._safe_float(r.get("cash_div_tax"))
                if v is not None:
                    dps_by_year[y] = dps_by_year.get(y, 0.0) + v
            for y, dps in dps_by_year.items():
                eps = eps_by_year.get(y)
                if eps and eps > 0:
                    m1_by_year[y] = dps / eps * 100

        # --- 法2: cashflow distribution / net profit ---
        m2_by_year = {}
        cf_df = self._get_annual_df("cashflow")
        if not cf_df.empty:
            for _, r in cf_df.iterrows():
                y = str(r["end_date"])[:4]
                dist = self._safe_float(r.get("c_pay_dist_dpcp_int_exp"))
                npv = np_by_year.get(y)
                if dist is not None and npv and npv > 0:
                    m2_by_year[y] = dist / npv * 100

        # --- 法3: 同币种派息率（港股＝年报宣告口径；A 股＝§17.1 口径）---
        m3_by_year = self._get_payout_by_year()

        if not m1_by_year and not m2_by_year and not m3_by_year:
            return None

        # ⚠️ **窗口补零**：`近3年均值` 的窗口是「最近 3 个**财年**」，窗口内**没有派息**必须
        # 按 **0** 计入，不能当缺失剔除 —— 剔除会把三年均值系统性抬高，且对"连续停派"的
        # 公司尤其致命（06078 实测：FY2024/FY2025 两个真零年被剔除后，法2 由 **4.61%** 被
        # 抬到 7.64%、法3 更因窗口内只剩一个点而**根本没做除法**，由 **4.53%** 被抬到 13.58%）。
        # 判据：§3（利润表）覆盖了该财年 ⇒ 该年"无分配"是**有证据的零**，而非未知。
        # 法1 不补（港股已停用；A 股路径的逐年 DPS 另有自身的填充异常守卫）。
        _win = list(years[:3])
        _has_declared_sidecar = bool(self._declared_dividends())
        _cf_years = set()
        if not cf_df.empty:
            for _, r in cf_df.iterrows():
                _cf_years.add(str(r["end_date"])[:4])
        for y in _win:
            if y not in np_by_year:
                continue   # 利润表都没这个财年 ⇒ 无法判定，不补
            if y not in m2_by_year and y in _cf_years:
                m2_by_year[y] = 0.0      # 现金流量表有该财年、但无分配 → 有证据的零
        for y in _win:
            if y not in np_by_year:
                continue
            # ⚠️ **只在 sidecar 存在时**才把"该年未登记"读作"已宣告零"：
            # sidecar 缺失 ≠ 公司未宣派（那是"未知"），把它补成 0 会凭空造出一个
            # "零派息"事实，并使法3 在完全无数据时也报 0%（旧行为是 return {}）。
            if y not in m3_by_year and _has_declared_sidecar:
                m3_by_year[y] = 0.0

        m1 = self._mean_first3(m1_by_year, years)
        m2 = self._mean_first3(m2_by_year, years)
        m3 = self._mean_first3(m3_by_year, years)

        # --- Recommendation logic ---
        warnings = []
        means = [(lab, v) for lab, v in [("法1", m1), ("法2", m2), ("法3", m3)] if v is not None]
        # ⚠️ `len(means) < 2` ⇒ 偏差**不可计算 ≠ 偏差为 0**（canonical 规则见
        # `turtle_core/decision.py::_max_pairwise_dev`，此处必须与之同口径）。
        # 旧实现把 max_dev 初值写成 0.0、max_pair 写成 None，于是只有一路可用时
        # （港股法1 停用 + 法3 缺 dividends_declared ⇒ 仅法2）两者自相矛盾：
        # 一边声称"最大相对偏差 0.0%"（单值无偏差可言），一边在下面 `max_pair[0]`
        # 下标 None ⇒ 抛异常 ⇒ §17.10 整节 missing ⇒ §17.9/§17.11/§17.14 连带消失
        # （06078 海吉亚实测，TypeError: 'NoneType' object is not subscriptable）。
        max_dev = None
        max_pair = None
        for i in range(len(means)):
            for j in range(i + 1, len(means)):
                a, b = means[i][1], means[j][1]
                denom = (abs(a) + abs(b)) / 2
                if denom > 0:
                    dev = abs(a - b) / denom
                    if max_dev is None or dev > max_dev:
                        max_dev = dev
                        max_pair = (means[i][0], means[j][0])

        identical_dps = False
        if div_df is not None and not div_df.empty and dps_by_year:
            dps_vals = [dps_by_year[y] for y in years if y in dps_by_year]
            if len(dps_vals) >= 3 and len({round(v, 6) for v in dps_vals}) == 1:
                identical_dps = True

        def pct_src(v):
            return f"{v:.2f}%"

        def _pick(label):
            return {"法1": m1, "法2": m2, "法3": m3}.get(label)

        # --- 承诺优先：文档的第一优先级（known_issues.md §3.21 21b）---
        # [1:32:56]「首先要搜查有没有公开的文件规定了最低的股息支付率」
        # [1:09:42]「写在了公开的公告里面，那就一票就直接成立」；
        #           口头/电话会议提及 → 须多次反复才可采信。
        commit = self._store.get("dividend_commitment") or {}
        commit_used = False
        _kind = str(commit.get("kind") or "无")
        _occ = int(commit.get("occurrences") or 1)
        if _kind in ("公告", "口头") and (_kind == "公告" or _occ >= 2):
            _pct = self._safe_float(commit.get("min_payout_pct"))
            _amt = self._safe_float(commit.get("min_amount_mm"))
            if _pct is not None:
                m_rec, m_rec_label = _pct, "承诺(支付率)"
                reason = (f"{_kind}承诺最低支付率 {pct_src(_pct)} → 一票成立"
                          f"（{commit.get('source') or '未标注来源'}）")
                commit_used = True
            elif _amt is not None:
                _np = None
                for _y in years:
                    _np = np_by_year.get(_y)
                    if _np:
                        break
                if _np:
                    # _amt 单位为百万，_np 为原始元 → 统一到百万再折算
                    m_rec = _amt / (_np / 1e6) * 100
                    m_rec_label = "承诺(金额折算)"
                    reason = (f"{_kind}承诺最低分红 {_amt:,.0f} 百万 → 按最新年归母净利 "
                              f"{_np:,.0f} 百万折算为等价支付率 {m_rec:.2f}%"
                              f"（{commit.get('source') or '未标注来源'}）")
                    commit_used = True
            if commit_used:
                if _kind == "口头":
                    warnings.append("承诺来自口头/电话会议，文档要求须多次反复才可采信"
                                    f"（occurrences={_occ}）")
                if commit.get("period"):
                    reason += f"；承诺期间 {commit['period']}"
                warnings.append(f"⚠️ M 锚定改为承诺值（{m_rec_label}），覆盖三法交叉校验")

        # ⚠️ 判据与文案必须用**同一个**规则阈值：原先写成 `max_dev > 0.15`（硬编码），
        # 而文案读 `strategy_rules` ⇒ 改规则只改文案不改判据（同 AA 窗口那处，T3 只接了一半）。
        _m_thr_pct = (_STRATEGY_RULES.get("stability_gates") or {}).get(
            "m_method_spread_pct", 15.0)
        # max_dev 为 None = **只有一路可用，偏差无从谈起**（不是 0%）。
        # 此时既不记稳定性项，也不走"偏差过大 ⇒ 法2"分支（单值恒不触发），
        # 由下面的 m3/m2 回退链自然给出结论并明说"仅一路可用"。
        if max_dev is None:
            _m_unstable = None
        else:
            _pair_txt = (f"（{max_pair[0]} vs {max_pair[1]}）" if max_pair else "")
            _m_unstable = self._record_stability(
                "M_method", max_dev * 100, _m_thr_pct,
                f"三法最大相对偏差 {max_dev * 100:.1f}%{_pair_txt}")
        if not commit_used and max_dev is not None and max_dev * 100 > _m_thr_pct and m2 is not None:
            m_rec_label = "法2"
            reason = (f"三种方法3年均值两两相对偏差最大 {max_dev * 100:.1f}%"
                      f"（{max_pair[0]} vs {max_pair[1]}）> {_m_thr_pct:g}%，"
                      f"采用法2（现金流最可靠）⇒ **该参数不收敛，不得单独绑定决策**")
            warnings.append(reason)
        elif not commit_used and identical_dps and m2 is not None:
            m_rec_label = "法2"
            reason = "A股逐年 DPS 在 ≥3 年完全相同，高度疑似 Tushare 数据填充错误，采用法2"
            warnings.append(reason)
        elif not commit_used and m3 is not None:
            m_rec_label = "法3"
            _m_thr = (_STRATEGY_RULES.get("stability_gates") or {}).get(
                "m_method_spread_pct", 15.0)
            reason = f"三法一致（偏差 ≤ {_m_thr:g}%），采用法3（§17.1 同币种口径）"
        elif not commit_used and m2 is not None:
            m_rec_label = "法2"
            _avail = "、".join(lab for lab, _ in means) or "无"
            reason = (f"仅 {_avail} 一路可用（法1 港股停用 / 法3 缺年报已宣告每股股息）"
                      f"⇒ 无法作三法交叉校验，回退至法2（现金流口径，系上界）"
                      f"；**该参数未经交叉校验，不得单独绑定决策**")
            warnings.append(reason)
        elif not commit_used:
            m_rec_label = "法1"
            reason = "仅法1可用"
        if not commit_used:
            m_rec = _pick(m_rec_label)

        if currency_note:
            warnings.append("港股 DPS 为「按公告快照」单值（00506 实测九期同为 0.177），"
                            "且 DPS(港元)/EPS(人民币) 需折汇 —— 法1 已停用，派息率取法3"
                            "（年报宣告口径）；分红绝对额见 §5 已付股息（上界口径）")
        if hk_dps_note:
            warnings.append(
                "港股法1 已**停用**：`dps_hkd`/`cash_div_tax` 是「按公告快照」的单值"
                "（00506 实测九期同为 0.177，年报实为 0.148→0.153→0.154），"
                "`invariants I5` 已点名该列不得用于派息率；且 DPS(港元)÷EPS(人民币) "
                "需折汇。逐年派息率改由法3（年报**已宣告**每股股息 ÷ 同币种 EPS，"
                "来源 dividends_declared.json）承担，M 的推荐值仍按三法偏差规则裁定。"
            )

        # M_pess：悲观猜测。文档要求"无规律且无承诺的公司只能按商业模式结合
        # 历史数据估算，且必须取悲观值"（因子2 [1:33:35] / [1:33:57]「哪个低就取哪一个」），
        # 并明确否定均值外推（[1:16:14]「不能说取个平均数」）。故取各法3年均值的最低者。
        _means = [v for v in (m1, m2, m3) if v is not None]
        m_pess = min(_means) if _means else m_rec

        self._store["payout_crosscheck"] = {
            "m1": m1, "m2": m2, "m3": m3,
            "m_rec": m_rec, "m_rec_label": m_rec_label,
            "m_pess": m_pess,
            "m_rec_reason": reason, "warnings": warnings,
        }
        # 改动 1/2：M 是 §17.11/§17.14 网格的共同输入，必须可绑定校验
        _m_year = years[0] if years else None
        _ledger = self._claim_ledger()
        _ledger.add("cm17.10.mrec", "推荐支付率 M_rec", round(m_rec, 2),
                    "%", time=_m_year, basis=f"{m_rec_label}；{reason}",
                    policy="round:2", section="17.10")
        _ledger.add("cm17.10.mpess", "悲观支付率 M_pess（各法3年均值最低者）",
                    round(m_pess, 2), "%", time=_m_year,
                    basis=f"m1={m1}, m2={m2}, m3={m3} 的最低者", policy="round:2",
                    section="17.10")
        # 三法各自登记：报告在「关键假设」里会并列 M_rec 与替代档（06862 就引用了
        # 法1 93.54% / 法3 89.70%），此前这些数没有任何可绑定的 id。
        for _lab, _val, _note in (
            ("法1", m1, "§6 DPS ÷ §3 EPS 3 年均值；港股 DPS 为按公告快照、且与 EPS 可能不同币种"),
            ("法2", m2, "§5 分配现金 ÷ 归母 3 年均值；含已付利息与少数股东股利，系**上界**"),
            ("法3", m3, "§17.1 同币种口径 3 年均值"),
        ):
            _ledger.add(f"cm17.10.m{_lab[-1]}", f"支付率 {_lab}（3年均值）", _val, "%",
                        time=_m_year, basis=_note, policy="round:2", section="17.10",
                        note=None if _val is not None else "该法数据不足，无法计算")

        # --- Build output ---
        lines = [format_header(3, "17.10 支付率 M 三重校验"), ""]
        lines.append("> 法2 含利息及少数股东股利，系支付率上界；法3 为 §17.1 同币种口径。"
                     "推荐规则已在 Python 内裁定，LLM 直接引用 M_rec。")
        lines.append("")

        def _pf(v):
            return f"{v:.2f}%" if v is not None else "—"
        lines.append(f"> **M_pess（悲观基准）= {_pf(m_pess)}** —— 各法3年均值的最低者，"
                     "供 §17.14 悲观档使用（文档否定均值外推，要求取悲观值）。")
        lines.append("")

        headers = ["方法"] + years + ["3年均值"]
        rows = [
            ["法1 §6 DPS/EPS"] + [_pf(m1_by_year.get(y)) for y in years] + [_pf(m1)],
            ["法2 §5 分配现金/归母"] + [_pf(m2_by_year.get(y)) for y in years] + [_pf(m2)],
            ["法3 §17.1 口径"] + [_pf(m3_by_year.get(y)) for y in years] + [_pf(m3)],
        ]
        lines.append(format_table(headers, rows, alignments=["l"] + ["r"] * (len(years) + 1)))
        lines.append("")
        lines.append(f"- **M_rec = {_pf(m_rec)}（{m_rec_label}）** — {reason}")
        if currency_note:
            lines.append("- ⚠️ 港股 DPS(HKD)/EPS 币种可能不一致，法1 未换算，仅供参考")
        for wm in warnings:
            lines.append(f"  ⚠️ {wm}")

        return "\n".join(lines)

    def _compute_penetration_grid(self, ts_code: str) -> str | None:
        """Compute §17.11: penetrating return-rate grid (税后).

        表A 粗算 R = M候选 × Q列（基准值 C = 最新年归母净利润）。
        表B 精算 GG = AA变体 × M候选 × Q列 + HH + GG−II + 目标买入价（默认Q）。
        ★ 标注推荐 M；O=0 默认（加法修正注释）；脚注声明 §17.9 为税前口径、以本节为准。
        """
        f3s = self._store.get("factor3_sensitivity")
        if not f3s:
            return None
        income_df = self._get_annual_df("income")
        if income_df.empty:
            return None

        # ── AA < 0 守卫（fail-closed，known_issues.md §二十九）──────────────────
        # AA 是可支配现金结余，**可以为负**（资本开支超过经营现金流 = 真实现金缺口，
        # 实测 600011 华能国际 2024 AA = −13,122.98、601186 中国铁建 2025 = −44,643.57）。
        # 此时「税后回报率」不是一个可比的收益率（负的"收益率"是亏损，不是回报），
        # 且「按 k 折扣取悲观」在负值上失去方向意义（账目上 ¥1 的缺口按 k=0.7 缩到 ¥0.7，
        # 看起来"改善了"，与"更悲观"相反）。故本节拒绝输出数值，不得手算。
        _aa_variants = [v for k, v in f3s.items()
                        if k in ("aa_2y", "aa_all", "aa_excl", "aa_latest", "aa_selected")
                        and isinstance(v, (int, float))]
        _aa_negative = any(v < 0 for v in _aa_variants)
        # 本节**不整节拒绝**：负 GG 本身是有诊断价值的读数（"按此价买入为负回报"），
        # 且既有回归测试锁定了"GG ≤ 0 ⇒ 目标买入价渲染 `—`，绝不输出负价格"
        # （2026-10-13：600941 曾算出 目标买入价 = −64.79 元）。
        # 本守卫只负责把基调显式写进表头，并禁止负 GG 推出任何"买点"。

        c = self._safe_float(income_df.iloc[0].get("n_income_attr_p"))
        # 声明的时间戳用最新年报年度（§17.11 全表都基于最新年口径）
        _latest_year = str(income_df.iloc[0].get("end_date") or "")[:4] or None
        if c is None:
            return None

        ms = self._get_mktcap_shares(ts_code)
        if ms is None:
            return None
        mkt_cap, total_shares, close = ms
        rf_val, ii = self._get_rf_ii(ts_code)
        if ii is None:
            return None

        # M candidates from §17.10, else fallback to §17.1 3yr mean
        pc = self._store.get("payout_crosscheck")
        raw_ms = []
        rec_label = None
        if pc:
            for lab, key in [("法1", "m1"), ("法2", "m2"), ("法3", "m3")]:
                v = pc.get(key)
                if v is not None:
                    raw_ms.append((lab, v))
            rec_label = pc.get("m_rec_label")
        else:
            years = [str(r["end_date"])[:4] for _, r in income_df.iterrows()]
            m3v = self._mean_first3(self._get_payout_by_year(), years)
            if m3v is not None:
                raw_ms.append(("法3", m3v))
                rec_label = "法3"
        if not raw_ms:
            return None
        if rec_label not in [lab for lab, _ in raw_ms]:
            rec_label = raw_ms[0][0]

        # Reorder recommended first, dedup within 0.5 pct
        ordered = [e for e in raw_ms if e[0] == rec_label] + [e for e in raw_ms if e[0] != rec_label]
        m_candidates = []
        for lab, v in ordered:
            if any(abs(v - kv) < 0.5 for _, kv in m_candidates):
                continue
            m_candidates.append((lab, v))

        # AA variants (dedup identical values)
        aa_raw = [("AA_2y", f3s.get("aa_2y")), ("AA_all", f3s.get("aa_all")),
                  ("AA_excl", f3s.get("aa_excl"))]
        aa_variants = []
        for lab, v in aa_raw:
            if v is None:
                continue
            if any(abs(v - kv) < 1e4 for _, kv in aa_variants):
                continue
            aa_variants.append((lab, v))
        if not aa_variants:
            return None

        q_rates, q_default = self._unique_tax_rates(ts_code)
        # ⚠️ **必须 ×1e6**：`o.json` / `cm17.11.o` 的单位是**百万**（与 §15 摘要、
        # `claim_ledger.UNIT_ENUM` 一致），而本节内部量纲是**元** ——
        # 实测插桩：`aa=2,126,605,006.91`（元）· `mkt_cap=22,533,626,329.52`（元）·
        # `o_val=123.4`（百万）。不加换算时 O 的贡献
        # = 123.4/22,533,626,329×100 = **0.00000055 pct**，效果等同"没接通"，
        # 而 claim 却如实写着 123.4 ⇒ 产物内部自相矛盾（findings D-3b）。
        # 这正是 known_issues P0-2 那类静默单位错，只不过量级是 10^6。
        o_val = self._o_input().o_annual_mm * 1e6   # 单点，同 §15/§17.9/§17.14
        # O 必须进账本：screener 经 `claims.json` 继承 O（`slow_layer.CLAIM_MAP["o"]`）。
        # **缺这条会让报告与筛选不同源** —— 填了 `o.json` 的产物报告 O≠0 而筛选仍读 0
        # （findings D-3 明确写过"不能只接一半"）。
        _o_in = self._o_input()
        # 来源标签必须如实：O 可能来自显式 `o.json`，也可能来自「承诺执行评估」的派生值
        # （后者见 `commitment_execution.json` + `shared/commitment_confidence.py`）。
        # 旧实现一律写「（来自 o.json）」，会在派生路径上说谎。
        _o_src = "来自 o.json"
        if not _o_in.is_default and "commitment_execution" in str(_o_in.source or ""):
            _o_src = "由承诺执行评估派生，见 commitment_execution.json"
        self._claim_ledger(ts_code).add(
            "cm17.11.o", "O（注销型回购年均）", round(_o_in.o_annual_mm, 2),
            # ⚠️ 单位必须取自 `claim_ledger.UNIT_ENUM`；`self._unit_label()` 返回
            # "百万元"（带「元」）**不在枚举里** ⇒ 会抛 ClaimError 并让整节 §17.11 变 missing
            # （实测 22 个测试红）。`'百万'` 在枚举内且与 §15 的行文案一致。
            "百万", time=None,
            basis=(_o_in.describe() if not _o_in.is_default
                   else "无 o.json ⇒ 0（上游无法区分注销型，待年报确认）"),
            policy="round:2", section="17.11")
        mkt_cap_mm = mkt_cap / 1e6
        o_increment = 100.0 / mkt_cap_mm * 100 if mkt_cap_mm > 0 else 0.0

        lines = [format_header(3, "17.11 穿透回报率网格（税后）"), ""]
        # 收盘价可能缺失（行情接口无权限/配额用尽）：此时只降级目标买入价，
        # 不让整个网格消失 —— GG/HH/GG−II 并不需要 close。
        _price_note = (f"当前股价 = {close:.2f} {self._price_unit()}，"
                       if close else
                       "**当前股价不可用（行情接口无权限/配额用尽）→ 目标买入价降级为 —**，")
        lines.append(f"> 基准值 C（最新年归母净利润）= {format_number(c)} {self._unit_label()}，"
                     f"市值 = {format_number(mkt_cap)} {self._unit_label()}，"
                     + _price_note
                     + f"门槛 II = {ii:.2f}%，"
                     # ⚠️ O 必须**回显实际取值**：旧实现把这句硬编码成「O = 0（默认）」，
                     # 于是产物带了 `o.json`（O≠0）时表 A/表 B 与 §17.14 都已按 O 算，
                     # 而**头部仍自称 O=0** —— 同一小节自相矛盾，且下游（含
                     # `numeric_audit` 的独立复算）照头部取 O=0，会把正确的 GG 判成
                     # MISMATCH（06078 实测：O=85.27 时表 B GG=1.81%，头部却写 O=0）。
                     + (f"O = {_o_in.o_annual_mm:.2f} {self._unit_label()}（{_o_src}）。"
                        if not _o_in.is_default else "O = 0（默认）。"))
        lines.append(f"> O 加法修正：每确认 100 {self._unit_label()} 年均注销型回购，"
                     f"穿透回报率 +{o_increment:.4f} pct（= 100/市值×100）。")
        # 门槛口径：全市场统一 max(5%, Rf+3%)，单点来源 constants.II_FLOOR/_SPREAD。
        # 视频方法论的原始档位（A股3-4% / 港股4-5%）自身矛盾，已在 constants 注释中
        # 记录为出处，不再作为对照档参与输出（known_issues.md P1-12）。
        lines.append("")

        def _qlabel(q):
            tag = "（默认）" if q == q_default else ""
            return f"Q={q:g}%{tag}"

        # --- Table A: 粗算 R ---
        # ── 实体归属披露（known_issues §四十五）──────────────────────────
        # GG = AA(合并) × M(归母)。两个口径混用 ⇒ 对少数股东占比高的标的，
        # GG 是"归母股东可得"的**上界**。这是**声明**，不是数值修正：
        # 框架刻意让粗算(R, 归母)与精算(GG, 合并)用不同基准（factor3 精算.md:281）。
        # 少数股东占比直接由 income_df 算（§17.2 与本节的公式同为 B/A）。
        # 刻意不依赖 §17.2 的 markdown（那需要正则回读，正是我们要消除的读取器差异）。
        _min_pct = None
        try:
            _row0 = income_df.iloc[0]
            _b = self._safe_float(_row0.get("minority_gain"))
            _ni = self._safe_float(_row0.get("n_income"))
            if _b is not None and _ni:
                _min_pct = _b / _ni * 100
        except Exception:  # noqa: BLE001
            _min_pct = None
        if _min_pct is not None and _min_pct > 5.0:
            lines.append(
                f"> ⚠️ **口径混用披露**：少数股东占净利润 **{_min_pct:.2f}%**。"
                f"本节 GG 的分子 AA 是**合并口径**、支付率 M 是**归母口径** ⇒ "
                f"GG 应理解为**归母股东可得的上界**（少数股东按持股分享子公司现金，"
                f"约 {_min_pct:.2f}% 的部分不归母）。见 `strategy_rules.json` 的 `entity_basis`。")
            lines.append("")
        if _aa_negative:
            lines.append("> ⚠️ **本节基调为负**：可支配现金结余 AA 存在负值变体 ⇒ 精算 GG 为负，"
                         "**负 GG 不构成「收益率」，目标买入价不可由负 GG 推导**（渲染 `—`）。"
                         "「按 k 折扣取悲观」在负值上没有方向意义，故悲观档 §17.14 对本标的"
                         "**拒绝输出数值**（reason=negative_basis）。见 known_issues.md §二十九。")
            lines.append("")
        lines.append("#### 表A：粗算穿透回报率 R（基准值 = C 归母净利润）")
        lines.append("")
        headers_a = ["支付率 M"] + [_qlabel(q) for q in q_rates]
        rows_a = []
        for lab, m in m_candidates:
            star = " ★" if lab == rec_label else ""
            row = [f"{lab} {m:.2f}%{star}"]
            for q in q_rates:
                r = (c * m / 100 * (1 - q / 100) + o_val) / mkt_cap * 100
                row.append(f"{r:.2f}%")
            rows_a.append(row)
            # 表A 的 R 是报告最常引用的交叉校验值（06862 报告第 34 行），却一直
            # 「未登记 claim id」—— 决策单元格 KV 只覆盖了表B，于是它只能裸写。
            # 这里按默认 Q 列登记每个 M 候选的 R（非默认列按 Q 比例可复核，不重复登记）。
            # **算术单点**：粗算 R 用 C 作基准值；GG 用 AA —— 同一个函数
            _r_def = _tc_dec.penetration_return(c, m, q_default, o_val, mkt_cap)
            self._claim_ledger(ts_code).add(
                f"cm17.11.ra.{self._method_slug(lab)}", f"粗算穿透回报率 R（{lab}）",
                round(_r_def, 2), "%", time=_latest_year,
                basis=(f"C={format_number(c)}, M={lab}({m:.2f}%), Q={q_default:g}%, "
                       f"市值={format_number(mkt_cap)}"),
                policy="round:2", section="17.11")
        lines.append(format_table(headers_a, rows_a, alignments=["l"] + ["r"] * len(q_rates)))
        lines.append("")

        # --- Table B: 精算 GG ---
        lines.append("#### 表B：精算穿透回报率 GG（基准值 = AA 真实可支配现金结余）")
        lines.append("")
        headers_b = (["AA×M"] + [f"GG {_qlabel(q)}" for q in q_rates]
                     + ["HH(默认Q) pct", "GG−II(默认Q) pct", f"目标买入价（{self._price_unit()}）"])
        rows_b = []
        decision_claims = []          # 决策单元格的 claim id（改动 1：KV 化）
        for aalab, aa in aa_variants:
            for mlab, m in m_candidates:
                star = " ★" if mlab == rec_label else ""
                row = [f"{aalab}×{mlab}{star}"]
                gg_default = None
                for q in q_rates:
                    # **算术单点**：R/GG 共用 `turtle_core.decision.penetration_return`，
                    # 区别只在基准值（R 传 C，GG 传 AA）—— screener 用的是同一份实现。
                    gg = _tc_dec.penetration_return(aa, m, q, o_val, mkt_cap)
                    if q == q_default:
                        gg_default = gg
                    row.append(f"{gg:.2f}%")
                r_default = _tc_dec.penetration_return(c, m, q_default, o_val, mkt_cap)
                hh = _tc_dec.hh(r_default, gg_default)
                row.append(f"{hh:+.2f}")
                row.append(f"{_tc_dec.margin(gg_default, ii):+.2f}")

                # 只为 ★（推荐 M）行登记 claims：那是 Agent 实际要引用的行，
                # 也是校验器需要绑定的目标。全表登记会制造大量永不引用的噪声。
                if mlab == rec_label:
                    _slug = f"{self._aa_slug(aalab)}.{self._method_slug(mlab)}"
                    _basis = (f"{aalab}={format_number(aa)}, M={mlab}({m:.2f}%), "
                              f"Q={q_default:g}%, II={ii:.2f}%, 市值={format_number(mkt_cap)}")
                    _pct_policy = "round:2"
                    self._claim_ledger(ts_code).add(
                        f"cm17.11.{_slug}.gg", f"精算穿透回报率 GG（{aalab}）",
                        round(gg_default, 2), "%", time=_latest_year,
                        basis=_basis, policy=_pct_policy, section="17.11")
                    self._claim_ledger(ts_code).add(
                        f"cm17.11.{_slug}.hh", f"HH 粗算−精算差（{aalab}）",
                        round(hh, 2), "pct点", time=_latest_year,
                        basis=_basis, policy=_pct_policy, section="17.11")
                    self._claim_ledger(ts_code).add(
                        f"cm17.11.{_slug}.vsii", f"GG−II（{aalab}）",
                        round(gg_default - ii, 2), "pct点", time=_latest_year,
                        basis=_basis + f", 门槛 II={ii:.2f}%", policy=_pct_policy,
                        section="17.11")
                    if close and ii > 0 and gg_default is not None and gg_default > 0:
                        tbp = _tc_dec.target_buy_price(close, gg_default, ii)
                        self._claim_ledger(ts_code).add(
                            f"cm17.11.{_slug}.tbp", f"目标买入价（{aalab}）",
                            round(tbp, 2), f"{self._price_unit()}/股",
                            time=_latest_year,
                            basis=_basis + (f", 当前股价={close}" if close else ""), policy=_pct_policy,
                            section="17.11")
                        decision_claims.append(f"cm17.11.{_slug}.tbp")
                    else:
                        self._claim_ledger(ts_code).add(
                            f"cm17.11.{_slug}.tbp", f"目标买入价（{aalab}）",
                            None, f"{self._price_unit()}/股",
                            basis=_basis, policy=_pct_policy,
                            note=("GG ≤ 0 → 无解（任何价格都达不到门槛）" if close
                                  else "收盘价不可用 → 无法计算目标买入价"),
                            section="17.11")
                    decision_claims.insert(0, f"cm17.11.{_slug}.gg")
                    decision_claims.insert(1, f"cm17.11.{_slug}.vsii")
                # 目标买入价 = 现价 × GG/II 仅在 GG > 0 时有定义。GG ≤ 0 含义是
                # 「按当前股东现金回报，任何价格都达不到门槛」，不存在解，
                # 因此输出 "—" 而不是负价格（600941 曾输出 -64.79 元）。
                if close and ii > 0 and gg_default is not None and gg_default > 0:
                    row.append(f"{close * gg_default / ii:.2f}")
                else:
                    row.append("—")
                rows_b.append(row)
        lines.append(format_table(headers_b, rows_b,
                                  alignments=["l"] + ["r"] * (len(q_rates) + 3)))
        lines.append("")
        # 改动 1：决策单元格另以 KV 呈现（数值检索准确率显著高于宽表格），
        # 每条带 claim id，供报告侧绑定校验（改动 2）。
        if decision_claims:
            lines.append(self._claim_ledger(ts_code).render_kv(
                decision_claims, title="表B 决策单元格（KV · 带 claim id）"))
            lines.append("")
            lines.append("> 引用时请写成 `<claim id=\"…\">值</claim>`；"
                         "校验器会按声明策略机械比对（未通过即 unverified）。")
            lines.append("")
        lines.append("> 目标买入价 = 当前股价 × GG / II（默认 Q）。★ 为推荐 M。")
        lines.append("> `—` 表示该组合下 GG ≤ 0：股东现金回报为负，目标买入价无解"
                     "（不是 0，也不可当作负价格）。")
        lines.append("> 脚注：§17.9 与本节使用**同一 M_rec 与默认 Q**，两表数值可直接比较"
                     "（2026-10-13 前 §17.9 为税前且用 §17.1 的 3 年均值，港股下与本节相差 25%）。")

        return "\n".join(lines)

    def _compute_g_grid(self, ts_code: str = "") -> str | None:
        """Compute §17.12: standalone G→维持性Capex 网格（12 行）。

        G = 0.7 … 1.8 步长 0.1；H = D×G；OE = C + D − H = C + D(1−G)。
        附 F 参考值（Capex/D&A 5年中位数）与档位标签（轻/轻中/中/中重/重）。
        LLM 仅选行，禁止自算。
        """
        income_df = self._get_annual_df("income")
        cf_df = self._get_annual_df("cashflow")
        if income_df.empty or cf_df.empty:
            return None
        c = self._safe_float(income_df.iloc[0].get("n_income_attr_p"))
        if c is None:
            return None

        latest_cf = cf_df.iloc[0]
        da = 0.0
        any_da = False
        for col in ["depr_fa_coga_dpba", "amort_intang_assets", "lt_amort_deferred_exp"]:
            v = self._safe_float(latest_cf.get(col))
            if v is not None:
                da += v
                any_da = True
        if not any_da or da <= 0:
            return None
        d = da

        # F reference: Capex/D&A 5yr median
        capex_da_vals = []
        for _, r in cf_df.iterrows():
            dep = self._safe_float(r.get("depr_fa_coga_dpba"))
            ai = self._safe_float(r.get("amort_intang_assets"))
            ad = self._safe_float(r.get("lt_amort_deferred_exp"))
            comps = [x for x in [dep, ai, ad] if x is not None]
            day = sum(comps) if comps else None
            cap = self._safe_float(r.get("c_pay_acq_const_fiolta"))
            if day and day > 0 and cap is not None:
                capex_da_vals.append(abs(cap) / day)
        f_ref = None
        if capex_da_vals:
            sv = sorted(capex_da_vals)
            mid = len(sv) // 2
            f_ref = sv[mid] if len(sv) % 2 else (sv[mid - 1] + sv[mid]) / 2

        def _band(g):
            if g <= 0.95:
                return "轻"
            if g <= 1.15:
                return "轻中"
            if g <= 1.35:
                return "中"
            if g <= 1.55:
                return "中重"
            return "重"

        lines = [format_header(3, "17.12 G 系数网格（维持性 Capex）"), ""]
        lines.append(f"> C 归母净利润 = {format_number(c)} {self._unit_label()}，"
                     f"D 折旧摊销 = {format_number(d)} {self._unit_label()}，"
                     f"F（Capex/D&A 5年中位数）= {f_ref:.2f}"
                     if f_ref is not None else
                     f"> C 归母净利润 = {format_number(c)} {self._unit_label()}，"
                     f"D 折旧摊销 = {format_number(d)} {self._unit_label()}，F = —")
        lines.append("> OE = C + D − H = C + D×(1−G)。**LLM 仅选行，禁止自算。**")
        lines.append("")
        headers = ["G", f"维持性Capex H = D×G（{self._unit_label()}）",
                   f"Owner Earnings OE（{self._unit_label()}）", "档位"]
        rows = []
        for i in range(12):
            g = round(0.7 + 0.1 * i, 1)
            h = d * g
            oe = c + d - h
            rows.append([f"{g:.1f}", format_number(h), format_number(oe), _band(g)])
        lines.append(format_table(headers, rows, alignments=["r", "r", "r", "c"]))

        # --- claim 账本：§17.12 每一档 G 的 H/OE ---
        # 报告会引用"选定行"以及替代档（06862 引用 G=0.7/1.0/1.8 三档），
        # 只登记选定档会让替代档的引用无法绑定；12 档 × 2 个数 = 24 条，成本可接受。
        _au = self._amount_unit()
        _g_year = self._ledger_time(income_df)
        for i in range(12):
            g = round(0.7 + 0.1 * i, 1)
            _tag = f"{int(round(g * 100)):03d}"
            _basis = (f"C={format_number(c)}, D={format_number(d)}, F="
                      f"{f_ref:.2f}" if f_ref is not None else f"C={format_number(c)}, D={format_number(d)}")
            self._register_claim(ts_code, f"cm17.12.g{_tag}.h", f"维持性Capex H（G={g:.1f}）",
                                 self._to_mm(d * g), _au, basis=f"H = D×G；{_basis}", section="17.12",
                                 time=_g_year)
            self._register_claim(ts_code, f"cm17.12.g{_tag}.oe", f"Owner Earnings OE（G={g:.1f}）",
                                 self._to_mm(c + d - d * g), _au,
                                 basis=f"OE = C + D×(1−G)；{_basis}", section="17.12",
                                 time=_g_year)

        return "\n".join(lines)

    # 文档要求的悲观折扣档位（因子2 [42:45]「我们要打八折」/ [48:45]「按七折重算」）。
    # 逐公司折扣是定性判断（茅台 −5%、五粮液 −8%、泸州老窖 −10%、分众 −5%），
    # 而 §17 在 Phase 1 生成、早于定性分析，Python 拿不到 —— 故做成网格由 Agent 选行，
    # 与 §17.12 的 G 网格同一模式。
    _PESS_K_VALUES = (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7)
    _PESS_K_DEFAULT = 0.8   # 有文档算例：318.5 × 0.8 × 0.7 ÷ 4850 = 3.68%

    def _compute_defensive_net_asset(self, ts_code: str) -> str | None:
        """Compute §17.15 防御性净值 —— 文档「商誉无形都是假资产」的落点。

        文档因子3 `[18:47]`：「如果你理解本质的话，你就知道商誉、无形资产都是假的资产，
        我会把它扣除掉」。落点选在**资产负债表净值**而不是 §17.5 的现金流结余 ——
        商誉/无形是存量科目，混进流量会破坏 §17.5 的语义。

        ⚠️ **文档自身存在张力**：因子2 `[32:45]` 用「高速公路的路权拿不到，所以它必须
        把 70–80% 分掉」来解释高分红 —— 而**路权本身就是无形资产**。全扣无形资产会
        推翻该论点。故同时输出两个口径，由 Agent 依商业模式选择：

          A 严口径：扣商誉 + 扣全部无形资产
          B 宽口径：只扣商誉（无形资产视为经营必需）
        """
        bs_df = self._get_annual_df("balance_sheet")
        if bs_df.empty:
            return None
        latest = bs_df.iloc[0]

        def _v(key):
            return self._safe_float(latest.get(key)) or 0.0

        cash = _v("money_cap") + _v("trad_asset")
        # 含租赁负债（口径单点，known_issues 2026-10-14：HKFRS 16 已资本化，
        # 是真实合同义务；漏掉会使"净现金 > 市值"这类判据偏乐观）
        debt = sum(_v(k) for k in ("st_borr", "lt_borr", "bond_payable",
                                   "non_cur_liab_due_1y",
                                   "lease_liab", "lease_liab_noncur"))
        goodwill = _v("goodwill")
        intangibles = _v("intang_assets")

        # ⚠️ **「无形资产」是否含商誉，两种编报惯例都存在，不能相加也不能一刀切**：
        #   · **包含**（06078 实测）：附註16 栏目头即 `商譽｜軟件｜合約權利｜醫療執照｜總計`，
        #     总计 3,650,802 千元与 §4「无形资产」逐位相等；合并财务状况表的非流动资产恰由
        #     这四项加总、**没有独立商誉行位**。此时 `goodwill + intangibles` 会把商誉扣两次
        #     （A 由 −5,408.29 被压到 −8,849.42，虚增 3,441.13 = 一个商誉）。
        #   · **不包含**（06862 实测）：无形资产 49.25 与商誉 84.84 各自独立成行，相加正确。
        # 故由 sidecar 显式声明成分（`balance_sheet_overrides.json` 的
        # `intangibles_composition`），**不猜**。缺省 `disjoint` ⇒ 与旧实现逐位一致，
        # 对既有产物零语义回退。
        _inclusive = bool(self._store.get("intangibles_includes_goodwill"))
        if _inclusive:
            net_A = cash - debt - intangibles               # 严：全扣（商誉已含其内）
            net_B = cash - debt - goodwill                  # 宽：只扣商誉，保留可辨认无形
        else:
            net_A = cash - debt - goodwill - intangibles    # 严：商誉 + 无形资产分别扣除
            net_B = cash - debt - goodwill                  # 宽：只扣商誉

        ms = self._get_mktcap_shares(ts_code)
        mkt_cap = ms[0] if ms else None

        lines = [format_header(3, "17.15 防御性净值（假资产剔除）"), ""]
        lines.append("> 文档因子3 `[18:47]`：「商誉、无形资产都是假的资产，我会把它扣除掉」。")
        lines.append(f"> 现金 = 货币资金 + 交易性金融资产 = {format_number(cash)} "
                     f"{self._unit_label()}；有息负债 = {format_number(debt)}。")
        lines.append("")
        if _inclusive:
            rows = [
                ["商誉", format_number(goodwill),
                 "**已含在无形资产行内** ⇒ A 口径不得再减一次；B 口径单独扣除"],
                ["无形资产（含商誉）", format_number(intangibles),
                 "A 口径全额扣除；B 口径只保留其中的可辨认部分"],
                ["其中：可辨认无形（= 无形 − 商誉）",
                 format_number(max(intangibles - goodwill, 0.0)),
                 "两口径的真正差异就在这里"],
                ["**A 严口径**（全扣含商誉的无形资产）", format_number(net_A),
                 "最保守的下行净值"],
                ["**B 宽口径**（扣商誉，保留可辨认无形）", format_number(net_B),
                 "无形资产视为经营必需"],
            ]
        else:
            rows = [
                ["商誉", format_number(goodwill), "全额扣除（两口径一致）"],
                ["无形资产", format_number(intangibles), "A 口径扣除；B 口径保留"],
                ["**A 严口径**（扣商誉+无形）", format_number(net_A), "最保守的下行净值"],
                ["**B 宽口径**（只扣商誉）", format_number(net_B), "无形资产视为经营必需"],
            ]
        if mkt_cap:
            rows.append(["A 口径 / 市值", f"{net_A / mkt_cap * 100:.1f}%", "—"])
            rows.append(["B 口径 / 市值", f"{net_B / mkt_cap * 100:.1f}%", "—"])
        lines.append(format_table(["项目", f"金额（{self._unit_label()}）", "说明"],
                                  rows, alignments=["l", "r", "l"]))
        lines.append("")
        if _inclusive:
            lines.append(
                "> ⚠️ **重叠科目已处理**：本标的「无形资产」行**包含**商誉"
                f"（商誉 {format_number(goodwill)}、可辨认无形 "
                f"{format_number(max(intangibles - goodwill, 0.0))}），"
                "故 A 严口径只扣**一次**（全扣无形资产），**不再单独减商誉**。"
                "两口径的真正差异只有可辨认无形那一项。成分由 "
                "`balance_sheet_overrides.json` 的 `intangibles_composition` 声明。")
            lines.append("")
        lines.append("> ⚠️ **文档张力**：因子2 `[32:45]` 用「路权拿不到 → 必须高分红」解释"
                     "高速公路，而路权是**无形资产** —— 全扣会推翻该论点。")
        lines.append("> 选择规则：无形资产若为**经营必需**（路权/牌照/专利）用 B 口径；"
                     "若主要为**收购溢价摊销**（软件资本化、客户关系）用 A 口径，并说明理由。")
        return "\n".join(lines)

    def _compute_defensive_gates(self, ts_code: str) -> str | None:
        """Compute §17.16 防御性门槛组 —— 文档的六条下行保护判据。

        出处：因子2 `[56:28]` 总现金>短期借款 · `[56:41]` 总现金>流动负债 ·
        `[1:32:31]` 现金储备占市值 30–40% · `[53:43]` 净现金>市值 · `[1:32:23]`
        现金流须兜住派息；因子3 `[42:08]` 剔除净现金后可支配现金占 ≥40%。

        **全部输出为档位/标记，不做硬否决** —— 文档自称"极端保守主义"，
        若做成硬否决会清空绝大多数标的（known_issues.md §3.21 21c）。
        """
        bs_df = self._get_annual_df("balance_sheet")
        if bs_df.empty:
            return None
        latest = bs_df.iloc[0]

        def _v(key):
            return self._safe_float(latest.get(key))

        money = _v("money_cap") or 0.0
        trad = _v("trad_asset") or 0.0
        st_dep = _v("st_deposit")
        lt_dep = _v("lt_dep") or 0.0
        cash_narrow = money + trad

        notes = []
        # Tushare 港股偶发把「现金及等价物」与「短期存款」返回成同一个数
        # （实测 06862 FY2025 两者均为 6,602.3 百万）。直接相加会双计现金，
        # 故检测到完全相等时只取一次并告警。
        dup = (st_dep is not None and money > 0 and abs(st_dep - money) < 1e-6)
        if dup:
            notes.append("`现金及等价物` 与 `短期存款` 取值完全相同（疑似 Tushare 重复），"
                         "广义现金只计一次")
        cash_broad = cash_narrow + lt_dep + (0.0 if dup else (st_dep or 0.0))

        st_borr = _v("st_borr") or 0.0
        debt = sum(x for x in (_v("st_borr"), _v("lt_borr"), _v("bond_payable"),
                               _v("non_cur_liab_due_1y"),
                               _v("lease_liab"), _v("lease_liab_noncur")) if x)
        cur_liab = _v("total_cur_liab")
        net_cash = cash_broad - debt

        ms = self._get_mktcap_shares(ts_code)
        mkt_cap = ms[0] if ms else None

        f3s = self._store.get("factor3_sensitivity") or {}
        aa_latest = f3s.get("aa_latest")

        pc = self._store.get("payout_crosscheck") or {}
        m_rec = pc.get("m_rec")
        income_df = self._get_annual_df("income")
        c_latest = self._safe_float(income_df.iloc[0].get("n_income_attr_p")) if not income_df.empty else None
        div_need = (c_latest * m_rec / 100) if (c_latest and m_rec) else None

        def _mark(ok):
            return "✅ PASS" if ok else "❌ FAIL"

        def _tier(value, bands, labels, unit=""):
            if value is None:
                return "—（数据缺失）"
            for threshold, label in zip(bands, labels):
                if value >= threshold:
                    return f"{label}（{value:.1f}{unit}）"
            return f"{labels[-1]}（{value:.1f}{unit}）"

        rows = []
        # ① 总现金 > 短期借款
        rows.append(["① 总现金 > 短期借款", "因子2 [56:28]",
                     f"{format_number(cash_narrow)} vs {format_number(st_borr)}",
                     _mark(cash_narrow > st_borr) if st_borr else "✅ PASS（无短期借款）"])
        # ② 总现金 > 流动负债合计（保守口径）
        if cur_liab is None:
            rows.append(["② 总现金 > 流动负债合计", "因子2 [56:41]",
                         f"{format_number(cash_narrow)} vs —", "—（数据缺失）"])
        else:
            rows.append(["② 总现金 > 流动负债合计", "因子2 [56:41]",
                         f"{format_number(cash_narrow)} vs {format_number(cur_liab)}",
                         _mark(cash_narrow > cur_liab)])
        # ③ 现金储备 / 市值
        if mkt_cap:
            rows.append(["③ 现金储备/市值", "因子2 [1:32:31]",
                         f"{format_number(cash_broad)} / {format_number(mkt_cap)}",
                         _tier(cash_broad / mkt_cap * 100, (40.0, 30.0),
                               ["≥40%（充裕）", "30–40%（达标）", "<30%（偏薄）"], "%")])
        else:
            rows.append(["③ 现金储备/市值", "因子2 [1:32:31]", "市值不可用", "—（数据缺失）"])
        # ④ 剔除净现金后的可支配现金占比
        if mkt_cap and aa_latest is not None:
            denom = mkt_cap - net_cash
            if denom > 0:
                rows.append(["④ AA_latest/(市值−净现金)", "因子3 [42:08]",
                             f"{format_number(aa_latest)} / {format_number(denom)}",
                             _tier(aa_latest / denom * 100, (40.0,),
                                   ["≥40%（达标）", "<40%（不达标）"], "%")])
            else:
                rows.append(["④ AA_latest/(市值−净现金)", "因子3 [42:08]",
                             f"分母 ≤ 0（净现金 {format_number(net_cash)} > 市值）",
                             "—（不适用；见⑤）"])
        else:
            rows.append(["④ AA_latest/(市值−净现金)", "因子3 [42:08]", "输入缺失", "—（数据缺失）"])
        # ⑤ 净现金 > 市值（免费选择权）
        if mkt_cap:
            rows.append(["⑤ 净现金 > 市值", "因子2 [53:43]",
                         f"{format_number(net_cash)} vs {format_number(mkt_cap)}",
                         _mark(net_cash > mkt_cap)])
        else:
            rows.append(["⑤ 净现金 > 市值", "因子2 [53:43]", "市值不可用", "—（数据缺失）"])
        # ⑥ 当期可支配现金流 ≥ 拟派息金额
        if aa_latest is not None and div_need is not None:
            rows.append(["⑥ AA_latest ≥ 拟派息(C×M_rec)", "因子2 [1:32:23]",
                         f"{format_number(aa_latest)} vs {format_number(div_need)}",
                         _mark(aa_latest >= div_need)])
        else:
            rows.append(["⑥ AA_latest ≥ 拟派息(C×M_rec)", "因子2 [1:32:23]",
                         "输入缺失", "—（数据缺失）"])

        lines = [format_header(3, "17.16 防御性门槛组"), ""]
        lines.append("> 文档的六条下行保护判据。**档位/标记，非硬否决** —— "
                     "文档自称「极端保守主义」，硬否决会清空绝大多数标的。")
        lines.append(f"> 现金口径：**狭义**（货币资金 + 交易性金融资产）= "
                     f"{format_number(cash_narrow)} {self._unit_label()}；"
                     f"**广义**（+存款）= {format_number(cash_broad)}。"
                     f"①② 用狭义（保守）；③ 与 ⑤ 用广义。")
        lines.append("")
        lines.append(format_table(["判据", "文档出处", "数值", "结果"],
                                  rows, alignments=["l", "l", "r", "l"]))
        if notes:
            lines.append("")
            for n in notes:
                lines.append(f"> ⚠️ {n}")
        lines.append("")
        _pass = sum(1 for r in rows if "PASS" in r[3] or "达标" in r[3] or "充裕" in r[3])
        lines.append(f"> 通过/达标 {_pass} / 6 项。**本表不参与「买入/观察/排除」裁决**，"
                     "仅作为风险提示输入报告的风险章节；裁决仍由 Agent C 依 §17.14 悲观档 + 仓位矩阵做出。")
        return "\n".join(lines)

    def _compute_pessimistic_grid(self, ts_code: str) -> str | None:
        """Compute §17.14 悲观基准网格（k 折扣）。

        文档要求「不可能使用正常的利润，我们要打八折」([42:45])、「在预测的最低值附近
        再打个 8.5 折」([33:44])、「穿透回报率中使用的各种参数都是最低值」([35:14])。
        但基准值现在是**中性口径** —— 本网格是把它折成悲观档的**纯输入端**手段：
        Agent B 选一行 + 说明理由，输出范式（§17.11 / 仓位矩阵）不变。
        """
        f3s = self._store.get("factor3_sensitivity")
        if not f3s:
            return None
        aa = f3s.get("aa_selected")
        # AA<0 时悲观档同样失去定义：见 §17.11 守卫与本文件上方说明。
        if isinstance(aa, (int, float)) and aa < 0:
            self._section_registry().mark(
                "17.14", "missing", "negative_basis",
                note=f"选定基准 AA = {aa:,.2f} 为负 ⇒ "
                     f"「按 k 折扣取悲观」在负值上失去方向意义，拒绝输出数值")
            return None
        if aa is None:
            return None

        ms = self._get_mktcap_shares(ts_code)
        if ms is None:
            return None
        mkt_cap, total_shares, close = ms
        rf_val, ii = self._get_rf_ii(ts_code)
        if ii is None:
            return None

        pc = self._store.get("payout_crosscheck") or {}
        m_pess = pc.get("m_pess")
        if m_pess is None:
            m_pess = pc.get("m_rec")
        if m_pess is None:
            return None

        _, q_default = self._unique_tax_rates(ts_code)
        keep = 1.0 - q_default / 100.0

        income_df = self._get_annual_df("income")
        c = self._safe_float(income_df.iloc[0].get("n_income_attr_p")) if not income_df.empty else None
        _pess_year = (str(income_df.iloc[0].get("end_date") or "")[:4] or None
                      if not income_df.empty else None)

        lines = [format_header(3, "17.14 悲观基准网格（k 折扣）"), ""]
        lines.append(f"> 基准值 AA = {format_number(aa)} {self._unit_label()}（§17.5 选定口径），"
                     f"**M_pess = {m_pess:.2f}%**（§17.10 各法最低者），"
                     f"默认 Q = {q_default:g}%（到手 {keep * 100:.0f}%），II = {ii:.2f}%。")
        lines.append(f"> ★ = k={self._PESS_K_DEFAULT}（文档算例档）；"
                     "文档另允许「判断衰退 >20% 时按七折重算」[48:45]。")
        lines.append("> **k 由商业模式/周期位置决定，Agent 选行并说明理由，不得自行插值。**")
        lines.append("")

        # 表A：精算（AA × k）
        lines.append("#### 表A：悲观精算穿透回报率 GG_pess")
        lines.append("")
        headers_a = ["k", f"AA_pess（{self._unit_label()}）", "GG_pess（默认Q）",
                     "vs 门槛 pct", f"目标买入价（{self._price_unit()}）", "vs 当前股价"]
        rows_a = []
        k_claims = []
        # **算术单点**：AA_pess / GG_pess / 目标买入价 / R_pess 全部由
        # `turtle_core.pessimistic` 计算，本节只负责渲染与登记 claim ——
        # screener 与报告因此用的是**同一份**公式实现。
        # ⚠️ `o=0.0` 与 canonical 一致（§17.14 的循环体**没有** O 项，findings D-3）；
        #    将来接通 §15 的年均回购时只需改这一处传参。
        _tc_grid = {r.k: r for r in _tc_pess.pessimistic_grid(
            aa_selected=aa, c=c, m_pess=m_pess, q_pct=q_default,
            # ⚠️ 同样必须 ×1e6（百万 → 元），与 §17.9/§17.11 一致；漏掉就等于 §17.14 仍 O-blind
            o=self._o_input().o_annual_mm * 1e6,
            mkt_cap=mkt_cap, close=close, ii=ii,
            k_values=self._PESS_K_VALUES, k_default=self._PESS_K_DEFAULT)}
        for k in self._PESS_K_VALUES:
            _row = _tc_grid[k]
            aa_p = _row.aa_pess
            gg_p = _row.gg_pess
            tbp = _row.target_buy_price
            star = " ★" if abs(k - self._PESS_K_DEFAULT) < 1e-9 else ""
            rows_a.append([
                f"{k:.2f}{star}",
                format_number(aa_p),
                f"{gg_p:.2f}%",
                f"{(gg_p - ii):+.2f}",
                f"{tbp:.2f}" if tbp is not None else "—",
                f"{(tbp / close - 1) * 100:+.1f}%" if (tbp and close) else "—",
            ])
            # 改动 1/2：每一档 k 都是 Agent 可选的决策单元格，因此都要登记声明
            _kslug = f"k{int(round(k * 100)):03d}"
            _kbasis = (f"k={k:.2f}, AA={format_number(aa)}, M_pess={m_pess:.2f}%, "
                       f"Q={q_default:g}%, II={ii:.2f}%, 市值={format_number(mkt_cap)}")
            ledger = self._claim_ledger(ts_code)
            ledger.add(f"cm17.14.{_kslug}.ggpess", f"悲观精算穿透回报率 GG_pess（k={k:.2f}）",
                       round(gg_p, 2), "%", time=_pess_year,
                       basis=_kbasis, policy="round:2", section="17.14")
            if tbp is not None:
                ledger.add(f"cm17.14.{_kslug}.tbp", f"悲观目标买入价（k={k:.2f}）",
                           round(tbp, 2), f"{self._price_unit()}/股", time=_pess_year,
                           basis=_kbasis + (f", 当前股价={close}" if close else ""), policy="round:2",
                           section="17.14")
            else:
                ledger.add(f"cm17.14.{_kslug}.tbp", f"悲观目标买入价（k={k:.2f}）",
                           None, f"{self._price_unit()}/股", basis=_kbasis,
                           policy="round:2",
                           note=("GG_pess ≤ 0 → 无解（任何价格都达不到门槛）" if close
                                 else "收盘价不可用 → 无法计算目标买入价"),
                           section="17.14")
            k_claims += [f"cm17.14.{_kslug}.ggpess", f"cm17.14.{_kslug}.tbp"]
        lines.append(format_table(headers_a, rows_a, alignments=["l"] + ["r"] * 5))
        lines.append("")
        lines.append(self._claim_ledger(ts_code).render_kv(
            k_claims, title="表A 决策单元格（KV · 带 claim id）"))
        lines.append("")
        lines.append("> 引用选定 k 档的数值时请写成 `<claim id=\"…\">值</claim>`。")
        lines.append("")

        # 表B：粗算（C × k）
        if c is not None:
            lines.append("#### 表B：悲观粗算穿透回报率 R_pess（基准值 = C 归母净利润）")
            lines.append("")
            headers_b = ["k", f"C_pess（{self._unit_label()}）", "R_pess（默认Q）", "vs 门槛 pct"]
            rows_b = []
            for k in self._PESS_K_VALUES:
                c_p = c * k
                r_p = _tc_grid[k].r_pess          # 同一份实现（turtle_core）
                star = " ★" if abs(k - self._PESS_K_DEFAULT) < 1e-9 else ""
                rows_b.append([f"{k:.2f}{star}", format_number(c_p),
                               f"{r_p:.2f}%", f"{(r_p - ii):+.2f}"])
            lines.append(format_table(headers_b, rows_b, alignments=["l"] + ["r"] * 3))
            lines.append("")

        lines.append("> 用途：**悲观档是决策口径** —— §17.9/§17.11 的中性结果仅作对照；"
                     "选定 k 后，把对应行的 GG_pess 代入 §17.11 表B 的「GG−II」与目标买入价。")
        lines.append("> 注意：仓位矩阵阈值不因折扣而调整（折扣是输入端，矩阵是输出约定）。")
        return "\n".join(lines)

    def _compute_revenue_sensitivity(self, ts_code: str) -> str | None:
        """Compute §17.13: λ revenue sensitivity on the DEFAULT combo.

        结余(k) = AA_default + λ×(k−1)×S_latest；m′ = M_rec/100×(1−Q_default/100)；
        GG(k) = 结余(k)×m′ / 市值 × 100；临界收入倍数 k* 令 GG(k)=II。
        λ 缺失/≈0 或 m′≤0 → 降级段（"—" + ⚠️）。
        """
        f3s = self._store.get("factor3_sensitivity")
        if not f3s:
            return None
        aa_default = f3s.get("aa_selected")
        if aa_default is None:
            return None
        lam = f3s.get("lambda_median")
        lam_reliability = f3s.get("lambda_reliability")
        lam_warnings = f3s.get("lambda_warnings") or []

        income_df = self._get_annual_df("income")
        if income_df.empty:
            return None
        s_latest = self._safe_float(income_df.iloc[0].get("revenue"))
        if s_latest is None or s_latest <= 0:
            return None

        ms = self._get_mktcap_shares(ts_code)
        if ms is None:
            return None
        mkt_cap, total_shares, close = ms
        rf_val, ii = self._get_rf_ii(ts_code)
        if ii is None:
            return None

        # M_rec + default Q
        pc = self._store.get("payout_crosscheck")
        m_rec = pc.get("m_rec") if pc else None
        if m_rec is None:
            years = [str(r["end_date"])[:4] for _, r in income_df.iterrows()]
            m_rec = self._mean_first3(self._get_payout_by_year(), years)
        if m_rec is None:
            return None
        q_rates, q_default = self._unique_tax_rates(ts_code)
        m_prime = m_rec / 100 * (1 - q_default / 100)

        lines = [format_header(3, "17.13 收入敏感性（λ 外推）"), ""]
        lines.append(f"> AA_default = {format_number(aa_default)} {self._unit_label()}，"
                     f"S 最新年收入 = {format_number(s_latest)} {self._unit_label()}，"
                     f"M_rec = {m_rec:.2f}%，默认 Q = {q_default:g}%，"
                     f"m′ = M_rec×(1−Q) = {m_prime * 100:.2f}%，II = {ii:.2f}%。")
        lines.append("> 本表基于**默认组合**（M_rec × 默认Q × AA_default）计算；"
                     "其他 M/Q 选择按 M×(1−Q) 比例缩放 GG。")
        lines.append("")

        degraded = lam is None or abs(lam) < 1e-9 or m_prime <= 0

        # ---- fail-closed：λ 方向不自洽时**拒绝输出数值** ----
        # 结余(k) = AA + λ(k−1)×S；k<1（收入下滑）时若 λ<0，结余会**上升**，
        # 于是收入越降、回报越高。06862 实测该表 0.9×/0.8×/0.7× 行给出结余
        # 10,415 / 16,721 / 23,026、GG 15.19% / 24.38% / 33.57% —— 方向性错误。
        # 旧实现只在表内附警告后照常出厂，等于把错误结论交给下游。现改为：
        # 标记 §17.13 为 input_inconsistent（原因+补救进状态表）且**不输出该小节**，
        # 下行压力由 §17.14 悲观档承担（AGENTS.md：status != ok ⇒ 停手并报告，绝不手算）。
        # 硬拒绝的唯一判据是**方向**：λ ≤ 0 时结余(k) = AA + λ(k−1)S 在 k<1 反而上升，
        # 等价于"收入越降、回报越高"（06862：λ=-1.4587 ⇒ 0.9×/0.8×/0.7× 给出 10,415/16,721/23,026）。
        # 至于"λ 序列符号不稳定"这类**可靠性**警告，只降低可信度评级、不构成方向性错误，
        # 仍照常出表并随表携带警告（否则会把大量标的的 §17.13 一并抹掉，属于过度封锁）。
        if not degraded and lam <= 0:
            self._section_registry().mark(
                "17.13", "missing", "input_inconsistent",
                note=f"λ = {lam:.4f} ≤ 0 ⇒ 收入下滑会抬高结余，方向不自洽")
            return None

        if degraded:
            reason = ("λ 缺失" if lam is None else
                      ("λ≈0（收入变动不传导至结余）" if abs(lam) < 1e-9 else "m′≤0"))
            lines.append(f"- ⚠️ 降级：{reason}，无法外推收入敏感性。")
            headers = ["收入情景", "结余", "精算回报率 GG", "vs 门槛"]
            rows = [[f"{k:.1f}×", "—", "—", "—"] for k in [1.0, 0.9, 0.8, 0.7]]
            lines.append(format_table(headers, rows, alignments=["l", "r", "r", "r"]))
            lines.append("- 临界收入倍数 k* = —")
            return "\n".join(lines)

        lines.append(f"- 经营杠杆 λ = {lam:.4f}（可靠性：{lam_reliability or '—'}）")
        for wm in lam_warnings:
            lines.append(f"  ⚠️ {wm}")
        lines.append("")

        headers = ["收入情景", f"结余（{self._unit_label()}）", "精算回报率 GG", "vs 门槛 pct"]
        rows = []
        computed: list[tuple[float, float]] = []
        for k in [1.0, 0.9, 0.8, 0.7]:
            bal = aa_default + lam * (k - 1) * s_latest
            gg = bal * m_prime / mkt_cap * 100
            computed.append((k, bal))
            rows.append([f"{k:.1f}×", format_number(bal), f"{gg:.2f}%", f"{(gg - ii):+.2f}"])

        # 自检（不变式 I6 的输出侧对应）：收入下降不得抬高结余。若仍被触发，
        # 说明除 λ 符号外还有其它病理（口径/单位错），此时宁可不给数字。
        ordered = sorted(computed, key=lambda t: t[0])
        for (k1, b1), (k2, b2) in zip(ordered, ordered[1:]):
            if b2 < b1 - 1e-9:
                self._section_registry().mark(
                    "17.13", "missing", "input_inconsistent",
                    note=f"结余在收入上升时反而下降（k={k1} → {b1:,.2f}；k={k2} → {b2:,.2f}）")
                return None
        lines.append(format_table(headers, rows, alignments=["l", "r", "r", "r"]))
        lines.append("")

        # Critical revenue multiple k* solving GG(k)=II
        denom = lam * s_latest
        if abs(denom) > 0:
            k_star = 1 + (ii / 100 * mkt_cap / m_prime - aa_default) / denom
            lines.append(f"- 临界收入倍数 k* = {k_star:.2f}（令 GG = II 反解）")
            if k_star >= 0.85:
                resilience = "敏感（k* ≥ 0.85）"
            elif k_star >= 0.70:
                resilience = "中等（0.70 ≤ k* < 0.85）"
            else:
                resilience = "强（k* < 0.70）"
            lines.append(f"- 安全边际韧性：{resilience}")
        else:
            lines.append("- 临界收入倍数 k* = —（λ×S ≈ 0）")

        return "\n".join(lines)
