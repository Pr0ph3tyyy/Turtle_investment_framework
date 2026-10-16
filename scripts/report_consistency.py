"""report_consistency.py — 杠杆6: cross-passage numeric consistency audit.

Scans a finished qualitative report (markdown) for the same metric being
written with different values in different passages (e.g. 毛利率 47.6% 前文 /
49.2% 后文) — the "叙述内部矛盾" class that quality_control.py (算式正确) does
not cover. Optionally (``--gates``) enforces deterministic hard gates (杠杆7):
per-dimension 字数下限 + presence of required sections.

Method (see plan §4.2):
  1. mask spans that must be skipped ([src:...] / §N / P.X / 8-digit dates / markdown emphasis)
  2. extract numbers carrying a money/percent unit only — excluding
     * range notation (``8%-15%`` is NOT the value ``-15``),
     * thresholds (``<18%``, ``30% 以上`` are limits, not observations),
     * deltas (``同比下降 11.65 个百分点`` is not a level)
  3. classify to one of ~14 metric categories: keywords **preceding** the number win, distance is
     measured from the keyword's end, ties break toward the more specific category
     (so 归母净利润 11.25 亿元 is not read as plain 净利润)
  4. assign periods **year-first**: each year attaches to the number it labels (right side first,
     ≤12 chars), which stops table rows lending their note-column years to the main value;
     a prose line mentioning exactly one year lets its other numbers inherit it, except after a
     change verb (由 2024 年 32.84% 回落至 21.28% ⇒ 21.28 is not a 2024 figure)
  5. cluster by (category, year); flag clusters spanning > tolerance

Known limitation (measured, not hypothetical): period attribution stays heuristic. On the 002984
report this reduced 12 advisories to 6 while keeping every genuine caliber difference
(母公司/合并 营收与净利润、加权/平均 ROE、分部/合并毛利率). The residual still mixes in a few
same-metric-different-account values (归母权益 vs 归母净利润、5 年均值 vs 单年、派生差额).
**Do not tune further against a single report** — that overfits. Treat the list as candidates:
the conflict list is **advisory** and a downstream numeric_audit agent adjudicates
真错误 vs 口径差异.

**交付退出码语义（2026-10-14 修正）**：advisory 冲突**不再**计入退出码 —— 只有
**硬门槛失败 / 声明校验失败 / 覆盖率未达标**才算失败。理由：冲突清单里有相当一部分
（年份归属错配、统计量 vs 单年、期间混同）是**抽取启发式噪声**，且其中若干条直接落在
`check_report_fidelity.py` I8 强制**逐字嵌入**的维度叙述内 —— 改写即违反保真。
把"不可在不违反 I8 的前提下消除的噪声"做成交付阻断，等于用两条互斥的门禁把交付路径锁死。
冲突清单仍照常渲染（`--output` 里可逐条复核），要恢复旧语义用 `--conflicts-are-failures`。

Exit codes: 0 无硬失败 · 1 硬门槛/声明校验/覆盖率失败（或 `--conflicts-are-failures` 下有冲突）· 2 file error.
"""

from __future__ import annotations

import argparse
import bisect
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

WINDOW = 30          # 向后（数值右侧）观察窗口
WINDOW_BACK = 45     # 向前（数值左侧）观察窗口：指标名常在句首，离数值可超过 30 字符

# (canonical, keywords, kind) — order matters: most-specific first.
CATEGORIES: List[Tuple[str, Tuple[str, ...], str]] = [
    ("扣非净利润", ("扣非净利润", "扣非归母", "扣非"), "money"),
    ("归母净利润", ("归母净利润", "归母"), "money"),
    ("营业利润", ("营业利润",), "money"),
    ("净利润", ("净利润",), "money"),
    ("营业收入", ("营业收入", "营收", "销售收入"), "money"),
    ("营业成本", ("营业成本",), "money"),
    ("总市值", ("总市值", "市值"), "money"),
    ("分红总额", ("总分红", "分红总额", "现金分红总额"), "money"),
    ("毛利率", ("毛利率",), "ratio"),
    ("净利率", ("净利率",), "ratio"),
    ("ROE", ("ROE", "净资产收益率", "加权roe"), "ratio"),
    ("资产负债率", ("资产负债率",), "ratio"),
    ("股息率", ("股息率",), "ratio"),
    ("分红率", ("分红支付率", "分红率", "支付率", "派息率"), "ratio"),
]

