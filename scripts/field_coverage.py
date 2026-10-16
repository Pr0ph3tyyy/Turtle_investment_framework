#!/usr/bin/env python3
"""field_coverage.py —— 数据包**字段覆盖**门禁（补上"自洽 ≠ 完整"这个洞）。

为什么需要
----------
`invariants.py` 的 I1–I7 全部检查**包内自洽**（A−B 是否等于 C、两处价格是否一致）。
它们**无法发现"包里少了一行"**。06862 实测的 P0 缺口正属此类：

  · 上游 `hk_balancesheet` **有** `应付债券 = 2,028,881 千元`（2025-12-31）；
  · 但 `HK_BALANCE_MAP["bond_payable"]` 被写成了 `'应付票据(非流动)'`（**标签张冠李戴**）；
  · 于是 §4 的「应付债券」行**永远为空**（该行确实在白名单里 → 所有 I1–I7 绿灯）；
  · 有息负债只算到银行借款 399.18，广义净现金 6,203.16 被高估 20.29 亿元。

三条检查
--------
- **C1 通用必填行**：关键报表必须具备的最小行集合；缺失即 FAIL。
- **C2 债务构成可核验**：`有息负债 > 0` 时，各债类（短期借款/长期借款/债券票据/租赁负债）
  必须在 §4 **各自成行**（值可为 0）——否则"没有"与"漏渲染"不可区分。
- **C3 声明行不得全空**：白名单里声明了的债务行，若在**所有年度都为空/缺失** ⇒ FAIL。
  这条正是"标签未命中上游"的机械探测器（06862 的债券行即如此）。

CLI::

    python3 scripts/field_coverage.py --pack output/06862_海底捞/data_pack_market.md
    python3 scripts/field_coverage.py --pack … --json field_coverage.json --strict

退出码：0 无 FAIL · 1 存在 FAIL · 2 输入不可读；`--strict` 下 UNEVALUATED 亦计失败。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Sequence, Tuple

FAIL, WARN, PASS, UNEVALUATED = "FAIL", "WARN", "PASS", "UNEVALUATED"


@dataclass
class Finding:
    code: str
    title: str
    status: str
    detail: str
    evidence: List[str] = field(default_factory=list)
    # 交付路径（--strict）下的升级状态：默认容忍、交付时阻断。
    # 为什么需要：某些信号（如"债类显式全为 0.00"）在包内**无法与漏映射区分**，
    # 判 FAIL 会造成大面积假阳性；但把交付门禁放行又留下洗白路径。
    # 拆成"默认 WARN / 严格 FAIL"两个语义档，两个问题同时解决。
    strict_status: Optional[str] = None

    def effective_status(self, strict: bool = False) -> str:
        return (self.strict_status or self.status) if strict else self.status

    def to_dict(self) -> dict:
        return asdict(self)


# ── 通用必填行：关键报表的最小行集合 ──────────────────────────────
# ⚠️ 别名必须**同时覆盖港股按性质列报与 A 股按功能列报**两套标签。
# 只写港股标签会让每个 A 股包都缺行 ⇒ 假 FAIL；而 C1 是**阻断级**门禁，
# 假阳性会让 --allow-degraded 变成常态，门禁随即失效（I8 的子串误判即前车之鉴）。
# 实测 A 股标签：利润总额 / 所得税费用 / 净利润 / 投资活动现金流 / 筹资活动现金流。
REQUIRED_ROWS: Dict[str, List[Tuple[str, List[str]]]] = {
    "3": [
        ("营业收入", ["营业额", "营业收入", "营运收入"]),
        ("除税前溢利", ["除税前溢利", "利润总额", "税前利润"]),
        ("税项", ["税项", "所得税费用", "所得税"]),
        ("除税后溢利", ["除税后溢利", "持续经营业务税后利润", "净利润"]),
        ("归母净利润", ["股东应占溢利", "归母净利润", "归属母公司", "归属于母公司"]),
    ],
    "4": [
        ("现金类", ["现金及等价物", "现金及现金等价物", "货币资金"]),
        ("总资产", ["总资产"]),
        ("总负债", ["总负债"]),
        ("股东权益", ["股东权益", "归母权益", "所有者权益"]),
        ("短期借款", ["短期贷款", "短期借款"]),
    ],
    "5": [
        ("经营现金流", ["经营业务现金净额", "经营活动产生的现金流量净额", "经营活动现金流",
                        "OCF"]),
        ("投资现金流", ["投资业务现金净额", "投资活动产生的现金流量净额", "投资活动现金流"]),
        ("融资现金流", ["融资业务现金净额", "筹资活动产生的现金流量净额", "筹资活动现金流"]),
        ("资本开支", ["资本开支", "资本支出", "购建固定资产"]),
    ],
}

# 债类：canonical → 可接受的 §4 行标签（命中任一即可）
# 注意：`使用权资产` **不是**负债科目（它是使用权资产的账面值），列在这里会
# 把资产行当成租赁负债，使"求和勾稽"凭空变大 —— 故不收录。
DEBT_CLASSES: Dict[str, List[str]] = {
    "短期借款": ["短期贷款", "短期借款", "短期负债"],
    "长期借款": ["长期贷款", "长期借款"],
    "债券/票据": ["应付债券", "应付票据(非流动)", "应付票据", "优先票据"],
    "租赁负债": ["融资租赁负债", "租赁负债"],
}

# 锚点必须是**金额行**：`有息负债/总资产（%）` 这类比率行被剥掉 % 后同样能通过
# 数字正则，会给出一个量纲完全不同的"锚点"（实测 13.72），必须在标签层排除。
_ANCHOR_EXCLUDE = ("%", "率", "总资产", "占比", "同比")


# ── markdown 表格解析 ────────────────────────────────────────────
def _section(text: str, number: str) -> str:
    r"""取小节段落 —— **委托给单点** `pack_metrics.section_text`（known_issues §五十一）。

    本函数被 `numeric_audit` 一并 import，所以修这里同时修三处消费者。
    原实现「遇到下一个 `##`/`###` 就停」是**层级盲**的：`## N.` 里的 `###` 子块
    会被当成边界，导致该小节后半段**静默读不到**。
    """
    from pack_metrics import section_text
    return section_text(text, number)


def _tables(text: str) -> List[Tuple[List[str], List[List[str]]]]:
    out, block = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            block.append(cells)
        elif block:
            if len(block) >= 2:
                out.append((block[0], block[1:]))
            block = []
    if len(block) >= 2:
        out.append((block[0], block[1:]))
    return out


def _row_of(rows: List[List[str]], aliases: Sequence[str]) -> Optional[List[str]]:
    for r in rows:
        if r and any(a in r[0] for a in aliases):
            return r
    return None


def _rows_of(rows: List[List[str]], aliases: Sequence[str]) -> List[List[str]]:
    """所有命中别名的行（不止第一行）。

    为什么必须返回全部：一个债类往往被拆成多行（如「融资租赁负债(流动)」+
    「融资租赁负债(非流动)」）。`_row_of` 只取第一行 —— 若流动行全年度为空、
    非流动行有值，整类会被误判为「声明但全空」而触发假 FAIL（00696 实测）。
    判定"全空"必须看**所有**命中行：任一行有值 ⇒ 该类可核验。
    """
    return [r for r in rows if r and any(a in r[0] for a in aliases)]


def _numeric_cells(row: Optional[List[str]]) -> List[float]:
    """取一行里的数值单元格 —— **委托单点** `pack_metrics.num`（known_issues §三十三）。

    原实现自带一份：`c.replace(...)` + `fullmatch(r"-?\d+...")`。
    与单点相比它会**漏掉全角负号**（`−1,234.5`），而那是真实出现过的输入形态。
    """
    if not row:
        return []
    from pack_metrics import num as _n
    return [v for v in (_n(c) for c in row[1:]) if v is not None]


def _table_with_row(text: str, aliases: Sequence[str]):
    for h, rows in _tables(text):
        if _row_of(rows, aliases):
            return h, rows
    return None, None


# ── 三条检查 ─────────────────────────────────────────────────────
def check_required_rows(pack: str) -> Finding:
    missing: List[str] = []
    for sec, reqs in REQUIRED_ROWS.items():
        body = _section(pack, sec)
        if not body:
            missing.append(f"§{sec} 整节缺失")
            continue
        _h, rows = _table_with_row(body, [a for _c, al in reqs for a in al])
        if rows is None:
            # 该节可能有多张表（如 §3 附表），退化为全文查找
            rows = [r for _h, rs in _tables(body) for r in rs]
        for canon, aliases in reqs:
            if _row_of(rows, aliases) is None:
                missing.append(f"§{sec} 缺行「{canon}」")
    if missing:
        return Finding("C1", "通用必填行覆盖", FAIL,
                       "关键报表缺少必备行 —— 缺失行对 I1–I7 不可见（它们只查自洽、不查完整）",
                       missing)
    return Finding("C1", "通用必填行覆盖", PASS, "§3/§4/§5 必备行齐备")


def _debt_anchor(sec4: str, sec17: str) -> Tuple[Optional[float], Optional[str]]:
    """有息负债锚点：只接受**金额行**，拒绝 `有息负债/总资产（%）` 一类比率行。

    为什么必须挑剔：`_numeric_cells` 会把 `%` 剥掉，于是比率行（13.72）与金额行
    （2,428.06）在数字层面无法区分；实测把金额行删掉后，比率行会被当成锚点，
    门禁给出与自身证据矛盾的 PASS。
    """
    for text in (sec4, sec17):
        for _h, rows in _tables(text):
            for r in rows:
                if not r or "有息负债" not in r[0]:
                    continue
                if any(x in r[0] for x in _ANCHOR_EXCLUDE):
                    continue
                vals = _numeric_cells(r)
                if vals:
                    return max(vals), r[0]
    return None, None


def check_debt_composition(pack: str, absence: Optional[Dict[str, dict]] = None) -> Finding:
    """C2 + C3：债务构成可核验 + 声明的债务行不得全空 + 债类求和不得低于锚点。

    ``absence`` 是采集器写出的 ``field_absence.json`` 的 ``fields``（见
    ``_record_debt_absence``）。它提供**上游科目清单**层面的证据，使
    「这家发行人真的没有银行借款」与「映射标签没命中上游」（06862 的 P0）可分：

      · ``verified_absent``（港股/美股行项目长表）：整张资产负债表的 ind_name
        集合里没有该科目名 ⇒ 空行是**正确表示**，降级为披露（WARN）；
      · ``reported_zero``：上游显式给 0（只出现在 A 股宽表）⇒ 仍按 WARN；
      · ``unreported`` 或**没有证据**：即使空行存在也不足以放行 ⇒ 原样 FAIL
        （fail-closed：删掉 sidecar 不是洗白路径）。
    """
    sec4 = _section(pack, "4")
    sec17 = _section(pack, "17.1")
    if not sec4 or not sec17:
        return Finding("C2", "债务构成可核验", UNEVALUATED, "未找到 §4 或 §17.1")
    _h4, rows4 = _table_with_row(sec4, ["现金", "货币资金"])
    if rows4 is None:
        return Finding("C2", "债务构成可核验", UNEVALUATED, "§4 未解析到资产负债表")

    debt_total, anchor_label = _debt_anchor(sec4, sec17)
    absence = absence or {}

    present, empty, absent, zero_only = [], [], [], []
    absent_verified: List[str] = []
    class_total: Dict[str, float] = {}
    for canon, aliases in DEBT_CLASSES.items():
        rows = _rows_of(rows4, aliases)
        if not rows:
            absent.append(canon)
            class_total[canon] = 0.0
            continue
        # 同一债类可能是**多行**（新增的「融资租赁负债(流动)」+「(非流动)」就是这种情况）。
        # 旧实现 `max(vals)` 只取到较大的那一行 ⇒ 与已含租赁的 §17.1 锚点对不上，
        # 对一份**完全正确**的包报 C2 FAIL（独立审计 P1，复现：锚点 3,300 vs 求和 2,300）。
        # 正确语义：某年该债类所有行的**合计**，再取各年最大值 ——
        # 「取最大值」是跨年约定（见证据行），不是跨行约定。
        rows_total = 0.0
        for _r in rows:
            _cells = _numeric_cells(_r)
            rows_total += max(_cells) if _cells else 0.0
        class_total[canon] = rows_total
        vals = [v for r in rows for v in _numeric_cells(r)]
        if not vals:
            # 所有命中行都无值 ⇒ 「声明但全空」。是否放行取决于**上游科目清单**的证据
            # （06862 的「应付债券」在上游 ind_name 里存在，永远拿不到 verified_absent）。
            if _verified_absent(absence, rows):
                absent_verified.append(canon)
                present.append(canon)
            else:
                empty.append(canon)
        elif all(v == 0 for v in vals):
            # 显式 0.00：与"映射未命中后渲染成 0"在**包内不可区分**。
            # 这里只 WARN 不 FAIL —— 真实为零的长期借款极常见，判 FAIL 会造成
            # 大面积假阳性（I8 的子串误判就是前车之鉴）。把不确定性显式披露，
            # 而不是假装已核验。
            zero_only.append(canon)
            present.append(canon)
        else:
            present.append(canon)

    ev = [f"§17.1 有息负债锚点（金额行）= {debt_total if debt_total is not None else '未取到'}"
          + (f"（行标签：{anchor_label}）" if anchor_label else ""),
          f"§4 已列债类：{present or '无'}",
          f"§4 声明但全空：{empty or '无'}",
          f"§4 声明为空但上游科目清单证实不存在（披露级）：{absent_verified or '无'}",
          f"§4 显式全为 0.00（无法与漏映射区分）：{zero_only or '无'}",
          f"§4 无对应行：{absent or '无'}",
          f"债类求和（各类最大年度值）= {sum(class_total.values()):,.2f}"]

    # C3（最强）：声明的债务行在所有年度都为空，且**上游科目清单里存在该科目**
    # ⇒ 标签未命中上游（06862 的债券行即如此）。
    if empty:
        return Finding("C2", "债务构成可核验", FAIL,
                       "§4 出现**声明了却全年度为空**的债务行 —— 典型是字段映射标签未命中上游"
                       "（06862 实测：`bond_payable` 被错映射为『应付票据(非流动)』，"
                       "上游真实标签是『应付债券』⇒ 债券值永远落不进来、有息负债漏计 20.29 亿元）",
                       ev)

    # 锚点取不到 ⇒ **不可核验 ≠ 通过**（旧的 `debt_total is None` 直接落到 PASS，
    # 于是"删掉锚点行"就是一条洗白路径）。
    if debt_total is None:
        return Finding("C2", "债务构成可核验", FAIL,
                       "取不到「有息负债」的**金额行**锚点 ⇒ 债务构成无法核验。"
                       "不得因为取不到值而判通过；请检查 §4/§17.1 是否被删行或只保留了比率行",
                       ev)

    # C2：有息负债 > 0 时，各债类必须各自成行（值可为 0）
    if debt_total > 0 and absent:
        return Finding("C2", "债务构成可核验", FAIL,
                       "有息负债 > 0，但 §4 缺少某些债类的行 ⇒ 无法区分『该债类为零』"
                       "与『该行被漏渲染』；必须显式列行（值可为 0）",
                       ev + [f"缺失债类：{absent}"])

    # C2′：各债类之和不得**低于**锚点（少了就是有类被漏/被清零）。
    # 只做单边检查：短期借款常含一年内到期的长期借款，求和大于锚点是正常的。
    present_total = sum(class_total.values())
    if debt_total > 0 and present_total < debt_total * 0.95:
        return Finding("C2", "债务构成可核验", FAIL,
                       "已列债类之和低于 §17.1 的「有息负债」锚点 ⇒ 至少一个债类被漏渲染"
                       "或被渲染成 0（求和方向只有单边意义：大于锚点属正常，小于锚点必是漏计）",
                       ev + [f"求和 {present_total:,.2f} < 锚点 {debt_total:,.2f}"])

    if zero_only:
        return Finding("C2", "债务构成可核验", WARN,
                       "债类行齐备、锚点可取、求和勾稽通过；但下列债类在**所有年度都渲染为 "
                       "0.00** —— 这与『上游标签未命中、值落不进来』在包内不可区分。"
                       "真实为零时无需处理；交付路径（--strict）下按 FAIL 处理，"
                       "请对照年报附注确认后显式声明",
                       ev, strict_status=FAIL)
    if absent_verified:
        # 上游科目清单里根本没有该科目 ⇒ 空行是**正确表示**，不是映射缺口。
        # 披露级而非 WARN 级：证据是可核验的（不依赖年报人工比对），因此
        # --strict 不应把它升级为 FAIL —— 否则 40% 的无借款发行人都会阻断交付，
        # 门禁随即失效（field_coverage.py 开头记录的就是这个教训）。
        return Finding("C2", "债务构成可核验", PASS,
                       "债类行齐备、锚点可取、求和勾稽通过；下列债类在全年度为空，"
                       "但**上游行项目清单里没有该科目名** ⇒ 空行表示『该负债不存在』，"
                       "不是映射缺口（证据见 field_absence.json）",
                       ev + ["披露：空行债类（上游已证实不存在）=" + str(absent_verified)])
    return Finding("C2", "债务构成可核验", PASS, "债类行齐备、锚点可取、求和勾稽通过", ev)


def _load_field_absence(path: str) -> Dict[str, dict]:
    """读 ``{output_dir}/field_absence.json`` 的 ``fields``；读不到就返回空（fail-closed）。

    为什么读不到要返回空而不是当作"全部已证实"：否则删掉 sidecar 就能让 C2 放行，
    这正是门禁最容易被绕过的方式。缺失 ⇒ C2 保持原有 FAIL 语义。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return {}
    fields = payload.get("fields")
    return fields if isinstance(fields, dict) else {}


