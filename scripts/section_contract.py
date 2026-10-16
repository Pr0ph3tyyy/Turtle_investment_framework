"""§17 小节契约与状态登记（known_issues.md P2-20，改动 4）。

为什么这个模块存在
------------------
§17 的 16 个小节**都是可选的** —— 任一小节算不出来时它就从数据包里消失。
而提示词为了兜住这种消失，写了 4 条「降级路径」手算公式（§17.10/11/12/13），
**直接违反核心不变量「LLM 不做算术」**。

根因不是 markdown 格式，是**缺席没有机器可读信号**：读者看到 §17.11 不在，
无法区分「没算」与「算出来是 0」，于是只能自己算。外部证据（arXiv 2509.06902）
同样指出：引用与检索都无法保证数值保真，必须靠**呈现层的机械校验**兜底。

本模块提供一个 fail-closed 的契约：

- 小节目录 ``SECTION_TITLES`` 是唯一权威清单
- 每个小节必须被显式登记状态；**未登记 = missing**（不是"没有就算通过"）
- ``status != ok`` 时必须给受控词表里的 ``reason``，据此决定补救动作
- 落盘 ``section17_status.json``，调用方（LLM 或脚本）据此**停止**，而不是手算

状态语义
--------
``ok``        本节已生成，数值可直接引用
``degraded``  本节已生成但带降级说明（如部分年份缺失）；引用时须一并读取 warning
``missing``   本节**未生成**；调用方必须停止并报告，**不得手算**
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

SCHEMA_VERSION = "1.0"

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_MISSING = "missing"
VALID_STATUSES = (STATUS_OK, STATUS_DEGRADED, STATUS_MISSING)

# ---------------------------------------------------------------------------
# §17 小节目录 —— 契约。
# 任何不在本目录里的编号都会被 mark() 拒绝（在产生点约束值域，而非事后统一）。
# 任何在本目录里但未被登记的小节，finalize 时一律标为 missing（fail-closed）。
# ---------------------------------------------------------------------------
SECTION_TITLES: Dict[str, str] = {
    "17.1": "财务趋势速览",
    "17.2": "因子2输入参数",
    "17.3": "因子3·步骤1 真实现金收入（保守基准）",
    "17.4": "因子3·步骤4 经营性现金支出",
    "17.5": "因子3·步骤7 基准可支配结余 + 敏感性输入",
    "17.6": "因子4·股价分位",
    "17.7": "控股结构辅助",
    "17.8": "因子4·绝对估值与「买入就是胜利」基准价",
    "17.9": "因子4·业绩下滑敏感性",
    "17.10": "支付率 M 三重校验",
    "17.11": "穿透回报率网格（税后）",
    "17.12": "G 系数网格（维持性 Capex）",
    "17.13": "收入敏感性（λ 外推）",
    "17.14": "悲观基准网格（k 折扣）",
    "17.15": "防御性净值（假资产剔除）",
    "17.16": "防御性门槛组",
}

# ---------------------------------------------------------------------------
# 缺失原因受控词表。
# 给 **code** 而不是自由文本：调用方据此决定补救动作，而不是猜。
# ---------------------------------------------------------------------------
REASONS: Dict[str, Dict[str, str]] = {
    "no_rf": {
        "label": "无风险利率不可用（yc_cb 无权限且未提供替代值）",
        "remedy": "在数据包 §14 补写无风险利率并用 `--rf <百分数>` 传入后**重跑全量采集**"
                  "（`--refresh-market` 不重建衍生指标）",
    },
    "no_cashflow": {
        "label": "现金流量表关键输入缺失（经营现金流或资本开支）",
        "remedy": "确认该标的的现金流量表接口是否有权限/是否有该字段；"
                  "港股可检查 `hk_cashflow` 的 `ind_name` 覆盖",
    },
    "no_price": {
        "label": "收盘价不可用（行情接口无权限或配额用尽）",
        "remedy": "港股 `hk_daily` 配额为 5次/天、1次/分钟；等配额恢复后用 "
                  "`--refresh-market` 重跑，并确认 yfinance 回退可用",
    },
    "no_market_cap": {
        "label": "市值/股数不可用",
        "remedy": "检查 §1 基本信息与 §2 市场行情是否齐备；港股需 `--fx-hkd-cny` 才能折算报表币种",
    },
    "no_payout": {
        "label": "支付率无法裁定（分红数据全缺）",
        "remedy": "检查 §6 分红历史与 §5 分配现金；港股 `dps_hkd` 为按公告快照，需人工核对年报附注",
    },
    "hk_field_gap": {
        "label": "港股接口缺少该小节所需字段（上游数据缺口）",
        "remedy": "见 diagnosis_hk_section17.md；该小节在港股下暂不可用，属已知缺口",
    },
    "not_applicable": {
        "label": "该小节对本标的不适用",
        "remedy": "无需补救；本小节不参与结论",
    },
    "no_balance_sheet": {
        "label": "资产负债表不可用",
        "remedy": "检查该标的资产负债表接口权限与字段覆盖",
    },
    "no_data": {
        "label": "该小节所需的原始数据不足",
        "remedy": "检查 §1-§16 原始数据是否齐备；必要时重跑全量采集",
    },
    "input_inconsistent": {
        "label": "输入参数方向不自洽（外推会给出方向性错误的结论）",
        "remedy": "该小节**拒绝输出数值**（fail-closed），不得手算替代；"
                  "下行压力改用 §17.14 悲观档。修复方向见 known_issues.md 对应条目"
                  "（如 λ 的 ΔAA/ΔRevenue 符号一致性检查）",
    },
    "negative_basis": {
        "label": "可支配现金结余（AA）为负 ⇒ 该派生指标在定义域外",
        "remedy": "AA<0 表示当期现金流不足以覆盖资本开支（真实现金缺口，非数据错误）。"
                  "此时「税后回报率」与「按 k 折扣取悲观」都失去定义（负值上折扣无方向意义），"
                  "**拒绝输出数值**、不得手算；结论改由 §17.16 防御性门槛组与定性判断承担。"
                  "修复方向见 known_issues.md §二十九",
    },
    "exception": {
        "label": "计算过程抛出异常",
        "remedy": "查看数据包内该小节位置的异常文本，按提示修复后重跑",
    },
}

# 允许缺省（None）的原因码，仅在 status == ok 时
_VALID_REASON_CODES = tuple(REASONS)


class SectionMarkError(ValueError):
    """Raised when a section is marked with an out-of-contract value."""


class SectionRegistry:
    """Collects per-section status for §17.

    Fail-closed by construction: :meth:`finalize` emits **every** section in
    :data:`SECTION_TITLES`, defaulting unmarked ones to ``missing``. So a section
    that silently stopped being produced shows up as an explicit absence instead
    of vanishing from the data pack.
    """

    def __init__(self) -> None:
        self._marks: Dict[str, Dict[str, Optional[str]]] = {}
        self._warnings: List[str] = []

    # -- producer API ------------------------------------------------------
    def mark(self, number: str, status: str = STATUS_OK,
             reason: Optional[str] = None, note: Optional[str] = None) -> None:
        """Register a section's status.

        ``status != ok`` requires a ``reason`` from :data:`REASONS` — an unknown
        or missing code is a hard error, so a new failure mode cannot slip in
        unlabelled (the same "constrain at the point of production" rule the
        qualitative contract follows).
        """
        if number not in SECTION_TITLES:
            raise SectionMarkError(
                f"未知的 §17 小节编号 {number!r}；契约目录见 section_contract.SECTION_TITLES")
        if status not in VALID_STATUSES:
            raise SectionMarkError(
                f"§{number} 状态 {status!r} 不在词表 {VALID_STATUSES}")
        if status != STATUS_OK:
            if reason is None:
                raise SectionMarkError(
                    f"§{number} 状态为 {status} 时必须给出 reason（受控词表：{_VALID_REASON_CODES}）")
            if reason not in REASONS:
                raise SectionMarkError(
                    f"§{number} 的 reason={reason!r} 不在受控词表 {_VALID_REASON_CODES}")
        elif reason is not None:
            raise SectionMarkError(f"§{number} 状态为 ok 时不应给出 reason")
        self._marks[number] = {"status": status, "reason": reason, "note": note}

    def warn(self, message: str) -> None:
        """Attach a pack-level warning (e.g. currency not confirmed)."""
        self._warnings.append(str(message))

    # -- consumer API ------------------------------------------------------
    def is_marked(self, number: str) -> bool:
        """True when the section already carries an explicit status."""
        return number in self._marks

    def status_of(self, number: str) -> str:
        return (self._marks.get(number) or {}).get("status", STATUS_MISSING)

    def finalize(self, stock_code: str = "", units: Optional[Dict] = None) -> Dict:
        """Return the complete contract payload (every catalogue section present)."""
        sections: Dict[str, Dict] = {}
        for number, title in SECTION_TITLES.items():
            mark = self._marks.get(number)
            if mark is None:
                entry = {
                    "title": title,
                    "status": STATUS_MISSING,
                    "reason": "not_generated",
                    "reason_label": "本次运行未产出该小节（未登记状态，按 fail-closed 记为缺失）",
                    "remedy": "重跑全量采集；若持续缺失请检查该小节的输入数据",
                }
            else:
                code = mark.get("reason")
                entry = {"title": title, "status": mark["status"]}
                if code:
                    entry["reason"] = code
                    entry["reason_label"] = REASONS[code]["label"]
                    entry["remedy"] = REASONS[code]["remedy"]
                if mark.get("note"):
                    entry["note"] = mark["note"]
            sections[number] = entry
        payload = {
            "schema_version": SCHEMA_VERSION,
            "stock_code": stock_code,
            "sections": sections,
        }
        if units:
            payload["units"] = units
        if self._warnings:
            payload["warnings"] = self._warnings
        return payload


def write_status(path: str | Path, registry: SectionRegistry,
                 stock_code: str = "", units: Optional[Dict] = None) -> Path:
    """Write ``section17_status.json`` next to the data pack."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = registry.finalize(stock_code, units)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# 数据包 ↔ 状态契约的一致性检查