MONEY_UNITS = {"亿元": 1.0, "亿": 1.0, "百万元": 0.01, "百万": 0.01}
# 前置字符守卫：区间写法「8%-15%」里的 '-' 是连接符而非负号。若不做这个守卫，
# 抽取器会把 15 读成 **-15**，凭空造出一个数值并触发伪冲突（实测 002984 的 8/12 处
# advisory 冲突即由此类朴素匹配产生）。'至' 同理（「8%至15%」）。
NUM_UNIT_RE = re.compile(
    r"(?<![\d%％~～\-–—至])"
    r"(-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*(亿元|亿|百万元|百万|%|％)"
)
# 阈值/比较语境（<18%、低于 8%、跌破 4%）说的是**门槛**，不是报告观测值，不参与一致性比对。
CMP_MARKERS = ("<", ">", "≤", "≥", "低于", "高于", "跌破", "超过", "不足", "达到", "以上", "以下", "区间", "阈值")
# 门槛标记也可能**后置**（「维持 30% 以上」「跌破 4% 」），故数值后 3 个字符也要查一次。
CMP_SUFFIX = ("以上", "以下", "左右", "附近", "区间", "阈值", "之内", "以内", "之间",
              "个百分点", "百分点", "同比变动", "pct", "pp")
# 变化动词：「由 2024 年 32.84% 回落至 21.28%」里的目标值 21.28% 不属于 2024 年，
# 因此它不继承该行的唯一一个年份（否则会制造"同年两个毛利率"的伪冲突）。
CHANGE_VERBS = ("回落至", "降至", "升至", "下滑至", "提升至", "增至", "减少至",
                "涨至", "跌至", "提高至", "下降至", "上升至", "收窄至", "扩大至")
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_MASK_PATTERNS = [
    re.compile(r"\[src:[^\]]*\]"),
    re.compile(r"§\s*\d+(?:\.\d+)?"),
    re.compile(r"[Pp]\.?\s*\d+"),
    re.compile(r"\d{8}"),
    re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}"),
    # Markdown 强调/行内代码：会夹在「回落至」与数值之间（"回落至 **21.28%**"），
    # 使变化动词匹配失效并制造伪冲突。抹掉它们（保持偏移不变）。
    re.compile(r"\*+"),
    re.compile(r"`[^`\n]*`"),
]


# ─────────────────────────── extraction ───────────────────────────────

def _mask(text: str) -> str:
    """Blank out skip-spans, preserving character offsets."""
    out = list(text)
    for pat in _MASK_PATTERNS:
        for m in pat.finditer(text):
            for i in range(m.start(), m.end()):
                out[i] = " "
    return "".join(out)


def _classify(window: str, num_offset: int) -> Optional[Tuple[str, str]]:
    """Pick the category whose keyword is nearest, **preferring keywords that precede** the number.

    Chinese financial prose names the metric before its value ("毛利率 21.28%"). Two rules make
    this reliable in practice:

    - **Preceding wins.** Letting a following keyword win makes a number inherit the *next*
      metric's name (实测：同比序列被算成毛利率).
    - **Distance is measured from the keyword's end, and ties break by specificity.**
      "归母净利润" and "净利润" end at the same offset, so a pure start-distance comparison
      classifies 归母净利润 11.25 亿元 as plain 净利润. Ties therefore resolve to the earlier
      (more specific) CATEGORIES entry.
    """
    low = window.lower()
    best_prev = None  # (distance, category_index, canon, kind)
    best_any = None
    for ci, (canon, kws, kind) in enumerate(CATEGORIES):
        for kw in kws:
            hay = low if kw.islower() else window
            start = 0
            while True:
                idx = hay.find(kw, start)
                if idx < 0:
                    break
                if idx <= num_offset:
                    dist = max(0, num_offset - (idx + len(kw)))
                    cand = (dist, ci, canon, kind)
                    if best_prev is None or cand[:2] < best_prev[:2]:
                        best_prev = cand
                else:
                    cand = (idx - num_offset, ci, canon, kind)
                    if best_any is None or cand[:2] < best_any[:2]:
                        best_any = cand
                start = idx + 1
    if best_prev is not None:
        return best_prev[2], best_prev[3]
    # 回退到后置指标名时必须**紧邻**数值（如「32.84% 的毛利率」），否则会误吞左侧序列。
    if best_any is not None and best_any[0] <= 8:
        return best_any[2], best_any[3]
    return None


