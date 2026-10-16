#!/usr/bin/env python3
"""numeric_audit.py —— 数值审核（**独立第二实现** + 提取可溯性）。

存在理由
--------
最终报告里的每一个决策数字都来自 §17 的 Python 预计算。若 §17 自己算错，
`invariants.py`（只查**包内自洽**）与 `field_coverage.py`（只查**是否少一行**）
都看不见 —— 它们都能与错误共存。实测逃过这两层的缺陷包括：

  · §17.9 门槛价格以**报表币种**计算却按**交易币种**渲染（港股差 1/fx，6 行 3 行符号翻转）；
  · §17.9 用 §17.1 的三年均值而不是 M_rec（装配次序导致），而两处脚注都声称"同一 M_rec"；
  · 门槛 II 是**已废止的 v1 公式**（3.67% 而非 5.00%）；
  · §17 用了某个输入值，而该输入在 §1–§16 里**没有可见来源行**（如 Rf 在 §14 显示「数据缺失」
    却被 §17.2 当作"来自 §14"使用）。

本脚本按 `strategies/turtle/references/shared_tables.md` 与提示词里的公式**重新实现一遍**
（**不 import** `derived_metrics`，也不复用它的任何计算函数），只从数据包 §1–§16 的表格取值，
再与 §17 渲染出来的数对账。这就是"清洁室"原则在数值层的落地：
第二个实现必须独立，否则同源错误会被一起复现。

三层检查
--------
- **E（提取/可溯性）**：§17 的输入能否在 §1–§16 里找到**可见来源**且取值一致；
- **D（派生对账）**：§17 的格子能否由 §1–§16 按声明公式复现；
- **X（跨表恒等）**：同一物理量在不同小节必须相等（§17.9 门槛价格 vs §17.11/§17.14 目标买入价）。

输出
----
- `numeric_audit.json`：逐条 finding（含 severity / expected / actual / basis），供门禁消费；
- `numeric_audit.md`：**裁定工作单** —— LLM 子代理必须逐条给出
  {真实错误 / 口径差异（写依据）/ 脚本误判（写依据）}，并补做需要 PDF 的提取核查。

退出码：0 = 无阻断级发现 · 1 = 存在阻断级发现 · 2 = 输入不可读。
`--allow-degraded` 显式接受带缺陷交付（写入 verdict.degraded）。

⚠️ 本脚本**只报差异，不判对错**。差异可能是真错，也可能是口径差异（例如 §17.8 的
FCF 定义刻意不同于 FCFF）；哪一种必须由子代理对照年报与文档裁定，不得由脚本替代。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from field_coverage import _section, _tables, _row_of, _rows_of, _numeric_cells  # noqa: E402

# ---------------------------------------------------------------------------
# 规范常量：**照文档抄**，不从代码 import（否则"第二实现"失去意义）
# ---------------------------------------------------------------------------
II_FLOOR = 5.0        # shared_tables.md：全市场统一 II = max(5%, Rf+3%)
II_SPREAD = 3.0
Q_DEFAULT_HK = 20.0   # 港股红筹/开曼、港股通 —— 默认档
Q_DEFAULT_A = 0.0     # A 股持有 > 1 年
Q_DEFAULT_US = 10.0   # W-8BEN

MATCH, MISMATCH, UNSOURCED, UNVERIFIABLE = "MATCH", "MISMATCH", "UNSOURCED", "UNVERIFIABLE"
CRITICAL, MAJOR, MINOR = "critical", "major", "minor"

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


# ---------------------------------------------------------------------------
# 数据包视图：把 markdown 表格变成 {行标签: {年份: 值}}
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    code: str
    title: str
    status: str
    severity: Optional[str]
    detail: str
    expected: Optional[float] = None
    actual: Optional[float] = None
    basis: str = ""
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class PackView:
    """Read-only view over a data pack. Parsing only — **no formulas here**."""

    def __init__(self, text: str) -> None:
        self.text = text or ""

    # -- raw access ---------------------------------------------------------
    def section(self, number: str) -> str:
        return _section(self.text, number)

    def annual_table(self, number: str) -> Tuple[List[str], List[List[str]]]:
        """Return ``(years, rows)`` from the first table in §number with ≥2 FY columns."""
        for header, rows in _tables(self.section(number)):
            years = [c.strip() for c in header if _YEAR_RE.match(c.strip())]
            if len(years) >= 2:
                return years, rows
        return [], []

    def series(self, number: str, aliases: Sequence[str]) -> Dict[str, float]:
        """FY series for the first row matching any alias: ``{year: value}``."""
        years, rows = self.annual_table(number)
        if not years or not rows:
            return {}
        row = _row_of(rows, aliases)
        if row is None:
            return {}
        header = self._header_of(number)
        out: Dict[str, float] = {}
        for i, cell in enumerate(row):
            if i == 0:
                continue
            label = header[i] if i < len(header) else ""
            if not _YEAR_RE.match(label.strip()):
                continue
            v = _cell(cell)
            if v is not None:
                out[label.strip()] = v
        return out

    def _header_of(self, number: str) -> List[str]:
        for header, rows in _tables(self.section(number)):
            if any(_YEAR_RE.match(c.strip()) for c in header):
                return [c.strip() for c in header]
        return []

    def has_row(self, number: str, aliases: Sequence[str]) -> Tuple[bool, bool]:
        """``(row_exists, row_has_any_value)``."""
        years, rows = self.annual_table(number)
        found = _rows_of(rows, aliases)
        if not found:
            return False, False
        return True, any(_numeric_cells(r) for r in found)

    def scalar(self, number: str, aliases: Sequence[str]) -> Optional[float]:
        """First numeric value of the first matching row (key-value sections).

        别名未命中时回落到「按行标签排除后的首个数值单元格」——不是为了放宽口径，
        而是因为同一份数据有两种渲染法：`| 无风险利率 | 1.689 |`（标签在首格）与
        `| 日期 | 10年期国债收益率 (%) |` + 数据行（标签在**表头**、值在数据行）。
        旧实现在第二种形态上直接返回 None，于是把一份**有** §14 的包报成 E5
        「§14 无可见数值」（实测 00506：门禁阻断在一条脚本假阳性上）。
        回落的匹配范围严格限定在**表内**（表头与分隔行已由 `_tables` 去掉），
        所以它拿不到正文里的散落数字；调用方若想要精确口径应传别名。
        """
        for _h, rows in _tables(self.section(number)):
            row = _row_of(rows, aliases)
            if row:
                vals = _numeric_cells(row)
                if vals:
                    return vals[0]
        excl = tuple(aliases) + ("日期", "项目", "指标", "变量")
        for _h, rows in _tables(self.section(number)):
            for row in rows:
                if not row or any(a in row[0] for a in excl):
                    continue
                vals = _numeric_cells(row)
                if vals:
                    return vals[0]
        return None

    def bullet_number(self, number: str, label: str) -> Optional[float]:
        body = self.section(number)
        m = re.search(rf"{re.escape(label)}[^=\n]*=\s*\**([\d,]+(?:\.\d+)?)", body)
        return _cell(m.group(1)) if m else None

    def latest_fy(self) -> Optional[str]:
        """最新的**年报**年度列（排除 2026H1 / 2026Q1 这类中期列）。"""
        fy = [y.strip() for y in self._header_of("3") if _YEAR_RE.match(y.strip())]
        return max(fy) if fy else None

    def value_at(self, number: str, aliases: Sequence[str], year: Optional[str],
                 missing_as_zero: bool = False) -> Optional[float]:
        """Read a row at a **specific FY**.

        Why this exists: 本脚本首版用「各行的最新年度」拼 EV，于是拿 2022 年的长期贷款
        去配 2025 年的市值 —— 一个审计脚本自己算错，等于制造假阳性。跨行必须同年。
        """
        if not year:
            return None
        s = self.series(number, aliases)
        if not s:
            return None
        if year in s:
            return s[year]
        return 0.0 if missing_as_zero else None

    def grid_rows(self, number: str) -> List[List[str]]:
        """All markdown table data rows of a §17 sub-section (for §17.11/12/14)."""
        out: List[List[str]] = []
        for _h, rows in _tables(self.section(number)):
            out.extend(rows)
        return out

    def kv_line(self, number: str, label: str) -> Optional[float]:
        """Match ``| label | 12.34 |`` in a key-value table."""
        for _h, rows in _tables(self.section(number)):
            row = _row_of(rows, [label])
            if row:
                vals = _numeric_cells(row)
                if vals:
                    return vals[0]
        return None


def _cell(text) -> Optional[float]:
    if text is None:
        return None
    s = (str(text).replace(",", "").replace("%", "").replace("**", "")
         .replace("x", "").replace("★", "").strip())
    s = s.replace("—", "").replace("–", "").strip()
    m = re.fullmatch(r"[-+]?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _pct(text) -> Optional[float]:
    return _cell(text)


def _close(a: Optional[float], b: Optional[float], tol: float) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# 规范公式（第二实现；每一条都注明出处）
# ---------------------------------------------------------------------------
def ii_of(rf_pct: float) -> float:
    """II = max(5%, Rf + 3%) —— shared_tables.md「门槛值 II 计算公式」。"""
    return max(II_FLOOR, rf_pct + II_SPREAD)


def default_q(ts_code: str) -> float:
    up = str(ts_code or "").upper()
    if up.endswith(".HK"):
        return Q_DEFAULT_HK
    if up.endswith(".US"):
        return Q_DEFAULT_US
    return Q_DEFAULT_A


def r_rate(c: float, m_pct: float, q_pct: float, market_cap: float, o: float = 0.0) -> float:
    """粗算 R = [C × M × (1−Q) + O] / 市值 —— shared_tables.md「穿透回报率公式」。"""
    return (c * m_pct / 100 * (1 - q_pct / 100) + o) / market_cap * 100


def gg_rate(aa: float, m_pct: float, q_pct: float, market_cap: float, o: float = 0.0) -> float:
    """精算 GG = [AA × M × (1−Q) + O] / 市值。

    ⚠️ `market_cap` 与 `o` 必须**同单位**：本脚本一律用**百万**（`cap_pack` 来自
    §17.8 的「百万元」行），故 `o` 传 `cm17.11.o` 的百万值，不要预先 ×1e6。
    """
    return (aa * m_pct / 100 * (1 - q_pct / 100) + o) / market_cap * 100


def _o_from_header(pv) -> float:
    """从 §17.11 头部读 O（注销型回购年均，单位：**百万**）。读不到 ⇒ 0（旧行为）。"""
    sec = pv.section("17.11") or ""
    # ⚠️ **别把 O 绑死在来源措辞上**。旧实现要求同一行里出现「来自」或「默认」，
    # 于是 06078 改成「由承诺执行评估派生」后立刻读成 0，把正确的 GG 判成 MISMATCH
    # （scripts 与产物措辞耦合的典型）。改为：只在**以「基准值」开头的那一行**里取
    # 「O = <数值>」，不依赖任何来源词。⚠️ 这一行同时含 `C = 164.53`，故必须锚定
    # 匹配 `O =`（用 `(?!C)` 防误配其它字段）。
    for line in sec.splitlines():
        if "基准值" not in line:
            continue
        m = re.search(r"O\s*=\s*([\d,]+(?:\.\d+)?)", line)
        if not m:
            break
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            break
    return 0.0


def tbp_of(price: float, gg_pct: float, ii_pct: float) -> float:
    """目标买入价 = 当前股价 × GG / II（phase3_valuation.md 步骤4-1）。"""
    return price * gg_pct / ii_pct


def mean3(values: Sequence[Optional[float]]) -> Optional[float]:
    vals = [v for v in values[:3] if v is not None]
    return sum(vals) / len(vals) if vals else None


# ---------------------------------------------------------------------------
# 检查项
# ---------------------------------------------------------------------------
def _f(code, title, status, severity, detail, expected=None, actual=None,
       basis="", evidence=None) -> Finding:
    return Finding(code, title, status, severity, detail, expected, actual, basis,
                   evidence or [])


def check_pack_internal(pv: PackView) -> List[Finding]:
    """E 组：§1–§16 自身的提取/一致性签名（不涉及 §17）。"""
    out: List[Finding] = []

    # E1 §1 价格 / §2 价格 / §11 最新收盘 必须同源
    p1 = pv.scalar("1", ["当前价格", "最新价格"])
    p2 = pv.scalar("2", ["最新价格", "收盘价"])
    p11 = pv.scalar("11", ["最新收盘"])
    # §1 与 §2 都是**当日**行情 ⇒ 必须同值；§11 是**周线**最后一根，日期可以不同，
    # 因此单独判、且只作披露（首版把 §11 也算进"同源"里 ⇒ 6 个包被误报为 critical）。
    if p1 is not None and p2 is not None:
        st = MATCH if _close(p1, p2, max(0.01, abs(p2) * 0.005)) else MISMATCH
        out.append(_f("E1", "价格同源（§1 ↔ §2，同日行情）", st,
                      None if st == MATCH else CRITICAL,
                      "§1 与 §2 必须展示同一交易日的同一价格", p2, p1, "", []))
    if p11 is not None and p2 is not None and not _close(p11, p2, max(0.01, abs(p2) * 0.005)):
        out.append(_f("E1b", "周线收盘（§11）与日线（§2）的比较", UNVERIFIABLE, MINOR,
                      "§11 的「最新收盘」是周线序列的最后一根，交易日可与 §2 不同 ⇒ "
                      "不可据此判定错误，但**必须**核对 §17.6 分位与 §17.8 ③ 十年最低价"
                      "用的是哪一套价格",
                      p11, p2, "", [f"§11={p11}", f"§2={p2}"]))

    # E2 §1 总市值 vs §1 价格 × 股数（股数只能从 §4 权益/每股净资产等间接推，若有）
    cap1 = pv.scalar("1", ["总市值"])
    cap2 = pv.scalar("2", ["总市值"])
    if cap1 is not None and cap2 is not None:
        st = MATCH if _close(cap1, cap2, max(0.01, abs(cap1) * 0.005)) else MISMATCH
        out.append(_f("E2", "总市值同源（§1/§2）", st, None if st == MATCH else CRITICAL,
                      "§1 与 §2 的总市值必须一致", cap1, cap2, "", [f"§1={cap1}", f"§2={cap2}"]))

    # E3 利润表同年口径自洽（**同口径**三元组，known_issues.md §18）
    pre = pv.series("3", ["除税前溢利", "利润总额", "税前利润"])
    tax = pv.series("3", ["税项", "所得税费用", "所得税"])
    post = pv.series("3", ["除税后溢利", "净利润"])
    cont = pv.series("3", ["持续经营业务税后利润"])
    disc = pv.series("3", ["终止", "非持续业务"])
    if pre and tax and (cont or post):
        bad = []
        if cont:
            for y in sorted(set(pre) & set(tax) & set(cont)):
                gap = pre[y] - tax[y] - cont[y]
                if abs(gap) > max(1.0, abs(cont[y]) * 0.005):
                    bad.append(f"{y}: 除税前(持续) {pre[y]:,.2f}−税项(持续) {tax[y]:,.2f}"
                               f"−持续经营税后 {cont[y]:,.2f} = {gap:,.2f}")
            for y in sorted(set(cont) & set(post)):
                gap = post[y] - (cont[y] + disc.get(y, 0.0))
                if abs(gap) > max(1.0, abs(post[y]) * 0.005):
                    bad.append(f"{y}: 合计 {post[y]:,.2f} ≠ 持续 {cont[y]:,.2f}"
                               f" + 终止 {disc.get(y, 0.0):,.2f}（差 {gap:,.2f}）")
            basis = "同口径：除税前(持续)−税项(持续)=持续经营税后；合计=持续+终止"
        else:
            for y in sorted(set(pre) & set(tax) & set(post)):
                gap = pre[y] - tax[y] - post[y]
                if abs(gap) > max(1.0, abs(post[y]) * 0.005):
                    bad.append(f"{y}: {pre[y]:,.2f}−{tax[y]:,.2f}−{post[y]:,.2f} = {gap:,.2f}")
            basis = "包内无「持续经营业务税后利润」行 ⇒ 只能按合计口径判（口径可靠性未知）"
        out.append(_f("E3", "§3 同年口径自洽", MISMATCH if bad else MATCH,
                      CRITICAL if bad else None,
                      ("同一年内**同口径**三元组无法自洽 ⇒ §3 混用了口径，"
                       "以除税前/除税后为基础的比率全被污染"
                       if bad else "逐年同口径闭合") + ("" if cont else "（仅合计口径）"),
                      None, None, basis, bad))

    # E4 §6 分红年度重复且取值不同 ⇒ 法1 不可算
    dup = []
    for _h, rows in _tables(pv.section("6")):
        by_year: Dict[str, set] = {}
        for r in rows:
            if len(r) < 2:
                continue
            y = r[0].strip()[:4]
            if not _YEAR_RE.match(y):
                continue
            v = _cell(r[1])
            if v is not None:
                by_year.setdefault(y, set()).add(round(v, 6))
        dup = [f"{y}: {sorted(vs)}" for y, vs in by_year.items() if len(vs) > 1]
        if by_year:
            break
    if dup:
        out.append(_f("E4", "§6 每股股息的年度唯一性", UNVERIFIABLE, MAJOR,
                      "同一年度出现多个不同股息值 ⇒ 按『年度单一值』口径计算的法1不可复核",
                      None, None, "港股 dps_hkd 为按公告快照，需人工核对年报附注", dup))

    return out


def check_sources(pv: PackView) -> List[Finding]:
    """E5 组：§17 用到的输入必须在 §1–§16 里有**可见来源行**。

    这一条抓的是"算得出但查不到"：数值来自 API/--rf 种子，却没有落到包里。
    实测 06862：§14 渲染「数据缺失」，而 §17.2 却写 Rf=1.68%"来自 §14"。
    """
    out: List[Finding] = []

    rf_pack = pv.scalar("14", ["无风险利率", "Rf", "收益率", "yield"])
    rf_used = pv.kv_line("17.2", "Rf（无风险利率）")
    if rf_used is not None and rf_pack is None:
        out.append(_f("E5", "Rf 的可见来源（§17.2 ← §14）", UNSOURCED, MAJOR,
                      "§17.2 使用了 Rf，但 §14 没有任何可见数值（渲染为「数据缺失」）⇒ "
                      "该输入不可追溯。手持 --rf 种子值时也必须在 §14 落盘，否则审计与"
                      "复算都无法确认用的是哪一天、哪一个利率",
                      rf_used, None, "§17 的每个输入都应能在 §1–§16 里查到出处",
                      [f"§17.2 Rf = {rf_used}", "§14 = 无可见数值"]))
    elif rf_used is not None:
        st = MATCH if _close(rf_used, rf_pack, 1e-6) else MISMATCH
        out.append(_f("E5", "Rf 的可见来源（§17.2 ← §14）", st,
                      None if st == MATCH else CRITICAL,
                      "§17.2 的 Rf 必须等于 §14 的可见值", rf_pack, rf_used, "", []))

    # E6 §17.2 的 D / E 必须来自可见的 §5 行
    for label, aliases_5, code in (
        ("D 折旧与摊销（§17.2 ← §5）", ["折旧与摊销", "折旧及摊销"], "E6"),
        ("E 资本开支（§17.2 ← §5）", ["资本开支", "资本支出", "购建固定资产"], "E7"),
    ):
        used = pv.kv_line("17.2", label.split("（")[0])
        exists, has_val = pv.has_row("5", aliases_5)
        if used is not None and not (exists and has_val):
            out.append(_f(code, label, UNSOURCED, MAJOR,
                          f"§17.2 使用了该输入，但 §5 里没有可见且非空的 {aliases_5[0]} 行 ⇒ "
                          "无法从包内复算，也无法判断取值是否取自正确科目",
                          used, None, "§17 输入需在 §1–§16 内可溯",
                          [f"§17.2 = {used}", f"§5 行存在={exists} 有值={has_val}"]))

    # E9 §1 必须给出股本及其来源（#25：股本决定每股锚点与目标买入价）
    _decl, _trusted = _declared_shares_mm(pv)
    if _decl is None:
        out.append(_f("E9", "数据包股本行及其来源", UNSOURCED, MAJOR,
                      "数据包内（§1 或 §17.8 的股本口径说明）没有「总股本（百万股）」⇒ 无法判断 §17.8 的每股锚点与 "
                      "§17.11/§17.14 的目标买入价用的是**年报已发行股数**还是「市值÷价」反推值。"
                      "修复：提供 --shares / TUSHARE_SHARES_MM / {output_dir}/shares.json"
                      "（known_issues.md #25）", None, None, "", []))
    elif not _trusted:
        out.append(_f("E9", "数据包股本行及其来源", UNVERIFIABLE, MAJOR,
                      "§1 的股本是**反推值（降级）**｜不是年报已发行股数 ⇒ 每股锚点与目标买入价"
                      "带着约 2.8% 的口径误差。请提供年报股本",
                      None, _decl, "known_issues.md #25", []))
    else:
        out.append(_f("E9", "数据包股本行及其来源", MATCH, None,
                      f"申报股本 {_decl:,.2f} 百万股（权威口径）", None, _decl, "", []))

    # E8 §17.5 的 OCF / 资本开支来源
    aa_note_rows = ["OCF", "经营业务现金净额", "经营活动现金流", "经营活动产生的现金流量净额"]
    exists, has_val = pv.has_row("5", aa_note_rows)
    if pv.bullet_number("17.5", "AA_2y") is not None and not (exists and has_val):
        out.append(_f("E8", "AA 的可见来源（§17.5 ← §5 OCF）", UNSOURCED, MAJOR,
                      "§17.5 给出了 AA，但 §5 没有可见且非空的经营现金流行",
                      None, None, "AA = OCF − |capex| − 扣减项①", []))

    return out


def check_derivation(pv: PackView, ts_code: str) -> List[Finding]:
    """D 组：§17 的格子能否由 §1–§16 按规范公式复现。"""
    out: List[Finding] = []
    q = default_q(ts_code)

    # 市场价与市值：§17.8 的两行
    price = pv.scalar("2", ["最新价格", "收盘价"]) or pv.scalar("1", ["当前价格"])
    cap_pack = pv.kv_line("17.8", "总市值")          # 报表币种（百万）
    cap_src = pv.scalar("1", ["总市值"]) or pv.scalar("2", ["总市值"])  # 交易币种
    fx = _load_fx(pv)

    # D1 II
    rf = pv.kv_line("17.2", "Rf（无风险利率）")
    ii_used = pv.kv_line("17.2", "II（门槛值）")
    if rf is not None:
        expect = ii_of(rf)
        if ii_used is None:
            out.append(_f("D1", "门槛 II（§17.2 ← max(5%, Rf+3%)）", MISMATCH, CRITICAL,
                          "§17.2 未渲染 II，但 Rf 可用 ⇒ 门槛无法确认",
                          expect, None, "shared_tables.md：全市场统一 max(5%, Rf+3%)", []))
        else:
            st = MATCH if _close(ii_used, expect, 0.005) else MISMATCH
            out.append(_f("D1", "门槛 II（§17.2 ← max(5%, Rf+3%)）", st,
                          None if st == MATCH else CRITICAL,
                          "II 必须等于 max(5%, Rf+3%)；不等通常意味着用了已废止的 v1 分市场档位",
                          expect, ii_used, f"Rf={rf}", []))

    # D2 §17.5 的 AA：内部勾稽 + 与 §5 对账 + 三个变体
    aa_tab = _aa_table(pv)
    ocf5 = pv.series("5", ["经营业务现金净额", "经营活动现金流", "经营活动产生的现金流量净额", "OCF"])
    capex5 = pv.series("5", ["资本开支", "资本支出", "购建固定资产"])
    if aa_tab:
        # D2a 内部算术
        bad = []
        for y, rec in sorted(aa_tab.items(), reverse=True):
            if {"ocf", "aa"} <= set(rec):
                expect = rec["ocf"] - rec.get("capex", 0.0) - rec.get("deduct", 0.0)
                if not _close(rec["aa"], expect, max(1.0, abs(expect) * 0.005)):
                    bad.append(f"{y}: AA 包={rec['aa']:,.2f} vs OCF−|capex|−扣减={expect:,.2f}")
        out.append(_f("D2i", "§17.5 内部勾稽 AA = OCF − |capex| − 扣减项①",
                      MISMATCH if bad else MATCH, MAJOR if bad else None,
                      "§17.5 表内三个分量必须自洽（不自洽说明列被错位渲染）",
                      None, None, "§17.5 表头声明的公式", bad))
        # D2b 与 §5 对账（提取一致性）
        bad_src = []
        for y, rec in sorted(aa_tab.items(), reverse=True):
            if "ocf" in rec and y in ocf5 and not _close(rec["ocf"], ocf5[y], max(1.0, abs(ocf5[y]) * 0.005)):
                bad_src.append(f"{y}: §17.5 OCF={rec['ocf']:,.2f} vs §5={ocf5[y]:,.2f}")
            if "capex" in rec and y in capex5 and not _close(rec["capex"], abs(capex5[y]),
                                                            max(1.0, abs(capex5[y]) * 0.005)):
                bad_src.append(f"{y}: §17.5 capex={rec['capex']:,.2f} vs §5={abs(capex5[y]):,.2f}")
        if bad_src:
            out.append(_f("D2s", "§17.5 的 OCF/capex 与 §5 一致（提取）", MISMATCH, CRITICAL,
                          "§17.5 的分量必须逐字取自 §5；不等说明取了不同行或不同币种",
                          None, None, "", bad_src))
        # D2c 三个变体
        vals = [aa_tab[y]["aa"] for y in sorted(aa_tab, reverse=True) if "aa" in aa_tab[y]]
        if vals:
            aa_all = sum(vals) / len(vals)
            aa_2y = sum(vals[:2]) / min(2, len(vals))
            pos = [v for v in vals if v >= 0]
            aa_excl = sum(pos) / len(pos) if pos else aa_all
            for label, expect, code in (("AA_2y", aa_2y, "D2a"),
                                        ("AA_all", aa_all, "D2b"),
                                        ("AA_excl", aa_excl, "D2c")):
                used = pv.bullet_number("17.5", label)
                if used is None:
                    continue
                st = MATCH if _close(used, expect, max(1.0, abs(expect) * 0.01)) else MISMATCH
                out.append(_f(code, f"{label}（§17.5 ← 表内 AA 列的均值）", st,
                              None if st == MATCH else MAJOR,
                              "AA 变体必须能由 §17.5 自己的 AA 列复现；不等说明均值窗口或"
                              "「剔除规则」与声明不同",
                              round(expect, 2), used,
                              f"窗口 {max(aa_tab)}–{min(aa_tab)}，{len(vals)} 年", []))

    # D3 §17.10 法1 / 法2 三年均值
    dps_single = _dps_by_year(pv, aggregate="single")
    dps_sum = _dps_by_year(pv, aggregate="sum")
    eps = pv.series("3", ["每股收益", "基本每股收益", "EPS"])
    dist = pv.series("5", ["已付股息", "分配股利"])
    np_parent = pv.series("3", ["股东应占溢利", "归母净利润", "归属于母公司"])
    years_eps = sorted(eps, reverse=True)

    def _m1(table):
        return _mean_first3([_safe_div(table.get(y), eps.get(y), 100) for y in years_eps])

    # 法1 的年度口径本身就是歧义点（A 股一年多次分红 vs 港股按公告快照取年度单一值），
    # 因此两个变体都算：命中任一即 MATCH，并写明命中的是哪一个；都不中才 MISMATCH。
    m1_cands = {"年度求和（A 股多次分红）": _m1(dps_sum),
                "年度单一值（港股快照口径）": _m1(dps_single)}
    m1 = next((v for v in m1_cands.values() if v is not None), None)
    # ⚠️ 窗口 = 最近 3 个**财年**，且窗口内"有证据的零"必须按 0 计入（见 `_window_mean3`）。
    # 旧实现用 `_mean_first3([...for y in sorted(np_parent, reverse=True)])` ⇒ 只对**非空**年份
    # 求前 3 个，跨过了更多财年（06078：{2025,2023,2021} 而非 {2025,2024,2023}），
    # 这正是 06078 数值审核 D3b 误报的根因。
    _np_years = sorted(np_parent, reverse=True)
    _np_covered = set(_np_years)
    m2 = _window_mean3(_np_years,
                       {y: _safe_div(dist.get(y), np_parent.get(y), 100) for y in _np_years},
                       covered=_np_covered)
    used1 = _grid_mean(pv, "法1")
    if used1 is None:
        pass
    elif all(v is None for v in m1_cands.values()):
        out.append(_unverifiable("D3a", "§17.10 法1 三年均值",
                                 ["§6 每股股息（年度值或年度和）", "§3 每股收益"],
                                 "法1 = DPS ÷ EPS 的三年均值"))
    else:
        hit = [name for name, v in m1_cands.items()
               if v is not None and _close(used1, v, max(1.0, abs(v) * 0.02))]
        st = MATCH if hit else MISMATCH
        out.append(_f("D3a", "§17.10 法1 三年均值", st, None if st == MATCH else MAJOR,
                      ("命中口径：" + "、".join(hit)) if hit else
                      "两种年度口径均无法复现 ⇒ 可能是币种未折算（港股 DPS=HKD、EPS=报表币种，"
                      "不折汇会高估约 1/fx−1），或用了别的分红口径",
                      round(next(v for v in m1_cands.values() if v is not None), 2), used1,
                      "shared_tables.md：同币种口径",
                      [f"{n} = {v:.2f}" for n, v in m1_cands.items() if v is not None]))
    used2 = _grid_mean(pv, "法2")
    if used2 is not None and m2 is None:
        out.append(_unverifiable("D3b", "§17.10 法2 三年均值",
                                 ["§5 已付股息", "§3 归母净利润"], "法2 = 分配现金 ÷ 归母"))
    elif used2 is not None:
        st = MATCH if _close(used2, m2, max(1.0, abs(m2) * 0.02)) else MISMATCH
        out.append(_f("D3b", "§17.10 法2 三年均值", st, None if st == MATCH else MAJOR,
                      "法2 = §5 分配现金 ÷ §3 归母净利；不等说明分母/分子年度不一致",
                      round(m2, 2), used2, "", []))

    # D4 §17.11 表A R 与表B GG/目标买入价
    # O（注销型回购年均）在 §17.11 头部回显，**R 与 GG 都要加它**：canonical
    # `turtle_core.decision.penetration_return` 的公式是
    # `[基准值 × M × (1−Q) + O] / 市值`，且「R 与 GG 共用本函数，区别只在 base
    # （R 传 C、GG 传 AA）」。旧实现调用 `r_rate/gg_rate` 时**从不传 o** ⇒ 只要产物
    # 带了 `o.json`（O≠0），就会把**正确的包**判成 MISMATCH —— 是审计脚本自己偏离
    # canonical 口径，不是包错（06078 实测：O=85.27 ⇒ 表A R=1.80%、表B GG=1.81%）。
    # ⚠️ 单位必须与 `cap_pack`（**百万**）一致 ⇒ 直接传 `_o_mm`，**不要** ×1e6。
    # 我第一次写成 ×1e6，把 GG 算成 1,683,587.57%（差 1e6 倍），当场被自己的
    # check_derivation 抓住 —— 这正是"独立第二实现"该有的样子。
    _o_mm = _o_from_header(pv)
    _o_val = _o_mm
    c = pv.kv_line("17.2", "C 归母净利润")
    m_rec = pv.bullet_number("17.10", "**M_rec")
    aa_2y = pv.bullet_number("17.5", "AA_2y")
    if c is not None and cap_pack:
        r_expect = r_rate(c, m_rec or 0.0, q, cap_pack, o=_o_val)
        r_used = _grid_cell(pv, "17.11", prefix="表A", row_contains="★", col="default")
        if r_used is not None and m_rec:
            st = MATCH if _close(r_used, r_expect, max(0.02, abs(r_expect) * 0.02)) else MISMATCH
            out.append(_f("D4a", "§17.11 表A 粗算 R（★行 × 默认Q）", st,
                          None if st == MATCH else CRITICAL,
                          "R 必须等于 C×M×(1−Q)/市值；不等说明基准值、支付率、税率或市值不同源",
                          round(r_expect, 2), r_used,
                          f"C={c}, M_rec={m_rec}, Q={q}, 市值={cap_pack}", []))
    if aa_2y is not None and m_rec is not None and cap_pack:
        gg_expect = gg_rate(aa_2y, m_rec, q, cap_pack, o=_o_val)
        gg_used = _grid_cell(pv, "17.11", prefix="表B", row_contains="AA_2y×", col="default")
        if gg_used is not None:
            st = MATCH if _close(gg_used, gg_expect, max(0.02, abs(gg_expect) * 0.02)) else MISMATCH
            out.append(_f("D4b", "§17.11 表B 精算 GG（AA_2y × ★行 × 默认Q）", st,
                          None if st == MATCH else CRITICAL,
                          "GG 必须等于 AA×M×(1−Q)/市值",
                          round(gg_expect, 2), gg_used,
                          f"AA_2y={aa_2y}, M_rec={m_rec}, Q={q}, O={_o_mm}, 市值={cap_pack}", []))
        # 目标买入价 = 现价 × GG / II
        ii_used = pv.kv_line("17.2", "II（门槛值）")
        tbp_used = _grid_last_price(pv, "17.11", row_contains="AA_2y×")
        if tbp_used is not None and ii_used and price:
            tbp_expect = tbp_of(price, gg_expect, ii_used)
            st = MATCH if _close(tbp_used, tbp_expect, max(0.02, abs(tbp_expect) * 0.02)) else MISMATCH
            out.append(_f("D4c", "§17.11 目标买入价（= 现价×GG/II）", st,
                          None if st == MATCH else MAJOR,
                          "目标买入价必须等于 当前股价 × GG / II",
                          round(tbp_expect, 2), tbp_used,
                          f"现价={price}, GG={gg_expect:.2f}%, II={ii_used}", []))

    # D5 §17.14 k 网格
    m_pess = pv.bullet_number("17.10", "M_pess")
    if aa_2y is not None and m_pess is not None and cap_pack:
        bad = []
        for row in pv.grid_rows("17.14"):
            if len(row) < 5:
                continue
            k = _cell(row[0])
            gg_used = _pct(row[2])
            if k is None or gg_used is None:
                continue
            gg_expect = gg_rate(aa_2y * k, m_pess, q, cap_pack, o=_o_val)
            if not _close(gg_used, gg_expect, max(0.02, abs(gg_expect) * 0.02)):
                bad.append(f"k={k}: 包={gg_used} vs 重算={gg_expect:.2f}")
        st = MISMATCH if bad else MATCH
        out.append(_f("D5", "§17.14 悲观档 GG（AA_2y×k×M_pess）", st,
                      MAJOR if bad else None,
                      "各 k 档的 GG 必须等于 AA_2y×k×M_pess×(1−Q)/市值",
                      None, None, f"AA_2y={aa_2y}, M_pess={m_pess}, Q={q}, O={_o_mm}", bad))

    # D6 §17.12 G 网格 H/OE
    d_used = pv.kv_line("17.2", "D 折旧与摊销")
    if c is not None and d_used:
        bad = []
        for row in pv.grid_rows("17.12"):
            if len(row) < 3:
                continue
            g = _cell(row[0])
            h_used, oe_used = _cell(row[1]), _cell(row[2])
            if g is None or h_used is None or oe_used is None:
                continue
            h_expect = d_used * g
            oe_expect = c + d_used - h_expect
            if not _close(h_used, h_expect, max(0.05, abs(h_expect) * 0.005)):
                bad.append(f"G={g}: H 包={h_used} vs 重算={h_expect:.2f}")
            if not _close(oe_used, oe_expect, max(0.05, abs(oe_expect) * 0.005)):
                bad.append(f"G={g}: OE 包={oe_used} vs 重算={oe_expect:.2f}")
        st = MISMATCH if bad else MATCH
        out.append(_f("D6", "§17.12 H = D×G、OE = C + D − H", st, MAJOR if bad else None,
                      "G 网格两列必须由 §17.2 的 C 与 D 复现", None, None,
                      f"C={c}, D={d_used}", bad))

    # D7 §17.8 EV/EBITDA/FCF收益率/PB
    fy = pv.latest_fy()
    oper = pv.series("3", ["经营溢利", "营业利润"])
    finexp = pv.series("3", ["融资成本", "财务费用"])
    da_latest = pv.value_at("5", ["折旧与摊销", "折旧及摊销"], fy)
    cash = pv.value_at("4", ["现金及等价物", "货币资金", "现金及现金等价物"], fy)
    equity = pv.value_at("4", ["股东权益", "归母权益", "归母所有者权益"], fy)
    ocf_latest = pv.value_at("5", ["经营业务现金净额", "经营活动现金流",
                                   "经营活动产生的现金流量净额", "OCF"], fy)
    capex_latest = pv.value_at("5", ["资本开支", "资本支出", "购建固定资产"], fy)
    if cap_pack and cash is not None:
        ibd = pv.value_at("4", ["短期贷款", "短期借款"], fy, missing_as_zero=True) or 0.0
        ibd += pv.value_at("4", ["长期贷款", "长期借款"], fy, missing_as_zero=True) or 0.0
        ibd += pv.value_at("4", ["应付债券"], fy, missing_as_zero=True) or 0.0
        ev_expect = cap_pack + ibd - cash
        # 有息负债取 **§17.1 已声明的锚点**，不在这里另立一套科目组合：
        # 本检查的职责是"§17.8 是否用了与 §17.1 同一个有息负债"，
        # 而"科目组合是否完整"是 field_coverage C2 的职责。混在一起会制造假阳性
        # （600941 实测：包内含租赁/一年内到期等项目，自建组合必然对不上）。
        ibd_anchor = pv.kv_line("17.1", "有息负债")
        _miss = _missing({"总市值（§17.8）": cap_pack, "货币资金（§4）": cash,
                          "§17.1 有息负债": ibd_anchor})
        if _miss:
            out.append(_unverifiable("D7a", "§17.8 企业价值 EV = 市值+有息负债−货币资金", _miss))
        if ibd_anchor is not None:
            ibd = ibd_anchor
        ev_expect = (cap_pack or 0.0) + ibd - (cash or 0.0)
        ev_used = pv.kv_line("17.8", "企业价值")
        if ev_used is not None and not _miss:
            st = MATCH if _close(ev_used, ev_expect, max(1.0, abs(ev_expect) * 0.01)) else MISMATCH
            out.append(_f("D7a", "§17.8 企业价值 EV = 市值+有息负债−货币资金", st,
                          MAJOR if st == MISMATCH else None,
                          "EV 的三个分量必须与 §1/§4 一致；有息负债漏科目（如应付债券未映射）"
                          "会在此处体现", round(ev_expect, 2), ev_used,
                          f"市值={cap_pack}, 有息负债={ibd}, 现金={cash}", []))
        _miss = _missing({"总市值（§17.8）": cap_pack, "货币资金（§4）": cash})
        if _miss:
            out.append(_unverifiable("D7b", "§17.8 EBITDA = 营业利润+财务费用+D&A", _miss))
        if oper and finexp and da_latest:
            ebitda_expect = (pv.value_at("3", ["经营溢利", "营业利润"], fy) or 0.0) \
                + (pv.value_at("3", ["融资成本", "财务费用"], fy) or 0.0) + da_latest
            ebitda_used = pv.kv_line("17.8", "EBITDA")
            if ebitda_used is not None:
                st = MATCH if _close(ebitda_used, ebitda_expect,
                                     max(1.0, abs(ebitda_expect) * 0.01)) else MISMATCH
                out.append(_f("D7b", "§17.8 EBITDA = 营业利润+财务费用+D&A", st,
                              MAJOR if st == MISMATCH else None,
                              "EBITDA 必须由 §3/§5 复现；若不等，请核对脚本取的行是否是"
                              "报表同一张表、同一个年度（脚本所用的行值与年度见证据）",
                              round(ebitda_expect, 2), ebitda_used, "",
                              [f"§3 营业利润={pv.value_at('3', ['经营溢利', '营业利润'], fy)}，"
                               f"§3 财务费用={pv.value_at('3', ['融资成本', '财务费用'], fy)}，"
                               f"§5 D&A={da_latest}，年度={fy}"]))
        _miss = _missing({"营业利润（§3）": _latest(oper), "财务费用（§3）": _latest(finexp), "D&A（§5）": da_latest})
        if _miss:
            out.append(_unverifiable("D7c", "§17.8 FCF收益率 = (OCF−|capex|)/市值", _miss))
        if ocf_latest is not None and capex_latest is not None:
            fcf_yield_expect = (ocf_latest - abs(capex_latest)) / cap_pack * 100
            fy_used = pv.kv_line("17.8", "FCF收益率")
            if fy_used is not None:
                st = MATCH if _close(fy_used, fcf_yield_expect,
                                     max(0.05, abs(fcf_yield_expect) * 0.02)) else MISMATCH
                out.append(_f("D7c", "§17.8 FCF收益率 = (OCF−|capex|)/市值", st,
                              MAJOR if st == MISMATCH else None,
                              "FCF 收益率必须由 §5 复现（注意：不是 FCFF 口径）",
                              round(fcf_yield_expect, 2), fy_used, "",
                              [f"OCF={ocf_latest}, capex={abs(capex_latest)}, 市值={cap_pack}, 年度={fy}"]))
        _miss = _missing({"经营现金流（§5）": ocf_latest, "资本开支（§5）": capex_latest, "总市值": cap_pack})
        if _miss:
            out.append(_unverifiable("D7d", "§17.8 P/B = 市值/归母权益", _miss))
        if equity:
            pb_expect = cap_pack / equity
            pb_used = pv.kv_line("17.8", "P/B")
            if pb_used is not None:
                st = MATCH if _close(pb_used, pb_expect, max(0.02, abs(pb_expect) * 0.02)) else MISMATCH
                out.append(_f("D7d", "§17.8 P/B = 市值/归母权益", st,
                              MINOR if st == MISMATCH else None,
                              "P/B 必须由 §1/§4 复现", round(pb_expect, 2), pb_used, "", []))

    # D8 §17.8 综合底价 = ①②③ 算术平均
    anchors = _baseline_anchors(pv)
    floor_used = pv.bullet_number("17.8", "**综合底价")
    if floor_used is None:
        floor_used = _composite_from_bold(pv, "综合底价")
    floor_parts = [v for k, v in anchors.items() if k in ("①", "②", "③")]
    if floor_used is not None and floor_parts:
        expect = sum(floor_parts) / len(floor_parts)
        st = MATCH if _close(floor_used, expect, max(0.02, abs(expect) * 0.02)) else MISMATCH
        out.append(_f("D8", "§17.8 综合底价 = ①②③ 算术平均", st,
                      MAJOR if st == MISMATCH else None,
                      "底价读数只能含底价型锚点（禁止把收益率型锚点跨语义平均进来）",
                      round(expect, 2), floor_used, f"锚点={anchors}", []))

    # D8a §17.8 ①②：每股量必须由 §4 + 隐含股数 + 报表→交易折算复现
    anchors = _baseline_anchors(pv)
    shares_mm = _implied_shares_mm(pv)
    fy = pv.latest_fy()
    cash = pv.value_at("4", ["现金及等价物", "货币资金", "现金及现金等价物"], fy)
    trad = pv.value_at("4", ["交易性金融资产"], fy, missing_as_zero=True)
    ibd_a = pv.kv_line("17.1", "有息负债")
    equity = pv.value_at("4", ["股东权益", "归母权益", "归母所有者权益"], fy)
    fxt = fx or 1.0
    sec4 = pv.section("4")
    _bank_like = any(k in sec4 for k in ("存放中央银行", "吸收存款", "客户贷款"))
    if "①" in anchors:
        miss = _missing({"§17.1 有息负债": ibd_a, "§4 现金": cash,
                         "隐含股数（§1 市值÷价）": shares_mm})
        if miss:
            f = _unverifiable("D8a1", "§17.8 ① 净流动资产/股", miss)
            if _bank_like:
                f.detail += ("；该标的 §4 使用金融企业科目（存放中央银行款项/吸收存款），"
                             "通用标签不适用 —— 且对银行而言「净流动资产」本身不构成清算底价")
            out.append(f)
        else:
            expect = (cash + (trad or 0.0) - ibd_a) / shares_mm / fxt
            st = MATCH if _close(anchors["①"], expect, max(0.02, abs(expect) * 0.03)) else MISMATCH
            out.append(_f("D8a1", "§17.8 ① 净流动资产/股", st, MAJOR if st == MISMATCH else None,
                          "① 必须由 §4（现金+交易性金融资产−有息负债）÷隐含股数÷折算因子复现；"
                          "不等最常见的原因是**每股量未折汇**（P0-2 的形态）",
                          round(expect, 2), anchors["①"],
                          _anchor_currency_note(fx),
                          [f"隐含股数={shares_mm:,.2f} 百万股, 有息负债={ibd_a}, 现金={cash}"]))
    if "②" in anchors and equity is not None and shares_mm:
        expect = equity / shares_mm / fxt
        st = MATCH if _close(anchors["②"], expect, max(0.02, abs(expect) * 0.03)) else MISMATCH
        out.append(_f("D8a2", "§17.8 ② 每股净资产", st, MAJOR if st == MISMATCH else None,
                      "② 必须由 §4 归母权益 ÷隐含股数÷折算因子复现",
                      round(expect, 2), anchors["②"], _anchor_currency_note(fx), []))
    if "③" in anchors:
        out.append(_f("D8b", "§17.8 ③ 十年最低价", UNVERIFIABLE, MINOR,
                      "③ 用的是**周线最低收盘价**，而 §11 只给出十年价格极值（最高/最低，"
                      "口径为价格而非收盘）⇒ 包内无法独立复算。这不等于通过："
                      "取值正确性需年报/行情源确认",
                      None, anchors["③"], "§11 无周线序列", []))
    if "④" in anchors:
        rf14 = pv.scalar("14", ["无风险利率", "收益率", "yield"])
        disc = max((rf14 or 0.0) / 100, 0.03)
        dps_single = _dps_by_year(pv, aggregate="single")
        dps_sum = _dps_by_year(pv, aggregate="sum")
        def _last3_mean(table):
            if not table:
                return None
            ys = sorted(table, reverse=True)[:3]
            return sum(table[y] for y in ys) / len(ys)
        ev_mean, fy_mean = _last3_mean(dps_single), _last3_mean(dps_sum)
        cands = {}
        events = _dps_events(pv, 3)
        if events:
            # 代码口径：最近 3 次分红**事件**（按 §6 行序）
            cands["最近3次分红事件（行序）"] = (sum(events) / len(events)) / disc
        if ev_mean is not None:
            cands["年度单一值口径的最近3年"] = ev_mean / disc
        if fy_mean is not None:
            cands["最近3个财年（年度求和）"] = fy_mean / disc
        if not cands:
            out.append(_unverifiable("D8c", "§17.8 ④ 股息隐含价",
                                     ["§6 每股股息", "§14 无风险利率"],
                                     "④ = 3年均DPS ÷ max(Rf,3%)"))
        else:
            hit = [n for n, v in cands.items() if _close(anchors["④"], v, max(0.05, abs(v) * 0.02))]
            vals = list(cands.values())
            spread = (max(vals) - min(vals)) / max(abs(max(vals)), 1e-9) if len(vals) > 1 else 0.0
            if hit and len(cands) > 1 and spread > 0.20:
                # 两种 DPS 口径差 >20%：数值只在"事件"口径下可复现，而文档说的是
                # "3年均DPS"（财年口径）。这不是通过 —— 必须由裁定者确认该用哪一个。
                out.append(_f("D8c", "§17.8 ④ 股息隐含价：DPS 年度口径冲突", MISMATCH, MAJOR,
                              "④ 只在「最近 3 次**分红事件**均值」口径下可复现，而文档要求的是"
                              "「3 个**财年**均 DPS」。含中期分红的标的（银行/电信）在此相差很大："
                              "事件口径会系统性**低估** ④ 与参考公允价",
                              None, anchors["④"], f"折扣率=max(Rf,3%)={disc:.4f}",
                              [f"{n} = {v:.2f}" for n, v in cands.items()]
                              + [f"命中：{'、'.join(hit)}；两口径相对差 {spread:.1%}"]))
            elif hit:
                note = ("命中口径：" + "、".join(hit))
                out.append(_f("D8c", "§17.8 ④ 股息隐含价", MATCH, None, note,
                              None, anchors["④"], f"折扣率=max(Rf,3%)={disc:.4f}",
                              [f"{n} = {v:.2f}" for n, v in cands.items()]))
            else:
                out.append(_f("D8c", "§17.8 ④ 股息隐含价", MISMATCH, MAJOR,
                              "④ 与两种 DPS 口径都对不上（或未按 max(Rf,3%) 折现）",
                              None, anchors["④"], f"折扣率={disc:.4f}",
                              [f"{n} = {v:.2f}" for n, v in cands.items()]))
    if "⑤" in anchors:
        rf14 = pv.scalar("14", ["无风险利率", "收益率", "yield"])
        fcf_series = {}
        ocf5 = pv.series("5", ["经营业务现金净额", "经营活动现金流",
                               "经营活动产生的现金流量净额", "OCF"])
        capex5 = pv.series("5", ["资本开支", "资本支出", "购建固定资产"])
        for y in set(ocf5) & set(capex5):
            fcf_series[y] = ocf5[y] - abs(capex5[y])
        if not fcf_series or not rf14 or not shares_mm:
            out.append(_unverifiable("D8d", "§17.8 ⑤ 悲观FCF资本化",
                                     [f"§5 FCF 序列({len(fcf_series)} 年)", "§14 Rf", "隐含股数"],
                                     "⑤ = min(5年FCF)÷Rf÷总股本"))
        elif min(fcf_series.values()) <= 0:
            out.append(_f("D8d", "§17.8 ⑤ 悲观FCF资本化", MATCH, None,
                          "存在负 FCF 年份 ⇒ 按文档跳过该锚点（包内亦未给出 ⑤）",
                          None, anchors.get("⑤"), f"min FCF={min(fcf_series.values()):,.2f}", []))
        else:
            expect = min(fcf_series.values()) / (rf14 / 100) / shares_mm / fxt
            st = MATCH if _close(anchors["⑤"], expect, max(0.05, abs(expect) * 0.02)) else MISMATCH
            out.append(_f("D8d", "§17.8 ⑤ 悲观FCF资本化", st, MAJOR if st == MISMATCH else None,
                          "⑤ 必须由 min(5年FCF) ÷ Rf ÷ 隐含股数 ÷ 折算因子复现",
                          round(expect, 2), anchors.get("⑤"), _anchor_currency_note(fx), []))

    # D7e §1/§2 的市值 vs §17.8 的市值：股本改用年报申报值后，港股会**故意**不同
    # （§1 渲染得更早、拿不到申报股本）。必须让它可见：有申报股本 ⇒ 披露（已解释）；
    # 没有 ⇒ 判不一致（两个市值无法解释）。
    cap_17 = pv.kv_line("17.8", "总市值")
    cap_12 = _market_cap_mm(pv)
    # §1/§2 的市值是**交易币种**，§17.8 的是**报表币种** ⇒ 必须先折汇再比。
    # （06862：53,343.18 百万港元 × 0.85849 = 45,794.59 = §17.8；不折汇会凭空报出 14.15% 的假差异。）
    fxd = _load_fx(pv)
    if cap_12 and fxd and abs(fxd - 1.0) > 1e-12:
        cap_12_rep = cap_12 * fxd
    else:
        cap_12_rep = cap_12
    if cap_17 is not None and cap_12_rep:
        cap_12 = cap_12_rep
        gap = abs(cap_17 - cap_12) / cap_12
        if gap > 0.005:
            declared, trusted = _declared_shares_mm(pv)
            if trusted:
                out.append(_f("D7e", "§1/§2 市值 vs §17.8 市值", UNVERIFIABLE, MINOR,
                              f"两处市值差 {gap:.2%}（§1/§2 折报表币种后 {cap_12:,.2f} "
                              f"vs §17.8 {cap_17:,.2f}）。"
                              "已声明年报股本 ⇒ §17.8 按「申报股本×价」重算，而 §1 在申报股本"
                              "可用之前就已渲染（上游市值内嵌另一个股本口径）。**属预期**，"
                              "但报告引用市值时必须指明用的是哪一个", cap_12, cap_17,
                              "known_issues.md #25", []))
            else:
                out.append(_f("D7e", "§1/§2 市值 vs §17.8 市值", MISMATCH, CRITICAL,
                              "同一份包内出现两个市值且无法用股本口径解释 ⇒ 所有以市值为"
                              "分母的比率（PE/PB/收益率/R/GG）都可疑",
                              cap_12, cap_17, "", []))
        else:
            out.append(_f("D7e", "§1/§2 市值 vs §17.8 市值", MATCH, None,
                          f"两处一致（差 {gap:.2%}）", cap_12, cap_17, "", []))

    # D11 §17.6 分位：包内**不可独立复算**（§11 只有价格极值、没有周线序列），
    # 但有三件能机械判的事：当前价必须与 §2 同源、分位必须落在 [0,100]、
    # 分位价格必须随分位单调不减。抓的是"行错位/上下颠倒/取错价格"这类渲染事故。
    out.extend(_check_price_percentiles(pv))

    # D9 λ 外推（§17.13）：结余(k) = AA + λ×(k−1)×S，GG(k) = 结余(k)×M×(1−Q)/市值
    out.extend(_check_lambda_sensitivity(pv, ts_code, q))

    # D10 跨表恒等：§17.9 门槛价格 应等于 §17.11/§17.14 同 k 的目标买入价
    out.extend(_check_cross_table_price(pv, price, fx))

    return out


def _check_lambda_sensitivity(pv: PackView, ts_code: str, q: float) -> List[Finding]:
    """§17.13：λ 外推的三个恒等式（只在 §17.13 存在且 λ 可解析时检查）。

    抓的是"λ 被改过/方向写反/缩放系数错"——这类错误会让整张敏感性表看起来合理却全错。
    """
    body = pv.section("17.13")
    if not body:
        return []
    lam = None
    m = re.search(r"λ\s*=\s*\**([-\d.]+)", body)
    if m:
        lam = float(m.group(1))
    aa = pv.bullet_number("17.13", "AA_default")
    if aa is None:
        mm = re.search(r"AA_default\s*=\s*\**([-\d,]+)", body)
        aa = _cell(mm.group(1)) if mm else None
    s_rev = None
    ms = re.search(r"S 最新年收入\s*=\s*\**([-\d,]+)", body)
    if ms:
        s_rev = _cell(ms.group(1))
    m_rec = pv.bullet_number("17.10", "**M_rec")
    cap = pv.kv_line("17.8", "总市值")
    ii = pv.kv_line("17.2", "II（门槛值）") or pv.kv_line("17.13", "II")
    miss = _missing({"λ": lam, "AA_default": aa, "S 最新年收入": s_rev,
                     "M_rec": m_rec, "市值": cap})
    if miss:
        return [_unverifiable("D9λ", "§17.13 λ 外推恒等式", miss,
                              "结余(k)=AA+λ(k−1)S；GG(k)=结余(k)·M(1−Q)/市值")]
    rows = [r for r in pv.grid_rows("17.13") if r and re.match(r"^[\d.]+×", str(r[0]).strip())]
    bad = []
    for r in rows:
        k = _cell(str(r[0]).replace("×", ""))
        surplus_used, gg_used = _cell(r[1]), _cell(r[2])
        if k is None or surplus_used is None or gg_used is None:
            continue
        surplus_expect = aa + lam * (k - 1) * s_rev
        gg_expect = surplus_expect * m_rec / 100 * (1 - q / 100) / cap * 100
        if not _close(surplus_used, surplus_expect, max(1.0, abs(surplus_expect) * 0.01)):
            bad.append(f"{k}×: 结余 包={surplus_used} vs 重算={surplus_expect:.2f}")
        if not _close(gg_used, gg_expect, max(0.02, abs(gg_expect) * 0.03)):
            bad.append(f"{k}×: GG 包={gg_used} vs 重算={gg_expect:.2f}")
    if not rows:
        return [_unverifiable("D9λ", "§17.13 λ 外推恒等式", ["情景表行"], "未解析到 0.9× 这类行")]
    return [_f("D9λ", "§17.13 λ 外推恒等式", MISMATCH if bad else MATCH,
               MAJOR if bad else None,
               "情景表的结余与回报率必须由 AA/λ/S/M/市值 复现；不等说明 λ、缩放或口径被改过",
               None, None, f"λ={lam}, AA={aa}, S={s_rev}, M_rec={m_rec}, Q={q}", bad)]


def _scenario_tp(row: Sequence[str]) -> Optional[float]:
    """§17.9 的门槛价格列：表1 有 6 列、表2 有 5 列，且相邻列分别带 %/pct 后缀。

    取「第 3 列起、第一个**不带** %/pct 的裸数」—— 对两表都成立。
    """
    for cell in list(row)[2:]:
        raw = str(cell)
        if "%" in raw or "pct" in raw:
            continue
        v = _cell(raw)
        if v is not None:
            return v
    return None


def _explain_ratio_179(pv: PackView, ratio: Optional[float],
                       fx: Optional[float]) -> str:
    """把 §17.9/§17.14 的比值翻译成可复核的成因假设（比值 = fx × M9/M_pess 的组合）。

    单看比值无法区分"币种错"与"用了错的 M"——两者会相乘。这里把两种候选都算出来，
    让裁定的子代理有明确的可证伪假设，而不是让脚本替它下结论。
    """
    if not ratio:
        return ""
    m9 = pv.kv_line("17.2", "M（支付率3年均值）")
    mp = pv.bullet_number("17.10", "M_pess")
    cands = []
    if fx:
        cands.append(("纯币种错（报表币种当交易币种渲染）", fx))
        if m9 and mp:
            cands.append(("币种错 + 用了 §17.1 均值而非 M_pess", fx * m9 / mp))
    if m9 and mp and not fx:
        cands.append(("仅 M 口径不同（用了 §17.1 均值）", m9 / mp))
    hits = [name for name, val in cands if _close(ratio, val, 0.02)]
    shown = "；".join(f"{n} ⇒ 预期比值 {v:.4f}" for n, v in cands)
    return (f"实测比值 {ratio:.4f}；候选成因：{shown}。"
            + (f" 命中：{'、'.join(hits)}。" if hits else " 均未命中 ⇒ 需人工查证。"))


def _cap_identity_holds(pv: PackView, price: Optional[float],
                        fx: Optional[float]) -> tuple[Optional[bool], str]:
    """`市值 == 现价 × 股数 × fx` 是否成立 —— 即 §17.9 与 §17.11/§17.14 的**分母是否同源**。

    返回 ``(True/False/None, detail)``；``None`` = 算不了（不得当作成立）。

    **实测归因（2026-10-14，00696.HK）**：差值精确等于**价格变动**，不是股本算错 ——

        市值隐含价 = 市值/(股数×fx) = 22,533.63/(3,228.18×0.85753) = **8.1400**
        §2 当日价                                                  = **8.98**
        8.98/8.14 = **1.1032** ⇒ 恰好是观测到的 +10.32% 偏差

    ⇒ HK 的市值取 `hk_fina_indicator` 的**期末**口径，而现价是**当日**值；
      §17.9 以**股数**为分母、§17.11/§17.14 以**市值**为分母，
      于是**价格一动，两者必然不同源**。

    ⚠️ 因此**不能**把这条恒等式做成包级不变式：它会在几乎所有港股包上误报
    （曾试作 I11，核算后撤回 —— 判据把"价格日期不同"误读成"缺陷"）。
    这里只把它用作 X1/X2 的**前提判定**：前提不成立 ⇒ 该恒等式不适用，判 UNVERIFIABLE。
    """
    cap_mm = pv.kv_line("17.8", "总市值")
    shares_mm = _implied_shares_mm(pv)
    if not cap_mm or not shares_mm or not price:
        return None, f"不可算（市值={cap_mm}, 股数={shares_mm}, 现价={price}）"
    implied = price * shares_mm * (fx or 1.0)
    gap = abs(implied - cap_mm) / cap_mm
    implied_price = cap_mm / (shares_mm * (fx or 1.0))
    detail = (f"市值={cap_mm:,.2f} vs 现价×股数×fx={implied:,.2f}（差 {gap*100:+.2f}%）；"
              f"市值隐含价={implied_price:.4f} vs §2 现价={price}"
              f"（比 {price/implied_price:.4f}）⇒ 差额来自**价格日期不同**"
              if gap > 0.005 else
              f"市值={cap_mm:,.2f} vs 现价×股数×fx={implied:,.2f}（差 {gap*100:+.2f}%）")
    return gap <= 0.005, detail


def _declared_m_179(pv: PackView) -> tuple[Optional[float], str]:
    """§17.9 正文**自己声明**的 M 值与来源标签（形如 ``M = 23.29%（法2）``）。"""
    body = pv.section("17.9")
    m = re.search(r"M\s*=\s*([\d,]+(?:\.\d+)?)\s*[%％]\s*[（(]([^）)]*)", body)
    if not m:
        return None, "§17.9 正文未声明 M 值"
    try:
        return float(m.group(1).replace(",", "")), m.group(2).strip()
    except ValueError:
        return None, "§17.9 声明的 M 值不可解析"


def _check_cross_table_price(pv: PackView, price: Optional[float],
                             fx: Optional[float]) -> List[Finding]:
    """X 组：§17.9「门槛价格」与 §17.11/§17.14「目标买入价」的跨表一致性。

    旧版**无条件断言两格相等**，其前提是
    `M_rec == M_pess` **且** `市值 == 现价×股数×fx`（D-10）。
    这两个前提在真实产物里**经常不成立**（`M_pess = min(m1,m2,m3)`；股本降级），
    于是旧版在**正确**的产物上报 critical 并阻断交付。

    现改为**前提守卫**：前提不成立时判 ``UNVERIFIABLE`` 并点名缺哪个前提
    （该前提本身由下面两条新检查负责），成立时才断言相等。

    - **X1** §17.9(表2) vs §17.14 —— 前提：`M_pess == M_rec`
    - **X2** §17.9(表1 基准) vs §17.11 AA_2y —— 前提：市值恒等式（两侧都用 M_rec）
    - **X3**（新增）§17.9 正文声明的 M vs §17.10 的 ``M_rec`` ——
      包内相邻两行自相矛盾（实测 06862：声明 89.70%「§17.1 3年均值」却写
      「与 §17.11 表B 一致（同一 M_rec）」，而 M_rec = 81.11%）
    """
    out: List[Finding] = []

    # --- X3：§17.9 声明的 M 必须就是 M_rec ---
    declared_m, m_src = _declared_m_179(pv)
    m_rec = pv.bullet_number("17.10", "M_rec")
    if declared_m is not None and m_rec:
        if _close(declared_m, m_rec, max(0.05, abs(m_rec) * 0.005)):
            out.append(_f("X3", "§17.9 声明口径：M == M_rec", MATCH, None,
                          "§17.9 与 §17.11/§17.14 用同一 M_rec（正文声称可直接比较）",
                          m_rec, declared_m, f"来源={m_src}", []))
        else:
            out.append(_f("X3", "§17.9 声明口径：M == M_rec", MISMATCH, CRITICAL,
                          f"§17.9 声明 M={declared_m}%（{m_src}），而 §17.10 的 M_rec={m_rec}%。"
                          "正文却声称『与 §17.11 表B 一致（同一 M_rec）、两表可直接比较』"
                          "⇒ 包内相邻两行自相矛盾，§17.9 的穿透回报率/门槛价格整套偏离。",
                          m_rec, declared_m, f"来源={m_src}", [m_src]))

    rows914 = {}
    for row in pv.grid_rows("17.14"):
        if len(row) >= 5:
            k = _cell(row[0])
            tp = _cell(row[4])
            if k is not None and tp is not None:
                rows914[round(k, 2)] = tp
    if not rows914:
        return out

    m_pess = pv.bullet_number("17.10", "M_pess")
    # 前提三态：True 成立 / False **已证明**不成立 / None **算不了**。
    # 纪律：只有**证明**前提不成立时才降级为 UNVERIFIABLE；算不了不等于不成立，
    # 此时仍按包的自身声明（「两表数值可直接比较」）断言相等 —— 否则会静默放过真缺陷。
    if m_rec is None or m_pess is None:
        m_premise: Optional[bool] = None
    else:
        m_premise = _close(m_rec, m_pess, max(0.05, abs(m_rec) * 0.005))
    cap_ok, cap_detail = _cap_identity_holds(pv, price, fx)

    def _emit(code: str, title: str, tp14: float, tp9: float,
              *, need_m: bool) -> None:
        if need_m and m_premise is False:
            out.append(_f(code, title, UNVERIFIABLE, None,
                          f"前提**已证明**不成立：§17.9 用 M_rec={m_rec} 而 §17.14 用 "
                          f"M_pess={m_pess} ⇒ 两格**本就不该相等**（M 口径不同，findings D-1）。"
                          " 该恒等式在此不适用，须人工按 M 比值复核。",
                          tp14, tp9, f"M_rec={m_rec}, M_pess={m_pess}", []))
            return
        if cap_ok is False:
            out.append(_f(code, title, UNVERIFIABLE, None,
                          f"前提**已证明**不成立：市值恒等式 {cap_detail}"
                          " ⇒ §17.9 用**股数**、§17.11/§17.14 用**市值**，分母不同源"
                          "（findings D-10 的形态）。⇒ 该恒等式在价格已变动时不适用，须人工按两节各自的"
                          " 口径复核；不作为阻断。",
                          tp14, tp9, cap_detail, [cap_detail]))
            return
        if _close(tp9, tp14, max(0.02, abs(tp14) * 0.02)):
            out.append(_f(code, title, MATCH, None,
                          "两格相等" + (f"（前提：{cap_detail}）" if cap_ok is True else ""),
                          tp14, tp9, cap_detail, []))
        else:
            hint = _explain_ratio_179(pv, (tp9 / tp14) if tp14 else None, fx)
            premise = "前提成立" if cap_ok is True else "前提无法计算（按包的自身声明断言）"
            out.append(_f(code, title, MISMATCH, CRITICAL,
                          f"{premise}却两格不等 ⇒ 其中一处算错（币种或 M 口径）。"
                          + (f" {hint}" if hint else ""),
                          tp14, tp9, cap_detail, [hint] if hint else []))

    # §17.9 表2 的 −20% 行 ≡ k=0.80；表2 −30% ≡ k=0.70
    for label, k in (("-20%", 0.80), ("-30%", 0.70)):
        tp9 = None
        for row in pv.grid_rows("17.9"):
            if row and row[0].strip().startswith(label):
                tp9 = _scenario_tp(row)
                break
        tp14 = rows914.get(k)
        if tp9 is None or tp14 is None:
            continue
        _emit("X1", f"跨表恒等：§17.9({label}) 门槛价格 = §17.14(k={k}) 目标买入价",
              tp14, tp9, need_m=True)

    # §17.9 表1 基准 ≡ §17.11 AA_2y 目标买入价（两侧都用 M_rec ⇒ 无 M 前提）
    tp9_base = None
    for row in pv.grid_rows("17.9"):
        if row and row[0].strip() == "基准":
            tp9_base = _scenario_tp(row)
            break
    tp11 = _grid_last_price(pv, "17.11", row_contains="AA_2y×")
    if tp9_base is not None and tp11 is not None:
        _emit("X2", "跨表恒等：§17.9 基准 门槛价格 = §17.11 AA_2y 目标买入价",
              tp11, tp9_base, need_m=False)
    return out


# ---------------------------------------------------------------------------
# 取值辅助
# ---------------------------------------------------------------------------
def _missing(inputs: Dict[str, object]) -> List[str]:
    """Names of inputs that could not be resolved.

    为什么这条最重要：审核脚本**不能**在输入不全时凑出一个数再去"对账"——
    那会制造假阳性，而假阳性与真错误同样有害（会让 --allow-degraded 变成常态，
    门禁随即失效）。输入不全 → UNVERIFIABLE + 点名缺哪个，交由子代理去查。
    """
    return [k for k, v in inputs.items() if v is None]


def _unverifiable(code: str, title: str, missing: Sequence[str],
                  basis: str = "") -> Finding:
    return _f(code, title, UNVERIFIABLE, MAJOR,
              "输入无法从包内解析，本项**不可判定**（不是通过）：缺 " + "、".join(missing)
              + "。子代理需读年报/上游确认这些值是否存在、科目是否正确",
              None, None, basis, [f"缺：{m}" for m in missing])


def _safe_div(a: Optional[float], b: Optional[float], scale: float) -> Optional[float]:
    if a is None or b in (None, 0):
        return None
    return a / b * scale


def _mean_first3(vals: Sequence[Optional[float]]) -> Optional[float]:
    """**前 3 个非空值**的均值（保留：调用点语义各不相同）。

    ⚠️ §17.10 的三年均值**不要**用它 —— 那里的窗口是「最近 3 个**财年**」，用「前 3 个非空值」
    会在中间年份缺值时**跨过更多财年**取样（06078 实测：窗口 {2025,2024,2023} 因 2024 缺值
    被取成 {2025,2023,2021}，均值由 5.10 变成 10.69）。§17.10 请改用 `_window_mean3`。
    """
    got = [v for v in vals if v is not None]
    return sum(got[:3]) / len(got[:3]) if got[:3] else None


def _window_mean3(window: Sequence[str],
                  by_year: Dict[str, Optional[float]],
                  covered: Optional[set] = None) -> Optional[float]:
    """「最近 3 个**财年**」窗口均值：窗口内**有证据的零**按 0 计入，**不剔除**。

    - `window`：财年，近 → 远；只取前 3 个。
    - `by_year`：该口径的逐年值。
    - `covered`：**该财年在报表里有行**的年份集合（通常来自 §3 年度轴）。只有"报表覆盖了这一年"
      才能把"没有值"读作**有证据的零**；否则是未知，必须剔除而不是补 0。

    为什么必须有这个函数（2026-10-16，06078）：公司连续停派，窗口内 FY2024/FY2025 无分配。
    "前 3 个非空值"把两个真零当缺失剔除 ⇒ 法2 由 5.10 抬到 7.64%；法3 更因窗口内只剩一个点
    而**根本没做除法**，由 4.53 抬到 13.58% —— 同一个洞把 M_pess 抬高约 68%，
    直接放大 §17.14 的买点。
    """
    win = list(window)[:3]
    if not win:
        return None
    tot, n = 0.0, 0
    for y in win:
        if y in by_year and by_year[y] is not None:
            tot += by_year[y]
            n += 1
        elif covered is not None and y in covered:
            n += 1          # 有证据的零：计入分母、分子为 0
    return tot / n if n else None


def _latest(series: Dict[str, float]) -> Optional[float]:
    if not series:
        return None
    return series[sorted(series, reverse=True)[0]]


def _aa_table(pv: PackView) -> Dict[str, Dict[str, float]]:
    """§17.5 的表是**转置**的（年份在行、科目在列），与 §3/§4/§5 相反。

    返回 ``{year: {"ocf","capex","deduct","aa"}}``。用它的三个理由：
    ① 可以对 §17.5 **自己的算术**做勾稽（AA == OCF − |capex| − 扣减项）；
    ② 可以把它的 OCF / capex 与 §5 对账（提取一致性）；
    ③ 不读这列扣减项会把「AA 少了 N」误报成不一致（首版的假阳性即此）。
    """
    out: Dict[str, Dict[str, float]] = {}
    for header, rows in _tables(pv.section("17.5")):
        if not any("OCF" in c or "经营现金流" in c for c in header):
            continue
        idx = {}
        for i, c in enumerate(header):
            cs = c.strip()
            # 首版把『= AA（OCF 锚定）』也当成 OCF 列（它含 "OCF"），于是拿 AA 去和 §5 的
            # OCF 对账 ⇒ 假阳性。判定必须互斥且"首个匹配即定"。
            if "AA" in cs:
                idx.setdefault("aa", i)
            elif "OCF" in cs or "经营现金流" in cs:
                idx.setdefault("ocf", i)
            elif "资本开支" in cs:
                idx.setdefault("capex", i)
            elif "扣减项" in cs:
                idx.setdefault("deduct", i)
        for r in rows:
            if not r or not _YEAR_RE.match(str(r[0]).strip()):
                continue
            year = str(r[0]).strip()
            rec = {}
            for key, i in idx.items():
                if i < len(r):
                    v = _cell(abs(_cell(r[i])) if key == "capex" and _cell(r[i]) is not None else r[i])
                    if v is not None:
                        rec[key] = abs(v) if key == "capex" else v
            if rec:
                out[year] = rec
        if out:
            break
    return out


def _deduction_series(pv: PackView) -> Dict[str, float]:
    """§17.5 的「- 文档扣减项①」列（收购附属公司＋偿还融资租赁＋购买少数股东权益＋已付利息(融资)）。

    为什么必须读它：不读就会把「AA 少了 1,073.86」误报成不一致，制造假阳性 ——
    审计脚本的假阳性与真错误同样有害（会让 --allow-degraded 变成常态）。
    """
    years, rows = pv.annual_table("17.5")
    if not years:
        return {}
    row = _row_of(rows, ["文档扣减项"])
    if row is None:
        return {}
    header = pv._header_of("17.5")
    out: Dict[str, float] = {}
    for i, cell in enumerate(row):
        if i == 0 or i >= len(header):
            continue
        label = header[i].strip()
        if not _YEAR_RE.match(label):
            continue
        v = _cell(cell)
        if v is not None:
            out[label] = v
    return out


def _dps_events(pv: PackView, limit: int = 3) -> List[float]:
    """§6 里**按行序**取前 N 个每股股息（= 代码 `sort_values(end_date).head(3)` 的口径）。

    包内只有年份没有具体日期，同年内多行的相对顺序未必与代码的 `end_date` 排序一致 ——
    这一点必须写进 basis，不能假装精确。
    """
    out: List[float] = []
    for _h, rows in _tables(pv.section("6")):
        for r in rows:
            if len(r) < 2 or not _YEAR_RE.match(str(r[0]).strip()[:4]):
                continue
            v = _cell(r[1])
            if v is not None:
                out.append(v)
        if out:
            break
    return out[:limit]


def _dps_by_year(pv: PackView, aggregate: str = "single") -> Dict[str, float]:
    """§6 每股股息的年度口径。

    A 股一年可多次分红（中期＋末期），港股 `dps_hkd` 则是"按公告快照"的年度单一值。
    两种口径**都可能是对的**，所以两个都提供：
      - ``aggregate="sum"``    → 该年度所有分红之和（A 股多次分红口径）
      - ``aggregate="single"`` → 仅当该年度只有一个不同取值时才取（港股快照口径）
    首版只做 single，于是 A 股包的法1 被静默降成 2 年均值 ⇒ 假阳性。
    """
    out: Dict[str, float] = {}
    for _h, rows in _tables(pv.section("6")):
        seen: Dict[str, List[float]] = {}
        for r in rows:
            if len(r) < 2:
                continue
            y = str(r[0]).strip()[:4]
            if not _YEAR_RE.match(y):
                continue
            v = _cell(r[1])
            if v is not None:
                seen.setdefault(y, []).append(v)
        for y, vs in seen.items():
            uniq = sorted(set(round(v, 6) for v in vs))
            if aggregate == "sum":
                out[y] = sum(uniq)
            elif len(uniq) == 1:
                out[y] = uniq[0]
        if seen:
            break
    return out


def _grid_mean(pv: PackView, method_label: str) -> Optional[float]:
    """§17.10 表格里某个方法的『3年均值』（最后一列）。"""
    for _h, rows in _tables(pv.section("17.10")):
        row = _row_of(rows, [method_label])
        if row:
            vals = _numeric_cells(row)
            if vals:
                return vals[-1]
    return None


def _default_q_col(header: List[str]) -> Optional[int]:
    for i, c in enumerate(header):
        if "默认" in c:
            return i
    return None


def _grid_cell(pv: PackView, section: str, *, prefix: str, row_contains: str,
               col: str) -> Optional[float]:
    """Read a cell from §17.11 表A/表B: default-Q column of the row containing the marker."""
    for header, rows in _tables(pv.section(section)):
        if prefix == "表A" and not header[0].startswith("支付率"):
            continue
        if prefix == "表B" and not header[0].startswith("AA×M"):
            continue
        idx = _default_q_col(header)
        if idx is None:
            continue
        for row in rows:
            if row and row_contains in row[0]:
                if idx < len(row):
                    return _pct(row[idx])
        break
    return None


def _grid_last_price(pv: PackView, section: str, *, row_contains: str) -> Optional[float]:
    for header, rows in _tables(pv.section(section)):
        if not header[0].startswith("AA×M"):
            continue
        for row in rows:
            if row and row_contains in row[0] and len(row) >= 2:
                return _cell(row[-1])
        break
    return None


_CAP_UNIT_FACTOR = {"万元": 0.01, "万": 0.01, "亿元": 100.0, "亿": 100.0,
                    "百万元": 1.0, "百万": 1.0, "百万港元": 1.0, "百万美元": 1.0,
                    "元": 1e-6, "千元": 1e-3}


def _market_cap_mm(pv: PackView) -> Optional[float]:
    """§1/§2 的总市值，**按行标签里的单位**折算成百万。

    为什么必须读标签：600941/601398 的 §1 写 `总市值 (万元) 215,461,887.56`
    （= 2,154,618.88 百万元），而 §17.8 写 `总市值（百万元）2,154,618.88`。
    把两者都当成百万会得出 100× 的隐含股数，进而把 ①② 全判成不一致 —— 假阳性。
    """
    for sec in ("1", "2"):
        for _h, rows in _tables(pv.section(sec)):
            row = _row_of(rows, ["总市值"])
            if not row:
                continue
            raw = _numeric_cells(row)
            if not raw:
                continue
            label = row[0]
            for unit, factor in _CAP_UNIT_FACTOR.items():
                if f"({unit})" in label or f"（{unit}）" in label:
                    return raw[0] * factor
            return raw[0]          # 无单位标签：按百万处理（§2 的惯例）
    return None


def _declared_shares_mm(pv: PackView) -> Tuple[Optional[float], bool]:
    """§1/§17.8 的申报股本 → ``(shares_mm, trusted)``（解析单点在 `shares_utils`）。"""
    from shares_utils import declared_from_pack
    val, trusted, _raw = declared_from_pack(pv.text)
    return val, trusted


def _implied_shares_mm(pv: PackView) -> Optional[float]:
    """股数（百万股）：**申报值优先**，否则用交易币种市值 ÷ 价格反推。

    为什么要优先申报值：§17.8 的 ①② 应当用年报已发行股数（#25）。若审核仍用反推值，
    它会与 §17.8 对不上，从而把**正确**的包判成不一致（假阳性）；反之若包用了反推值，
    这里也会因为口径不同而报出来 —— 两种情形都需要申报值作为基准。
    """
    declared, _trusted = _declared_shares_mm(pv)
    if declared:
        return declared
    cap = _market_cap_mm(pv)
    price = pv.scalar("2", ["最新价格", "收盘价"]) or pv.scalar("1", ["当前价格"])
    if not cap or not price:
        return None
    return cap / price


def _anchor_currency_note(fx: Optional[float]) -> str:
    return (f"已按 1/{fx:.5f} 由报表币种折算" if fx and abs(fx - 1.0) > 1e-12
            else "报表币种=交易币种，无需折算")



def _check_price_percentiles(pv: PackView) -> List[Finding]:
    """§17.6 分位表的可判部分（known_issues：该表在包内不可独立复算）。"""
    body = pv.section("17.6")
    if not body:
        return []
    rows = pv.grid_rows("17.6")
    kv = {}
    for r in rows:
        if len(r) >= 2 and r[0]:
            kv[r[0].strip()] = (_cell(r[1]), r[1])
    price_176 = kv.get("当前股价", (None, ""))[0]
    pct_176 = kv.get("当前股价历史分位", (None, ""))[0]
    # 标签可能是「50%分位价格（中位数）」⇒ 必须**前缀**匹配，否则 50% 那一档会静默漏掉
    q = {}
    for p in (10, 25, 50, 75, 90):
        hit = next((v for k, (v, _raw) in kv.items() if k.startswith(f"{p}%分位价格")), None)
        q[p] = hit
    price_2 = pv.scalar("2", ["最新价格", "收盘价"]) or pv.scalar("1", ["当前价格"])

    ev, bad = [], []
    if price_176 is not None and price_2 is not None:
        ev.append(f"当前股价 §17.6={price_176} vs §2={price_2}")
        if not _close(price_176, price_2, max(0.01, abs(price_2) * 0.005)):
            bad.append(f"§17.6 当前股价 {price_176} ≠ §2 {price_2}")
    if pct_176 is not None and not (0 <= pct_176 <= 100):
        bad.append(f"分位 {pct_176} 不在 [0,100]")
    got = [(p, v) for p, v in sorted(q.items()) if v is not None]
    ev.append("分位价格：" + "、".join(f"{p}%={v}" for p, v in got) if got else "分位价格缺失")
    for (p1, v1), (p2, v2) in zip(got, got[1:]):
        if v2 < v1 - max(0.01, abs(v1) * 0.005):
            bad.append(f"{p2}%分位价 {v2} < {p1}%分位价 {v1}（分位价格必须单调不减）")
    if not got and price_176 is None:
        return []
    if bad:
        return [_f("D11", "§17.6 分位表可判部分", MISMATCH, MAJOR,
                   "分位表出现行错位/价格不同源/分位越界 ⇒ 该表的读数不可用"
                   "（注意：完整复算需要周线序列，包内没有，故本检查**不能**证明数值正确，"
                   "只能证伪明显错误）", None, None,
                   "§11 只有价格极值 ⇒ §17.6 在包内不可独立复算", bad)]
    return [_f("D11", "§17.6 分位表可判部分", MATCH, None,
               "当前价与 §2 同源、分位在 [0,100]、分位价格单调不减"
               "（完整复算需周线序列，包内不可得）", None, price_176, "", ev)]


def _baseline_anchors(pv: PackView) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for _h, rows in _tables(pv.section("17.8")):
        for row in rows:
            if row and row[0] and row[0][0] in "①②③④⑤":
                v = _cell(row[1]) if len(row) > 1 else None
                if v is not None:
                    out[row[0][0]] = v
    return out


def _composite_from_bold(pv: PackView, label: str) -> Optional[float]:
    body = pv.section("17.8")
    m = re.search(rf"\*\*{re.escape(label)}[^=]*=\s*([\d,]+(?:\.\d+)?)", body)
    return _cell(m.group(1)) if m else None


def _load_fx(pv: PackView) -> Optional[float]:
    """Extract the reporting→trading factor the pack declares (1/fx for per-share items)."""
    body = pv.text
    m = re.search(r"1/(\d\.\d{4,6})", body)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# 编排 / 输出
# ---------------------------------------------------------------------------
CHECKS = ("pack_internal", "sources", "derivation")


def run_all(pack_text: str, ts_code: str = "") -> List[Finding]:
    pv = PackView(pack_text)
    out: List[Finding] = []
    for fn in (check_pack_internal, check_sources):
        try:
            out.extend(fn(pv))
        except Exception as exc:  # noqa: BLE001 — 审核自身故障必须可见
            out.append(_f("NA", f"{fn.__name__} 执行失败", UNVERIFIABLE, MAJOR,
                          f"{type(exc).__name__}: {exc}"))
    try:
        out.extend(check_derivation(pv, ts_code))
    except Exception as exc:  # noqa: BLE001
        out.append(_f("NA", "check_derivation 执行失败", UNVERIFIABLE, MAJOR,
                      f"{type(exc).__name__}: {exc}"))
    return out


def render(findings: Sequence[Finding]) -> str:
    icon = {MATCH: "✅", MISMATCH: "❌", UNSOURCED: "⛔", UNVERIFIABLE: "❓"}
    lines = ["# 数值审核工作单（numeric_audit）", "",
             "> 本表由 `scripts/numeric_audit.py` 生成：按 shared_tables.md **独立重写**的",
             "> 第二实现，只读数据包 §1–§16 的表格，与 §17 渲染值对账。",
             "> **脚本只报差异，不判对错** —— 子代理必须逐条给出裁定：",
             "> {真实错误 / 口径差异（须写依据）/ 脚本误判（须写依据）}，",
             "> 并补做需要年报 PDF 的提取核查（脚本没有 PDF，只能标 UNVERIFIABLE）。", ""]
    lines.append("| # | 代码 | 检查 | 状态 | 严重度 | 期望 | 实际 | 依据/证据 |")
    lines.append("| --- | --- | --- | --- | --- | ---: | ---: | --- |")
    for i, f in enumerate(findings, 1):
        ev = "；".join(f.evidence[:3]) if f.evidence else f.basis
        exp = "—" if f.expected is None else f"{f.expected:,.2f}"
        act = "—" if f.actual is None else f"{f.actual:,.2f}"
        lines.append(f"| {i} | {f.code} | {f.title} | {icon.get(f.status,'?')} {f.status} | "
                     f"{f.severity or '—'} | {exp} | {act} | {ev[:150]} |")
    lines.append("")
    n_bad = sum(1 for f in findings if f.status in (MISMATCH, UNSOURCED))
    n_unk = sum(1 for f in findings if f.status == UNVERIFIABLE)
    lines.append(f"> 汇总：不一致 {n_bad} · 不可判定 {n_unk} · 一致 "
                 f"{sum(1 for f in findings if f.status == MATCH)}")
    lines.append("")
    lines.append("## 裁定要求（子代理）")
    lines.append("")
    lines.append("1. **每一条非 MATCH 的 finding 都必须裁定**，三选一：真实错误 / 口径差异 / 脚本误判；")
    lines.append("   选后两者必须写出依据（文档原文、年报页码、或口径定义）。")
    lines.append("2. **补做脚本做不到的提取核查**：§3/§4/§5 的行是否取自年报对应科目（需读年报），"
                 "以及被渲染成 `0.00` 或 `—` 的行是「真的没有」还是「映射未命中」。")
    lines.append("3. 末行必须是 `NUMERIC_AUDIT_RESULT: PASS` 或 `NUMERIC_AUDIT_RESULT: FIX_REQUIRED`。")
    lines.append("   存在未裁定的非 MATCH 项 ⇒ 不得给出 PASS。")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="数值审核（独立第二实现 + 提取可溯性）")
    ap.add_argument("--pack", required=True, help="data_pack_market.md")
    ap.add_argument("--output-dir", default=None, help="写出 numeric_audit.{json,md} 的目录")
    ap.add_argument("--stock-code", default="", help="用于选默认税率档（.HK/.US/其他）")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--md", dest="md_out", default=None)
    ap.add_argument("--allow-degraded", action="store_true",
                    help="显式接受带缺陷交付（verdict.degraded=true，必须随报告披露）")
    ap.add_argument("--strict", action="store_true",
                    help="UNVERIFIABLE 也计为失败（交付路径建议开启）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        pack = Path(args.pack).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error: 无法读取数据包 {args.pack}: {exc}", file=sys.stderr)
        return 2

    ts_code = args.stock_code
    if not ts_code:
        m = re.search(r"\|\s*股票代码\s*\|\s*([0-9A-Za-z.]+)\s*\|", pack)
        ts_code = m.group(1) if m else ""

    findings = run_all(pack, ts_code)
    from gate_policy import verdict, print_verdict, exit_code
    payload = [f.to_dict() for f in findings]
    if args.strict:
        for item in payload:
            if item["status"] == UNVERIFIABLE:
                item["severity"] = item.get("severity") or MAJOR
    v = verdict(("numeric_audit", payload), allow_degraded=args.allow_degraded,
                stock_code=ts_code)
    v["findings"] = payload

    out_dir = Path(args.output_dir) if args.output_dir else Path(args.pack).parent
    json_out = Path(args.json_out) if args.json_out else out_dir / "numeric_audit.json"
    md_out = Path(args.md_out) if args.md_out else out_dir / "numeric_audit.md"
    try:
        json_out.write_text(json.dumps(v, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        md_out.write_text(render(findings), encoding="utf-8")
    except OSError as exc:
        print(f"Error: 无法写出审核结果: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        print(render(findings))
        print_verdict(v, "数值审核")
        print(f"  → {json_out} / {md_out}")
    return exit_code(v)


if __name__ == "__main__":
    sys.exit(main())