#
# 旧契约（2026-10 前）：「缺失通知里不得出现小节号」，以免 `"17.11" in pack`
# 这类子串检查把"通知里提到了 17.11"误当成"§17.11 已生成"。
#
# 新契约（更强、更贴近消费方真实的定位方式）：
#     `### 17.N` 标题存在  ⟺  该节 status == ok
#
# 状态表本身用 ``§17.N`` 前缀引用小节，因此不会与 ``### 17.N`` 标题混淆；
# 而"缺席"从此是**显式枚举**的，不再需要靠"字符串不出现"来推断。
# ---------------------------------------------------------------------------
SECTION_HEADING_RE = re.compile(r"^###\s+(17\.\d+)\s", re.MULTILINE)


def headings_in_pack(text: str) -> set:
    """Return the §17 section numbers that have a real ``### 17.N`` heading."""
    return set(SECTION_HEADING_RE.findall(text or ""))


def check_pack_consistency(pack_text: str, payload: Dict) -> List[str]:
    """Verify the ``heading present ⟺ status ok`` invariant.

    Returns a list of human-readable violations (empty = consistent).
    """
    headings = headings_in_pack(pack_text)
    problems: List[str] = []
    for number, entry in (payload or {}).get("sections", {}).items():
        status = entry.get("status")
        if status == STATUS_OK and number not in headings:
            problems.append(f"§{number} 状态为 ok，但数据包中找不到 `### {number}` 标题")
        elif status != STATUS_OK and number in headings:
            problems.append(f"§{number} 状态为 {status}，但数据包中存在 `### {number}` 标题")
    return problems


def missing_summary(payload: Dict) -> List[Dict]:
    """Return the non-ok sections with their reason + remedy (for prompts/reports)."""
    out = []
    for number, entry in sorted((payload or {}).get("sections", {}).items(),
                                key=lambda kv: int(kv[0].split(".")[1])):
        if entry.get("status") != STATUS_OK:
            out.append({
                "section": number,
                "title": entry.get("title"),
                "status": entry.get("status"),
                "reason": entry.get("reason_label"),
                "remedy": entry.get("remedy"),
            })
    return out
