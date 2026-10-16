"""声明账本与数值保真校验（known_issues.md P2-20 改动 1/2/3）。

为什么需要它
------------
§17 的数值以 markdown 表格交给 LLM，然后 LLM 在报告里**重新打一遍**这些数字。
重打这一步没有任何机械校验：数字可能被误抄、按错误单位换算、或被编造，
而报告读起来完全正常。

外部依据 [PCN: Proof-Carrying Numbers](https://ar5iv.labs.arxiv.org/html/2509.06902)
把这类失效定性为**呈现层问题**：检索与引用都无法保证保真，
必须由**渲染器**做机械校验并 **fail-closed**（未通过校验的数字默认不可信）。
论文同时指出 "schema-constrained decoding … fabricated numbers can still appear
structurally 'valid' without being faithful" —— 即"只把格式换成 JSON"并不解决问题。

本模块提供三件事
----------------
1. **声明（claim）**：Python 为每个可引用数值登记一条声明，形状取自 PCN：
   ``⟨claim_id, indicator, entity, time, value, unit, basis⟩``
   —— 单位是声明的一部分（XBRL 要求 ``monetaryItemType`` 必须带 ISO 4217 的
   ``unitRef``；FRED 的 ``units`` 是枚举，同一惯例）。
2. **KV 呈现**：决策单元格（Agent 要"选一行/选一格"的地方）以 ``key: value``
   形式输出并带上 ``claim_id``。11 种格式的对照实验显示 Markdown-KV 的数值检索
   准确率 60.7%，高于 markdown 表格 51.9% 与 JSON 52.3%
   （https://www.improvingagents.com/blog/best-input-data-format-for-llms）。
3. **校验**：``verify`` 按声明自带的策略（exact / round / alias / tolerance）
   机械比对，**fail-closed** —— 对不上就是 unverified，不会因为"看起来像"而放行。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# 单位枚举 —— 在**产生点**约束值域（照搬定性契约 dim_result.schema.json 的 unit enum）
# ---------------------------------------------------------------------------
UNIT_ENUM = (
    "百万", "百万港元", "百万美元", "百万人民币",
    "亿", "万元", "元", "港元", "美元",
    "元/股", "港元/股", "美元/股",
    "%", "pct", "pct点", "倍", "股", "—",
)

# ---------------------------------------------------------------------------
# 别名/尺度换算：同一物理量的不同单位写法。
# 这正是 `_HK_MKT_CAP_RAW_FLOOR`（百万 vs 原始单位）与"亿元/百万元"混用
# 那一类缺陷的机械解 —— PCN 的 "alias equivalence" 策略。
# 因子含义：给定单位的数值 × 因子 = 以「基准单位」（百万 / %）表示的数值。
# ---------------------------------------------------------------------------
UNIT_SCALES: Dict[str, float] = {
    "百万": 1.0, "百万港元": 1.0, "百万美元": 1.0, "百万人民币": 1.0,
    "亿": 100.0, "亿元": 100.0,
    "万元": 0.01, "万": 0.01,
    "元": 1e-6,
    "千元": 1e-3, "千": 1e-3,
    "%": 1.0, "pct": 1.0, "pct点": 1.0,
}

# 校验策略
POLICY_EXACT = "exact"
POLICY_ROUND = "round"            # round:<d> 四舍五入到 d 位小数
POLICY_ALIAS = "alias"            # 允许 UNIT_SCALES 内的单位换算；``alias:<abs_tol>`` 可显式放行换算后的精度损失
POLICY_TOLERANCE = "tolerance"    # tolerance:<abs>,<rel>，须带限定词
VALID_POLICIES = (POLICY_EXACT, POLICY_ROUND, POLICY_ALIAS, POLICY_TOLERANCE)

# 允许"约/≈/大约"这类限定词触发 tolerance
_QUALIFIERS = ("约", "大约", "≈", "~", "左右", "大致")

_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
# 注意交替顺序：**长单位必须排在短单位之前**，否则 '港元/股' 会被 '港元' 抢先匹配。
_UNIT_SUFFIX_RE = re.compile(
    r"^\s*("
    r"百万港元|百万美元|百万人民币|百万元|百万"
    r"|元/股|港元/股|美元/股"
    r"|亿元|万元|千元|亿|万|千"
    r"|港元|美元|元"
    r"|pct点|pct|%|倍|股"
    r")?"
)


class ClaimError(ValueError):
    """Raised on an out-of-contract claim."""


def parse_quantity(text: str) -> Tuple[Optional[float], Optional[str]]:
    """Parse ``(value, unit)`` from a numeric fragment.

    Returns ``(None, None)`` when no number is present. The unit is ``None`` when
    the text carries no recognised suffix (the caller must then decide whether a
    bare number is acceptable — by default it is *not*, see :meth:`ClaimLedger.verify`).
    """
    match = _NUM_RE.search(text or "")
    if not match:
        return None, None
    try:
        value = float(match.group(0).replace(",", ""))
    except ValueError:
        return None, None
    rest = (text or "")[match.end():]
    unit_match = _UNIT_SUFFIX_RE.match(rest)
    unit = unit_match.group(1) if unit_match and unit_match.group(1) else None
    return value, unit


def units_compatible(a: Optional[str], b: Optional[str]) -> bool:
    """True when two unit labels denote the same magnitude (no scale change).

    ``%`` / ``pct`` / ``pct点`` all carry scale 1.0 — they differ in *meaning*
    (a rate vs a difference of rates), not in dimension, and the pack renders all
    of them with a ``%`` sign. Requiring literal label equality produced a false
    verification failure on a live run: a claim declared as ``pct点`` displays as
    ``0.78%``, so a report copying the displayed value was rejected. Scale
    conversion (百万 ↔ 亿) is a *different* concession and stays behind `alias`.
    """
    if a is None or b is None:
        return True
    if a == b:
        return True
    fa, fb = UNIT_SCALES.get(a), UNIT_SCALES.get(b)
    return fa is not None and fb is not None and fa == fb


def _to_base(value: float, unit: Optional[str], base_unit: str) -> Optional[float]:
    """Convert ``value`` expressed in ``unit`` into ``base_unit``.

    Only defined for units present in :data:`UNIT_SCALES`. Returns ``None`` when
    the conversion is not sanctioned — which the caller must treat as a failure,
    never as "probably fine".
    """
    if unit is None:
        return value if base_unit in ("%", "pct", "pct点", "倍", "股", "元/股", "港元/股", "美元/股") else None
    if unit == base_unit:
        return value
    factor = UNIT_SCALES.get(unit)
    base_factor = UNIT_SCALES.get(base_unit)
    if factor is None or base_factor is None:
        return None
    return value * factor / base_factor


@dataclass
class Claim:
    """A single authoritative numeric statement (PCN claim shape)."""

    claim_id: str
    indicator: str
    value: Optional[float]
    unit: str
    entity: str = ""
    time: Optional[str] = None
    basis: str = ""
    policy: str = POLICY_EXACT
    note: Optional[str] = None
    section: str = ""

    def __post_init__(self) -> None:
        if self.unit not in UNIT_ENUM:
            raise ClaimError(
                f"claim {self.claim_id}: unit={self.unit!r} 不在 UNIT_ENUM 中")
        base = self.policy.split(":")[0]
        if base not in VALID_POLICIES:
            raise ClaimError(
                f"claim {self.claim_id}: policy={self.policy!r} 不在 {VALID_POLICIES}")
        # fail-closed：不可得的数值必须带 note 说明原因（null ≠ 0）
        if self.value is None and not self.note:
            raise ClaimError(
                f"claim {self.claim_id}: value 为 null 时必须给出 note（说明不可得原因）")

    @property
    def display(self) -> str:
        if self.value is None:
            return "—"
        if self.unit in ("%", "pct", "pct点"):
            return f"{self.value:.2f}%"
        if self.unit in ("倍",):
            return f"{self.value:.2f}"
        return f"{self.value:,.2f}"

    def as_dict(self) -> Dict:
        payload = asdict(self)
        payload["display"] = self.display
        return payload


class ClaimLedger:
    """Registry of authoritative claims for one stock."""

    def __init__(self, stock_code: str = "") -> None:
        self.stock_code = stock_code
        self._claims: Dict[str, Claim] = {}
        self._order: List[str] = []

    # -- producer API ------------------------------------------------------
    def add(self, claim_id: str, indicator: str, value: Optional[float], unit: str,
            *, time: Optional[str] = None, basis: str = "", policy: str = POLICY_EXACT,
            note: Optional[str] = None, section: str = "") -> Claim:
        existing = self._claims.get(claim_id)
        if existing is not None:
            # 重算同一小节必须**替换**该声明，而不是报重复 —— 但同 id 异指标
            # 是真冲突（两个不同的量共用一个 id），必须硬错误，不能静默覆盖。
            if existing.indicator != indicator:
                raise ClaimError(
                    f"claim_id {claim_id} 已被指标 {existing.indicator!r} 占用，"
                    f"不能改指 {indicator!r}")
        claim = Claim(claim_id=claim_id, indicator=indicator, value=value, unit=unit,
                      entity=self.stock_code, time=time, basis=basis, policy=policy,
                      note=note, section=section)
        self._claims[claim_id] = claim
        if claim_id not in self._order:
            self._order.append(claim_id)
        return claim

    # -- consumer API ------------------------------------------------------
    def __len__(self) -> int:
        return len(self._claims)

    def __contains__(self, claim_id: object) -> bool:
        return claim_id in self._claims

    def get(self, claim_id: str) -> Optional[Claim]:
        return self._claims.get(claim_id)

    def claims(self) -> List[Claim]:
        return [self._claims[cid] for cid in self._order]

    def render_kv(self, claim_ids: Iterable[str], title: str = "") -> str:
        """Render claim-bound KV blocks (改动 1).

        ``key: value`` beats markdown tables for numeric retrieval by ~8.4 points
        in the 11-format benchmark, and each block carries its ``claim_id`` so a
        verifier can bind what the LLM later quotes.
        """
        lines = []
        if title:
            lines.append(f"#### {title}")
            lines.append("")
        lines.append("```")
        for cid in claim_ids:
            claim = self._claims.get(cid)
            if claim is None:
                continue
            lines.append(f"claim: {claim.claim_id}")
            lines.append(f"indicator: {claim.indicator}")
            if claim.time:
                lines.append(f"time: {claim.time}")
            lines.append(f"unit: {claim.unit}")
            if claim.basis:
                lines.append(f"basis: {claim.basis}")
            if claim.value is None:
                lines.append(f"value: null   # {claim.note}")
            else:
                lines.append(f"value: {claim.display}")
            lines.append(f"policy: {claim.policy}")
            lines.append("")
        lines.append("```")
        return "\n".join(lines)

    # -- verification ------------------------------------------------------
    def verify(self, claim_id: str, quoted: str) -> Tuple[bool, str]:
        """Check a quoted fragment against its claim, **fail-closed**.

        Returns ``(verified, detail)``. Any ambiguity — unknown claim, unparsable
        number, un-sanctioned unit — returns ``False``: trust is earned by a
        mechanical match, never assumed.
        """
        claim = self._claims.get(claim_id)
        if claim is None:
            return False, f"未知声明 {claim_id}（声明绑定的 id 必须在 claims.json 中存在）"
        if claim.value is None:
            return False, f"{claim_id} 的值为 null（不可得），不得引用为数值"
        value, unit = parse_quantity(quoted)
        if value is None:
            return False, f"{claim_id}: 未能从 {quoted!r} 中解析出数值"

        base = claim.policy.split(":")[0]
        if base == POLICY_EXACT:
            if not units_compatible(unit, claim.unit):
                return False, f"{claim_id}: 单位不符（引用 {unit} vs 声明 {claim.unit}）"
            return (abs(value - claim.value) < 1e-9,
                    "精确匹配" if abs(value - claim.value) < 1e-9
                    else f"{claim_id}: {value} != {claim.value}")

        if base == POLICY_ROUND:
            digits = int(claim.policy.split(":")[1]) if ":" in claim.policy else 2
            if not units_compatible(unit, claim.unit):
                return False, f"{claim_id}: 单位不符（引用 {unit} vs 声明 {claim.unit}）"
            ok = round(value, digits) == round(claim.value, digits)
            return ok, ("四舍五入匹配" if ok else
                        f"{claim_id}: round{digits}({value}) != round{digits}({claim.value})")

        if base == POLICY_ALIAS:
            converted = _to_base(value, unit, claim.unit)
            if converted is None:
                return False, f"{claim_id}: 单位 {unit!r} 无法换算到 {claim.unit!r}（未获准的换算）"
            # `alias:<abs_tol>` 显式声明"换算后允许的精度损失"。
            # 无容差时要求换算后严格相等 —— 例如把 46,129.05 百万写成 461.29 亿
            # 是**降精度**表述，必须由声明方显式放行，而不是校验方默认宽容。
            parts = claim.policy.split(":", 1)
            if len(parts) > 1 and parts[1]:
                abs_tol = float(parts[1].split(",")[0])
            else:
                abs_tol = 1e-6 * max(1.0, abs(claim.value))
            ok = abs(converted - claim.value) <= abs_tol
            return ok, ("单位换算后匹配" if ok else
                        f"{claim_id}: 换算后 {converted} 与 {claim.value} 相差 "
                        f"{abs(converted - claim.value):g} > 容差 {abs_tol:g}"
                        + ("（如需允许降精度表述，请把策略写成 alias:<容差>）" if len(parts) == 1 else ""))

        if base == POLICY_TOLERANCE:
            if not any(q in quoted for q in _QUALIFIERS):
                return False, f"{claim_id}: tolerance 策略要求引用处带限定词（约/≈/左右 等）"
            # policy 形如 `tolerance:<abs>,<rel>`（逗号分隔参数）
            params = claim.policy.split(":", 1)[1].split(",") if ":" in claim.policy else []
            abs_tol = float(params[0]) if len(params) > 0 and params[0] else 0.0
            rel_tol = float(params[1]) if len(params) > 1 and params[1] else 0.0
            window = max(abs_tol, rel_tol * abs(claim.value))
            ok = abs(value - claim.value) <= window
            return ok, ("容差内匹配" if ok else
                        f"{claim_id}: |{value} − {claim.value}| > {window}")

        return False, f"{claim_id}: 未知策略 {claim.policy}"

    # -- serialisation -----------------------------------------------------
    def snapshot(self, units: Optional[Dict] = None) -> Dict:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "stock_code": self.stock_code,
            "claims": [c.as_dict() for c in self.claims()],
        }
        if units:
            payload["units"] = units
        return payload


def write_claims(path: str | Path, ledger: ClaimLedger,
                 units: Optional[Dict] = None) -> Path:
    """Write ``claims.json`` next to the data pack."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ledger.snapshot(units), ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    return out


