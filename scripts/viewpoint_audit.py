#!/usr/bin/env python3
"""viewpoint_audit.py —— 观点审核（报告生成**之后**）。

存在理由
--------
报告级门禁此前只做三件事：claim 绑定（数字抄得对不对）、I8 保真（叙述有没有被改写）、
I9 规则序（底价 ≤ 买点）。**没有一件检查"结论本身是否自洽"**：

  · `仓位建议` 是否真的是把 `KK/可信度/陷阱` 代入矩阵算出来的？（矩阵是偏函数，
    存在无对应行的组合；也可能被直接写成结论）
  · 同一指标在不同章节是否被写成两个值？（§17.9 与 §17.11 口径不同却并列引用）
  · Executive Summary 的结论与正文的「综合结论」是否一致？
  · 已经披露的阻断级/披露级信号，有没有被**真的**带进结论（而不是披露完照用）？
  · 有没有出现被明令禁止的读数（"低于底价—买入就是胜利"、手算降级、引用已封锁的格子）？

本脚本把**能机械判的**判完，余下的论证质量问题（理由是否有数据支撑、评级是否与所引事实
矛盾、因果跳跃、以结论反推参数）留给子代理 —— 那是判断，不是算术。

输出
----
- `viewpoint_audit.json`：findings（severity: critical/major/minor）+ 交付裁决；
- `viewpoint_audit.md`：裁定工作单。

退出码：0 无阻断 · 1 存在阻断 · 2 输入不可读。
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

MATCH, MISMATCH, MISSING, UNVERIFIABLE = "MATCH", "MISMATCH", "MISSING", "UNVERIFIABLE"
CRITICAL, MAJOR, MINOR = "critical", "major", "minor"

_CLAIM_RE = re.compile(r"<claim\s+id=\"(?P<id>[^\"]+)\"(?:\s+policy=\"[^\"]*\")?\s*>(?P<val>[^<]*)</claim>")
_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")

# 矩阵：**照 phase3_valuation.md 步骤3 抄**（顺序即文档顺序）。它是**偏函数** ——
# 多个条件可能同时成立、也可能一个都不成立，本脚本如实返回"命中哪些行"，
# 而不是假装它是全函数（那正是它此前被误当成"机械可判"的原因）。
MATRIX_ROWS: List[Tuple[str, str]] = [
    ("KK>=1.5|cred=高|trap=低", "标准仓位"),
    ("KK>=1.5|cred=高|trap=中", "70%"),
    ("KK>=1.5|cred=中|trap=低", "70%"),
    ("KK0.5-1.5|cred=高|trap=低", "50%"),
    ("KK0.5-1.5|cred=中|trap=低", "观察（30%）"),
    ("KK0-0.5|—|—", "观察或等待"),
    ("KK<0|—|—", "不建仓"),
    ("—|cred=低|—", "观察或不建仓"),
    ("—|—|trap=高 & GG<II×1.5", "排除"),
]


@dataclass
class Finding:
    code: str
    title: str
    status: str
    severity: Optional[str]
    detail: str
    expected: Optional[str] = None
    actual: Optional[str] = None
    basis: str = ""
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _f(code, title, status, severity, detail, expected=None, actual=None,
       basis="", evidence=None) -> Finding:
    return Finding(code, title, status, severity, detail, expected, actual, basis,
                   evidence or [])


# ---------------------------------------------------------------------------
# 报告解析（容错 + fail-closed：找不到就 MISSING，绝不猜）
# ---------------------------------------------------------------------------
_MINUS_MAP = {ord("−"): "-", ord("–"): "-", ord("—"): "-", ord("－"): "-",
              ord("﹣"): "-", ord("－"): "-"}


def _norm_minus(text: str) -> str:
    """Unicode 减号（U+2212）／en dash 必须归一成 ASCII '-'。

    报告真的在用 `KK = −0.34 pct`（U+2212）。不归一就会被解析成 **+0.34** ——
    一个符号错误会让审核把"不建仓"判成"观察或等待"。这类字节级差异在中文报告里
    很常见（claim_ledger 也栽过一次）。
    """
    return (text or "").translate(_MINUS_MAP)


def _strip_claims(text: str) -> str:
    return _CLAIM_RE.sub(lambda m: m.group("val"), text)


def _zh_num(text: str) -> Optional[float]:
    """从一段文本里取第一个数（含负号与逗号）。"""
    m = _NUM_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


class Report:
    def __init__(self, text: str) -> None:
        self.raw = _norm_minus(text)
        self.text = _strip_claims(self.raw)

    # -- sections -----------------------------------------------------------
    def section(self, *patterns: str) -> str:
        for pat in patterns:
            m = re.search(rf"^#{{2,3}}\s*[^\n]*{pat}[^\n]*$", self.text, re.M)
            if not m:
                continue
            rest = self.text[m.end():]
            nxt = re.search(r"^#{2,3}\s", rest, re.M)
            return rest[:nxt.start()] if nxt else rest
        return ""

    # -- labelled values ----------------------------------------------------
    def table_value(self, label: str, section: str = "") -> Optional[float]:
        scope = section or self.text
        for line in scope.splitlines():
            s = line.strip()
            if not (s.startswith("|") and s.endswith("|")):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and label in cells[0]:
                for c in cells[1:]:
                    v = _zh_num(c.replace("*", ""))
                    if v is not None:
                        return v
        return None

    def inline_value(self, pattern: str) -> Optional[float]:
        m = re.search(pattern, self.text)
        return _zh_num(m.group(1)) if m and m.group(1) is not None else None

    def label_text(self, label: str, section: str = "") -> str:
        scope = section or self.text
        for line in scope.splitlines():
            s = line.strip()
            if s.startswith("|") and s.endswith("|"):
                cells = [c.strip() for c in s.strip("|").split("|")]
                if cells and label in cells[0] and len(cells) > 1:
                    return cells[1]
        return ""


def _adopted_k(rep: Report) -> Optional[float]:
    return rep.inline_value(r"(?:选定|采纳|决策口径)[^\n]{0,40}?k\s*[=＝]\s*\**([\d.]+)")


def _claim_value(claims: Optional[dict], claim_id: str) -> Optional[float]:
    if not claims:
        return None
    for c in claims.get("claims", []):
        if (c.get("claim_id") or c.get("id")) == claim_id:
            v = c.get("value")
            return float(v) if isinstance(v, (int, float)) else None
    return None


def _adopted_buy_point(rep: Report, claims: Optional[dict]) -> Tuple[Optional[float], str]:
    """采纳档目标买入价：**账本优先**，正文兜底。

    为什么账本优先：正文对同一个数的写法有十几种（`目标买入价 = 8.67 港元/股`、
    `目标买入价_pess = …`、表格末列…），正则抓不全会把检查整体降级成 UNVERIFIABLE；
    而账本里的 `cm17.14.k080.tbp` 是唯一的、机器可读的权威值 —— 与 I9 的处理一致。
    """
    k = _adopted_k(rep)
    if k is not None:
        v = _claim_value(claims, "cm17.14.k%03d.tbp" % round(k * 100))
        if v is not None:
            return v, f"claims.json cm17.14.k{round(k*100):03d}.tbp"
    for pat in (r"目标买入价[_ ]?pess\s*[=＝]\s*\**([\d.]+)",
                r"目标买入价[^\n=]{0,12}[=＝]\s*\**([\d.]+)"):
        m = re.search(pat, rep.text)
        if m:
            return _zh_num(m.group(1)), "报告正文"
    return None, "未找到"


def _rating(rep: Report, labels: Sequence[str], keys: Sequence[str]) -> Optional[str]:
    """取一个**评级**字段（可信度 / 价值陷阱风险）。

    两个坑（首版都踩了）：
    ① `label_text("可信度")` 会先命中「可信度评级」表里的维度行（那里也有高/中/低），
       于是 06862 的「低」被读成「高」—— 评级读错会让矩阵判定整体反转；
    ② 应先看 Executive Summary，再看全篇。
    因此：优先精确标签（如「外推可信度」），限定在 ES 段落内先找，并只在**单元格**里取。
    """
    scopes = [rep.section("Executive Summary", "执行摘要"), rep.text]
    for scope in scopes:
        for lab in labels:
            for line in scope.splitlines():
                s = line.strip()
                if not (s.startswith("|") and s.endswith("|")):
                    continue
                cells = [c.strip().replace("*", "") for c in s.strip("|").split("|")]
                if cells and lab in cells[0] and len(cells) > 1:
                    cell = cells[1]
                    # 单元格常写成 `**低**（1 项高 / 1 项中 / 3 项低）` —— 括号里是**分布**，
                    # 不是评级本身。不剥括号会把「低」读成「高」（首版实测）。
                    cell = re.sub(r"[（(][^）)]*[）)]", "", cell)
                    for k in keys:
                        if k in cell:
                            return k
    return None


def _credibility(rep: Report) -> Optional[str]:
    return _rating(rep, ("外推可信度", "可信度"), ("高", "中", "低"))


def _trap(rep: Report) -> Optional[str]:
    return _rating(rep, ("价值陷阱风险", "价值陷阱"), ("低", "中", "高"))


def _position(rep: Report) -> str:
    t = rep.label_text("仓位建议") or rep.inline_value("仓位建议") or ""
    return str(t)


def _norm_position(text: str) -> str:
    """把仓位建议归一成可比类别（文档措辞不统一：'70%' / '标准仓位上限' / '观察（30%）'）。"""
    t = str(text or "")
    if "排除" in t:
        return "排除"
    if "标准仓位" in t:
        return "标准仓位"
    if "不建仓" in t:
        return "不建仓"
    if "70%" in t:
        return "70%"
    if "50%" in t:
        return "50%"
    if "30%" in t or "观察" in t:
        return "观察"
    if "等待" in t:
        return "观察或等待"
    return t.strip() or "（未给出）"


def matrix_rows(kk: Optional[float], cred: Optional[str], trap: Optional[str],
                gg: Optional[float], ii: Optional[float]) -> List[str]:
    """返回**同时适用**的矩阵行标签。矩阵是偏函数：可能 0 行、1 行或多行。"""
    if kk is None:
        return []
    hits: List[str] = []
    if kk >= 1.5:
        band = "KK>=1.5"
    elif kk >= 0.5:
        band = "KK0.5-1.5"
    elif kk >= 0:
        band = "KK0-0.5"
    else:
        band = "KK<0"
    for cond, advice in MATRIX_ROWS:
        c, cr, ct = (cond.split("|") + ["—", "—"])[:3]
        if c != "—" and c != band:
            continue
        if cr != "—":
            want = cr.split("=")[1]
            if cred != want:
                continue
        if ct != "—":
            if "trap=高" in ct and trap != "高":
                continue
            if "trap=低" in ct and trap != "低":
                continue
            if "trap=中" in ct and trap != "中":
                continue
            if "GG<II×1.5" in ct:
                if not (gg is not None and ii is not None and gg < ii * 1.5):
                    continue
        hits.append(advice)
    return hits


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------
def check_decision_chain(rep: Report, claims: Optional[dict] = None) -> List[Finding]:
    """V1：结论必须能由它自己引用的数字算出来。"""
    out: List[Finding] = []
    es = rep.section("Executive Summary", "执行摘要")
    val = rep.section("估值与定价", "门槛与安全边际")
    scope = val or rep.text

    gg_pess = rep.table_value("精算穿透回报率", es) or _zh_num(
        (re.search(r"GG_pess\s*=\s*\**([-\d.]+)", rep.text) or [None, None])[1]
        if re.search(r"GG_pess\s*=\s*\**([-\d.]+)", rep.text) else "")
    # 决策口径：优先取"悲观档"行/pess 目标价
    k = rep.inline_value(r"(?:选定|采纳|决策口径)[^\n]{0,40}?k\s*[=＝]\s*\**([\d.]+)")
    gg_pess = rep.inline_value(r"GG_pess\s*[=＝]\s*\**([\d.]+)") or gg_pess
    tbp_pess, tbp_src = _adopted_buy_point(rep, claims)
    ii = rep.table_value("门槛值", es) or rep.table_value("门槛 II", scope) or \
        rep.inline_value(r"门槛\s*(?:值)?\s*II?\s*[=＝]\s*\**([\d.]+)")
    kk_stated = rep.inline_value(r"KK\s*[=＝]\s*\**([-−]?[\d.]+)") or \
        rep.inline_value(r"安全边际\s*[=＝]?\s*\**KK\s*[=＝]\s*\**([-−]?[\d.]+)")
    if kk_stated is None:
        m = re.search(r"KK\s*=\s*\**\s*[-−]?\s*([\d.]+)", rep.text)
        kk_stated = -float(m.group(1)) if (m and "−" in m.group(0)) else (float(m.group(1)) if m else None)

    if ii is not None and gg_pess is not None:
        expect = gg_pess - ii
        if kk_stated is None:
            out.append(_f("V1a", "安全边际 KK = GG_pess − II", MISSING, MAJOR,
                          "报告未给出可解析的 KK，无法核对结论链", f"{expect:.2f}", None,
                          f"GG_pess={gg_pess}, II={ii}", []))
        else:
            st = MATCH if abs(kk_stated - expect) <= 0.02 else MISMATCH
            out.append(_f("V1a", "安全边际 KK = GG_pess − II", st,
                          None if st == MATCH else CRITICAL,
                          "报告里的 KK 必须等于它自己引用的 GG_pess 减 II；"
                          "不等说明结论链在报告内部就不闭合",
                          f"{expect:.2f}", f"{kk_stated:.2f}",
                          f"GG_pess={gg_pess}, II={ii}", []))
    else:
        out.append(_f("V1a", "安全边际 KK = GG_pess − II", UNVERIFIABLE, MINOR,
                      f"无法从报告解析 门槛II={ii} 或 GG_pess={gg_pess} ⇒ 不可判定（不是通过）",
                      None, None, "", []))

    cred, trap = _credibility(rep), _trap(rep)
    hits = matrix_rows(kk_stated, cred, trap, gg_pess, ii)
    stated = _norm_position(_position(rep))
    if kk_stated is not None and not hits:
        out.append(_f("V1b", "仓位矩阵覆盖性", UNVERIFIABLE, MAJOR,
                      f"KK={kk_stated:.2f}、可信度={cred}、陷阱={trap} 在矩阵里**没有对应行** ⇒ "
                      "矩阵是偏函数，此处结论无法机械复核，必须在报告中说明所依据的行与理由",
                      None, stated, "phase3_valuation.md 步骤3", []))
    elif hits:
        ok = any(_norm_position(h) == stated or stated in h or h in stated for h in hits)
        out.append(_f("V1b", "仓位建议 = 矩阵代入结果", MATCH if ok else MISMATCH,
                      None if ok else CRITICAL,
                      ("命中矩阵行：" + "、".join(hits)) if ok else
                      ("矩阵给出的行是 " + "、".join(hits) + "，与报告写的仓位不一致 ⇒ "
                       "结论没有按它自己声明的矩阵得出"),
                      "、".join(hits), stated,
                      f"KK={kk_stated}, 可信度={cred}, 陷阱={trap}, GG_pess={gg_pess}, II={ii}",
                      []))
        if len(set(hits)) > 1:
            out.append(_f("V1c", "矩阵多行同时适用", UNVERIFIABLE, MINOR,
                          "同一组输入命中多行（" + "、".join(hits) + "）⇒ 需在报告中显式说明"
                          "优先级；矩阵本身缺优先级规则", None, stated,
                          "phase3_valuation.md 步骤3", []))

    # 触发判定：现价 vs 悲观档买点
    price = rep.inline_value(r"最新股价\s*\|\s*\**([\d.]+)") or \
        rep.inline_value(r"当前股价[^\n]{0,12}?([\d.]+)")
    if price is not None and tbp_pess is not None:
        triggered = price <= tbp_pess
        tail = rep.section("综合结论") or rep.text
        says_hit = ("已达标" in tail) or ("价格条件已达标" in tail) or ("低于买点" in tail)
        says_not = ("未达标" in tail) or ("高于买点" in tail)
        if triggered and says_not:
            out.append(_f("V1d", "买点触发判定与数字一致", MISMATCH, MAJOR,
                          f"现价 {price} ≤ 悲观档买点 {tbp_pess} 却写成未达标", "已达标", "未达标",
                          "", []))
        elif (not triggered) and says_hit:
            out.append(_f("V1d", "买点触发判定与数字一致", MISMATCH, MAJOR,
                          f"现价 {price} > 悲观档买点 {tbp_pess} 却写成已达标", "未达标", "已达标",
                          "", []))
        else:
            out.append(_f("V1d", "买点触发判定与数字一致", MATCH, None,
                          f"现价 {price} vs 买点 {tbp_pess}（来源：{tbp_src}）⇒ "
                          + ("已达标" if triggered else "未达标"), None, None, "", []))
    else:
        out.append(_f("V1d", "买点触发判定与数字一致", UNVERIFIABLE, MINOR,
                      f"无法解析 现价={price} 或 悲观档买点={tbp_pess}", None, None, "", []))
    return out


def check_binding_and_status(rep: Report, claims: Optional[dict],
                             section_status: Optional[dict]) -> List[Finding]:
    """V2：引用合规 —— 该绑定的必须绑定；不得引用 status≠ok 的小节。"""
    out: List[Finding] = []
    decision_scopes = [rep.section("Executive Summary", "执行摘要"),
                       rep.section("穿透回报率分析"),
                       rep.section("估值与定价"),
                       rep.section("综合结论")]
    scope = "\n".join(s for s in decision_scopes if s) or rep.text
    bound = len(_CLAIM_RE.findall(rep.raw))
    unbound_scopes = _CLAIM_RE.sub(" ", scope)
    unbound = len(_NUM_RE.findall(unbound_scopes))
    out.append(_f("V2a", "决策区段的 claim 绑定密度", MATCH if bound else MISMATCH,
                  None if bound else CRITICAL,
                  f"决策区段内绑定 {bound} 个 claim token、未绑定 {unbound} 个数值 token。"
                  "绑定为 0 意味着结论数字全部未经机械校验",
                  ">0", str(bound), "PCN 契约：裸数字默认不可信", []))

    if claims:
        ids = {c.get("claim_id") or c.get("id") for c in claims.get("claims", [])}
        null_ids = {c.get("claim_id") or c.get("id") for c in claims.get("claims", [])
                    if c.get("value") is None}
        used = {m.group("id") for m in _CLAIM_RE.finditer(rep.raw)}
        unknown = sorted(used - ids)
        blocked = sorted(used & null_ids)
        out.append(_f("V2b", "claim id 必须存在于账本", MATCH if not unknown else MISMATCH,
                      None if not unknown else CRITICAL,
                      "引用了账本里不存在的 id（编造）" if unknown else "全部 id 存在",
                      None, None, "", unknown[:6]))
        if blocked:
            out.append(_f("V2c", "不得引用被封锁的格子（value=null）", MISMATCH, CRITICAL,
                          "账本已把这些格子登记为 value=null（不可引用，如跨币种的 §17.9 门槛价格），"
                          "报告仍在引用", None, None, "", blocked[:6]))
    else:
        out.append(_f("V2b", "claim id 必须存在于账本", UNVERIFIABLE, MAJOR,
                      "未提供 claims.json ⇒ 无法核对 id 是否编造", None, None, "", []))

    if section_status:
        non_ok = {k for k, v in (section_status.get("sections") or {}).items()
                  if (v or {}).get("status") != "ok"}
        if non_ok:
            # 引用非 ok 小节**本身不是违规** —— 报告常常正是要说明"该小节未输出、不得引用"。
            # 违规 = 拿它当**数据来源**。判据收窄到"小节号与数值**紧邻**"（±12 字符内），
            # 而不是"同一句里出现数字"：后者会把"§17.13 收入敏感性、有息负债口径缺口"
            # 这类**待修缺口清单**误判成引用（实测 06862 被误报 7 处）。
            _UNAVAIL = ("未输出", "缺失", "不可用", "fail-closed", "未生成", "不适用",
                        "不得引用", "未纳入", "跳过", "not_applicable", "input_inconsistent",
                        "拒绝输出", "拒答", "不引用", "不阻断", "缺口", "修复", "missing")
            cited, compliant = set(), set()
            for m in re.finditer(r"§17\.(\d+)", rep.text):
                key = f"17.{m.group(1)}"
                if key not in non_ok:
                    continue
                window = rep.text[max(0, m.start() - 12): m.end() + 12]
                sentence = rep.text[max(0, m.start() - 60): m.end() + 60]
                if any(mk in window for mk in _UNAVAIL) or any(mk in sentence for mk in _UNAVAIL):
                    compliant.add(key)
                elif _NUM_RE.search(window):
                    cited.add(key)
            cited, compliant = sorted(cited), sorted(compliant)
            if cited:
                out.append(_f("V2d", "不得引用 status≠ok 的 §17 小节", MISMATCH, CRITICAL,
                              "报告把状态非 ok 的小节当作数据来源引用（小节号与数值紧邻）；"
                              "这些数按契约不得引用",
                              None, None, "§17 状态契约：status≠ok ⇒ 停止引用", cited))
            else:
                out.append(_f("V2d", "不得引用 status≠ok 的 §17 小节", MATCH, None,
                              f"非 ok 小节 {sorted(non_ok)} 未被当作数据来源引用"
                              + (f"（其中 {compliant} 以「不可用」形式提及，属合规）"
                                 if compliant else ""), None, None, "", []))
    return out


def check_cross_section(rep: Report) -> List[Finding]:
    """V3：同一物理量在不同章节不得写成两个值。"""
    out: List[Finding] = []
    watch = {
        "精算穿透回报率 GG": r"精算穿透回报率[^\n|]*\|[^\n|]*?([-\d.]+)%",
        "门槛值 II": r"门槛值[^\n|]*\|[^\n|]*?([-\d.]+)%",
        "目标买入价": r"目标买入价[^\n|]*\|[^\n|]*?([\d.]+)",
    }
    for name, pat in watch.items():
        vals = []
        for m in re.finditer(pat, rep.text):
            v = _zh_num(m.group(1))
            if v is not None:
                vals.append(v)
        uniq = sorted(set(round(v, 2) for v in vals))
        if len(uniq) > 1:
            spread = (max(uniq) - min(uniq)) / max(abs(max(uniq)), 1e-9)
            out.append(_f("V3", f"跨段一致：{name}",
                          MISMATCH if spread > 0.02 else MATCH,
                          MAJOR if spread > 0.02 else None,
                          f"同名指标出现多个取值 {uniq}；若确为不同口径（如中性档 vs 悲观档）"
                          "必须在同处显式标注，否则读者无法判断哪个是结论输入",
                          f"{uniq[0]}", f"{uniq[-1]}", "", [f"出现 {len(vals)} 次"]))
        else:
            out.append(_f("V3", f"跨段一致：{name}", MATCH, None, f"取值唯一 {uniq}",
                          None, None, "", []))
    return out


BANNED = [
    ("低于底价", "「低于底价 — 买入就是胜利」是**买点语义**：只有在综合底价 ≤ 采纳档买点时才可发出；"
                 "否则框架会在它自己判定不可买的价位上给出买入信号"),
    ("买入就是胜利", "同上：该读数必须服从 §17.8 综合底价与采纳档买点的序关系（I9）"),
    ("降级路径", "「降级路径手算」已被删除：§17 小节状态非 ok 时应报告缺失，而不是手算替代"),
    ("手算降级", "同上"),
]


def check_banned(rep: Report, claims: Optional[dict]) -> List[Finding]:
    out: List[Finding] = []
    # 底价 vs 买点（若报告自己给了两个数）
    floor = rep.inline_value(r"综合底价[^\n=]{0,24}[=＝]\s*\**([\d.]+)")
    if floor is None:
        floor = rep.table_value("综合底价")
    tbp, _tbp_src = _adopted_buy_point(rep, claims)
    if floor is None or tbp is None:
        out.append(_f("V4a", "禁令：低于底价读数需满足 综合底价 ≤ 采纳档买点", UNVERIFIABLE, MINOR,
                      f"无法解析 综合底价={floor} 或 采纳档买点={tbp}", None, None,
                      "phase3_valuation.md 步骤5 规则优先级", []))
    else:
        fires = "低于底价" in rep.text or "买入就是胜利" in rep.text
        ok = floor <= tbp + 1e-9
        if fires and not ok:
            out.append(_f("V4a", "禁令：低于底价读数需满足 综合底价 ≤ 采纳档买点", MISMATCH, CRITICAL,
                          f"综合底价 {floor} > 采纳档买点 {tbp}，报告仍出现「低于底价/买入就是胜利」读数",
                          f"综合底价 ≤ {tbp}", str(floor), "", []))
        else:
            out.append(_f("V4a", "禁令：低于底价读数需满足 综合底价 ≤ 采纳档买点", MATCH, None,
                          f"综合底价 {floor} ≤ 采纳档买点 {tbp}（读数{'出现' if fires else '未出现'}）",
                          None, None, "", []))

    for word, why in BANNED[2:]:
        if word in rep.text:
            out.append(_f("V4b", f"禁令：不得出现「{word}」", MISMATCH, MAJOR, why,
                          None, None, "known_issues P2-20（手算降级路径已删）",
                          [f"出现位置附近：…{rep.text[max(0, rep.text.find(word)-40):rep.text.find(word)+40]}…"]))
    if claims:
        quarantined = {c.get("claim_id") or c.get("id") for c in claims.get("claims", [])
                       if c.get("value") is None}
        used = {m.group("id") for m in _CLAIM_RE.finditer(rep.raw)}
        hit = sorted(used & quarantined)
        if hit:
            out.append(_f("V4c", "禁令：不得引用已封锁格子", MISMATCH, CRITICAL,
                          "账本将其登记为 value=null（不可引用）", None, None,
                          "fail-closed：不可引用 ≠ 可以随便写", hit))
    return out


def check_es_vs_body(rep: Report) -> List[Finding]:
    """V5：Executive Summary 与「综合结论」必须同结论。"""
    out: List[Finding] = []
    es = rep.section("Executive Summary", "执行摘要")
    body = rep.section("综合结论")
    if not es or not body:
        out.append(_f("V5", "ES 与综合结论一致", UNVERIFIABLE, MINOR,
                      f"找不到 Executive Summary({bool(es)}) 或 综合结论({bool(body)})",
                      None, None, "报告模板要求两处给出同一结论", []))
        return out
    es_pos = _norm_position(rep.label_text("仓位建议", es) or es)
    body_pos = _norm_position(body)
    st = MATCH if es_pos == body_pos else MISMATCH
    out.append(_f("V5", "ES 与综合结论的仓位建议一致", st,
                  None if st == MATCH else CRITICAL,
                  "两处必须给出同一结论；不一致会让读者按自己先读到的段落行动",
                  es_pos, body_pos, "", []))
    return out


def check_disclosure_carry(rep: Report, gate_status: Optional[dict],
                           invariants: Optional[dict]) -> List[Finding]:
    """V6：已披露的门禁信号必须**带进**报告，而不是披露完照用。"""
    out: List[Finding] = []
    if not gate_status and not invariants:
        out.append(_f("V6", "门禁信号已被带进报告", UNVERIFIABLE, MINOR,
                      "未提供 gate_status.json / invariants.json ⇒ 无法核对闭环",
                      None, None, "", []))
        return out
    codes: List[str] = []
    for b in (gate_status or {}).get("blocking", []):
        codes.append(b.get("code") or "?")
    for d in (gate_status or {}).get("disclosure", []):
        codes.append(d.get("code") or "?")
    for f in (invariants or {}).get("findings", []):
        if f.get("status") in ("FAIL", "UNEVALUATED", "WARN"):
            codes.append(f.get("code") or "?")
    codes = sorted(set(c for c in codes if c and c != "?"))
    if not codes:
        out.append(_f("V6", "门禁信号已被带进报告", MATCH, None, "上游无 FAIL/WARN/阻断项",
                      None, None, "", []))
        return out
    missing = [c for c in codes if c not in rep.text]
    if missing:
        out.append(_f("V6", "门禁信号已被带进报告", MISMATCH, MAJOR,
                      "上游门禁报出的失败/披露项未在报告中出现；" 
                      "『披露过』与『带进结论』是两件事 —— 报告必须让读者看到这些限制",
                      None, None, "known_issues §十二：自洽 ≠ 完整", missing))
    else:
        out.append(_f("V6", "门禁信号已被带进报告", MATCH, None,
                      f"{len(codes)} 个上游信号均已出现", None, None, "", []))
    return out


# ---------------------------------------------------------------------------
# 三条流程共用的检查集。`turtle` 有仓位矩阵/买点/门槛这些专有语义，
# `valuation` 与 `qualitative` 没有 —— 对它们跑 V1 只会得到一片 UNVERIFIABLE，
# 而 `--strict` 下 UNVERIFIABLE 会阻断交付 ⇒ 等于把共享工具变成龟龟专用工具。
# `binding`（V2）只在**龟龟**流程里成立：claim 账本覆盖 §17 决策单元格，而
# 估值/定性的 coordinator 里根本没有"claim"这个概念（grep 为 0 命中）⇒ 在那里要求
# 绑定会得到「V2a 绑定 0 个 claim」critical + V2b major，`--strict` 下一份完全正常的
# 定性报告无法交付 —— 正是 `--profile` 要避免的"把共享工具变成龟龟专用工具"。
PROFILE_CHECKS = {
    "turtle": ("decision_chain", "binding", "cross_section", "banned", "es_body", "disclosure"),
    "valuation": ("cross_section", "banned", "es_body", "disclosure"),
    "qualitative": ("cross_section", "banned", "es_body", "disclosure"),
}


def run_all(report_text: str, claims: Optional[dict] = None,
            section_status: Optional[dict] = None,
            gate_status: Optional[dict] = None,
            invariants: Optional[dict] = None,
            profile: str = "turtle") -> List[Finding]:
    rep = Report(report_text)
    out: List[Finding] = []
    enabled = PROFILE_CHECKS.get(profile, PROFILE_CHECKS["turtle"])
    if "decision_chain" not in enabled:
        out.append(_f("V1", "决策链（仓位矩阵/买点/门槛）", UNVERIFIABLE, MINOR,
                      f"profile={profile} 下不适用：该流程没有仓位矩阵与买点语义，"
                      "本项跳过（不是通过）", None, None, "", []))
    if "binding" not in enabled:
        # claim 账本由采集器为 **§17 决策单元格**生成，而定性/估值报告不引用这些单元格
        # （两份 coordinator 里都没有"claim"一词）⇒ 在这里要求绑定是范畴错误：
        # 实测定性报告会得到「V2a 绑定 0 个 claim」critical + V2b major ⇒ `--strict` 下
        # 一份**完全正常**的定性报告无法交付。与 V1 同样处理：如实声明"未检查"，不是通过。
        out.append(_f("V2", "claim 绑定与 §17 状态", UNVERIFIABLE, MINOR,
                      f"profile={profile} 下不适用：该流程不要求 claim 绑定"
                      "（claim 账本覆盖 §17 决策单元格，本报告不引用它们）；"
                      "本项跳过（不是通过）", None, None, "", []))
    for fn, args in (
            (() if "decision_chain" not in enabled
             else ((check_decision_chain, (rep, claims)),))
            + (() if "binding" not in enabled
               else ((check_binding_and_status, (rep, claims, section_status)),))
            + ((check_cross_section, (rep,)),
               (check_banned, (rep, claims)),
               (check_es_vs_body, (rep,)),
               (check_disclosure_carry, (rep, gate_status, invariants)))):
        try:
            out.extend(fn(*args))
        except Exception as exc:  # noqa: BLE001 — 审核自身故障必须可见
            out.append(_f("VA", f"{fn.__name__} 执行失败", UNVERIFIABLE, MAJOR,
                          f"{type(exc).__name__}: {exc}"))
    return out


def render(findings: Sequence[Finding]) -> str:
    icon = {MATCH: "✅", MISMATCH: "❌", MISSING: "⛔", UNVERIFIABLE: "❓"}
    lines = ["# 观点审核工作单（viewpoint_audit）", "",
             "> 报告生成**之后**的审核：结论是否由它自己引用的数字按文档规则得出、",
             "> 是否自洽、有没有被明令禁止的读数、上游限制有没有带进结论。",
             "> **机械能判的都在这里；论证质量（理由是否有数据支撑、评级是否与事实矛盾、",
             "> 因果跳跃、以结论反推参数）由子代理裁定。**", "",
             "| # | 代码 | 检查 | 状态 | 严重度 | 期望 | 实际 | 依据/证据 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for i, f in enumerate(findings, 1):
        ev = "；".join(str(x)[:80] for x in f.evidence[:3]) if f.evidence else f.basis
        lines.append(f"| {i} | {f.code} | {f.title} | {icon.get(f.status,'?')} {f.status} | "
                     f"{f.severity or '—'} | {f.expected or '—'} | {f.actual or '—'} | {ev[:130]} |")
    lines.append("")
    lines.append(f"> 汇总：不一致 {sum(1 for f in findings if f.status==MISMATCH)} · "
                 f"缺失 {sum(1 for f in findings if f.status==MISSING)} · "
                 f"不可判定 {sum(1 for f in findings if f.status==UNVERIFIABLE)} · "
                 f"一致 {sum(1 for f in findings if f.status==MATCH)}")
    lines.append("")
    lines.append("## 裁定要求（子代理）")
    lines.append("")
    lines.append("1. 逐条裁定非 MATCH 项：{真实问题 / 口径差异（写依据）/ 脚本误判（写依据）}。")
    lines.append("2. **补做机械检查覆盖不到的论证质量审核**：")
    lines.append("   - 每条买/卖理由是否由**可溯源的数字**支撑（引用 `[src:]`/claim id 且数字一致）；")
    lines.append("   - 定性评级（护城河/管理层/周期）与同一份报告里引用的事实是否矛盾；")
    lines.append("   - 是否把**推断**写成事实（未标注来源的因果、预测、行业断言）；")
    lines.append("   - Warnings 里披露的限制是否**真的影响**了措辞与结论（而不是披露完照用）；")
    lines.append("   - 关键假设的替代值与敏感性是否被诚实呈现（有无只给支持结论的那一档）；")
    lines.append("   - 是否存在以结论反推参数的痕迹（如「因现价高于买点故不改 k」）。")
    lines.append("3. 末行必须是 `VIEWPOINT_AUDIT_RESULT: PASS` 或 `VIEWPOINT_AUDIT_RESULT: FIX_REQUIRED`；")
    lines.append("   存在未裁定的非 MATCH 项 ⇒ 不得给出 PASS。")
    return "\n".join(lines) + "\n"


def _load_json(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="观点审核：结论自洽性 + 引用合规 + 禁令 + 披露闭环")
    ap.add_argument("--report", required=True)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--claims", default=None)
    ap.add_argument("--section-status", default=None)
    ap.add_argument("--gate-status", default=None)
    ap.add_argument("--invariants", default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--md", dest="md_out", default=None)
    ap.add_argument("--profile", default="turtle", choices=sorted(PROFILE_CHECKS),
                    help="流程档案：turtle（含仓位矩阵/买点检查）｜valuation｜qualitative"
                         "（后两者跳过龟龟专有的决策链检查）")
    ap.add_argument("--allow-degraded", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="UNVERIFIABLE/MISSING 也计为失败（交付路径建议开启）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        report_text = Path(args.report).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error: 无法读取报告 {args.report}: {exc}", file=sys.stderr)
        return 2

    rep_dir = Path(args.report).parent
    out_dir = Path(args.output_dir) if args.output_dir else rep_dir

    def _find(explicit: Optional[str], name: str) -> Optional[dict]:
        """sidecar 查找：显式路径 → 报告所在目录 → 输出目录。

        首版只查输出目录，于是 `--output-dir /tmp/...` 时连 claims.json 都找不到，
        把"引用合规"整块降级成 UNVERIFIABLE —— 假阴性比假阳性更危险。
        """
        if explicit:
            return _load_json(explicit)
        for base in (rep_dir, out_dir):
            got = _load_json(str(base / name))
            if got is not None:
                return got
        return None

    claims = _find(args.claims, "claims.json")
    section_status = _find(args.section_status, "section17_status.json")
    gate_status = _find(args.gate_status, "gate_status.json")
    invariants = _find(args.invariants, "invariants.json")

    findings = run_all(report_text, claims, section_status, gate_status, invariants,
                       profile=args.profile)
    payload = [f.to_dict() for f in findings]
    if args.strict:
        for item in payload:
            if item["status"] in (UNVERIFIABLE, MISSING):
                item["severity"] = item.get("severity") or MAJOR

    from gate_policy import verdict, print_verdict, exit_code
    v = verdict(("viewpoint_audit", payload), allow_degraded=args.allow_degraded)
    v["findings"] = payload

    json_out = Path(args.json_out) if args.json_out else out_dir / "viewpoint_audit.json"
    md_out = Path(args.md_out) if args.md_out else out_dir / "viewpoint_audit.md"
    try:
        json_out.write_text(json.dumps(v, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        md_out.write_text(render(findings), encoding="utf-8")
    except OSError as exc:
        print(f"Error: 无法写出审核结果: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        print(render(findings))
        print_verdict(v, "观点审核")
        print(f"  → {json_out} / {md_out}")
    return exit_code(v)


if __name__ == "__main__":
    sys.exit(main())