def _verified_absent(absence: Dict[str, dict], rows: Sequence[Sequence[str]]) -> bool:
    """这些 §4 行全部空时，上游科目清单是否**证实**它们不存在？

    键是 ``{section}.{field}``，值里带 ``row_labels``；这里用行标签反查（C2 手上只有
    行文本）。判定要求：该 field 的证据是 ``verified_absent``，且它的某个 row_label
    出现在本次命中的行里。``reported_zero``/``unreported`` 一律不算。
    """
    labels_seen = {r[0] for r in rows if r}
    for entry in absence.values():
        if not isinstance(entry, dict) or entry.get("status") != "verified_absent":
            continue
        for lbl in (entry.get("row_labels") or []):
            if any(lbl in seen for seen in labels_seen):
                return True
    return False


#: §4 里**决策相关**的关键行 —— 它们各自喂一个下游小节，缺一个就静默算错。
#: 为什么单列（known_issues §四十三）：Tushare 对**不存在的字段名静默忽略**，
#: 请求 `intang_assets`（正确名是 `intan_assets`）导致 §4「无形资产」行
#: **100/103 个产物全空**，而 §17.15 的 A 严口径就把它按 0 扣除
#: ⇒ 系统性高估下行净值（000708 实测高估 71.8 亿），且当时 §17.15 无门禁，无人发现。
#: 所以"声明了但从未有值"必须被机械发现 —— C2 对债类做了这件事，这里对 §4 做同样的事。
SECTION4_KEY_ROWS = (
    ("无形资产", "§17.15 A 严口径的扣减项"),
    ("商誉", "§17.15 两口径的扣减项"),
    ("货币资金", "§17.15/§17.16 现金口径与 §17.8"),
    ("现金及等价物", "§17.15/§17.16 现金口径（港股标签）"),
    ("总资产", "§17.8 商誉/总资产、§17.15 比率分母"),
    ("总负债", "§17.8 净负债口径"),
)


