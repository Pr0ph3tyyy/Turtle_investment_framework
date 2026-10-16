"""O（注销型回购）输入 —— 单点解析与 fail-closed 校验。

为什么需要这个模块（findings **D-3**）
------------------------------------
龟龟公式的 `O` 是「**注销型**回购年均金额」（只有真正减少股本的那类算），
`penetration_return(base, m, q, o, mkt_cap)` 早已支持它。

**历史与现状（2026-10-15 核实并更新本段）**：canonical 侧的 `o_val` 原先在
§17.9 / §17.11 / §17.14 **三处各自硬编码 0.0**，`claims.json` 里也没有 O 的 claim，
公式里的 O 项是**死参数**。**该问题已经接线完毕**，本段此前未同步、会误导读者：

* §17.9 / §17.11 / §17.14 现在都读 `derived_metrics._o_input()`（**单点**）；
* `claims.json` 里有 `cm17.11.o`；
* 实测（00506 注入 `o.json` 的 O=200 百万）：GG 18.48 → **21.00**、
  GG_pess 7.19 → **9.52**、目标买入价 5.44 → **7.09** ⇒ O 确实进了结论。

根因不是"忘了算"，而是 **Tushare 无法区分注销型与库存股回购**，
设计上把「从年报确认注销型金额」留成人工步骤 —— 这一步现已接通，
无 `o.json` 时 O 仍恒为 0（与旧行为逐字节相同）。

设计取舍（两条都重要）
----------------------
1. **默认必须与现状逐字节相同** —— 没有 `o.json` 的产物，O 恒为 0 且说明文字不变。
   所以这是一个**纯增量**能力，不会动任何既有数值。
2. **非零 O 必须有来源** —— `o_annual_mm > 0` 却缺 `source` / `confirmed_by` 时
   **拒绝该文件并按 0 处理**。理由：一个凭空填进去的非零 O 会静默抬高穿透回报率，
   比老实写 0 危害大得多（这正是本仓库反复强调的「不得静默补数」）。

契约（`{product_dir}/o.json`，与 `fx.json` / `dividend_commitment.json` 同构）::

    {"o_annual_mm": 123.4,
     "years": 5,
     "source": "2025年报 p.56「注销式回购」",
     "confirmed_by": "human",
     "as_of": "2026-10-14"}
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from .reasons import remedy_of

__all__ = ["OInput", "resolve_o", "O_FILENAME", "CONFIRMERS"]

O_FILENAME = "o.json"

#: 允许的确认者。**刻意限定**：只有人或在人工监督下工作的 LLM 能签字，
#: 将来若要加 `"api"` 之类，必须先想清楚它凭什么能区分注销型。
CONFIRMERS = ("human", "llm")


@dataclass(frozen=True)
class OInput:
    """O 的最终取值与来源。``is_default`` 区分「声明为 0」与「无声明」。"""

    o_annual_mm: float = 0.0
    source: Optional[str] = None
    confirmed_by: Optional[str] = None
    years: Optional[int] = None
    as_of: Optional[str] = None
    #: True = 没有可用声明（文件缺失或被拒），按 0 处理
    is_default: bool = True

    @property
    def is_nonzero(self) -> bool:
        return self.o_annual_mm > 0

    def describe(self) -> str:
        """给人读的来源说明 —— 必须能区分"确认过是 0"与"根本没确认"。"""
        if self.is_default:
            return "默认 0（无 o.json：Tushare 无法区分注销型回购，待年报确认）"
        if self.o_annual_mm == 0:
            return f"0（已声明：{self.source or '无来源'}）"
        who = {"human": "人工", "llm": "LLM 在人工监督下"}.get(self.confirmed_by or "", "?")
        yrs = f"，{self.years} 年均值" if self.years else ""
        return f"{self.o_annual_mm:,.2f} 百万（{self.source}；{who}确认{yrs}）"


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _finite_nonnegative(value: Any) -> Optional[float]:
    """非法 ⇒ ``None``（区别于合法的 0.0）。JSON 里 `true` 也是 int 的子类，须排除。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or math.isinf(f) or f < 0:
        return None
    return f


def resolve_o(product_dir: Union[str, Path]) -> tuple[OInput, Optional[str]]:
    """读 `{product_dir}/o.json`。

    返回 ``(OInput, reason_code)``；``reason_code`` 非 ``None`` 时表示**该文件被拒**，
    调用方**必须**把它登记进 §17 的状态/警告（不得静默忽略 ——
    一个被拒的 o.json 意味着有人以为非零 O 生效了，而实际没有）。
    """
    path = Path(product_dir) / O_FILENAME
    if not path.exists():
        return OInput(), None                    # 现状：无声明 ⇒ O=0

    data = _read_json(path)
    if data is None:
        return OInput(), "o_invalid"

    value = _finite_nonnegative(data.get("o_annual_mm"))
    if value is None:
        return OInput(), "o_invalid"

    source = (data.get("source") or "").strip() if isinstance(data.get("source"), str) else ""
    who = (data.get("confirmed_by") or "").strip().lower() if isinstance(
        data.get("confirmed_by"), str) else ""
    years = data.get("years")
    years = int(years) if isinstance(years, int) and not isinstance(years, bool) and years > 0 else None
    as_of = data.get("as_of") if isinstance(data.get("as_of"), str) else None

    if value == 0:
        # 显式声明为 0：合法，但仍要求来源（否则与"没确认"无法区分）
        if not source:
            return OInput(), "o_unsourced"
        return OInput(0.0, source, who or None, years, as_of, is_default=False), None

    # 非零：来源与确认者**缺一不可**
    if not source or who not in CONFIRMERS:
        why = "缺 source" if not source else f"confirmed_by={who!r} 不在 {CONFIRMERS}"
        return OInput(), "o_unsourced"

    return OInput(value, source, who, years, as_of, is_default=False), None


def explain_rejection(code: str) -> str:
    """被拒时给调用方拼一句带补救办法的话（补救动作必须可执行）。"""
    return f"{code}：{remedy_of(code)}"
