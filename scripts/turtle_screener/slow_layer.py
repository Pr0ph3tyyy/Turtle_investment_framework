"""慢层构建器：**从已审计的产物目录**读出一个 `FinancialSnapshot`。

为什么从产物读，而不是从 Tushare 重算
------------------------------------
1. **天然同源**：`claims.json` 就是 canonical 公式链的结构化输出。
   从它构造快照，screener 与个股报告的 GG_pess / 目标买入价 / 安全边际
   **在构造上** 就是同一批数字 —— 这是验收标准第 1 条最直接的实现方式。
2. **不碰在飞文件**：不需要改 `derived_metrics.py` / `tushare_collector.py`。
3. **复用已审计产物**：产物已经过 invariants / field_coverage / 数值审核 / 观点审核，
   再独立算一遍等于把审计过的数字换成没审计过的。

代价与纪律
----------
- 慢层随**采集**更新，不随行情更新；日报只需读快照 + 抓当日价（设计 §C2）。
- 产物缺失/字段缺失 ⇒ 登记受控原因码，**绝不** `or 0` 把缺失变成零值
  （冒烟测试第一版就踩过：`or 0` 让 06862 的 GG_pess 变成 0.0 而不是 None）。
- `section17_status.json` 里 `status != ok` 的小节，其 `reason` 若在受控词表内就带进
  `reasons`；否则记 warning（不假装无事发生）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from turtle_core import o_input as _tc_o
from turtle_core.decision import market_kind, Q_DEFAULT
from turtle_core.reasons import SCREEN_REASONS
from turtle_core.snapshot import FinancialSnapshot

__all__ = ["load_product", "load_pool", "CLAIM_MAP",
           "TRADING_CURRENCY_BY_MARKET", "REPORTING_CURRENCY_BY_MARKET"]

#: **交易**币种由代码后缀完全确定（与 `infrastructure._detect_trading_currency` 一致）。
TRADING_CURRENCY_BY_MARKET: dict[str, str] = {"A": "CNY", "HK": "HKD", "US": "USD"}

#: **报表**币种：A 股与美股由后缀确定；**港股刻意不设默认** ——
#: 多数内地 A+H / 红筹发行人的报表币种是 CNY 而交易是 HKD（known_issues P0-2，
#: 不折算会静默低估约 15.65%），必须由 `fx.json` 的 `reporting_currency` 声明。
#: 若在这里给港股一个默认值，等于把"未确认"伪装成"已确认"。
REPORTING_CURRENCY_BY_MARKET: dict[str, str] = {"A": "CNY", "US": "USD"}

#: 快照字段 ← claim id（金额类需 ×1e6 转"元"）
CLAIM_MAP: dict[str, str] = {
    # O（注销型回购）：与报告同源 —— 报告侧由 `derived_metrics` 的 `_o_input()` 单点给出
    "o": "cm17.11.o",
    "c": "cm17.2.c",
    "aa_2y": "cm17.5.aa2y",
    "aa_all": "cm17.5.aaall",
    "aa_excl": "cm17.5.aaexcl",
    "m1": "cm17.10.m1",
    "m2": "cm17.10.m2",
    "m3": "cm17.10.m3",
    "m_rec": "cm17.10.mrec",
    "m_pess": "cm17.10.mpess",
    "mktcap": "cm17.8.mktcap",
    "anchor_nla": "cm17.8.anchor_nla",
    "anchor_bvps": "cm17.8.anchor_bvps",
    "anchor_low10y": "cm17.8.anchor_low10y",
    "anchor_div_implied": "cm17.8.anchor_div_implied",
    "anchor_fcf_cap": "cm17.8.anchor_fcf_cap",
    "floor": "cm17.8.floor",
    "fair": "cm17.8.fair",
}

#: 金额类 claim（单位「百万」）—— 读入后 ×1e6 变"元"
#: 以**百万**为单位、需 ×1e6 归一到元的 claim。`o` 必须在内 ——
#: `penetration_return(aa, m, q, o, mkt_cap)` 要求 O 与 mkt_cap **同单位（元）**，
#: 漏掉它会让 O 小 100 万倍（静默恒等于 0 的效果）。
_MM_KEYS = frozenset({"c", "aa_2y", "aa_all", "aa_excl", "mktcap", "o"})


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _claims_index(payload: Optional[dict]) -> dict[str, dict]:
    if not payload:
        return {}
    return {c.get("claim_id"): c for c in payload.get("claims", []) if c.get("claim_id")}


def _value(idx: dict[str, dict], cid: str) -> Optional[float]:
    c = idx.get(cid)
    if c is None:
        return None
    v = c.get("value")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_product(product_dir: str | Path,
                 ts_code: Optional[str] = None) -> tuple[Optional[FinancialSnapshot], list[str]]:
    """读一个产物目录 → ``(快照 | None, warnings)``。

    需要的文件：``claims.json``（必需）、``fx.json``、``data_pack_market.md``（取股本）、
    ``section17_status.json``（取缺失小节的 reason）。
    """
    d = Path(product_dir)
    warnings: list[str] = []
    idx = _claims_index(_read_json(d / "claims.json"))
    if not idx:
        return None, [f"{d}：缺 claims.json 或内容为空 —— 无法构造快照"]

    code = ts_code
    if not code:
        payload = _read_json(d / "claims.json") or {}
        code = payload.get("stock_code") or ""
    if not code:
        # 由目录名兜底：output/{code}_{公司}
        name = d.name
        if "_" in name:
            code = name.split("_", 1)[0]
    if not code:
        return None, [f"{d}：无法确定 ts_code"]

    fx = _read_json(d / "fx.json") or {}
    kind = market_kind(code)
    report_ccy = fx.get("reporting_currency")
    trade_ccy = fx.get("trading_currency") or TRADING_CURRENCY_BY_MARKET.get(kind)
    factor = fx.get("factor_to_reporting")

    # 后缀能确定的就补上（A→CNY / US→USD）。这不降低严格性：
    # 这两个市场的报表币种由上市地唯一决定，不存在 P0-2 那类歧义。
    if not report_ccy:
        inferred = REPORTING_CURRENCY_BY_MARKET.get(kind)
        if inferred:
            report_ccy = inferred
            factor = 1.0 if factor is None else factor
            warnings.append(
                f"fx.json 未声明 reporting_currency；按代码后缀（{kind}）推断为 "
                f"{inferred}（该市场币种由上市地唯一确定，无需 fx.json）")
        else:
            # 港股：绝不推断。`_unit_label()` 会变成「百万·币种未确认」，
            # 且跨币种折算不可判定 ⇒ 后续登记 fx_missing（fail-closed）。
            warnings.append(
                "报表币种未确认（港股无 fx.json 的 reporting_currency 声明）"
                "⇒ 是否跨币种不可判定，须补 fx.json（known_issues P0-2）")

    vals = {k: _value(idx, cid) for k, cid in CLAIM_MAP.items()}
    for k in _MM_KEYS:
        if vals.get(k) is not None:
            vals[k] = vals[k] * 1e6

    aa = {k: vals[f"aa_{k}"] for k in ("2y", "all", "excl")}
    aa_map = {f"aa_{k}": v for k, v in aa.items() if v is not None}
    # §17.9/§17.13/§17.14 用 aa_selected（= aa_2y，因 §17.5 硬门禁保证 ≥2 年）
    if vals.get("aa_2y") is not None:
        aa_map["aa_selected"] = vals["aa_2y"]

    payout = {
        "m1": vals.get("m1"), "m2": vals.get("m2"), "m3": vals.get("m3"),
        "m_rec": vals.get("m_rec"), "m_pess": vals.get("m_pess"),
    }

    floor = {k: vals[k] for k in
             ("anchor_nla", "anchor_bvps", "anchor_low10y",
              "anchor_div_implied", "anchor_fcf_cap", "floor", "fair")
             if vals.get(k) is not None}

    # ---- 股本（复用仓库单点解析，不自己写正则） ----
    # 优先级与 `shares_utils.resolve_shares` 一致：显式 sidecar > 产物内申报值。
    total_shares: Optional[float] = None
    shares_kind: Optional[str] = None
    sidecar = _read_json(d / "shares.json")
    if sidecar and sidecar.get("shares_mm"):
        try:
            total_shares = float(sidecar["shares_mm"]) * 1e6
            shares_kind = str(sidecar.get("kind") or "reported")
        except (TypeError, ValueError):
            total_shares, shares_kind = None, None
    if total_shares is None:
        try:
            import shares_utils  # type: ignore
            pack_path = d / "data_pack_market.md"
            if pack_path.exists():
                shares_mm, trusted, raw = shares_utils.declared_from_pack(
                    pack_path.read_text(encoding="utf-8"))
                if shares_mm:
                    total_shares = shares_mm * 1e6
                    shares_kind = "reported" if trusted else "implied"
                    if not trusted:
                        warnings.append(
                            f"股本非申报值（降级）：「{raw.strip()[:80]}…」"
                            "⇒ 市值口径可能与报告不一致（findings D-10）")
                else:
                    warnings.append(
                        "产物内无「总股本（百万股）」行（旧版产物常见）"
                        "⇒ 需补 shares.json 或重跑采集")
        except Exception as exc:                              # noqa: BLE001
            warnings.append(f"股本解析失败：{type(exc).__name__}: {exc}")

    # ---- 原因码（fail-closed：缺失就是缺失，不写 0） ----
    reasons: dict[str, str] = {}
    if vals.get("c") is None:
        reasons["c"] = "no_annual_3y"
    if vals.get("aa_2y") is None:
        reasons["aa"] = "no_cashflow"
    if vals.get("m_pess") is None and vals.get("m_rec") is None:
        reasons["payout"] = "no_payout"
    if total_shares is None:
        reasons["shares"] = "no_shares"

    # ---- O（注销型回购）----
    # ⚠️ **半接线闸（findings D-3）**：报告侧已接通 `o.json`（`cm17.11.o` + §15 注明来源），
    # 但筛选侧的快照尚未把 O 归一化到与 `mkt_cap` **同币种同单位**（元），
    # 所以**暂时不能**把它喂给 `penetration_return`。
    # 若此时有产物带非零 `o.json`，报告 O≠0 而筛选仍按 0 ⇒ **不同源** —— 正是验收①要防的事。
    # 取舍：**宁可排除该股，也不产出一个与报告不一致的数**（fail-closed）。
    # 去掉这道闸的唯一条件：把 O 按快照的量纲归一后接通引擎，并用逐字节等价验证。
    # O 经 `claims.json` 的 `cm17.11.o`（受控 claim，同一单点来源）随 `_MM_KEYS` 归一到元。
    # `o.json` 被拒时报告侧已按 0 并登记，此处再确认一次（两侧同源 ⇒ 结论一致）。
    _o_res, _o_why = _tc_o.resolve_o(d)
    if _o_why:
        warnings.append(f"⚠️ o.json 被拒（{_o_why}）⇒ O 按 0：{_tc_o.explain_rejection(_o_why)}")
    if _o_res.is_nonzero:
        # **不是闸，只是披露**：2026-10-14 插桩实测确认筛选侧 O 已正确生效 ——
        #   `### ENG: o=123400000.0 cap=25,384,955,161 aa=2,126,610,000 k=0.8` → GG_pess=1.735%
        #   手算 (2126.61e6×0.8×0.2329×0.8 + 123.4e6)/25,384,955,161×100 = **1.734** ✓ 逐位吻合
        # 我曾据「报告 1.95 vs 筛选 1.4067」判定筛选侧没接 O，并加了排除闸 ——
        # **那是误诊**：两者的差完全来自**市值口径**（报告 22,533.63e6 vs 自洽 25,385e6），
        # 即 D-10，与 O 无关。闸已撤除（基于错误诊断的闸会无谓排除带 o.json 的股票）。
        warnings.append(f"O（注销型回购）= {_o_res.describe()}")

    # ---- 决策小节是否算出来了 ----
    # 若 §17.11/§17.14 在**报告侧**没算出来（status != ok），报告里就没有可对账的决策数字
    # ⇒ 该股**无法通过验收①的同源校验**，不得进短名单。
    # 实测（2026-10-14）：`002555.SZ` 的 §17.2/§17.9/§17.11/§17.14 全为 `missing reason=exception`，
    # 而 screener 仍从 §17.5/§17.10 的输入算出了 GG_pess=5.939 —— 当时靠 `below_threshold`
    # 顺带排除（**潜在缺口，非活跃**）。这里把它变成**显式**排除。
    # 刻意只盯**决策小节**：§17.13 之类非决策小节的 `input_inconsistent` 属披露，不排除。
    _sec17 = _read_json(d / "section17_status.json") or {}
    _secs = _sec17.get("sections") or {}
    _DECISION_SECTIONS = ("17.11", "17.14")
    _dead = [k for k in _DECISION_SECTIONS
             if (k in _secs) and (_secs[k] or {}).get("status") != "ok"]
    if _dead:
        # 排除理由取**决策小节的真实 reason**（受控词表内），而不是硬编码一个：
        # 硬编码会把「AA 为负 ⇒ 定义域外」记成「输入参数方向不自洽」，两者补救方向完全不同。
        # 取不到（无 reason / 不在词表内）才回落到通用码 —— 这里刻意**不用** `require()`，
        # 正是为了不因一个未知码打断整批装载。
        _detail = next(
            (str((_secs[k] or {}).get("reason")) for k in _dead
             if (_secs[k] or {}).get("reason") in SCREEN_REASONS),
            "input_inconsistent")
        reasons["decision_section_missing"] = _detail
        warnings.append(
            f"⛔ 报告侧决策小节未产出（{'、'.join('§' + k for k in _dead)}）⇒ 无同源数字可对账，"
            f"**已排除该股**（fail-closed：宁可少一只也不给不可校验的数）；真实原因 = {_detail}")

    # ---- 上游门禁裁决（**必须读**：采集器可能已判定该包不得作为分析输入） ----
    gate = _read_json(d / "gate_status.json") or {}
    gate_deliverable = gate.get("deliverable")
    gate_blocking = [str(b.get("code")) for b in (gate.get("blocking") or []) if b.get("code")]
    gate_note = ""
    if gate:
        # **按当前政策重算裁决**，而不是直接信任记录里的 `deliverable`。
        # 理由：`deliverable` 是**采集时刻**的政策算出来的；政策若修正（例如
        # 2026-10-14 把 WARN 从阻断清单移除），旧产物不该继续被旧政策挡死。
        # 政策是单点（gate_policy），消费者按当前口径重算才是正确语义。
        recorded = gate_deliverable
        try:
            import gate_policy
            fresh = gate_policy.verdict(
                list(gate.get("blocking") or []) + list(gate.get("disclosure") or []),
                allow_degraded=bool(gate.get("degraded")), stock_code=code)
            gate_deliverable = fresh["deliverable"]
            gate_blocking = [str(b.get("code")) for b in fresh["blocking"]]
            gate_note = (f"（记录值 deliverable={recorded}；按当前 gate_policy 重算 = "
                         f"{gate_deliverable}）"
                         if recorded is not None and recorded != gate_deliverable else "")
        except Exception as exc:                              # noqa: BLE001
            # 重算失败 ⇒ **回退到记录值**（fail-closed：不因重算失败而放行）
            warnings.append(f"门禁重算失败（{type(exc).__name__}: {exc}），回退到记录值")
        if gate_deliverable is False:
            reasons["pack_gate"] = "upstream_gate_failed"
            warnings.append(
                f"⚠️ 上游门禁：deliverable=False，阻断项 {gate_blocking or '（未列出）'}"
                f"{gate_note} ⇒ 该产物的结论数字按契约不得引用"
                "（screener 默认不纳入排名）")
        elif gate_note:
            warnings.append(f"上游门禁：{gate_note} ⇒ 已放行（阻断项已降为披露级）")

    # ---- 缺失小节的 reason 带进快照（受控词表内才带） ----
    status = _read_json(d / "section17_status.json") or {}
    for num, entry in (status.get("sections") or {}).items():
        if entry.get("status") == "ok":
            continue
        code_r = entry.get("reason")
        if code_r in SCREEN_REASONS:
            reasons.setdefault(f"s17.{num}", code_r)
        elif code_r:
            warnings.append(f"§{num} 缺失（reason={code_r}）不在选股器受控词表内，未登记")

    fy_end = ""
    for key in ("cm17.5.aa2y", "cm17.2.c", "cm17.10.mrec"):
        c = idx.get(key)
        if c and c.get("time"):
            fy_end = str(c["time"])
            break
    if not fy_end:
        warnings.append("无法确定财报期（claim 的 time 为空）⇒ 过期判定失效")

    # ── AA 定义域检查（**独立**于 section17_status，不能只信状态文件）───────────
    # AA 是可支配现金结余，可以为负（资本开支 > 经营现金流 = 真实现金缺口）。
    # 此时「按 k 折扣取悲观」没有方向意义（¥1 缺口按 k=0.7 缩到 ¥0.7，看起来"改善"），
    # 买点不可推导 ⇒ 排除。
    #
    # ⚠️ 为什么必须在这里独立查：上面那条 `decision_section_missing` 依赖
    # `section17_status.json` 说 §17.14 非 ok。但存量产物是**改动前**生成的，
    # 状态仍是 `ok` —— 实测 600016/000157/601186/600309 四个标的 AA 已为负而状态为 ok，
    # 选股器会照样排名（用一个方向反了的买点）。状态文件会陈旧，事实不会。
    _AA_KEYS = ("cm17.5.aa2y", "cm17.5.aaall", "cm17.5.aaexcl")
    _aa_neg = [(k, (idx.get(k) or {}).get("value")) for k in _AA_KEYS
               if isinstance((idx.get(k) or {}).get("value"), (int, float))
               and (idx.get(k) or {})["value"] < 0]
    if _aa_neg:
        reasons.setdefault("decision_section_missing", "negative_basis")
        _shown = "、".join(f"{k.replace('cm17.5.', '')}={v:,.2f}" for k, v in _aa_neg)
        warnings.append(
            f"⛔ AA 为负（{_shown}）⇒ 可支配现金结余为负，悲观档在定义域外，"
            f"买点不可推导 ⇒ **已排除该股**（与报告侧 §17.14 的 negative_basis 守卫一致）")

    snap = FinancialSnapshot.new(
        code,
        fy_end=fy_end,
        reporting_currency=report_ccy,
        trading_currency=trade_ccy,
        factor_to_reporting=factor,
        c_yuan=vals.get("c"),
        aa=aa_map,
        payout=payout,
        q_default=Q_DEFAULT[market_kind(code)],
        q_scenarios=[],
        total_shares=total_shares,
        shares_kind=shares_kind,
        artifact_market_cap_yuan=vals.get("mktcap"),
        floor=floor,
        o=vals.get("o"),
        reasons=reasons,
        source={"product_dir": str(d), "claims": "claims.json"},
    )
    # 门禁裁决随快照带出（供引擎/清单披露；不进 reasons —— 它由引擎按开关决定是否排除）
    snap.gate_deliverable = gate_deliverable
    snap.gate_blocking = gate_blocking
    return snap, warnings


def load_pool(product_dirs: Iterable[str | Path],
              codes: Optional[dict[str, str]] = None
              ) -> tuple[list[FinancialSnapshot], list[str]]:
    """批量读产物 → 快照列表。``codes`` 为 ``{目录: ts_code}`` 覆盖（可选）。"""
    snaps: list[FinancialSnapshot] = []
    warnings: list[str] = []
    codes = codes or {}
    for d in product_dirs:
        snap, warns = load_product(d, ts_code=codes.get(str(d)))
        warnings.extend(warns)
        if snap is not None:
            snaps.append(snap)
    return snaps, warnings
