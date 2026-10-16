#!/usr/bin/env python3
"""invariants.py — 数据包级**不变式引擎**（机械校验，失败即报告，不靠人眼）。

为什么需要它
------------
现有校验分两层：Python 保证**算式正确**，`report_consistency.py` 保证**同一报告内数字不打架**。
两者都管不到「**跨小节的语义/单位/期间关系**」——而 06862 真实运行暴露的错误全部落在这一层：

- §17.1 的「5年CAGR」列里，营收用 4 期、净利润用 3 期（负基数年被静默丢弃），列头却相同；
- §17.8「买入就是胜利」基准价把**报表币种**的每股价（①②）与**交易币种**的价（③④）求算术平均；
- §5 现金流量表三节合计与 §4 现金及等价物变动**每一年都对不上**（差额 0.4–2.5 亿元）；
- §17.13 收入敏感性方向反了（收入越降、结余越高）；
- §6 分红表逐年 DPS 完全相同（疑似上游填充错误），却被用来推导派息率。

以上每一条单看都"像对的"，只有把两个小节放在一起做**不变式检查**才会暴露。
因此本引擎的原则是：**能机械判定的关系一律机械判定；判不了就报 UNEVALUATED，绝不默认通过**
（fail-closed，与 `section_contract.py` 的状态契约一致）。

CLI::

    python3 scripts/invariants.py --pack output/06862_海底捞/data_pack_market.md
    python3 scripts/invariants.py --pack ... --json invariants.json --strict

退出码：0 全部通过（无 FAIL）· 1 存在 FAIL · 2 输入不可读
`--strict` 下 UNEVALUATED 也计为失败。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence, Tuple

FAIL, WARN, PASS, UNEVALUATED = "FAIL", "WARN", "PASS", "UNEVALUATED"

# 勾稽容差：绝对 5 百万元或相对 1%，取较大者（覆盖四舍五入与折算尾差）
CASH_ABS_TOL = 5.0
CASH_REL_TOL = 0.01


@dataclass
class Finding:
    code: str
    title: str
    status: str
    detail: str
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────── markdown 表格解析 ───────────────────────────

def tables(text: str) -> List[Tuple[List[str], List[List[str]]]]:
    """Return [(header_cells, rows)] for every markdown table in ``text``."""
    out: List[Tuple[List[str], List[List[str]]]] = []
    block: List[List[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):        # separator row
                continue
            block.append(cells)
        elif block:
            if len(block) >= 2:
                out.append((block[0], block[1:]))
            block = []
    if len(block) >= 2:
        out.append((block[0], block[1:]))
    return out


def find_table(text: str, header_contains: Sequence[str], row_prefix: Optional[Sequence[str]] = None):
    for header, rows in tables(text):
        joined = " ".join(header)
        if all(k in joined for k in header_contains):
            if row_prefix and not any(r and r[0].startswith(tuple(row_prefix)) for r in rows):
                continue
            return header, rows
    return None, None



def _longest_equal_run(values: Sequence[float]) -> int:
    """最长「连续取值完全相同」的段长。

    上游「按公告快照」回填的特征**不是整列恒定**，而是**较早的若干年相同**：
    06862 §12 的 PE(TTM) = 11.62 / 11.39 / 14.05 / 14.05 / 14.05（2021–2023 连续 3 年 14.05），
    PB 同理 5.74——要求整列恒定会漏判。故按"连续 ≥3 年相同"识别。
    """
    best = run = 1 if values else 0
    for a, b in zip(values, values[1:]):
        run = run + 1 if abs(a - b) < 1e-12 else 1
        best = max(best, run)
    return best


def find_table_by_rows(text: str, row_keys: Sequence[str]):
    """Return (header, rows) of the first table containing a row whose first cell
    matches any of ``row_keys``. Section keywords such as 「经营业务现金净额」
    live in row labels, not in table headers."""
    for header, rows in tables(text):
        for r in rows:
            if r and any(k in r[0] for k in row_keys):
                return header, rows
    return None, None


def _num(cell):
    r"""单元格 → float —— **委托单点** `pack_metrics.num`（known_issues §三十三）。

    原实现自带一份 `replace(",", ...)` + `re.match(r"^-?\d+...")`：
    它会漏掉全角负号 `−1,234.5`（真实出现过的输入形态），也不认 `N/A`。
    委托后与其余消费者**同一套语义**。
    """
    from pack_metrics import num
    return num(cell)



def _section(text: str, number: str) -> str:
    r"""取编号为 `number` 的小节 —— **委托给单点** `pack_metrics.section_text`。

    ⚠️ 这里原先自带一份实现（`^#{2,3}\s*N\b` + 「遇到下一个 `##`/`###` 就停」），
    有两个缺陷（known_issues §五十一）：
      ① **层级盲**：`## 11.` 里出现 `### 年度行情汇总` 就会截断 ⇒ §11 的年度表
         **永远读不到**（实测 I17 因此 UNEVALUATED）；
      ② 与 `pack_metrics` 那份是**两个实现**，行为不一致（多读取器，§三十三）。

    委托后：到下一个**同级或更高级**标题才停；`## 11.` 因此包含它自己的 `###` 子块。
    """
    from pack_metrics import section_text
    return section_text(text, number)


# ─────────────────────────── 不变式实现 ───────────────────────────

def invariant_cagr_periods(pack: str) -> Finding:
    """I1 期间标注：「CAGR」列每个数字必须自证其基年与期数；负基数只能记 N/A。"""
    sec = _section(pack, "17.1")
    if not sec:
        return Finding("I1", "§17.1 CAGR 期间标注", UNEVALUATED, "未找到 §17.1 小节")
    header, rows = find_table(sec, ["5年CAGR"])
    if header is None:
        return Finding("I1", "§17.1 CAGR 期间标注", UNEVALUATED, "§17.1 未找到含「5年CAGR」的表")

    cagr_labels = [r[0] for r in rows if r and r[-1] not in ("—", "")]
    notes = [ln.strip("> ").strip() for ln in sec.splitlines() if ln.strip().startswith(">")]
    ev: List[str] = []
    bad: List[str] = []
    for row in rows:
        if not row:
            continue
        label, cell = row[0], row[-1]
        if cell in ("—", ""):
            continue
        label_key = re.sub(r"（.*?）", "", label).strip()
        noted = [n for n in notes if label_key and label_key in n]
        if _num(cell) is not None:
            if not noted:
                bad.append(f"{label}: 数字 {cell} 未标注基年/期数")
            ev.append(f"{label} = {cell}" + (f"｜{noted[0][:80]}" if noted else ""))
        elif "N/A" in cell or "无定义" in cell:
            if not noted:
                bad.append(f"{label}: N/A 缺少原因说明")
            ev.append(f"{label} = {cell}｜{noted[0][:80] if noted else '无说明'}")
        else:
            bad.append(f"{label}: 无法判定的 CAGR 单元格 {cell!r}")

    if not cagr_labels:
        return Finding("I1", "§17.1 CAGR 期间标注", UNEVALUATED, "§17.1 的 CAGR 列全为空")
    if bad:
        return Finding("I1", "§17.1 CAGR 期间标注", FAIL,
                       "CAGR 列存在未自证期间的数字或未说明的 N/A；"
                       "同一列头下不同行可能使用了不同期数（06862 实测 43.36% 实为 3 期）", bad + ev)
    return Finding("I1", "§17.1 CAGR 期间标注", PASS, "每个 CAGR 数字都带基年与期数标注", ev)


def invariant_per_share_currency(pack: str, fx: Optional[dict] = None) -> Finding:
    """I2 每股价币种：报表币种 ≠ 交易币种时，§17.8 基准价表必须显式声明折算。"""
    sec = _section(pack, "17.8")
    if not sec:
        return Finding("I2", "§17.8 每股价币种一致性", UNEVALUATED, "未找到 §17.8 小节")
    header, rows = find_table(sec, ["基准价（"], row_prefix=["①", "②", "③", "④", "⑤"])
    if header is None:
        return Finding("I2", "§17.8 每股价币种一致性", UNEVALUATED, "§17.8 未找到基准价表")

    factor = None
    if fx:
        try:
            factor = float(fx.get("factor_to_reporting"))
        except (TypeError, ValueError):
            factor = None
    price_unit = re.search(r"基准价（([^）]+)）", " ".join(header))
    unit = price_unit.group(1) if price_unit else "?"

    if factor is None or abs(factor - 1.0) < 1e-12:
        return Finding("I2", "§17.8 每股价币种一致性", PASS,
                       f"交易币种与报表币种一致（或多币种折算不适用）；表头单位 {unit}",
                       [f"factor_to_reporting={factor}"])

    declared = ("币种说明" in sec)
    per_share_rows = [r for r in rows if r and r[0].startswith(("①", "②", "⑤"))]
    ev = [f"factor_to_reporting={factor}", f"表头单位={unit}",
          f"①②⑤ 行数={len(per_share_rows)}", f"币种说明={'有' if declared else '无'}"]
    if not declared:
        return Finding("I2", "§17.8 每股价币种一致性", FAIL,
                       "报表币种与交易币种不同，但基准价表未声明每股量已折算——"
                       "未经折算的元/股会与港元/股一起求算术平均（06862 实测综合值 9.34 应为 9.46）",
                       ev)
    return Finding("I2", "§17.8 每股价币种一致性", PASS,
                   "基准价表已声明交易币种与折算口径", ev)


def invariant_cash_tie_out(pack: str) -> Finding:
    """I3 现金勾稽：§5 三节合计必须等于 §4 现金及等价物的当年变动。"""
    sec5, sec4 = _section(pack, "5"), _section(pack, "4")
    if not sec5 or not sec4:
        return Finding("I3", "§5↔§4 现金勾稽", UNEVALUATED, "未找到 §4 或 §5 小节")
    h5, r5 = find_table_by_rows(sec5, ["经营业务现金净额", "OCF"])
    h4, r4 = find_table_by_rows(sec4, ["现金及现金等价物", "现金及等价物", "货币资金"])
    if h5 is None or h4 is None:
        return Finding("I3", "§5↔§4 现金勾稽", UNEVALUATED, "§5 或 §4 缺少所需行")

    years = [c for c in h5[1:] if re.fullmatch(r"\d{4}", c)]
    flows: Dict[str, Dict[str, float]] = {}
    for label, keys in (("ocf", ["经营业务现金净额", "OCF"]),
                        ("icf", ["投资业务现金净额"]),
                        ("fin", ["融资业务现金净额"])):
        row = next((r for r in r5 if r and any(k in r[0] for k in keys)), None)
        if row is None:
            return Finding("I3", "§5↔§4 现金勾稽", UNEVALUATED, f"§5 缺少 {keys[0]} 行")
        flows[label] = {y: _num(row[h5.index(y)]) for y in years}
    cash_row = next((r for r in r4 if r and "现金" in r[0]), None)
    if cash_row is None:
        return Finding("I3", "§5↔§4 现金勾稽", UNEVALUATED, "§4 缺少现金类行")

    # 口径必须先对齐，否则这个不变式会**误报**（2026-10-13 实测纠正）：
    # 现金流量表的净变动是「现金及现金等价物」口径，而数据包 §4 给的是**更宽口径**——
    # 港股 `money_cap` = 「銀行結餘及現金」（含原到期日 3 个月以上的银行存款），
    # A 股 = 「货币资金」。两者相差的定期存款重分类 + 汇率折算影响不构成错误。
    # 06862 实测：§5 三节合计 −2,311.71 与年报「現金及現金等價物減少淨額 (2,311,717) 千元」
    # 完全一致；差额 1,439.24 = 定期存款重分类 1,583.49 + 汇率变动 −144.24。
    # ⇒ 口径不匹配时只能记 UNEVALUATED（判不了绝不默认通过，也绝不误报为 FAIL）。
    if "现金及现金等价物" not in cash_row[0]:
        return Finding("I3", "§5↔§4 现金勾稽", UNEVALUATED,
                       f"§4 的现金行是「{cash_row[0]}」——与现金流量表的"
                       "「现金及现金等价物」不是同一口径（前者含 3 个月以上定期存款等）；"
                       "数据包层无法勾稽，须用年报现金流量表『现金及现金等价物净变动 + 汇率变动影响』核对。"
                       "不得据此指控数据错误，也不得由该差额反推现金净额",
                       [f"§4 现金行：{cash_row[0]}"])
    cash = {y: _num(cash_row[h4.index(y)]) for y in years if y in h4}

    ev: List[str] = []
    bad: List[str] = []
    checked = 0
    ordered = sorted(cash, reverse=True)
    for i in range(len(ordered) - 1):
        y, prev = ordered[i], ordered[i + 1]
        if cash.get(y) is None or cash.get(prev) is None:
            continue
        net = sum(flows[k].get(y) or 0.0 for k in ("ocf", "icf", "fin"))
        fx_row = next((r for r in r5 if r and "汇率变动" in r[0]), None)
        if fx_row is not None:
            idx = h5.index(y) if y in h5 else None
            net += (_num(fx_row[idx]) or 0.0) if idx is not None else 0.0
        actual = cash[y] - cash[prev]
        gap = actual - net
        tol = max(CASH_ABS_TOL, abs(actual) * CASH_REL_TOL)
        checked += 1
        ev.append(f"{y}: 三节合计 {net:,.2f} vs 实际Δ现金 {actual:,.2f} ⇒ 差额 {gap:,.2f}"
                  f"（容差 {tol:,.2f}）")
        if abs(gap) > tol:
            bad.append(f"{y} 差额 {gap:,.2f}")
    if checked == 0:
        return Finding("I3", "§5↔§4 现金勾稽", UNEVALUATED, "没有可比较的相邻年度现金余额")
    if bad:
        # 差额的常见来源是**汇率折算影响**：上游 `hk_cashflow` 没有
        # 「汇率变动对现金及现金等价物的影响」科目，也没有期初/期末现金余额，
        # 因此对以 CNY 为报表币种、有海外业务的港股，三节合计**在口径上不可能**等于
        # §4 现金及等价物的变动（06862 实测差额 408/2,521/−1,768/1,439 百万）。
        # 结论不变：该缺口无法用现有 API 关闭 ⇒ 仍 fail-closed（不得据此下净额结论），
        # 但必须把"原因与补救"写明，而不是留一句含糊的"对不上"。
        sec5_all = _section(pack, "5")
        fx_line_present = any(k in sec5_all for k in ("汇率变动", "汇兑差额对现金"))
        cause = ("上游缺「汇率变动对现金的影响」科目 ⇒ 差额极可能即汇率折算影响；"
                 "须用年报现金流量表该行核对（不得据此下净额结论）"
                 if not fx_line_present else
                 "现金变动与三节合计不一致，须逐年核对汇率影响行")
        return Finding("I3", "§5↔§4 现金勾稽", FAIL,
                       "现金流量表三节合计与现金及等价物变动无法勾稽——"
                       "任何建立在『某年净减/净增 X』上的叙事都不可靠（06862 实测 4/4 年不符）。"
                       + cause,
                       ev + [f"不符年度：{', '.join(bad)}"])
    return Finding("I3", "§5↔§4 现金勾稽", PASS, f"{checked} 个年度全部在容差内", ev)


def invariant_dividend_integrity(pack: str) -> Finding:
    """I5 历史表数值列完整性：跨年度完全恒定的列不得用于趋势/派息率推断。

    06862 实测两类同源缺陷（同属上游"按公告快照"填充）：
    - §6 `dps_hkd` 在 2023–2026 各年恒为 0.7610（且同年度重复行）⇒ 由此推得的派息率不可复现；
    - §12 `PE (TTM)` 2021–2023 恒 14.05、`PB` 恒 5.74 ⇒ 任何"历史 PE/PB 分位"判断失效。
    只查 DPS 会漏掉 PE/PB 这一类，故本不变式按"列"而非"指标"判定。
    """
    sec = _section(pack, "6")
    if not sec:
        return Finding("I5", "历史表数值列完整性", UNEVALUATED, "未找到 §6 小节")
    header, rows = find_table(sec, ["每股股息"])
    if header is None:
        return Finding("I5", "历史表数值列完整性", UNEVALUATED, "§6 未找到每股股息表")

    dps = [(r[0], _num(r[1])) for r in rows if len(r) > 1 and _num(r[1]) is not None]
    years = [y for y, _ in dps]
    values = [v for _, v in dps]
    ev = [f"§6 {y}: {v}" for y, v in dps]

    # 泛化：其它历史表（当前为 §12）中跨年度恒定的数值列
    constants: List[str] = []
    h12, r12 = find_table_by_rows(_section(pack, "12"), ["PE (TTM)", "PB"])
    if h12 is not None:
        ycols = [c for c in h12[1:] if re.fullmatch(r"\d{4}", c)]
        for row in r12:
            if not row:
                continue
            series = [_num(row[h12.index(y)]) if h12.index(y) < len(row) else None for y in ycols]
            run = _longest_equal_run([v for v in series if v is not None])
            if run >= 3:
                constants.append(f"§12 {row[0]}：连续 {run} 个年度取值完全相同"
                                 f"（{series[0]}…{series[-1]}，回填特征）")

    duplicated = len(years) != len(set(years))
    constant_dps = len(values) >= 3 and len(set(values)) == 1

    if constants:
        return Finding("I5", "历史表数值列完整性", FAIL,
                       "历史表存在跨年度完全恒定的数值列 —— 高度疑似上游按公告快照重复填充，"
                       "该列不得用于任何趋势/分位/派息率判断：" + "；".join(constants),
                       ev + constants)
    if duplicated:
        return Finding("I5", "历史表数值列完整性", FAIL,
                       "§6 出现同年度重复行——年度口径不明，派息率不可逐年复现", ev)
    if constant_dps:
        return Finding("I5", "历史表数值列完整性", FAIL,
                       f"§6 的每股股息在 {len(values)} 个年度上完全相同（{values[0]}）——"
                       "高度疑似上游填充错误；由它推导的任何派息率（§17.1 列 / §17.10 法1·法3）"
                       "不得作为决策输入", ev)
    return Finding("I5", "历史表数值列完整性", PASS, "分红序列无重复年度、非恒定，且未发现恒定历史列", ev)


def invariant_sensitivity_direction(pack: str) -> Finding:
    """I6 敏感性方向：收入下滑不得抬高可支配结余。"""
    sec = _section(pack, "17.13")
    if not sec:
        # 没有 ### 17.13 标题 ⇒ 该小节未输出。fail-closed 拒发本身就是正确结果
        # （状态表里会以 §17.13 形式登记 reason），因此记 PASS 而不是 UNEVALUATED。
        return Finding("I6", "§17.13 方向一致性", PASS,
                       "§17.13 未输出数值（fail-closed 或缺失）；原因见 §17 状态表")
    header, rows = find_table(sec, ["收入情景"])
    if header is None:
        return Finding("I6", "§17.13 方向一致性", UNEVALUATED, "§17.13 无收入情景表")
    pairs: List[Tuple[float, float]] = []
    for r in rows:
        k = _num(r[0].rstrip("×")) if r else None
        bal = _num(r[1].replace(",", "")) if len(r) > 1 else None
        if k is not None and bal is not None:
            pairs.append((k, bal))
    if not pairs:
        return Finding("I6", "§17.13 方向一致性", UNEVALUATED, "§17.13 表格无数值行")
    pairs.sort(key=lambda t: t[0])
    ev = [f"k={k}: 结余 {b:,.2f}" for k, b in pairs]
    for (k1, b1), (k2, b2) in zip(pairs, pairs[1:]):
        if b2 < b1 - 1e-9:
            return Finding("I6", "§17.13 方向一致性", FAIL,
                           f"收入倍数由 {k1} 升到 {k2} 时结余反而下降（{b1:,.2f} → {b2:,.2f}）——"
                           "等价于『收入越降、回报越高』的方向性错误，该表不得出厂"
                           "（fail-closed：应标记 input_inconsistent 并整节不输出）", ev)
    return Finding("I6", "§17.13 方向一致性", PASS, "结余随收入下降而下降", ev)



def invariant_income_caliber(pack: str) -> Finding:
    """I4 利表同年口径自洽：**同口径**三元组必须闭合。

    06862 实测（clean-room Step 2X 发现）：数据包 §3 在同一年内把收入/除税前溢利用
    「持续经营」口径、把除税后/归母净利润用「含已终止经营业务（特海）」口径，于是
    2021 年 −3,070.14 − 177.70 = −3,247.84 却写 除税后 −4,161.21（差 913.37），
    2022 年差 264.09 —— 恰为特海当年税前亏损。单行看起来都正常，只有把同一列
    三个数放在一起才暴露；且它会污染一切以「除税前/除税后」为基础的比率。

    2026-10-14 判据升级（known_issues.md §18 第 3 条）：§3 现在**分列**持续经营口径，
    因此本不变式改为按**同口径**勾稽，两条同时成立才 PASS：
      ① 除税前（持续） − 税项（持续） == 持续经营业务税后利润
      ② 除税后（合计） == 持续经营税后 + 终止/非持续业务溢利
    旧包（无「持续经营业务税后利润」行）回落到原来的合计口径判据并给 WARN 说明 ——
    这样 §3 的修复能让 I4 从 FAIL 转 PASS 而**不掩盖**合计口径。
    """
    sec = _section(pack, "3")
    if not sec:
        return Finding("I4", "§3 利表同年口径自洽", UNEVALUATED, "未找到 §3 小节")
    # A 股 §3 用「利润总额 / 所得税费用 / 净利润」，港股用「除税前溢利 / 税项 / 除税后溢利」
    # ⇒ 必须与 field_coverage.REQUIRED_ROWS["3"] 同口径取别名，否则 I4 对**每一只 A 股**
    # 都返回 UNEVALUATED，而 I4 属阻断级 ⇒ 全部 A 股包不可交付（独立审计 P1）。
    header, rows = find_table_by_rows(sec, ["除税前溢利", "利润总额", "税前利润"])
    if header is None:
        return Finding("I4", "§3 利表同年口径自洽", UNEVALUATED,
                       "§3 未找到含「除税前溢利/利润总额」的表")

    def row_of(*keys):
        return next((r for r in rows if r and any(k in r[0] for k in keys)), None)

    pre = row_of("除税前溢利", "利润总额", "税前利润")
    tax = row_of("税项", "所得税费用", "所得税")
    cont = row_of("持续经营业务税后利润")
    disc = row_of("终止", "非持续业务")
    post = row_of("除税后溢利", "净利润")
    if pre is None or tax is None:
        return Finding("I4", "§3 利表同年口径自洽", UNEVALUATED,
                       "§3 缺少 除税前溢利/税项 之一")
    years = [c for c in header[1:] if re.fullmatch(r"\d{4}", c)]
    if not years:
        return Finding("I4", "§3 利表同年口径自洽", UNEVALUATED, "没有可比较的年度列")

    def _at(row, y):
        if row is None:
            return None
        i = header.index(y)
        return _num(row[i]) if i < len(row) else None

    ev: List[str] = []
    bad: List[str] = []
    checked = 0
    same_caliber = cont is not None
    for y in years:
        a, b = _at(pre, y), _at(tax, y)
        if a is None or b is None:
            continue
        checked += 1
        if same_caliber:
            c = _at(cont, y)
            if c is None:
                continue
            gap = a - b - c
            tol = max(1.0, abs(c) * 0.005)
            ev.append(f"{y}: 除税前(持续) {a:,.2f} − 税项(持续) {b:,.2f} = {a - b:,.2f} "
                      f"vs 持续经营税后 {c:,.2f} ⇒ 差 {gap:,.2f}")
            if abs(gap) > tol:
                bad.append(f"{y} 同口径差 {gap:,.2f}")
            d, e = _at(disc, y), _at(post, y)
            if e is not None:
                expect = c + (d or 0.0)
                gap2 = e - expect
                tol2 = max(1.0, abs(e) * 0.005)
                ev.append(f"{y}: 合计 {e:,.2f} vs 持续 {c:,.2f} + 终止 {d or 0.0:,.2f} "
                          f"⇒ 差 {gap2:,.2f}")
                if abs(gap2) > tol2:
                    bad.append(f"{y} 合计勾稽差 {gap2:,.2f}")
        else:
            c = _at(post, y)
            if c is None:
                continue
            gap = a - b - c
            tol = max(1.0, abs(c) * 0.005)
            ev.append(f"{y}: {a:,.2f} − {b:,.2f} = {a - b:,.2f} vs 除税后 {c:,.2f} ⇒ 差 {gap:,.2f}")
            if abs(gap) > tol:
                bad.append(f"{y} 差 {gap:,.2f}")
    if checked == 0:
        return Finding("I4", "§3 利表同年口径自洽", UNEVALUATED, "没有可比较的年度列")
    if bad:
        return Finding("I4", "§3 利表同年口径自洽", FAIL,
                       ("同一年内**同口径**三元组无法自洽 ⇒ §3 仍混用了口径"
                        if same_caliber else
                        "同一年的三个利表数字无法自洽 ⇒ §3 混用了不同口径"
                        "（如持续经营 vs 含已终止经营业务），任何以除税前/除税后为基础的比率都被污染"),
                       ev + [f"不符年度：{', '.join(bad)}"])
    if not same_caliber:
        return Finding("I4", "§3 利表同年口径自洽", WARN,
                       "合计口径三数自洽；但上游未提供「持续经营业务税后利润」行 ⇒ "
                       "无法确认除税前/税项与除税后是否同口径（known_issues.md §18）", ev)
    return Finding("I4", "§3 利表同年口径自洽", PASS,
                   f"{checked} 个年度：持续经营三元组与合计勾稽**双闭合**", ev)


def invariant_price_consistency(pack: str) -> Finding:
    """I7 价格一致性：§2 的最新价与 §17 网格所用价格必须是同一个值。

    为什么需要：`tushare_collector.py --refresh-market` 只刷 §1/§2/§11/§14，**明确不重建 §17**
    （见 AGENTS.md）。若刷新后收盘价变了，§17.8 的溢价、§17.11/§17.14 的目标买入价仍按旧价算，
    数据包里就同时存在**同一价格的两个值**，而 I1–I6 都不看这一项（06862 第二轮重跑时因此
    主动跳过了 --refresh-market，并登记为 known_issues §十三）。
    """
    sec2, sec178 = _section(pack, "2"), _section(pack, "17.8")
    if not sec2 or not sec178:
        return Finding("I7", "§2↔§17 价格一致性", UNEVALUATED, "未找到 §2 或 §17.8 小节")
    m2 = re.search(r"最新价格[^|]*\|\s*\**([\d,]+(?:\.\d+)?)", sec2)
    if not m2:
        m2 = re.search(r"收盘价[^|]*\|\s*\**([\d,]+(?:\.\d+)?)", sec2)
    m17 = re.search(r"当前股价\s*\**([\d,]+(?:\.\d+)?)", sec178)
    if not m2 or not m17:
        return Finding("I7", "§2↔§17 价格一致性", UNEVALUATED,
                       "§2 未给出「最新价格/收盘价」或 §17.8 未给出「当前股价」，无法比对")
    p2 = float(m2.group(1).replace(",", ""))
    p17 = float(m17.group(1).replace(",", ""))
    tol = max(0.01, p2 * 0.005)
    ev = [f"§2 最新价 = {p2}", f"§17.8 当前股价 = {p17}", f"容差 = {tol:.3f}"]
    if abs(p2 - p17) > tol:
        return Finding("I7", "§2↔§17 价格一致性", FAIL,
                       "§2 的最新价与 §17 网格所用价格不一致 ⇒ §17 全部价格敏感结果"
                       "（溢价 / 目标买入价 / 分位）与 §2 展示的价格脱钩（多因 --refresh-market 未重建 §17）",
                       ev)
    return Finding("I7", "§2↔§17 价格一致性", PASS, "两处价格一致", ev)



def invariant_balance_identity(pack: str) -> Finding:
    """I8 资产负债恒等式：总资产 = 总负债 + 股东权益 + 少数股东权益。

    为什么需要：不变式原先只查"跨小节关系"（CAGR 期间、币种、现金勾稽…），**没有一条查会计恒等式**。
    06862 实测该式**精确成立**（含少数股东权益：12,085.07 + 10,013.93 + (−8.80) = 22,090.20），
    所以它是低成本高收益的守卫：任何"漏行/串行/口径错位"都会先在这里破形。
    """
    sec = _section(pack, "4")
    if not sec:
        return Finding("I8", "资产负债恒等式", UNEVALUATED, "未找到 §4 小节")
    header, rows = find_table_by_rows(sec, ["总资产"])
    if header is None:
        return Finding("I8", "资产负债恒等式", UNEVALUATED, "§4 未找到含「总资产」的表")

    def row_of(*keys):
        return next((r for r in rows if r and any(k in r[0] for k in keys)), None)

    # 母公司权益行的识别必须**排除少数股东权益**：`股东权益` 是 `少数股东权益`
    # 的子串，原先的 `row_of("股东权益", "归母权益")` 会在任何把少数股东权益排在
    # 前面的包上先命中它 —— 于是母权益被丢掉、少数股东权益被加两遍，得到**假 FAIL**
    # （600941/002027 实测：恒等式其实逐年精确成立，却被报成差 1,388,799.00）。
    # 同时补上包内真实使用的标签 `归母所有者权益`（两个旧 key 都不匹配它）。
    # 语义边界：若只能找到**合计**权益行（所有者权益合计），则它已含少数股东权益，
    # 不能再叠加 mi，否则重复计入 —— 故此时把 mi 置空。
    def _equity_row():
        for label in ("归母所有者权益", "归属于母公司", "归母权益", "股东权益"):
            for r in rows:
                if not r or "少数" in r[0]:
                    continue
                if label in r[0]:
                    return r, False
        for r in rows:
            if not r or "少数" in r[0]:
                continue
            if "所有者权益" in r[0]:
                return r, ("合计" in r[0])
        return None, False

    ta, tl = row_of("总资产"), row_of("总负债")
    eq, _eq_is_total = _equity_row()
    mi = None if _eq_is_total else row_of("少数股东权益")
    if ta is None or tl is None or eq is None:
        return Finding("I8", "资产负债恒等式", UNEVALUATED,
                       "§4 缺少 总资产/总负债/（归母）股东权益 之一")
    years = [c for c in header[1:] if re.fullmatch(r"\d{4}", c)]
    ev = [f"行标签：总资产={ta[0]}｜总负债={tl[0]}｜权益={eq[0]}"
          + (f"｜少数股东权益={mi[0]}" if mi is not None
             else "（权益行为合计口径 ⇒ 不再叠加少数股东权益）")]
    bad, checked = [], 0
    for y in years:
        i = header.index(y)
        a, b, c = _num(ta[i]), _num(tl[i]), _num(eq[i])
        if a is None or b is None or c is None:
            continue
        d = _num(mi[i]) if (mi is not None and i < len(mi)) else 0.0
        checked += 1
        gap = a - (b + c + (d or 0.0))
        tol = max(1.0, abs(a) * 0.001)
        ev.append(f"{y}: {a:,.2f} vs {b:,.2f}+{c:,.2f}+{(d or 0.0):,.2f} ⇒ 差 {gap:,.2f}")
        if abs(gap) > tol:
            bad.append(f"{y} 差 {gap:,.2f}")
    if checked == 0:
        return Finding("I8", "资产负债恒等式", UNEVALUATED, "没有可比较的年度列")
    if bad:
        return Finding("I8", "资产负债恒等式", FAIL,
                       "资产负债表恒等式不成立 ⇒ 存在漏行、串行或口径错位"
                       "（该式在任何一本账上都应精确成立）", ev + [f"不符年度：{', '.join(bad)}"])
    return Finding("I8", "资产负债恒等式", PASS, f"{checked} 个年度精确成立", ev)


def invariant_share_count_consistency(pack: str) -> Finding:
    """I9 股数口径交叉核验（**包内**）：两条独立路径隐含的股数必须一致。

    路径 A：总市值 ÷ 最新价（§2）——交易币种，币种相消 ⇒ 百万股；
    路径 B：股东权益 ÷ 每股净资产（§4 / §12）——报表币种，币种相消 ⇒ 百万股。

    06862 实测 A = 53,343.18/9.30 = **5,735.83** 百万股、B = 10,013.93/1.80 = **5,563.29** 百万股
    ⇒ 差 **3.10%**：包内市值用了不同于报表的股本口径（年报附注为 5,574.00 百万股）。
    这条把 known_issues #25「total_shares 口径混用」从"需要外部年报才能发现"变成**包内可判**。
    """
    sec2, sec4, sec12 = _section(pack, "2"), _section(pack, "4"), _section(pack, "12")
    if not sec2 or not sec4 or not sec12:
        return Finding("I9", "股数口径交叉核验", UNEVALUATED, "未找到 §2/§4/§12 之一")
    m_price = re.search(r"最新价格[^|]*\|\s*\**([\d,]+(?:\.\d+)?)", sec2) or \
        re.search(r"收盘价[^|]*\|\s*\**([\d,]+(?:\.\d+)?)", sec2)
    # 总市值：港股在 §2，A 股在 §1（§2 只有「最新收盘价」）⇒ 两处都要找，
    # **且必须按行标签里的单位归一**：A 股 §1 写的是「总市值 (万元)」，
    # 直接当百万用会让路径 A 大 100 倍（实测 600941：2,169,589 vs 21,683 百万股，
    # 假报 9905% 不一致）。路径 A 要的是**百万股** ⇒ 先把市值化到百万。
    cap_mm = None
    for _sec_text in (sec2, _section(pack, "1")):
        if not _sec_text:
            continue
        _m = re.search(r"总市值([^|\n]*)\|\s*\**([\d,]+(?:\.\d+)?)", _sec_text)
        if not _m:
            continue
        _label, _raw = _m.group(1), float(_m.group(2).replace(",", ""))
        # ⚠️ **判断顺序即正确性**：单位标签是**子串包含**关系 ——
        # "百万元" 含 "万元"、"百万港元" 含 "百万"。必须**从最具体到最宽泛**判定。
        # 原顺序（万元→亿元→百万→元）遇到 "百万元" 会误除 100（潜在 100 倍错）。
        # 对现有标签（"万元" / "百万港元"）行为不变，仅消除这一潜在陷阱。
        if "百万" in _label:
            _mm = _raw
        elif "万元" in _label:
            _mm = _raw / 100.0
        elif "亿元" in _label:
            _mm = _raw * 100.0
        elif "元" in _label:
            _mm = _raw / 1e6
        else:
            _mm = _raw
        cap_mm = _mm
        m_cap = True
        break
    # A 股 §4 的行标签是「归母所有者权益」，港股是「股东权益/归母权益」⇒ 两套都要认
    _h4, r4 = find_table_by_rows(sec4, ["股东权益", "归母权益", "所有者权益"])
    _h12, r12 = find_table_by_rows(sec12, ["每股净资产"])
    if not (m_price and cap_mm) or r4 is None or r12 is None:
        return Finding("I9", "股数口径交叉核验", UNEVALUATED,
                       "缺「最新价格/总市值」（§1 或 §2）或 §4/§12 缺「股东权益/每股净资产」")
    price = float(m_price.group(1).replace(",", ""))
    cap = cap_mm
    eq_row = next((r for r in r4 if r and any(
        k in r[0] for k in ("股东权益", "归母权益", "所有者权益"))), None)
    # 必须按**行标签**取「每股净资产」，不能取 §12 的首行（首行通常是 ROE）
    bv_row = next((r for r in r12 if r and "每股净资产" in r[0]), None)
    if price <= 0 or not eq_row or not bv_row:
        return Finding("I9", "股数口径交叉核验", UNEVALUATED, "价格或权益/每股净资产不可用")
    # 取最近一个同时有两个数的年度列
    headers = _h4 if _h4 else []
    shares_a = shares_b = None
    used = ""
    for i in range(1, min(len(eq_row), len(bv_row))):
        eq, bv = _num(eq_row[i]) if i < len(eq_row) else None, _num(bv_row[i]) if i < len(bv_row) else None
        if eq and bv and bv > 0:
            shares_b = eq / bv
            used = headers[i] if i < len(headers) else f"col{i}"
            break
    if shares_b is None:
        return Finding("I9", "股数口径交叉核验", UNEVALUATED, "找不到同时有权益与每股净资产的列")
    shares_a = cap / price
    diff = abs(shares_a - shares_b) / shares_b * 100
    ev = [f"路径A 市值÷价 = {cap:,.2f}/{price:,.2f} = {shares_a:,.2f} 百万股",
          f"路径B 权益÷每股净资产 = {shares_b:,.2f} 百万股（{used}）",
          f"相对差 {diff:.2f}%"]
    # --- #25（2026-10-14 用户拍板）：**以年报的已发行股数为准** -------------------
    # 若 §1 已声明股本（`shares.json` / `--shares` → "年报/人工申报值"），则它是**权威**：
    # 与此前唯一可用的两条反推路径不一致是**预期**的，属于披露而非违规 ——
    # 旧判据（两路径必须一致）在有了申报股本之后就会把正确答案判成 FAIL。
    declared, declared_trusted = _declared_share_count(pack)
    if declared is not None and declared_trusted:
        gap_a = abs(shares_a - declared) / declared * 100
        gap_b = abs(shares_b - declared) / declared * 100
        ev.append(f"§1 申报股本 = {declared:,.2f} 百万股（**权威口径**）")
        ev.append(f"与反推路径的偏差：A {gap_a:.2f}% / B {gap_b:.2f}% —— "
                  "申报值与反推值不一致属预期，已按申报值重算市值")
        if gap_a > 0.5:
            return Finding("I9", "股数口径交叉核验", WARN,
                           "§1 的申报股本与「市值÷价」反推值不一致 ⇒ 上游市值内嵌的可能是"
                           "**另一个股本口径**（期末 vs 当前）或未按申报股本重算。"
                           "披露即可，但报告不得混用两个股本口径", ev)
        return Finding("I9", "股数口径交叉核验", PASS,
                       f"申报股本与反推路径一致（差 {gap_a:.2f}%）", ev)
    if declared is not None:
        ev.append(f"§1 股本 = {declared:,.2f} 百万股（**降级值：反推**）⇒ 仍需交叉核验")
    if diff > 2.0:
        return Finding("I9", "股数口径交叉核验", FAIL,
                       "包内两条独立路径隐含的股数不一致 ⇒ 市值或每股指标用了不同的股本口径"
                       "（两者币种各自相消，故此差异不能归因于汇率）。"
                       "**修复方式**：提供年报已发行股数（`--shares` / "
                       "`{output_dir}/shares.json`），见 known_issues.md #25", ev)
    return Finding("I9", "股数口径交叉核验", PASS, f"两路径一致（差 {diff:.2f}%）", ev)


def _declared_share_count(pack: str):
    """读数据包里的申报股本 → ``(shares_mm, trusted)``。

    解析单点在 `shares_utils.declared_from_pack`（两种渲染形态都认）。
    这样 I9 与数值审核 E9/D7e 不会各自解释同一个数。
    """
    try:
        import sys as _sys
        from pathlib import Path as _P
        _sys.path.insert(0, str(_P(__file__).resolve().parent))
        from shares_utils import declared_from_pack
        val, trusted, _raw = declared_from_pack(pack)
        return val, trusted
    except Exception:  # noqa: BLE001 — 解析失败按"未申报"处理（fail-closed）
        return None, False


def invariant_constant_history_columns(pack: str) -> Finding:
    """I10 恒定历史列**泛化**检测（WARN）：任意多年度表中连续 ≥3 年取值相同的数值列。

    I5 只覆盖已被证明会失真的两处（§6 dps_hkd、§12 PE/PB）。本不变式把同一检测**推广到所有表**，
    以 WARN（而非 FAIL）呈现：恒定有时是真实的（如固定税率、零负债），但**值得逐条确认**
    ——上游"按公告快照"回填是这一类风险的已知来源。
    """
    hits: List[str] = []
    for header, rows in tables(pack):
        years = [c for c in header[1:] if re.fullmatch(r"\d{4}", c)]
        if len(years) < 3:
            continue
        idx = [header.index(y) for y in years]
        for row in rows:
            if not row:
                continue
            vals = [_num(row[i]) if i < len(row) else None for i in idx]
            nums = [v for v in vals if v is not None]
            if len(nums) < 3:
                continue
            run = _longest_equal_run(nums)
            if run >= 3:
                label = row[0][:26]
                if label.startswith(("每股股息", "PE", "PB")):
                    continue          # 已由 I5 以 FAIL 覆盖
                hits.append(f"{label}：连续 {run} 年取值相同（{nums[0]}）")
    if hits:
        return Finding("I10", "恒定历史列（泛化）", WARN,
                       "以下数值列在多个年度上取值相同 —— 需确认是真实恒定还是上游回填：",
                       hits[:12])
    return Finding("I10", "恒定历史列（泛化）", PASS, "未发现可疑的恒定历史列")


def invariant_share_count_across_sections(pack: str) -> Finding:
    """I11 §16 总股本 ↔ §17.8 股本口径一致性（**跨小节**的单位/口径交叉核验）。

    为什么新增（known_issues §四十一）：§16「股权质押」此前**不受任何门禁覆盖**，
    于是它的单位误标活了很久 —— Tushare `pledge_stat` 返回**万股**，
    `divider=1e4` 化成**亿股**，标签却写「万股」⇒ **10,000 倍**错。
    实测工商银行写成「总股本 (万股) = 2696.12」（实为 2,696.12 亿股），
    而 §17.8 同一份包里写「总股本（百万股） = 269,612.21」—— 同一事实两个数，差 10,000 倍。

    内部「质押比例」不受影响（两个同单位值相除），所以这个错误不会自己暴露；
    只有**跨小节**比对才能看见。本不变式把「单位标签错」变成机器可判。

    路径：§16 总股本（按标签单位）→ 百万股；§17.8「总股本（百万股） = X」→ 百万股。
    """
    sec16 = _section(pack, "16")
    if not sec16:
        return Finding("I11", "§16↔§17.8 股本口径一致性", UNEVALUATED, "未找到 §16 小节")
    m16 = re.search(r"总股本\s*[\(（]([^)）]+)[\)）]\s*\|\s*\**([\d,]+(?:\.\d+)?)", sec16)
    if not m16:
        return Finding("I11", "§16↔§17.8 股本口径一致性", UNEVALUATED,
                       "§16 未找到「总股本 (单位)」行")
    unit, raw = m16.group(1).strip(), float(m16.group(2).replace(",", ""))
    # 单位 → 百万股的换算因子。**从最具体到最宽泛判定**（"百万股" 含 "万股"）。
    if "百万股" in unit:
        f = 1.0
    elif "亿股" in unit:
        f = 100.0
    elif "万股" in unit:
        f = 0.01
    else:
        return Finding("I11", "§16↔§17.8 股本口径一致性", UNEVALUATED,
                       f"§16 总股本单位 {unit!r} 无法识别")
    a_mm = raw * f

    sec17 = _section(pack, "17.8")
    if not sec17:
        return Finding("I11", "§16↔§17.8 股本口径一致性", UNEVALUATED, "未找到 §17.8 小节")
    m17 = re.search(r"总股本[（(]百万股[)）]\s*=\s*\**([\d,]+(?:\.\d+)?)", sec17)
    if not m17:
        return Finding("I11", "§16↔§17.8 股本口径一致性", UNEVALUATED,
                       "§17.8 未找到「总股本（百万股） = X」声明")
    b_mm = float(m17.group(1).replace(",", ""))

    if b_mm <= 0:
        return Finding("I11", "§16↔§17.8 股本口径一致性", UNEVALUATED, "§17.8 股本非正")
    dev = abs(a_mm - b_mm) / b_mm * 100
    ev = [f"§16 总股本 = {raw:,.2f} {unit} ⇒ {a_mm:,.2f} 百万股",
          f"§17.8 总股本 = {b_mm:,.2f} 百万股", f"相对偏差 = {dev:.4f}%"]
    if dev > 1.0:
        return Finding("I11", "§16↔§17.8 股本口径一致性", FAIL,
                       "同一条「总股本」在两个小节对不上 ⇒ 单位标签或口径之一错了"
                       "（两者都是同一事实，1% 容差已很宽松）", ev)
    return Finding("I11", "§16↔§17.8 股本口径一致性", PASS,
                   f"两处口径一致（偏差 {dev:.4f}%）", ev)


# ---------------------------------------------------------------------------
# §17.15 / §17.16 值级核验（known_issues §四十二）
#
# 为什么需要：这两节**决策相关**（§17.16 表尾自述"作为风险提示输入报告的风险章节"）
# 却在 2026-10-15 的盘点中"无任何值级交叉核验"—— 即一个算错的 §17.15 能通过**全部**
# 包级门禁。00506 的商誉缺陷当时只被**报告级**的 viewpoint_audit 抓到，正是这个缺口的实证。
#
# 本节做三类核验（都与年报无关，纯包内机械可判）：
#   ① 算术自洽   A = 现金 − 有息负债 − 商誉 − 无形；B = 现金 − 有息负债 − 商誉
#   ② 跨小节一致 §17.15 的扣减项必须等于 §4 **最新年报列**的同名科目
#   ③ 比率可复算 A/市值、B/市值 必须能由 §17.8 的市值复算出来
# 另加一条**歧义告警**：商誉/无形在两处都空或零时，"确实没有商誉"与"上游停更"
# 无法区分 —— 记 WARN 提示人工确认（这正是 00506 那类缺陷的形态）。
# ---------------------------------------------------------------------------

def _annual_year(sec4: str) -> Optional[str]:
    """§4 表头里**最新的年报年**（§17.15 用的是年报口径，不是 2026H1 那类中期列）。"""
    m = re.search(r"^\|\s*项目[^|]*\|(.+)\|\s*$", sec4, re.M)
    if not m:
        return None
    for cell in (c.strip() for c in m.group(1).split("|")):
        if re.fullmatch(r"\d{4}", cell):
            return cell
    return None


def _cell_in_col(sec4: str, year: str, *keywords: str) -> Optional[float]:
    """取 §4 中「标签含任一 keyword」的行在 `year` 列的值；缺失/破折号返回 None。"""
    header = None
    for line in sec4.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        if not any(k in cells[0] for k in keywords):
            continue
        try:
            idx = header.index(year)
        except ValueError:
            return None
        if idx >= len(cells):
            return None
        return _num(cells[idx])
    return None


def _blockquote_money(sec: str, *patterns: str) -> Optional[float]:
    """从行内声明里取「… = 1,234.56 百万元」这类金额。

    ⚠️ 必须取**行内最后一个** `= 数值`：§17.15/§17.16 的口径行形如
    `现金 = 货币资金 + 交易性金融资产 = 4,549.48 百万元` —— 第一个 `=` 后面
    跟的是**科目名**而不是数字（初版用 `[^=]*=` 只认第一个 `=` ⇒ 一个数都取不到，
    I12 直接 UNEVALUATED）。
    """
    for pat in patterns:
        for line in sec.splitlines():
            for m in re.finditer(pat, line):
                # 取**该关键字之后第一个** "= 数值"。两个陷阱都要避开：
                #  · 取行内最后一个 ⇒ 错误：§17.15 把「现金 … = 4,549.48；有息负债 = 68.28」
                #    写在**同一行**，取最后一个会把 现金 读成 68.28（实测踩过）；
                #  · 取关键字之后第一个 `=` ⇒ 错误：`现金 = 货币资金 + …` 的第一个 `=`
                #    后面跟的是**科目名**，所以必须要求 `=` 后**紧跟数字**。
                mm = re.search(r"=\s*\**([\d,]+(?:\.\d+)?)", line[m.end():])
                if mm:
                    return float(mm.group(1).replace(",", ""))
    return None


def _table_row_money(sec: str, *labels: str) -> Optional[float]:
    """从表格行里取第 2 列金额（标签含任一 label）。"""
    for line in sec.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells or set("".join(cells)) <= set("-: "):
            continue
        if any(lb in cells[0] for lb in labels):
            return _num(cells[1]) if len(cells) > 1 else None
    return None


def _pct_pair(sec: str, *labels: str, marker: str = ""):
    """取判据行里形如「X vs Y」或「X / Y」的两个数。

    `marker` 用于**精确锚定判据序号**：④ 的文案是「AA_latest/(市值−净现金)」，
    里面**也含「净现金」**，会让按标签找 ⑤ 的调用先命中 ④（实测踩过：
    把 AA_latest=2,115.96 当成了"净现金"，报出一条假 FAIL）。
    """
    for line in sec.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells:
            continue
        if marker and marker not in cells[0]:
            continue
        if not any(lb in cells[0] for lb in labels):
            continue
        nums = [float(x.replace(",", "")) for x in re.findall(r"-?[\d,]+(?:\.\d+)?", " ".join(cells[2:3]) or "")]
        if len(nums) >= 2:
            return nums[0], nums[1]
    return None


def _row_nums(sec: str, marker: str) -> list:
    """取判据行（首列含 `marker`）**数值单元格**里的所有数字。

    为什么必须按单元格取：§17.16 的判据文案会互相包含 —— ④ 写
    「AA_latest/(市值−净现金)」、⑤ 写「净现金 > 市值」，按"行里含市值"抓数字
    会把 ④ 的**分母**（市值−净现金）也当成一个"用到的市值"⇒ 假 FAIL（实测）。
    按单元格 + 判据序号锚定才稳。
    """
    for line in sec.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3 or marker not in cells[0]:
            continue
        # ⚠️ 必须 `-?`：漏掉负号会把 -17,282.44 读成 17,282.44
        # （实测：净现金在 有息负债>现金 时本为负，38 个包因此假 FAIL）
        return [float(x.replace(",", ""))
                for x in re.findall(r"-?[\d,]+(?:\.\d+)?", cells[2])]
    return []


def _row_first_num(sec: str, marker: str):
    nums = _row_nums(sec, marker)
    return nums[0] if nums else None


def invariant_defensive_net_asset(pack: str) -> Finding:
    """I12 §17.15 防御性净值：算术 / §4 一致 / 比率可复算。"""
    sec = _section(pack, "17.15")
    if not sec:
        return Finding("I12", "§17.15 防御性净值核验", UNEVALUATED, "未找到 §17.15 小节")
    sec4 = _section(pack, "4")
    year = _annual_year(sec4) if sec4 else None

    cash = _blockquote_money(sec, r"现金")
    debt = _blockquote_money(sec, r"有息负债")
    gw = _table_row_money(sec, "商誉")
    it = _table_row_money(sec, "无形资产")
    net_a = _table_row_money(sec, "A 严口径")
    net_b = _table_row_money(sec, "B 宽口径")
    if None in (cash, debt, gw, it, net_a, net_b):
        return Finding("I12", "§17.15 防御性净值核验", UNEVALUATED,
                       "§17.15 缺少可核验的数值行（现金/有息负债/商誉/无形资产/A/B）")

    ev, fails, warns = [], [], []
    # ① 算术自洽 —— 「无形资产」行的**成分**决定 A 口径能否把商誉与无形资产相加：
    #   · disjoint（缺省，06862 形态）：两者各自独立 ⇒ A = 现金−债−商誉−无形
    #   · inclusive（06078 形态）：该行是**含商誉的无形资产总计**（附註16 栏目头即
    #     `商譽｜軟件｜合約權利｜醫療執照｜總計`，总计与 §4 行逐位相等、财务状况表无独立
    #     商誉行位）⇒ A 只能 = 现金−债−无形，相加会把商誉**扣两次**。
    # 旧实现只认 disjoint 一种算术 ⇒ 对 inclusive 包要么误报 FAIL、要么（当两式恰好自洽时）
    # 对重复扣减**谎报 PASS**：06078 实测 A 由 −5,408.29 被压到 −8,849.42（虚增一个商誉）。
    _inclusive = "全扣含商誉的无形资产" in sec
    _a_expect = cash - debt - it if _inclusive else cash - debt - gw - it
    # B 宽口径两形态同为「只扣商誉」：inclusive 下商誉单独列示、可辨认无形保留 ⇒ cash−债−商誉；
    # disjoint 下商誉与无形各自独立 ⇒ 也只能扣商誉，否则等于把无形也扣了、与 A 无区别。
    _b_expect = cash - debt - gw
    if abs(_a_expect - net_a) > max(0.02, abs(net_a) * 0.01):
        fails.append(f"A 口径算不平：复算 = {_a_expect:,.2f}，但表内 A = {net_a:,.2f}"
                     f"（成分={'含商誉' if _inclusive else '独立'}）")
    if abs(_b_expect - net_b) > max(0.02, abs(net_b) * 0.01):
        fails.append(f"B 口径算不平：复算 = {_b_expect:,.2f}，但表内 B = {net_b:,.2f}")
    # ② **重叠科目判据**（旧实现完全缺失）：已声明含商誉、却仍把商誉单独减一次 = 重复扣减
    if _inclusive and gw > 0:
        _dup = cash - debt - gw - it
        if abs(net_a - _dup) <= max(0.02, abs(_dup) * 0.01):
            fails.append(f"A 口径**重复扣减商誉**：无形资产行已含商誉 {gw:,.2f}，"
                         f"A 却又单独减了一次（= {_dup:,.2f}）；正确 A = {_a_expect:,.2f}")
    ev.append(f"现金={cash:,.2f} 有息负债={debt:,.2f} 商誉={gw:,.2f} 无形资产={it:,.2f}"
              f"（成分={'含商誉' if _inclusive else '独立'}）")
    ev.append(f"A={net_a:,.2f}（复算 {_a_expect:,.2f}）  B={net_b:,.2f}（复算 {_b_expect:,.2f}）")

    # ② 与 §4 最新年报列一致
    if year:
        g4 = _cell_in_col(sec4, year, "商誉")
        i4 = _cell_in_col(sec4, year, "无形")
        m4 = _cell_in_col(sec4, year, "货币资金", "现金及等价物")
        t4 = _cell_in_col(sec4, year, "交易性金融")
        ev.append(f"§4（{year} 列）：商誉={g4} 无形={i4} 现金类={m4} 交易性={t4}")
        for nm, a, b in (("商誉", gw, g4), ("无形资产", it, i4)):
            if b is None:
                if a == 0:
                    warns.append(f"{nm}：§4 的 {year} 列为空，§17.15 按 0 处理 ⇒ "
                                 f"无法区分「确实没有{nm}」与「上游停更」——需人工确认")
            elif abs(a - b) > max(0.02, abs(b) * 0.01):
                fails.append(f"{nm}与 §4 不一致：§17.15={a:,.2f} vs §4({year})={b:,.2f}")
        # 现金 = 货币资金 + 交易性金融资产
        if m4 is not None:
            expect_cash = m4 + (t4 or 0.0)
            if abs(cash - expect_cash) > max(0.02, abs(expect_cash) * 0.01):
                fails.append(f"现金与 §4 不一致：§17.15={cash:,.2f} vs "
                             f"§4(货币资金+trad_asset)={expect_cash:,.2f}")

    # ③ 比率可复算（用 §17.8 市值）
    cap = _blockquote_money(_section(pack, "17.8"), r"总市值")
    if cap is None:
        mcap = _table_row_money(_section(pack, "17.8"), "总市值")
        cap = mcap
    if cap:
        ev.append(f"§17.8 市值={cap:,.2f}")
        for label, val in (("A 口径 / 市值", net_a), ("B 口径 / 市值", net_b)):
            row = _table_row_money(sec, label)
            if row is None:
                continue
            expect = val / cap * 100
            txt = " ".join(ln for ln in sec.splitlines() if label in ln)
            m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", txt)
            if m and abs(float(m.group(1)) - expect) > 0.15:
                fails.append(f"{label} 不可复算：表内 {m.group(1)}% vs 复算 {expect:.1f}%")
    if fails:
        return Finding("I12", "§17.15 防御性净值核验", FAIL,
                       "§17.15 的数值与自身算术 / §4 / §17.8 对不上", fails + ev)
    if warns:
        return Finding("I12", "§17.15 防御性净值核验", WARN, "；".join(warns), ev)
    return Finding("I12", "§17.15 防御性净值核验", PASS,
                   "算术自洽、与 §4 一致、比率可复算", ev)


def invariant_defensive_gates(pack: str) -> Finding:
    """I13 §17.16 防御性门槛组：现金口径 / 净现金 / 市值 / AA_latest 的跨小节一致。"""
    sec = _section(pack, "17.16")
    if not sec:
        return Finding("I13", "§17.16 防御性门槛组核验", UNEVALUATED, "未找到 §17.16 小节")
    sec1515 = _section(pack, "17.15")
    narrow = _blockquote_money(sec, r"狭义", r"现金口径")
    if narrow is None:
        return Finding("I13", "§17.16 防御性门槛组核验", UNEVALUATED,
                       "§17.16 未声明狭义现金口径")

    ev, fails = [], []
    ev.append(f"§17.16 狭义现金 = {narrow:,.2f}")
    # ① 与 §17.15 的现金一致（两者都是 money_cap + trad_asset）
    c15 = _blockquote_money(sec1515, r"现金") if sec1515 else None
    if c15 is not None:
        ev.append(f"§17.15 现金 = {c15:,.2f}")
        if abs(narrow - c15) > max(0.02, c15 * 0.01):
            fails.append(f"狭义现金与 §17.15 的现金不一致：{narrow:,.2f} vs {c15:,.2f}")
    # ② 净现金 = 现金 − 有息负债（用 §17.1 的有息负债，单点）
    debt = None
    m = re.search(r"有息负债[^|\n]*\|\s*\**([\d,]+(?:\.\d+)?)", _section(pack, "17.1"))
    if m:
        debt = float(m.group(1).replace(",", ""))
    if debt is not None:
        main = _row_nums(sec, "⑤")
        ev.append(f"有息负债(§17.1) = {debt:,.2f}")
        if len(main) >= 2:
            x, _y = main[0], main[1]
            expect = narrow - debt
            if abs(x - expect) > max(0.03, abs(expect) * 0.02):
                fails.append(f"净现金算不平：表内 {x:,.2f} vs 狭义现金−有息负债 = {expect:,.2f}")
    # ③ 与市值直接比较的判据（③ 现金储备/市值、⑤ 净现金 > 市值）里，
    #    数值单元格的**最后一个**数就是市值；它必须等于 §17.8 的市值。
    #    （刻意不含 ④：它的数值是 AA_latest/(市值−净现金)，末位是**分母**而不是市值。）
    cap178 = _blockquote_money(_section(pack, "17.8"), r"总市值")
    caps = []
    for marker in ("③", "⑤"):
        nums = _row_nums(sec, marker)
        if len(nums) >= 2:
            caps.append(nums[-1])
    if cap178 and caps:
        mism = [c for c in caps if abs(c - cap178) > max(0.02, cap178 * 0.01)]
        ev.append(f"§17.8 市值 = {cap178:,.2f}；§17.16 的 ③/⑤ 用 {caps}")
        if mism:
            fails.append(f"§17.16 用的市值与 §17.8 不一致：{mism} vs {cap178:,.2f}")
    # ④ AA_latest 必须等于 §17.5 的 AA_latest
    # ⑥ 的数值单元格是「AA_latest vs 拟派息」⇒ 第一个数是 AA_latest。
    # （原先用 `AA_latest[^|]*\|\s*(\d)` 松散匹配：`AA_latest` 与数字之间隔着
    #   「≥ 拟派息 | 因子2 |」两个竖线，`[^|]*` 只跨一个 ⇒ 永远匹配不到，检查被静默跳过。）
    aa16 = _row_first_num(sec, "⑥")
    sec175 = _section(pack, "17.5")
    m = re.search(r"AA_latest[^=]*=\s*\**([\d,]+(?:\.\d+)?)", sec175)
    if m and aa16 is not None:
        aa5 = float(m.group(1).replace(",", ""))
        ev.append(f"AA_latest：§17.16={aa16:,.2f} vs §17.5={aa5:,.2f}")
        if abs(aa16 - aa5) > max(0.02, aa5 * 0.01):
            fails.append(f"AA_latest 与 §17.5 不一致：{aa16:,.2f} vs {aa5:,.2f}")
    if fails:
        return Finding("I13", "§17.16 防御性门槛组核验", FAIL,
                       "§17.16 的输入与 §17.1/§17.5/§17.8/§17.15 对不上", fails + ev)
    return Finding("I13", "§17.16 防御性门槛组核验", PASS,
                   "现金口径、净现金、市值、AA_latest 均与上游小节一致", ev)


# ---------------------------------------------------------------------------
# §17.3 / §17.4 / §17.7 值级核验（known_issues §四十四）
#
# 这三节在 2026-10-15 的盘点里是"无任何值级交叉核验"的最后三节。
# 补之前先**实证**了公式（对全部 107 个产物验算，全部成立）：
#   §17.3 真实现金收入 = S − max(0, ΔAR) − max(0, −ΔCL)   ← **非对称保守**规则
#   §17.4 W 合计 = W1+W2+W3+W4
#   §17.7 净现金 = 现金 − 有息负债；子公司占比 = (合并−母公司)/合并
# 所以这三节本身没算错；不变式的价值是**回归保护**与**跨小节一致**。
# ---------------------------------------------------------------------------

def _float_or_none(x):
    n = _num(x)
    return n


def invariant_real_cash_revenue(pack: str) -> Finding:
    """I14 §17.3 真实现金收入：保守规则 + 收款比率可复算。"""
    sec = _section(pack, "17.3")
    if not sec:
        return Finding("I14", "§17.3 真实现金收入核验", UNEVALUATED, "未找到 §17.3 小节")
    rows = [l.strip() for l in sec.splitlines()
            if l.strip().startswith("|") and re.match(r"\|\s*\d{4}", l.strip())]
    if not rows:
        return Finding("I14", "§17.3 真实现金收入核验", UNEVALUATED, "§17.3 无逐年数据行")
    fails, ev, n = [], [], 0
    for line in rows:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        year, s_, t_, u_, real, ratio = (cells[0], _num(cells[1]), _num(cells[2]),
                                        _num(cells[3]), _num(cells[4]), _num(cells[5]))
        if None in (s_, t_, u_, real):
            continue
        n += 1
        # 保守规则：扣 AR 增加、扣 CL 减少；**不加回** AR 减少与 CL 增加
        expect = s_ - max(0.0, t_) - max(0.0, -u_)
        if abs(expect - real) > max(0.03, abs(real) * 0.001):
            fails.append(f"{year}：S−max(0,ΔAR)−max(0,−ΔCL) = {expect:,.2f}"
                         f"，但表内真实现金收入 = {real:,.2f}")
        if real > s_ + max(0.03, abs(s_) * 0.001):
            fails.append(f"{year}：真实现金收入 {real:,.2f} **超过**营业收入 {s_:,.2f}"
                         f"（保守基准不应超过）")
        if ratio is not None and s_:
            exp_r = real / s_ * 100
            if abs(exp_r - ratio) > 0.15:
                fails.append(f"{year}：收款比率不可复算 —— 表内 {ratio}% vs {exp_r:.2f}%")
        if len(ev) < 3:
            ev.append(f"{year}: S={s_:,.2f} ΔAR={t_:,.2f} ΔCL={u_:,.2f} ⇒ {real:,.2f}")
    if not n:
        return Finding("I14", "§17.3 真实现金收入核验", UNEVALUATED, "§17.3 无完整数据行")
    if fails:
        return Finding("I14", "§17.3 真实现金收入核验", FAIL,
                       "§17.3 与自身的保守规则不符", fails[:8] + ev)
    return Finding("I14", "§17.3 真实现金收入核验", PASS,
                   f"{n} 个年度均满足保守规则且收款比率可复算", ev)


def invariant_operating_cash_outflow(pack: str) -> Finding:
    """I15 §17.4 经营性现金支出：W 合计 = W1+W2+W3+W4，且各项非负。"""
    sec = _section(pack, "17.4")
    if not sec:
        return Finding("I15", "§17.4 经营性现金支出核验", UNEVALUATED, "未找到 §17.4 小节")
    rows = [l.strip() for l in sec.splitlines()
            if l.strip().startswith("|") and re.match(r"\|\s*\d{4}", l.strip())]
    if not rows:
        return Finding("I15", "§17.4 经营性现金支出核验", UNEVALUATED, "§17.4 无逐年数据行")
    fails, ev, n = [], [], 0
    for line in rows:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        year = cells[0]
        vals = [_num(c) for c in cells[1:6]]
        if None in vals:
            continue
        n += 1
        w1, w2, w3, w4, tot = vals
        if abs((w1 + w2 + w3 + w4) - tot) > max(0.03, abs(tot) * 0.001):
            fails.append(f"{year}：W1..W4 之和 = {w1 + w2 + w3 + w4:,.2f}"
                         f"，但表内 W 合计 = {tot:,.2f}")
        # 刻意**不**断言各项非负：W3 是现金税（可因退税为负）、W4 是利息
        # （可因利息收入>支出为负）。初版加了"现金支出不应为负"，在 61/107 个产物上
        # 误报 —— 那是**未经证实的假设**，不是发现的缺陷。只保留经验证过的求和恒等式。
        if len(ev) < 2:
            ev.append(f"{year}: {w1:,.2f}+{w2:,.2f}+{w3:,.2f}+{w4:,.2f} = {tot:,.2f}")
    if not n:
        return Finding("I15", "§17.4 经营性现金支出核验", UNEVALUATED, "§17.4 无完整数据行")
    if fails:
        return Finding("I15", "§17.4 经营性现金支出核验", FAIL,
                       "§17.4 的 W 合计与各项之和不符", fails[:8] + ev)
    return Finding("I15", "§17.4 经营性现金支出核验", PASS,
                   f"{n} 个年度 W 合计 = W1+W2+W3+W4", ev)


def invariant_holding_structure(pack: str) -> Finding:
    """I16 §17.7 控股结构辅助：净现金算术、子公司占比、与 §17.1/§4 一致。"""
    sec = _section(pack, "17.7")
    if not sec:
        return Finding("I16", "§17.7 控股结构辅助核验", UNEVALUATED, "未找到 §17.7 小节")
    rows = {}
    for line in sec.splitlines():
        st = line.strip()
        if not st.startswith("|"):
            continue
        cells = [c.strip() for c in st.strip("|").split("|")]
        # 分隔行判据：**是** `-: ` 的子集才跳过。初版写成 `>`（超集）⇒ 恒为 False，
        # 连正常数据行都被丢弃，I16 在 107/107 个产物上 UNEVALUATED（静默失效）。
        if len(cells) >= 3 and not (set("".join(cells)) <= set("-: ")):
            rows[cells[0]] = cells[1:]
    if not rows:
        return Finding("I16", "§17.7 控股结构辅助核验", UNEVALUATED, "§17.7 无表格行")

    def pick(*keys):
        for k in keys:
            for lab, vals in rows.items():
                if k in lab:
                    return _num(vals[0]), (_num(vals[1]) if len(vals) > 1 else None), lab
        return None, None, None

    ibd_c, ibd_p, ibd_lab = pick("有息负债")
    cash_c, cash_p, cash_lab = pick("现金")
    nc_c, nc_p, nc_lab = pick("净现金")
    sub, _x, sub_lab = pick("子公司")
    if None in (ibd_c, ibd_p, cash_c, cash_p, nc_c, nc_p):
        return Finding("I16", "§17.7 控股结构辅助核验", UNEVALUATED,
                       "§17.7 缺少可核验的数值行（有息负债/现金/净现金）")

    fails, ev = [], []
    ev.append(f"{ibd_lab}={ibd_c:,.2f}/{ibd_p:,.2f} {cash_lab}={cash_c:,.2f}/{cash_p:,.2f}")
    if abs((cash_c - ibd_c) - nc_c) > max(0.03, abs(nc_c) * 0.01):
        fails.append(f"合并净现金算不平：{cash_c:,.2f}−{ibd_c:,.2f} = {cash_c - ibd_c:,.2f}"
                     f"，但表内 = {nc_c:,.2f}")
    if abs((cash_p - ibd_p) - nc_p) > max(0.03, abs(nc_p) * 0.01):
        fails.append(f"母公司净现金算不平：{cash_p:,.2f}−{ibd_p:,.2f} = {cash_p - ibd_p:,.2f}"
                     f"，但表内 = {nc_p:,.2f}")
    if sub is not None and ibd_c:
        exp = (ibd_c - ibd_p) / ibd_c * 100
        ev.append(f"{sub_lab}={sub}% 复算={exp:.1f}%")
        if abs(exp - sub) > 0.15:
            fails.append(f"子公司层面负债占比不可复算：复算 {exp:.1f}% vs 表内 {sub}%")
    # 跨小节：有息负债（合并）必须等于 §17.1 的单点
    m = re.search(r"有息负债[^|\n]*\|\s*\**([\d,]+(?:\.\d+)?)", _section(pack, "17.1"))
    if m:
        ibd1 = float(m.group(1).replace(",", ""))
        ev.append(f"§17.1 有息负债 = {ibd1:,.2f}")
        if abs(ibd_c - ibd1) > max(0.03, abs(ibd1) * 0.01):
            fails.append(f"有息负债与 §17.1 不一致：§17.7={ibd_c:,.2f} vs §17.1={ibd1:,.2f}")
    # 跨小节：本节的「现金」只取 money_cap ⇒ 必须等于 §4 的货币资金（其自身声明口径）
    sec4 = _section(pack, "4")
    year = _annual_year(sec4) if sec4 else None
    if year:
        m4 = _cell_in_col(sec4, year, "货币资金", "现金及等价物")
        if m4 is not None:
            ev.append(f"§4（{year}）货币资金 = {m4:,.2f}")
            if abs(cash_c - m4) > max(0.03, abs(m4) * 0.01):
                fails.append(f"§17.7 的现金与 §4 货币资金不一致：{cash_c:,.2f} vs {m4:,.2f}")
    if fails:
        return Finding("I16", "§17.7 控股结构辅助核验", FAIL,
                       "§17.7 的算术或跨小节口径对不上", fails + ev)
    return Finding("I16", "§17.7 控股结构辅助核验", PASS,
                   "净现金算术自洽、子公司占比可复算、与 §17.1/§4 一致", ev)


def invariant_volume_scale_consistency(pack: str) -> Finding:
    """I17 §11「周均成交量」与 §2「日均成交量」的量级必须相容（跨小节单位核验）。

    为什么新增（known_issues §五十）：Tushare `weekly.vol` 的单位是**股**，
    而 `daily.vol` 是**手**（1 手 = 100 股）。原先 §11 取 `mean(vol)` 却把列标成「(手)」
    ⇒ **100 倍**误标（实测 000721/000708：周均/日均 = 455，正确应≈5）。
    §11 此前**不受任何门禁覆盖**，所以这个错误长期存活 —— 与 §16 的单位误标同一模式。

    判据：周均(手) ÷ 日均(手) 应落在 [2, 12]（一周 5 个交易日；放宽以容纳停牌、
    节假日与「日均」取的是 52 周窗口而「周均」按自然年分组的口径差异）。
    超出则该判 FAIL —— 足以抓住 ×100 这类量级错，又不会因口径细节误报。
    """
    sec2, sec11 = _section(pack, "2"), _section(pack, "11")
    if not sec2 or not sec11:
        return Finding("I17", "§11↔§2 成交量量级一致性", UNEVALUATED, "未找到 §2 或 §11")
    m2 = re.search(r"日均成交量[^|]*\|\s*\**([\d,]+(?:\.\d+)?)", sec2)
    if not m2:
        return Finding("I17", "§11↔§2 成交量量级一致性", UNEVALUATED, "§2 未找到日均成交量")
    day = float(m2.group(1).replace(",", ""))
    # §11 年度汇总：取第一个年度行的第 5 列（周均成交量）
    week = None
    for line in sec11.splitlines():
        st = line.strip()
        if not st.startswith("|") or not re.match(r"\|\s*\d{4}\s*\|", st):
            continue
        cells = [c.strip() for c in st.strip("|").split("|")]
        if len(cells) >= 5:
            week = _num(cells[4])
            if week is not None:
                break
    if week is None:
        return Finding("I17", "§11↔§2 成交量量级一致性", UNEVALUATED, "§11 未找到年度周均成交量")
    if not day:
        return Finding("I17", "§11↔§2 成交量量级一致性", UNEVALUATED, "§2 日均量为 0")
    ratio = week / day
    ev = [f"§2 日均 = {day:,.0f}（手）", f"§11 周均 = {week:,.0f}（标为手）",
          f"周均/日均 = {ratio:.1f}（预期 ≈5）"]
    if not (2.0 <= ratio <= 12.0):
        return Finding("I17", "§11↔§2 成交量量级一致性", FAIL,
                       "两节的成交量量级不相容 ⇒ 单位标签之一错了"
                       "（weekly.vol 是「股」、daily.vol 是「手」，差 100 倍）", ev)
    return Finding("I17", "§11↔§2 成交量量级一致性", PASS, f"量级相容（比值 {ratio:.1f}）", ev)


INVARIANTS = (
    invariant_cagr_periods,
    invariant_income_caliber,
    invariant_per_share_currency,
    invariant_cash_tie_out,
    invariant_dividend_integrity,
    invariant_sensitivity_direction,
    invariant_price_consistency,
    invariant_balance_identity,
    invariant_share_count_consistency,
    invariant_constant_history_columns,
    invariant_share_count_across_sections,
    invariant_defensive_net_asset,
    invariant_defensive_gates,
    invariant_real_cash_revenue,
    invariant_operating_cash_outflow,
    invariant_holding_structure,
    invariant_volume_scale_consistency,
)



def run_all(pack: str, fx: Optional[dict] = None) -> List[Finding]:
    findings: List[Finding] = []
    for fn in INVARIANTS:
        try:
            findings.append(fn(pack, fx) if fn is invariant_per_share_currency else fn(pack))
        except Exception as exc:                       # noqa: BLE001 — 引擎自身故障必须可见
            findings.append(Finding(getattr(fn, "__name__", "?"), "不变式执行失败",
                                    FAIL, f"{type(exc).__name__}: {exc}"))
    return findings


def render(findings: Sequence[Finding]) -> str:
    icon = {PASS: "✅", FAIL: "❌", WARN: "⚠️", UNEVALUATED: "❓"}
    lines = []
    for f in findings:
        lines.append(f"{icon.get(f.status, '?')} [{f.code}] {f.title} — {f.status}")
        lines.append(f"    {f.detail}")
        for e in f.evidence[:8]:
            lines.append(f"      · {e}")
    fails = [f for f in findings if f.status == FAIL]
    unk = [f for f in findings if f.status == UNEVALUATED]
    lines.append("")
    lines.append(f"汇总：FAIL {len(fails)} · UNEVALUATED {len(unk)} · "
                 f"PASS {sum(1 for f in findings if f.status == PASS)}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="数据包级不变式引擎（fail-closed）")
    ap.add_argument("--pack", required=True, help="data_pack_market.md 路径")
    ap.add_argument("--fx", default=None, help="fx.json（判断每股价币种是否需要折算）")
    ap.add_argument("--json", dest="json_out", default=None, help="把结果写入 JSON")
    ap.add_argument("--strict", action="store_true", help="UNEVALUATED 也计为失败")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        pack = open(args.pack, encoding="utf-8").read()
    except OSError as exc:
        print(f"Error: 无法读取数据包 {args.pack}: {exc}", file=sys.stderr)
        return 2
    fx = None
    if args.fx:
        try:
            fx = json.load(open(args.fx, encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error: 无法读取 fx.json: {exc}", file=sys.stderr)
            return 2

    findings = run_all(pack, fx)
    if not args.quiet:
        print(render(findings))
    if args.json_out:
        payload = {"pack": args.pack, "findings": [f.to_dict() for f in findings]}
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    fails = [f for f in findings if f.status == FAIL]
    if fails or (args.strict and any(f.status == UNEVALUATED for f in findings)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