def _num_single(v):
    """用**单点**解析器读单元格（`pack_metrics.num`）。

    刻意不在这里再写一份 `_num`：本仓库的教训是"同一语义多份实现 ⇒ 多读取器故障"
    （known_issues §三十三）。`—`/空/`N/A` 一律 None，**不是 0**。
    """
    try:
        from pack_metrics import num as _n
        return _n(v)
    except Exception:  # noqa: BLE001
        return None


def check_section4_key_rows(pack: str) -> Finding:
    """C3 §4 决策相关关键行不得"声明但从未有值"。

    区分两种缺口（都记 WARN，`--strict` 升 FAIL —— 与 C2 同一哲学：
    包内**无法**区分"确实没有该科目"与"映射漏了上游字段"，
    所以默认披露、交付路径阻断，而不是二选一地猜）：
      · 行**存在**但展示窗口内全部为空/破折号；
      · 行**完全不存在**（映射把标签弄丢了）。
    """
    sec4 = _section(pack, "4")
    if not sec4:
        return Finding("C3", "§4 决策关键行覆盖", UNEVALUATED, "未找到 §4 小节")

    present: Dict[str, List[str]] = {}
    for line in sec4.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2 or set("".join(cells)) <= set("-: "):
            continue
        present[cells[0]] = cells[1:]

    problems, ok = [], []
    for label, why in SECTION4_KEY_ROWS:
        key = next((k for k in present if label in k), None)
        if key is None:
            # 「现金及等价物」与「货币资金」互为港股/A股别名，任一存在即可
            alt = {"货币资金": "现金及等价物", "现金及等价物": "货币资金"}.get(label)
            if alt and any(alt in k for k in present):
                ok.append(f"{label}：由别名行 {alt} 提供")
                continue
            problems.append(f"§4 没有「{label}」行（{why}）⇒ 该下游小节会按 0/缺失处理")
            continue
        vals = present[key]
        if all(v in ("—", "-", "", "N/A", "n/a") or _num_single(v) is None for v in vals):
            problems.append(f"§4「{key}」行在展示窗口内**全为空**（{why}）"
                            f"⇒ 下游会按 0 处理；若该科目实际有值，说明上游字段名/映射漏了")
        else:
            ok.append(f"{label}：{sum(1 for v in vals if _num_single(v) is not None)} 个年度有值")

    if problems:
        return Finding("C3", "§4 决策关键行覆盖", WARN,
                       "以下决策关键行无法提供数值 —— 下游小节会静默按 0/缺失处理",
                       problems + ok, strict_status=FAIL)
    return Finding("C3", "§4 决策关键行覆盖", PASS, f"{len(ok)} 个关键行均有值", ok)


