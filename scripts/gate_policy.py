#!/usr/bin/env python3
"""gate_policy.py —— FAIL 分级与交付裁决的**单点实现**。

为什么单独成模块
----------------
三处需要同一套语义（采集器、数值审核、观点审核），而本仓库已经发生过
"同一条阈值/公式在三处重复实现"的事故（`known_issues.md` P1-12 门槛 II 的
`max(5%, Rf+3%)`）。分级是**政策**而不是事实：它决定"哪个失败会阻断交付"，
必须只有一份、可被测试锁定、可被评审。

两档语义
--------
- ``blocking``   —— 该失败会**改变结论数字**（错值/漏行/币种/口径），
  默认阻断交付；只有 ``--allow-degraded`` 能显式放行，且放行必须留痕披露。
- ``disclosure`` —— 相关数字**不得引用**，但不阻断交付（某列上游回填、
  某小节不可判定、口径待确认等）。

判据来源
--------
1. 已有的包级门禁按 **code** 分级（I1–I10 / C1–C2 / SC / STALE17）；
2. 两个审核层按 **severity**（critical/major/minor）分级 —— 审核脚本自己
   判定严重度，本模块只负责把它翻译成交付语义，不重复判断内容。

⚠️ 改动本表等于改动交付政策：必须同时更新 `known_issues.md` 与相关测试，
不得在调用点就地放宽。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

BLOCKING = "blocking"
DISCLOSURE = "disclosure"

SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# 包级门禁：按 code 分级
# ---------------------------------------------------------------------------
_CODE_TIER: Dict[str, tuple] = {
    # —— 阻断级：数字本身错/缺，或错到会改变结论 ——
    "I1": (BLOCKING, "CAGR 缺基年/期数标注 ⇒ 增长率含义不确定"),
    "I2": (BLOCKING, "每股价币种混用 ⇒ R/GG/买点直接算错（P0-2 的形态）"),
    "I4": (BLOCKING, "§3 同年口径混用（持续经营 vs 含已终止）⇒ 以除税前/除税后为基础的比率被污染"),
    "I7": (BLOCKING, "§2 与 §17 所用价格不一致 ⇒ 价格敏感结果与展示价脱钩"),
    "I8": (BLOCKING, "资产负债表恒等式不成立 ⇒ 存在漏行/串行/口径错位"),
    "I9": (BLOCKING, "股数口径不一致 ⇒ 市值与每股指标不同源"),
    "C1": (BLOCKING, "关键报表缺必备行 ⇒ 缺行对自洽类不变式不可见"),
    "C2": (BLOCKING, "债务构成不可核验或声明行全空 ⇒ 有息负债可能漏计"),
    # C3 与 C1/C2 是**同一失败模式**（"声明了但从未有值"），此前未登记 ⇒ 落到
    # `DEFAULT_TIER=DISCLOSURE` 并带"未登记分级"的说明。登记为阻断级是为了语义正确：
    # 该缺口一旦为真就会改变结论数字。但必须同时知道它的**两处边界**：
    #   ① `check_section4_key_rows` 默认只返回 `WARN`、把 FAIL 放在 `strict_status`；
    #   ② `_STATUS_NEEDS_TIER` 刻意不含 `WARN`。
    # 于是**采集器从不以 --strict 调 C3** ⇒ 默认交付路径下 C3 永不阻断。
    # 实测 2026-10-16（06078）：§4「商誉」行全空而 §17.15 按 0 扣除、
    # §17.8 商誉/总资产渲染 0.00%，C3 如实报了 WARN，门禁仍判「可交付」——
    # 这正是 C3 自己 docstring 里预警过的 000708 形态（当年高估下行净值 71.8 亿）。
    "C3": (BLOCKING, "§4 决策关键行声明但全空 ⇒ 下游（§17.8 商誉/总资产、§17.15 防御性净值）"
                     "静默按 0 处理；因默认状态为 WARN，仅 --strict 才升级为 FAIL"),
    "NA": (BLOCKING, "数值审核发现派生不一致/输入无来源/单位错"),
    # —— 披露级：相关数字不得引用，但不阻断 ——
    "I3": (DISCLOSURE, "§5↔§4 现金勾稽不可判定 ⇒ 不得作现金净增减叙事"),
    "I5": (DISCLOSURE, "历史列跨年恒定（上游回填）⇒ 该列不得用于趋势/分位/派息率"),
    "I6": (DISCLOSURE, "敏感性方向一致性不可判定"),
    "I10": (DISCLOSURE, "恒定历史列（泛化）——需确认是真实恒定还是回填"),
    "SC": (DISCLOSURE, "§17 状态契约与包不一致 ⇒ 小节存在性不可判定"),
    "STALE17": (DISCLOSURE, "§17 未随本次市场刷新重建 ⇒ 网格与展示价可能不同源"),
    # —— 选股器（turtle_screener）：只登记**全局**输入缺口 ——
    # 逐股的数据缺口（no_cashflow / no_price / stale_snapshot …）**刻意不登记**：
    # 它们是"该股被合格地排除"，属每只股票的正常结果，写进 excluded.json 并带受控
    # 原因码即可，不应阻断当日交付。只有会让**整批**口径失真的缺口才阻断。
    "rf_missing": (BLOCKING, "选股器无风险利率不可用 ⇒ II 错 ⇒ 全市场排名口径全错"),
    "calendar_missing": (BLOCKING, "该市场交易日历不可用 ⇒ 无法判定当日是否可跑"),
    "market_data_not_ready": (BLOCKING, "当日行情未就绪 ⇒ 用昨日数据假装今日会静默产出错排名"),
}
DEFAULT_TIER = DISCLOSURE

# ---------------------------------------------------------------------------
# 审核层：按 severity 分级（审核脚本判定严重度，本模块只翻译语义）
# ---------------------------------------------------------------------------
_SEVERITY_TIER: Dict[str, tuple] = {
    "critical": (BLOCKING, "审核判定为 critical：错误足以改变投资结论"),
    "major": (BLOCKING, "审核判定为 major：数值/论证实质错误，修复前不得交付"),
    "minor": (DISCLOSURE, "审核判定为 minor：表述或口径瑕疵，不阻断但需登记"),
}

#: 会触发分级裁决的失败状态。**刻意不含 `WARN`** ——
#: `WARN` 按定义是**咨询性**状态（"需确认/无法确认"），不是"算错了"；
#: 真正要阻断的发现必须由不变式返回 `FAIL`/`MISMATCH` 等，而不是 `WARN`。
#:
#: 为什么必须这样改（实测 2026-10-14）：`I4` 属阻断级，而它对**每一只 A 股**都返回
#: `WARN`（上游利润表没有「持续经营业务税后利润」行 ⇒ 无法确认口径，
#: 但三数逐年差 0.00，合计口径是自洽的）。把 `WARN` 计入 ⇒ **全部 A 股产物不可交付**，
#: 整条 A 股选股链路被一个咨询性状态挡死。
#: `invariants.py` 的 I4 docstring 本就写着要避免这件事（"否则…全部 A 股包不可交付"），
#: 但别名修复只把它从 `UNEVALUATED` 变成 `WARN`，而两者都在本表里 ⇒ 问题未解决。
_STATUS_NEEDS_TIER = ("FAIL", "MISMATCH", "UNSOURCED", "UNVERIFIABLE", "UNEVALUATED")


def classify(code: Optional[str] = None,
             severity: Optional[str] = None) -> tuple:
    """Return ``(tier, reason)``. ``severity`` wins over ``code`` when both given.

    Why severity first: the audit layers judge *how bad* a finding is from content
    (R/GG 差 10× is critical, a labelling nit is minor) — a code table cannot know
    that. The code table exists for the package gates, whose severity is fixed.
    """
    if severity:
        hit = _SEVERITY_TIER.get(str(severity).lower())
        if hit:
            return hit
    if code and code in _CODE_TIER:
        return _CODE_TIER[code]
    return DEFAULT_TIER, "未登记分级（默认按披露处理，需在 gate_policy 中登记）"


def _norm(findings: Optional[Iterable]) -> List[dict]:
    out = []
    for f in (findings or []):
        if isinstance(f, dict):
            out.append(f)
        else:  # dataclass-ish (invariants.Finding / field_coverage.Finding)
            out.append({
                "code": getattr(f, "code", None),
                "status": getattr(f, "status", None),
                "title": getattr(f, "title", None),
                "detail": getattr(f, "detail", None),
                "severity": getattr(f, "severity", None),
                "gate": getattr(f, "gate", None),
            })
    return out


def verdict(*finding_groups, allow_degraded: bool = False,
            stock_code: str = "") -> dict:
    """Fold findings from every gate/audit layer into one delivery verdict.

    Each group is either a list of findings or a ``(gate_name, findings)`` tuple.
    A finding is **blocking** when its severity (preferred) or its code maps to
    ``blocking`` *and* its status is a failure-ish status; everything else becomes
    disclosure. ``allow_degraded`` flips ``deliverable`` but never hides the list.
    """
    blocking: List[dict] = []
    disclosure: List[dict] = []
    for group in finding_groups:
        if isinstance(group, tuple) and len(group) == 2 and not isinstance(group[0], dict):
            gate_name, items = group
        else:
            gate_name, items = "", group
        for f in _norm(items if not isinstance(items, dict) else [items]):
            status = str(f.get("status") or "").upper()
            code = f.get("code")
            severity = f.get("severity")
            tier, why = classify(code, severity)
            rec = {
                "gate": f.get("gate") or gate_name or "",
                "code": code or severity or "?",
                "title": f.get("title"),
                "detail": f.get("detail"),
                "status": status or None,
                "severity": severity,
                "why": why,
            }
            if status in ("PASS", "MATCH", "OK", "SOURCED"):
                continue
            if tier == BLOCKING and status in _STATUS_NEEDS_TIER:
                blocking.append(rec)
            elif status:
                disclosure.append(rec)

    degraded = bool(blocking) and allow_degraded
    return {
        "schema_version": SCHEMA_VERSION,
        "stock_code": stock_code,
        "deliverable": not blocking or allow_degraded,
        "degraded": degraded,
        "blocking": blocking,
        "blocking_count": len(blocking),
        "disclosure": disclosure,
        "disclosure_count": len(disclosure),
        "policy": {
            "blocking_codes": sorted(c for c, (t, _) in _CODE_TIER.items() if t == BLOCKING),
            "blocking_severities": sorted(s for s, (t, _) in _SEVERITY_TIER.items() if t == BLOCKING),
            "overridden_by": "--allow-degraded" if allow_degraded else None,
        },
    }


def print_verdict(v: dict, label: str = "门禁") -> None:
    """Human-readable verdict to stdout (exit code carries the machine signal)."""
    if v.get("blocking"):
        print(f"  ⛔ {label}：阻断级失败 {v['blocking_count']} 项 —— 按契约不得交付：")
        for b in v["blocking"]:
            print(f"     - [{b['gate']}/{b['code']}] {b.get('why')}")
        if v.get("degraded"):
            print("  ⚠️ 已用 --allow-degraded 显式降级：本次产物**带缺陷交付**，"
                  "degraded=true 必须随报告披露。")
        else:
            print("  → 修数据后重跑；或确知影响面时用 --allow-degraded 显式降级重跑。")
    if v.get("disclosure"):
        print(f"  ⚠️ {label}：披露级信号 {v['disclosure_count']} 项"
              "（不阻断，但相关数字不得被引用）")
    if v.get("deliverable") and not v.get("blocking"):
        print(f"  ✅ {label}：可交付（无阻断级失败）")


def exit_code(v: dict) -> int:
    """1 when not deliverable, else 0 — the only signal every caller reads."""
    return 0 if v.get("deliverable", True) else 1