def _assign_years(masked: str,
                  cands: List[Tuple[int, int, float, str]]) -> Dict[Tuple[int, int], str]:
    """年份 → 它所标注的那个数字（**年份中心**归属）。

    中文财报行文惯例是「年份 + 数值」（"2025 年营收 86.12 亿"）或「数值 + （年份）」，
    因此对每个年份找它就近的数值：**右侧优先**（≤12 字符），其次左侧（≤8）。
    这能消除两类实测頑疾：表格跨行污染（溯源表备注列列举多个年份，会把它们抢给本行主值）
    与“一句话两个值只提一个年份”（“毛利率由 2024 年 32.84% 回落至 21.28%”）。

    行内**只有一个年份**时该行无歧义，其余数字继承它（用于找回“合并营收 86.12 亿…
    母公司口径营收 2,522.56 百万”这类真实口径差异），但：
    - **表格行不继承**：表格自带年份列，备注列又会列举其它年份；
    - **变化动词后的目标值不继承**（见 CHANGE_VERBS）。
    """
    starts = [c[0] for c in cands]
    assigned: Dict[Tuple[int, int], str] = {}
    for ym in YEAR_RE.finditer(masked):
        line_start = masked.rfind("\n", 0, ym.start()) + 1
        line_end = masked.find("\n", ym.end())
        if line_end < 0:
            line_end = len(masked)
        lo = bisect.bisect_left(starts, line_start)
        hi = bisect.bisect_right(starts, line_end)
        best = None  # ((side_rank, gap), (start, end))
        for s, e, _v, _u in cands[lo:hi]:
            if s >= ym.end():
                gap, rank = s - ym.end(), 0
                if gap > 12:
                    continue
            elif e <= ym.start():
                gap, rank = ym.start() - e, 1
                if gap > 8:
                    continue
            else:
                continue
            score = (rank, gap)
            if best is None or score < best[0]:
                best = (score, (s, e))
        if best is not None and best[1] not in assigned:
            assigned[best[1]] = ym.group(1)

    by_line: Dict[int, List[Tuple[int, int, float, str]]] = {}
    for cand in cands:
        by_line.setdefault(masked.rfind("\n", 0, cand[0]) + 1, []).append(cand)
    for line_start, items in by_line.items():
        if masked[line_start:line_start + 1] == "|":
            continue  # 表格行自带年份列，不做继承
        line_end = masked.find("\n", line_start)
        if line_end < 0:
            line_end = len(masked)
        years = {m.group(1) for m in YEAR_RE.finditer(masked[line_start:line_end])}
        if len(years) != 1:
            continue
        only_year = next(iter(years))
        for s, e, _v, _u in items:
            if (s, e) in assigned:
                continue
            if any(v in masked[max(0, s - 6):s] for v in CHANGE_VERBS):
                continue
            assigned[(s, e)] = only_year
    return assigned


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


class Obs:
    __slots__ = ("value", "line", "snippet")

    def __init__(self, value: float, line: int, snippet: str):
        self.value = value
        self.line = line
        self.snippet = snippet


def _gap(a0: int, a1: int, b0: int, b1: int) -> int:
    """Character gap between two spans (0 when they touch/overlap)."""
    if b0 >= a1:
        return b0 - a1
    if a0 >= b1:
        return a0 - b1
    return 0


def extract(text: str) -> Dict[Tuple[str, str], List[Obs]]:
    """Return {(category, year): [Obs, ...]} of normalized values."""
    masked = _mask(text)

    # Pass 1: candidate numbers, minus thresholds and percentage-point CHANGES.
    cands: List[Tuple[int, int, float, str]] = []
    for m in NUM_UNIT_RE.finditer(masked):
        raw, unit = m.group(1), m.group(2)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        # 阈值/比较语境（"<18%"、"低于 8%"）记录的是门槛而非报告值，不参与一致性比对。
        # 前缀只看紧邻 4 字符（避免误伤"高于 2024 年的 23.63%"这类真实观测值）；
        # 后缀放宽到 10 字符，以识别"个百分点 / 同比变动 / pct"这类**变化量**——
        # 变化量与水平值不是同一类观测量（实测：轮胎业务毛利率 21.13% 与其同比
        # -11.65 个百分点被混为一谈，制造伪冲突）。
        pre = masked[max(0, m.start() - 4):m.start()]
        if any(mk in pre for mk in CMP_MARKERS):
            continue
        post = masked[m.end():m.end() + 10]
        if any(mk in post for mk in CMP_SUFFIX):
            continue
        cands.append((m.start(), m.end(), value, unit))

    assigned = _assign_years(masked, cands)
    clusters: Dict[Tuple[str, str], List[Obs]] = {}
    for start, end, value, unit in cands:
        w0, w1 = max(0, start - WINDOW_BACK), min(len(masked), end + WINDOW)
        window = masked[w0:w1]
        cls = _classify(window, start - w0)
        if cls is None:
            continue
        canon, kind = cls
        if kind == "money":
            if unit not in MONEY_UNITS:
                continue
            norm = value * MONEY_UNITS[unit]
        else:  # ratio
            if unit not in ("%", "％"):
                continue
            norm = value
        year = assigned.get((start, end))
        if not year:
            continue  # 期间不可判定（窗口内无年份，或该年份已归属更贴近的数字）→ 跳过而不猜
        snippet = re.sub(r"\s+", " ", text[w0:w1]).strip()
        clusters.setdefault((canon, year), []).append(
            Obs(norm, _line_of(text, start), snippet))
    return clusters