CHECKS = (check_required_rows, check_debt_composition,
          check_section4_key_rows)


def run_all(pack: str, sidecar_dir: Optional[str] = None,
            pack_path: Optional[str] = None) -> List[Finding]:
    """跑全部检查。

    ``pack`` 是**数据包正文**（与 ``invariants.run_all`` 一致）；sidecar 目录默认由
    ``pack_path``（采集器传入真实文件路径）或 ``sidecar_dir`` 给出。两者都缺时回落
    到当前工作目录 —— 拿不到 sidecar 时 C2 按原语义 FAIL，所以这条回落不会静默放行。
    """
    if sidecar_dir is None and pack_path:
        sidecar_dir = os.path.dirname(os.path.abspath(pack_path))
    base = sidecar_dir or os.getcwd()
    absence = _load_field_absence(os.path.join(base, "field_absence.json"))
    out: List[Finding] = []
    for fn in CHECKS:
        try:
            if fn is check_debt_composition:
                out.append(fn(pack, absence))
            else:
                out.append(fn(pack))
        except Exception as exc:  # noqa: BLE001 — 门禁自身故障必须可见
            out.append(Finding(getattr(fn, "__name__", "?"), "字段覆盖检查失败", FAIL,
                               f"{type(exc).__name__}: {exc}"))
    return out


