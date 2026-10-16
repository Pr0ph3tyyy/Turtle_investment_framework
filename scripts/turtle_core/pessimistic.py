"""悲观档（§17.14）与仓位矩阵 —— 纯函数，无取数、无渲染。

悲观档公式（`derived_metrics.py:2499-2501`）
------------------------------------------
::

    AA_pess(k) = aa_selected × k                    # **乘法折扣**，非重新推导
    GG_pess(k) = [AA_pess(k) × M_pess/100 × (1−Q) + O] / 市值 × 100     [%]
    R_pess(k)  = [C × k       × M_pess/100 × (1−Q) + O] / 市值 × 100     [%]
    安全边际    = GG_pess(k*) − II                  # k* = Agent 采纳档，通常 0.80 ★
    目标买入价  = 现价 × GG_pess(k) / II            # 交易币种/股

k 档位与出处（`derived_metrics.py:2242-2247`）
--------------------------------------------
``(1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7)``，默认 ★ = **0.80**。
来源为因子2 原文：「打八折」→0.8；「衰退 >20% 按七折」→0.7；「最低值再打 8.5 折」→0.85。
``k = 1.0`` 是**未打折的对照行**（AA_pess = AA），不是悲观输入，排名时不得把它当悲观档。

三处必须显式声明的口径分歧（否则自动化会产出自相矛盾的报告）
--------------------------------------------------------
1. **O 在 canonical 里是硬编码 0**：§17.14 的循环体**连 O 项都没有**
   （`derived_metrics.py:2500`），§17.9/§17.11 用 `o_val = 0.0`。
   而 §15 其实算了年均回购却无人消费（`turtle_core_extraction_findings.md` D-3）。
   本模块把 ``o`` 列为**显式参数**（默认 0.0），从而让"接通 §15"成为一次调用方的选择，
   而不是一个被硬编码掩盖的缺口。
2. **M_pess 忽略承诺值**：``m_pess = min(m1, m2, m3)``，即使存在公开承诺
   （findings D-2/D-3）。调用方若要用承诺口径，必须另传 ``m_pess`` 并在报告里披露。
3. **安全边际的周期性修正只在文档里**：`phase3_valuation.md:98-103` 要求强周期标的
   在周期底部 ``II−1``、顶部 ``II+2``；而 `viewpoint_audit.py` V1a 按**未修正**口径
   卡 ``±0.02``。本模块只给未修正值；若做修正，必须把修正后值登记为 claim，
   否则审计必然判 critical。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .decision import margin, penetration_return, target_buy_price

__all__ = [
    "PESS_K_VALUES",
    "PESS_K_DEFAULT",
    "aa_domain_ok",
    "k_slug",
    "PessimisticRow",
    "pessimistic_grid",
    "MATRIX_ROWS",
    "position_band",
    "position_advice",
]

def _load_k_grid():
    """k 网格读 `strategy_rules.json`（单点）；读不到则回落原字面量（行为不变）。

    为什么接线：k 档位是**策略常量**（因子2「打八折」→0.8 等），此前只活在代码里，
    规则单点里没有 ⇒ 改了规则不生效、改了代码无人知。2026-10-15 完备性扫描补入。
    """
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "strategy_rules.json"
    try:
        spec = json.loads(p.read_text(encoding="utf-8")).get("pessimistic_k_grid") or {}
        vals = tuple(float(x) for x in (spec.get("values_desc") or []))
        dflt = float(spec.get("default_star", 0.8))
        return (vals, dflt) if vals else ((1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7), 0.8)
    except Exception as e:  # noqa: BLE001 —— 不崩，但**必须可见**
        # 与 derived_metrics.STRATEGY_RULES_ERROR 同一条纪律（known_issues §四十九）：
        # 回落到内嵌默认值 = 产物可能与 strategy_rules.json 矛盾，而失败方向最难察觉。
        # 这里只警告不抛：turtle_core 被选股器 import，抛出会连带打断无关工具链。
        # 交付侧的阻断由 `verify_product` 的"规则单点"门禁负责。
        import sys as _sys
        print(f"⛔ 规则单点不可用（k 网格回落内嵌默认值）：{type(e).__name__}: {e}",
              file=_sys.stderr)
        return (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7), 0.8


#: 悲观折扣档（降序），★ = :data:`PESS_K_DEFAULT` —— 值来自 strategy_rules.json
PESS_K_VALUES: tuple[float, ...]
PESS_K_DEFAULT: float
PESS_K_VALUES, PESS_K_DEFAULT = _load_k_grid()


def k_slug(k: float) -> str:
    """k → claim id 后缀，如 ``0.8 → "k080"``（`derived_metrics.py:2512`）。"""
    return f"k{int(round(k * 100)):03d}"


@dataclass(frozen=True)
class PessimisticRow:
    """一档 k 的完整决策行。金额单位与输入一致；回报率单位为 **%**，边际为 **pct 点**。"""

    k: float
    aa_pess: float
    gg_pess: Optional[float]
    r_pess: Optional[float]
    vs_threshold_pct: Optional[float]      # = gg_pess − II（即安全边际）
    target_buy_price: Optional[float]      # 交易币种/股
    is_default: bool = False

    @property
    def claim_slug(self) -> str:
        return k_slug(self.k)


def aa_domain_ok(aa_selected: Optional[float]) -> bool:
    """可支配现金结余 AA 是否落在**悲观档的定义域**内。

    AA 是可支配现金结余，**可以为负**（资本开支超过经营现金流 = 真实现金缺口，
    实测 600011 华能国际 2024 AA = −13,122.98、601186 中国铁建 2025 = −44,643.57）。
    AA < 0 时 `AA_pess = AA × k` **没有方向意义**：账目上 ¥1 的缺口按 k=0.7 缩到 ¥0.7，
    看起来"改善了"，与"更悲观"相反 ⇒ 买点不可推导。

    ⚠️ **必须与 `derived_metrics._compute_pessimistic_grid` 的守卫一致**：
    报告侧已 `mark("17.14","missing","negative_basis")` 并拒绝出数
    （`known_issues.md` §二十九）。本函数是那条守卫在**公式单点**侧的镜像 ——
    没有它，选股器会为一个框架已拒绝的标的算出买点，
    直接违反"选股器与 Workflow 2 报告的 §17 claim 逐字段相等"（验收标准①）。

    实测缺口（2026-10-15）：`600016/000157/601186/600309` 的 `section17_status.json`
    仍是**改动前**的 `§17.14 = ok`，而 AA 已经是负的 —— 选股器只看小节状态，
    于是会照样排名。故选股器必须**独立**做这个定义域检查，不能只信状态文件。
    """
    return aa_selected is None or aa_selected >= 0


def pessimistic_grid(aa_selected: Optional[float],
                     c: Optional[float],
                     m_pess: Optional[float],
                     q_pct: Optional[float],
                     o: Optional[float],
                     mkt_cap: Optional[float],
                     close: Optional[float],
                     ii: Optional[float],
                     k_values: Sequence[float] = PESS_K_VALUES,
                     k_default: float = PESS_K_DEFAULT,
                     ) -> list[PessimisticRow]:
    """生成 §17.14 的全部 k 档决策行。

    ``aa_selected`` / ``c`` / ``o`` / ``mkt_cap`` 必须**同币种**（canonical 为报表币种原始元）；
    ``close`` 为**交易币种**/股；``m_pess`` / ``q_pct`` / ``ii`` 为**百分数**。

    任一回回报率算不出（``mkt_cap`` 缺失/非正、``m_pess`` 缺失等）时，
    该行仍会产出但 ``gg_pess`` 为 ``None`` —— 保持"行数恒定"，
    便于调用方按 :func:`k_slug` 生成 claim id 并显式登记缺失原因（fail-closed）。

    **AA < 0 时全部 ``gg_pess`` / ``target_buy_price`` 为 ``None``**（见 :func:`aa_domain_ok`）：
    行仍产出（保持行数恒定与 claim 形状），但买点是"不可推导"而非一个数。
    宁可给 `None` 并附原因，也不给一个方向反了的买点。
    """
    # AA < 0 ⇒ 买点"不可推导"，而不是一个方向反了的数（见 aa_domain_ok）
    in_domain = aa_domain_ok(aa_selected)
    rows: list[PessimisticRow] = []
    for k in k_values:
        aa_pess = None if aa_selected is None else aa_selected * k
        gg = (penetration_return(aa_pess, m_pess, q_pct, o, mkt_cap)
              if (aa_pess is not None and in_domain) else None)
        r = (penetration_return(None if c is None else c * k, m_pess, q_pct, o, mkt_cap)
             if c is not None else None)
        rows.append(PessimisticRow(
            k=k,
            aa_pess=aa_pess if aa_pess is not None else float("nan"),
            gg_pess=gg,
            r_pess=r,
            vs_threshold_pct=margin(gg, ii),
            target_buy_price=target_buy_price(close, gg, ii),
            is_default=abs(k - k_default) < 1e-9,
        ))
    return rows


# ---------------------------------------------------------------------------
# 仓位矩阵（`phase3_valuation.md:111-123`；审计侧的机器副本 `viewpoint_audit.py:48-58`）
# ---------------------------------------------------------------------------

#: (条件串, 仓位建议)。条件串三段以 ``|`` 分隔：``KK档|外推可信度|价值陷阱``；
#: ``—`` 表示该维度不约束。**顺序即文件顺序**，且匹配是"全部返回"而非首个命中。
MATRIX_ROWS: tuple[tuple[str, str], ...] = (
    ("KK>=1.5|cred=高|trap=低", "标准仓位"),
    ("KK>=1.5|cred=高|trap=中", "70%"),
    ("KK>=1.5|cred=中|trap=低", "70%"),
    ("KK0.5-1.5|cred=高|trap=低", "50%"),
    ("KK0.5-1.5|cred=中|trap=低", "观察（30%）"),
    ("KK0-0.5|—|—", "观察或等待"),
    ("KK<0|—|—", "不建仓"),
    ("—|cred=低|—", "观察或不建仓"),
    ("—|—|trap=高 & GG<II×1.5", "排除"),
)

#: 仓位建议的规范形式（`viewpoint_audit._norm_position` 的子串顺序）
POSITION_CANONICAL: tuple[str, ...] = (
    "排除", "标准仓位", "不建仓", "70%", "50%", "观察", "观察或等待",
)


def position_band(kk: Optional[float]) -> Optional[str]:
    """安全边际 → 档位串。边界归属：``0.5`` 属 ``KK0.5-1.5``，``0.0`` 属 ``KK0-0.5``。"""
    if kk is None:
        return None
    if kk >= 1.5:
        return "KK>=1.5"
    if kk >= 0.5:
        return "KK0.5-1.5"
    if kk >= 0.0:
        return "KK0-0.5"
    return "KK<0"


def norm_position(text: Optional[str]) -> str:
    """把报告里的仓位表述规范到 :data:`POSITION_CANONICAL` 之一。"""
    if not text:
        return "（未给出）"
    t = str(text)
    # 顺序重要：先判"排除"，再"标准仓位"，…（与审计一致）
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


def position_advice(kk: Optional[float],
                    credibility: Optional[str],
                    trap_risk: Optional[str],
                    gg: Optional[float],
                    ii: Optional[float]) -> list[str]:
    """按仓位矩阵给出**全部**命中的仓位建议。

    ⚠️ 矩阵是**偏函数**：可能命中 0 行、1 行或多行。
    - 0 行 ⇒ 无对应档位（审计判 UNVERIFIABLE/MAJOR，不得猜一个仓位）
    - >1 行且建议不同 ⇒ 矩阵缺少优先级规则（审计判 MINOR 并披露）

    ``credibility`` ∈ {高, 中, 低}；``trap_risk`` ∈ {高, 中, 低}。
    ``gg`` / ``ii`` 仅用于最后一行 ``GG < II×1.5`` 子句。
    """
    band = position_band(kk)
    hits: list[str] = []
    for cond, advice in MATRIX_ROWS:
        parts = (cond.split("|") + ["—", "—"])[:3]
        band_c, cred_c, trap_c = parts[0], parts[1], parts[2]

        if band_c != "—" and band_c != band:
            continue
        if cred_c != "—":
            if not credibility or cred_c.split("=")[1] != credibility:
                continue
        if trap_c != "—":
            if "trap=高" in trap_c:
                if trap_risk != "高":
                    continue
                if "GG<II×1.5" in trap_c:
                    if gg is None or ii is None or not (gg < ii * 1.5):
                        continue
            elif "trap=低" in trap_c:
                if trap_risk != "低":
                    continue
            elif "trap=中" in trap_c:
                if trap_risk != "中":
                    continue
        hits.append(advice)
    return hits