def load_claims(path: str | Path) -> ClaimLedger:
    """Load a ``claims.json`` file back into a ledger."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ledger = ClaimLedger(data.get("stock_code", ""))
    for entry in data.get("claims", []):
        ledger.add(
            entry["claim_id"], entry["indicator"], entry.get("value"), entry["unit"],
            time=entry.get("time"), basis=entry.get("basis", ""),
            policy=entry.get("policy", POLICY_EXACT), note=entry.get("note"),
            section=entry.get("section", ""),
        )
    return ledger


# ---------------------------------------------------------------------------
# 报告侧的声明绑定 token（PCN 的 `<claim id="CID" policy="P">VAL</claim>`）
# ---------------------------------------------------------------------------
TOKEN_RE = re.compile(
    r"<claim\s+id=\"(?P<id>[^\"]+)\"(?:\s+policy=\"(?P<policy>[^\"]+)\")?\s*>(?P<val>[^<]*)</claim>")


_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_HEAD_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.M)

# 量纲标记：紧跟数字之后的这些字符说明它是**量**，不是章节/列表序号。
_UNIT_AFTER = ("%", "pct", "倍", "万", "亿", "百万", "千元", "元", "港元", "美元",
               "股", "个", "家", "次", "x", "X", "×", "‰")
_SKIP_YEARS = {"2020", "2021", "2022", "2023", "2024", "2025", "2026", "2027", "2028"}

# **逐字嵌入**的叙事段落：I8 要求这些段落与 dims 源文逐字一致，其内部数字**既不能
# 绑定也不能删改**。把它们的裸数字计入覆盖率分母，等于逼作者在「违反保真」与
# 「覆盖率不及格」之间二选一 —— 故必须排除在分母之外。
# 两套标题风格都要覆盖：当前龟龟报告用「## 维度一：…」，旧报告用「### 1. 商业模式与资本特征」。
_NARRATIVE_SECTION_RE = re.compile(
    r"维度[一二三四五六]|商业模式与资本特征|竞争优势|护城河|外部环境|管理层与|控股结构"
    r"|商业质量|MD&A|口径缺口|警告|Warnings|禁止引用|风险提示|待验证|数据来源|免责")


def _is_quantity(raw: str, after: str, before: str) -> bool:
    """判断数字 token 是不是**量**（而不是 1./2. 这类序号）。"""
    if any(after.startswith(u) for u in _UNIT_AFTER):
        return True
    if "." in raw:
        return True
    digits = raw.replace(",", "").lstrip("+-")
    if len(digits) >= 3:
        return True
    return any(before.endswith(q) for q in ("约", "≈", "~", "近", "超过", "不足"))


def bare_sections(text: str, limit: int = 8) -> Dict:
    """枚举**未绑定**的数值 token，按最近的上级标题分组。

    为什么需要：``verify_report`` 只遍历 ``<claim>`` 标记（见 ``extract_tokens``），
    未绑定的裸数字**既不失败、也不计数** —— 而 AGENTS.md 声称 "a bare number is
    untrusted by default"。本函数让"未绑定"变成**可度量**。

    2026-10-14 两处收紧（原实现把"覆盖率"这个指标做成了不可用的数）：

    ① **过滤非量 token**：原实现用 ``-?\\d[\\d,]*`` 抓全部数字，实测 06862 报告
       2,421 个"裸数字"里 214 个是四位年份、967 个是 1–2 位序号（``5、2025、4``），
       占 49%。分母被噪声撑大后，"覆盖率 1.9%" 既不反映真实情况，也永远无法达标。
       现按**上下文**判定（带单位/百分号、带小数点、有效位数≥3），序号与年份不计入。
    ② **分母排除逐字叙事段**：六个维度段是 dims 的**逐字嵌入**（I8 强制），其数字
       不能绑定也不能改。把它们计入分母会与保真要求直接冲突。

    返回 ``bare_tokens``（计入分母的未绑定数）、``bare_all``（未过滤总数）与
    按区段分组的明细（``narrative`` 标明该段是否被排除）。
    """
    masked = TOKEN_RE.sub(lambda m: " " * len(m.group(0)), text or "")
    heads = [(m.start(), m.group(1)) for m in _HEAD_RE.finditer(masked)]
    all_count = 0
    counted = 0
    buckets: Dict[str, List[str]] = {}
    for m in _NUM_RE.finditer(masked):
        all_count += 1
        raw = m.group(0)
        after = masked[m.end():m.end() + 6].lstrip(" *")
        before = masked[max(0, m.start() - 3):m.start()]
        if raw in _SKIP_YEARS or not _is_quantity(raw, after, before):
            continue
        sec = "（无标题）"
        for pos, title in heads:
            if pos < m.start():
                sec = title
            else:
                break
        buckets.setdefault(sec, []).append(raw)

    detail = []
    for sec, toks in buckets.items():
        narrative = bool(_NARRATIVE_SECTION_RE.search(sec))
        detail.append({"section": sec, "count": len(toks), "narrative": narrative,
                       "samples": toks[:5]})
        if not narrative:
            counted += len(toks)
    detail.sort(key=lambda d: -d["count"])
    return {"bare_tokens": counted, "bare_all": all_count,
            "bare_quantity_all": sum(d["count"] for d in detail),
            "sections": detail[:limit], "sections_all": detail}


def extract_tokens(text: str) -> List[Dict[str, str]]:
    """Return every claim-bound token in ``text``."""
    return [m.groupdict() for m in TOKEN_RE.finditer(text or "")]


def verify_report(text: str, ledger: ClaimLedger) -> Dict:
    """Verify every claim-bound token in a report.

    Returns ``{"tokens": n, "verified": [...], "failed": [...], "bare": {...}}``.

    ``bare`` 统计**未绑定**的数值 token（按区段），使"裸数字默认不可信"这条原则可度量。
    A token that cannot be bound, or whose value fails its policy, is reported as
    **failed** — callers should treat a non-empty ``failed`` as a gate failure.
    """
    verified, failed = [], []
    for token in extract_tokens(text):
        ok, detail = ledger.verify(token["id"], token["val"])
        record = {"claim_id": token["id"], "quoted": token["val"].strip(), "detail": detail}
        (verified if ok else failed).append(record)
    return {"tokens": len(verified) + len(failed), "verified": verified,
            "failed": failed, "bare": bare_sections(text)}