def _distinct_groups(values: List[float], tol: float) -> List[float]:
    """Greedy grouping: representatives that pairwise differ by > tol."""
    reps: List[float] = []
    for v in sorted(values):
        if not any(abs(v - r) / max(abs(v), abs(r), 1e-9) <= tol for r in reps):
            reps.append(v)
    return reps


def find_conflicts(clusters, tol: float) -> List[dict]:
    conflicts = []
    for (canon, year), obs in clusters.items():
        vals = [o.value for o in obs]
        reps = _distinct_groups(vals, tol)
        if len(reps) < 2:
            continue
        rng = max(vals) - min(vals)
        spread = rng / max(abs(max(vals)), abs(min(vals)), 1e-9)
        conflicts.append({
            "category": canon, "year": year, "spread": spread,
            "values": sorted(set(round(v, 4) for v in vals)),
            "locations": sorted({o.line for o in obs}),
            "obs": obs,
        })
    conflicts.sort(key=lambda c: (-c["spread"], c["category"]))
    return conflicts


# ─────────────────────────── hard gates (杠杆7) ────────────────────────

DIMENSION_MINS = [("维度一", 600), ("维度二", 800), ("维度六", 300)]
REQUIRED_SECTIONS = ["## 数字溯源汇总", "## 结构化参数"]
_DEGRADE_MARKERS = ("不适用", "未触发", "不属于控股结构", "不涉及控股")

# 交付路径（--strict）下的**默认**声明覆盖率下限。
# 为什么是这个数：旧指标把年份/序号也算进分母（实测 2421 个"裸数字"里 49% 是噪声），
# 且把 I8 强制逐字嵌入的六个维度段计入分母（那些数字既不能绑定也不能改）。修正后
# 06862 实测决策区段覆盖率为 5.7%，旧产物为 0%。下限取 20% 是**起点而非目标**：
# 账本已从 28 条扩到 ~100 条，一份按契约逐值绑定的新报告可达 20–30%。
# 提高该值应当作为「账本继续长大」的伴随动作，而不是单独调数字。
DEFAULT_MIN_CLAIM_COVERAGE = 20.0


def _section_body(text: str, name: str) -> Optional[str]:
    m = re.search(r"^##\s*[^\n]*" + re.escape(name) + r"[^\n]*$", text, re.M)
    if not m:
        return None
    start = m.end()
    nxt = re.search(r"^##\s", text[start:], re.M)
    return text[start:start + nxt.start()] if nxt else text[start:]


def _cjk_count(s: str) -> int:
    return sum(1 for ch in s if "一" <= ch <= "鿿")


def check_gates(text: str) -> List[str]:
    failures = []
    for name, minimum in DIMENSION_MINS:
        body = _section_body(text, name)
        if body is None:
            if name == "维度六":  # conditional — absence is allowed
                continue
            failures.append(f"缺少章节「{name}」")
            continue
        if name == "维度六" and any(mk in body for mk in _DEGRADE_MARKERS):
            continue  # not triggered
        n = _cjk_count(body)
        if n < minimum:
            failures.append(f"「{name}」正文 {n} 字 < 门槛 {minimum} 字")
    for header in REQUIRED_SECTIONS:
        if header not in text:
            failures.append(f"缺少必备章节标题「{header}」")
    return failures


# ─────────────────────────── report render ────────────────────────────

# ─────────────────────── 声明校验（P2-20 改动 2 · PCN） ───────────────────────