def render(findings: Sequence[Finding], strict: bool = False) -> str:
    icon = {PASS: "✅", FAIL: "❌", WARN: "⚠️", UNEVALUATED: "❓"}
    lines = []
    for f in findings:
        st = f.effective_status(strict)
        mark = "" if st == f.status else f"（默认 {f.status}）"
        lines.append(f"{icon.get(st, '?')} [{f.code}] {f.title} — {st}{mark}")
        lines.append(f"    {f.detail}")
        for e in f.evidence[:8]:
            lines.append(f"      · {e}")
    fails = [f for f in findings if f.effective_status(strict) == FAIL]
    unk = [f for f in findings if f.effective_status(strict) == UNEVALUATED]
    lines.append("")
    lines.append(f"汇总：FAIL {len(fails)} · UNEVALUATED {len(unk)} · "
                 f"PASS {sum(1 for f in findings if f.effective_status(strict) == PASS)}"
                 + ("（--strict：WARN 级信号已升级为 FAIL）" if strict else ""))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="数据包字段覆盖门禁（fail-closed）")
    ap.add_argument("--pack", required=True)
    ap.add_argument("--json", dest="json_out", default=None)

    ap.add_argument("--strict", action="store_true",
                    help="交付路径：WARN 级信号（如债类显式全 0.00）升级为 FAIL，"
                         "UNEVALUATED 亦计失败")
    ap.add_argument("--sidecar-dir", default=None,
                    help="field_absence.json 所在目录（默认取数据包同级目录）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    try:
        pack = open(args.pack, encoding="utf-8").read()
    except OSError as exc:
        print(f"Error: 无法读取数据包 {args.pack}: {exc}", file=sys.stderr)
        return 2
    findings = run_all(pack, args.sidecar_dir)
    if not args.quiet:
        print(render(findings, strict=args.strict))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"pack": args.pack,
                       "findings": [{**f.to_dict(),
                                     "effective_status": f.effective_status(args.strict)}
                                    for f in findings]},
                      fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    if any(f.effective_status(args.strict) == FAIL for f in findings):
        return 1
    if args.strict and any(f.effective_status(True) == UNEVALUATED for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