def check_claims(report_text: str, claims_path: str):
    """Verify every claim-bound token against the §17 claim ledger.

    Fail-closed, per the PCN protocol (arXiv 2509.06902): a number is trusted only
    if it mechanically matches a claim under that claim's declared policy.
    Returns ``None`` on an unreadable ledger (caller exits 2).
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from claim_ledger import load_claims, verify_report
    except ImportError as e:  # pragma: no cover
        sys.stderr.write(f"[report_consistency] 无法加载 claim_ledger: {e}\n")
        return None
    if not os.path.isfile(claims_path):
        sys.stderr.write(f"[report_consistency] 声明账本不存在: {claims_path}\n")
        return None
    try:
        ledger = load_claims(claims_path)
    except Exception as e:
        sys.stderr.write(f"[report_consistency] 声明账本解析失败: {e}\n")
        return None
    return verify_report(report_text, ledger)


def render_claims(result: Dict) -> str:
    """Render the claim-verification section (appended to the consistency report)."""
    out = ["", "## §17 声明校验（--claims · fail-closed）", ""]
    total = result["tokens"]
    bare = result.get("bare") or {}
    if total == 0:
        out.append("> 报告中**没有任何** `<claim id=\"…\">值</claim>` 绑定 token。")
        out.append("> 若报告引用了 §17 数值却未绑定，说明提示词的绑定要求未被遵守 ——")
        out.append("> 这类数字按 PCN 契约**默认不可信**（Bare number）。")
        # 2026-10-14：**不再提前 return**。原实现在 tokens==0 时直接返回，
        # 于是覆盖率恰好为 0% 的最坏情形反而**连裸数字明细都不打印** ——
        # 静默发生在最需要它发声的时候。
        out.append("")
    else:
        out.append(f"> 绑定 token {total} 个：✅ 通过 {len(result['verified'])} · "
                   f"❌ 失败 {len(result['failed'])}")
        out.append("")
        if result["failed"]:
            out.append("| claim | 报告中的值 | 失败原因 |")
            out.append("| --- | --- | --- |")
            for item in result["failed"]:
                out.append(f"| `{item['claim_id']}` | {item['quoted']} | {item['detail']} |")
            out.append("")
            out.append("⚠️ **以上数字未能通过机械校验，不得作为结论依据。**")
        else:
            out.append("✅ 全部绑定 token 通过校验。")

    if bare.get("bare_tokens") is not None:
        bound = result["tokens"]
        tot = bound + bare["bare_tokens"]
        cov = (bound / tot * 100) if tot else 100.0
        out += ["", "### 未绑定数值 token（Bare number · 默认不可信）", "",
                f"> 绑定 {bound} · 未绑定 **{bare['bare_tokens']}**（已剔除年份/序号，"
                f"并排除逐字叙事段）· 决策区段覆盖率 **{cov:.1f}%**（共 {tot} 个量级 token）", ""]
        if bare.get("bare_all") is not None:
            out.append(f"> 参考：全文数字 token {bare['bare_all']} 个，其中量级 token "
                       f"{bare.get('bare_quantity_all', '—')} 个；逐字叙事段内的量"
                       "**不计入分母**（它们由 I8 保真约束，既不能绑定也不能删改）。")
            out.append("")
        if bare.get("sections"):
            out.append("| 区段（按未绑定数量降序，仅列计入分母者） | 未绑定数 | 样例 |")
            out.append("| --- | ---: | --- |")
            for sec in bare["sections"]:
                if sec.get("narrative"):
                    continue
                samples = "、".join(sec["samples"][:4])
                out.append(f"| {sec['section'][:48]} | {sec['count']} | {samples} |")
            out.append("")
        out.append("> 未绑定 ≠ 错误，但也**未获机械信任**：它们的可信度来自 `[src:]` 标注与"
                   "审计抽查。用 `--min-claim-coverage` 设定门限（`--strict` 下取默认下限）。")
    return "\n".join(out) + "\n"


def render(conflicts: List[dict], gate_failures: Optional[List[str]], tol: float) -> str:
    out = ["# 一致性审计 — consistency_report.md\n",
           f"> 容差 {tol:.0%}；本表为 **advisory**，真错误 vs 口径差异由 numeric_audit 裁定。\n"]
    out.append("## 跨段数值冲突\n")
    if not conflicts:
        out.append("✅ 未发现跨段数值冲突。\n")
    else:
        out.append(f"发现 **{len(conflicts)}** 处潜在冲突：\n")
        out.append("| 指标 | 年份 | 冲突数值 | 相对差 | 出现行号 |")
        out.append("| --- | --- | --- | ---: | --- |")
        for c in conflicts:
            vals = " / ".join(f"{v:g}" for v in c["values"])
            locs = ", ".join(str(l) for l in c["locations"])
            out.append(f"| {c['category']} | {c['year']} | {vals} | {c['spread']:.1%} | {locs} |")
        out.append("")
    if gate_failures is not None:
        out.append("## 硬门槛检查（--gates）\n")
        if not gate_failures:
            out.append("✅ 全部硬门槛通过。\n")
        else:
            out.append(f"❌ **{len(gate_failures)}** 项门槛未达标：\n")
            for f in gate_failures:
                out.append(f"- {f}")
            out.append("")
    out.append("---\n*consistency_report.md · 由 scripts/report_consistency.py 生成 · 内部工件（非交付物）*")
    return "\n".join(out) + "\n"


# ─────────────────────────────── CLI ──────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="定性报告跨段一致性 + 硬门槛审计")
    ap.add_argument("--report", required=True, help="qualitative_report.md 路径")
    ap.add_argument("--output", help="consistency_report.md 输出路径（可选）")
    ap.add_argument("--tolerance", type=float, default=0.05, help="同类判定容差（默认 0.05）")
    ap.add_argument("--gates", action="store_true", help="附加执行硬门槛检查（杠杆7）")
    ap.add_argument("--conflicts-are-failures", action="store_true",
                    help="恢复旧语义：advisory 跨段冲突也计入退出码（默认不计入；"
                         "冲突清单仍照常渲染，只是不阻断交付）")
    ap.add_argument("--strict", action="store_true",
                    help="交付路径：未加权重的 advisory 冲突仍不阻断，但**声明覆盖率**启用"
                         "默认下限（见 DEFAULT_MIN_CLAIM_COVERAGE）")
    ap.add_argument("--min-claim-coverage", type=float, default=None, metavar="PCT",
                    help="claim 绑定覆盖率下限（百分数）；低于则计为门槛失败"
                         "（未显式给出时：--strict 用默认下限，否则不启用）")
    ap.add_argument("--claims", metavar="CLAIMS_JSON",
                    help="§17 声明账本（claims.json）：对报告中每个 claim 绑定 token 做"
                         "机械校验（PCN 式，fail-closed）。需要 --gates 一并启用。")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.report):
        sys.stderr.write(f"[report_consistency] 文件不存在: {args.report}\n")
        return 2
    with open(args.report, "r", encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        sys.stderr.write("[report_consistency] 报告为空。\n")
        return 2

    conflicts = find_conflicts(extract(text), args.tolerance)
    gate_failures = check_gates(text) if args.gates else None
    claim_result = None
    if args.claims:
        if not args.gates:
            sys.stderr.write("[report_consistency] --claims 需要与 --gates 同时使用\n")
            return 2
        claim_result = check_claims(text, args.claims)
        if claim_result is None:
            return 2
        min_cov = args.min_claim_coverage
        if min_cov is None and args.strict:
            min_cov = DEFAULT_MIN_CLAIM_COVERAGE
        if min_cov is not None:
            bare_n = (claim_result.get("bare") or {}).get("bare_tokens", 0)
            tot = claim_result["tokens"] + bare_n
            cov = (claim_result["tokens"] / tot * 100) if tot else 100.0
            if cov < min_cov:
                _top = [s for s in (claim_result.get("bare") or {}).get("sections", [])
                        if not s.get("narrative")][:3]
                gate_failures.append(
                    f"claim 绑定覆盖率 {cov:.1f}% < 门槛 {min_cov:.1f}%"
                    f"（绑定 {claim_result['tokens']} / 共 {tot} 个量级 token；"
                    f"未绑定最集中的区段："
                    + "、".join(f"{s['section'][:24]}×{s['count']}" for s in _top) + "）")

    report = render(conflicts, gate_failures, args.tolerance)
    if claim_result is not None:
        report += render_claims(claim_result)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
    else:
        sys.stdout.write(report)

    n_gate = len(gate_failures) if gate_failures else 0
    n_claim = len(claim_result["failed"]) if claim_result else 0
    sys.stderr.write(
        f"[report_consistency] 冲突 {len(conflicts)} 处（advisory"
        + ("，已计入退出码" if getattr(args, "conflicts_are_failures", False) else "，不计入退出码")
        + f"） · 门槛失败 {n_gate} 项"
        + (f" · 声明校验失败 {n_claim} 处" if claim_result else "") + "\n")
    blocking = n_gate or n_claim
    if getattr(args, "conflicts_are_failures", False):
        blocking = blocking or bool(conflicts)
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
