"""选股器的 error reason 受控词表 —— **fail-closed**，未知码是硬错误。

为什么必须有
------------
`section_contract.REASONS` 解决的是"§17 小节缺失"不可判定；选股器面对同一问题：
日报每天要排除掉成千上万只股票，如果排除没有**受控的**原因码，
自动跑三个月后没人说得清"某只票为什么从没出现过"。

设计约束（与 `section_contract.py` 一致）
---------------------------------------
- 每个码必有 ``label``（人读）与 ``remedy``（怎么办）；
- :func:`require` 对未知码**抛异常**，不静默通过 —— 否则词表就退化成注释；
- 词表是**单点**：新增码必须同时补 label/remedy 与测试。

码的语义分层（供日报汇总与门禁分级复用）
-------------------------------------
- ``UNIVERSE_*`` —— 宇宙卫生：本就不该看的标的（ST/退市/非普通股）
- ``LIQUIDITY_*`` —— 交易性不足
- ``DATA_*`` —— 算不出来（数据缺口，fail-closed，绝不兜底）
- ``FX_*`` / ``RF_*`` —— 全局输入缺口（会影响整批，不只是单只）
- ``STALE_*`` —— 快照过期（用旧财报排名比不给排名更危险）
- ``THRESHOLD_*`` —— 算得出来但不达标（不是错误）
"""

from __future__ import annotations

from typing import Dict, Mapping

__all__ = ["SCREEN_REASONS", "require", "label_of", "remedy_of", "is_global"]


SCREEN_REASONS: Dict[str, Dict[str, str]] = {
    # --- 宇宙卫生 ---
    "st": {
        "label": "ST / *ST / PT / 退市整理期标的",
        "remedy": "无需补救；名称含风险标记的标的不进入选股池",
    },
    "delisted": {
        "label": "已退市或已停止上市",
        "remedy": "无需补救；确认 `delist_date` 非空即排除",
    },
    "non_equity_class": {
        "label": "非普通股（优先股/存托凭证/权证/单位等）",
        "remedy": "按 `us_basic.classify` 只保留 EQ（可选 ADR），剔除 PF/GDR/其他；"
                  "美股宇宙实测混入韩股（035420）与权证（D06P59）",
    },
    "not_target_market": {
        "label": "不属于本次筛选的目标市场",
        "remedy": "检查 ts_code 后缀（.SH/.SZ/.HK/.US）与 `market` 参数",
    },
    # --- 流动性 / 规模 ---
    "too_young": {
        "label": "上市年限不足",
        "remedy": "阈值见 ScreenerConfig.min_listing_years；新股无 3 年财报，本就无法过可达性门",
    },
    "too_small": {
        "label": "市值低于下限",
        "remedy": "阈值见 ScreenerConfig.min_market_cap_yi",
    },
    "illiquid": {
        "label": "换手率低于下限",
        "remedy": "阈值见 ScreenerConfig.min_turnover_pct",
    },
    "pe_out_of_band": {
        "label": "PE(TTM) 不在通道区间内",
        "remedy": "主通道要求 0 < PE ≤ max_pe；亏损股走观察通道（PE 为 NaN）",
    },
    "o_unsourced": {
        "label": "O（注销型回购）> 0 但来源未声明",
        "remedy": "`o.json` 里 `o_annual_mm > 0` 时必须同时给 `source`（年报页码/链接）与 "
                  "`confirmed_by`（human|llm）。**没有来源的非零 O 比 0 更糟** —— "
                  "它是凭空补进去的数，会静默抬高穿透回报率。缺来源时本项按 0 处理",
    },
    "o_invalid": {
        "label": "O（注销型回购）取值非法",
        "remedy": "`o_annual_mm` 必须是非负有限数（单位：百万，与 §15 摘要一致）。"
                  "负值或非数值一律拒绝并按 0 处理，不得取绝对值",
    },
    "no_dividend": {
        "label": "股息率为 0 或不可用",
        "remedy": "龟龟链的分配意愿 M 依赖分红历史；建池时要求 dv_ttm > 0。"
                  "若某只确实不分红，它无法进入龟龟的穿透回报率评估",
    },
    # --- 数据缺口（fail-closed） ---
    "no_annual_3y": {
        "label": "可用的年度财报不足 3 年",
        "remedy": "`_get_annual_df` 按财年末月过滤；港股/美股需确认报表接口覆盖与财年非 12 月的情形",
    },
    "no_cashflow": {
        "label": "现金流量表关键输入缺失（经营现金流或资本开支）",
        "remedy": "canonical 硬门禁要求 ≥2 年同时有 OCF 与 capex；缺失 ≠ 零值，**不得手算替代**",
    },
    "no_balance_sheet": {
        "label": "资产负债表不可用",
        "remedy": "检查该标的资产负债表接口权限与字段覆盖",
    },
    "no_payout": {
        "label": "支付率无法裁定（分红数据全缺）",
        "remedy": "检查分红历史与分配现金流；港股 `dps_hkd` 为按公告快照，需人工核对年报附注",
    },
    "no_price": {
        "label": "收盘价不可用",
        "remedy": "港股 `hk_daily` 配额为 1 次/小时且实测批量不可用 ⇒ 走直连 Yahoo chart；"
                  "仍失败则该股当日不参与排名",
    },
    "no_market_cap": {
        "label": "市值/股数不可用",
        "remedy": "检查基本信息与行情是否齐备；港股需 fx.json 才能折算报表币种",
    },
    "no_shares": {
        "label": "股本不可用（申报股本与上游股本均缺）",
        "remedy": "按 `shares_utils` 优先级补 `{output_dir}/shares.json`；"
                  "**不得**用「市值 ÷ 现价」反推作为可信值（known_issues #25）",
    },
    "hk_field_gap": {
        "label": "港股接口缺少该小节所需字段（上游数据缺口）",
        "remedy": "见 diagnosis_hk_section17.md；该小节在港股下暂不可用，属已知缺口",
    },
    "input_inconsistent": {
        "label": "输入参数方向不自洽（外推会给出方向性错误的结论）",
        "remedy": "该股**拒绝输出数值**（fail-closed），不得手算替代",
    },
    "exception": {
        "label": "计算过程抛出异常",
        "remedy": "查看该股的错误文本，按提示修复后重跑；不要跳过该股再看下一只",
    },
    # --- 全局输入缺口（影响整批） ---
    "rf_missing": {
        "label": "无风险利率不可用（yc_cb 无权限且 rf.json 缺失）",
        "remedy": "**全局阻断**：II 错则全市场排名口径全错。"
                  "补 `{output_dir}/rf.json`（照 fx.json 的 option-A 契约）后用 `--rf` 传入",
    },
    "fx_missing": {
        "label": "跨币种汇率不可用",
        "remedy": "补 `{output_dir}/fx.json`（含 `reporting_currency` 声明）；"
                  "**不得**按 1.0 兜底 —— 港股会静默低估约 15.65%（P0-2）",
    },
    "calendar_missing": {
        "label": "该市场交易日历不可用",
        "remedy": "Tushare `trade_cal` 无 HKEX（实测 0 行）⇒ 港股用自建节假日表，"
                  "或由价格序列的实际有数据日期反推；缺失时该市场整体 ABORT，不猜交易日",
    },
    "market_data_not_ready": {
        "label": "当日行情未就绪",
        "remedy": "A 股 `daily_basic` 当日数据约 19:00 后完整；港股财务指标约 18:00 更新。"
                  "**ABORT**，不得用昨日数据假装今日",
    },
    # --- 快照过期 ---
    "upstream_gate_failed": {
        "label": "上游产物的门禁未通过（采集器判定该包不得作为分析输入）",
        "remedy": "读该产物目录的 `gate_status.json` 的 `blocking[]`，按其中的 code 修数据后重跑采集；"
                  "确知影响面时用采集器的 `--allow-degraded` 显式降级（会把 degraded 写进裁决并须披露）。"
                  "**不得**直接引用被阻断的数字 —— 那正是本码存在的理由",
    },
    "stale_snapshot": {
        "label": "慢层快照过期（已有更新的财报期）",
        "remedy": "重跑该股的慢层财务链；用旧盈利排名比不排名更危险",
    },
    # --- 阈值（非错误） ---
    "below_threshold": {
        "label": "安全边际未达门槛（算得出来但不达标）",
        "remedy": "无需补救；这是**合格的排除**，日报应输出「今日 0 只达标」而不是硬凑 N",
    },
    "not_in_pool": {
        "label": "不在常备池内且未通过候选发现",
        "remedy": "池外标的走异步慢路径补齐定性分析（Workflow 1），不阻塞当日交付",
    },
    # ---- 与采集器/analysis 侧 `section_contract.REASONS` 对齐的码 ----------------
    # 为什么必须有这几个：§17 小节状态里的 reason 直接来自 section_contract 的受控词表，
    # 若选股器词表缺码，`slow_layer` 只能打一条"未登记"警告 ⇒ excluded.json 的排除理由
    # 不可诊断。两侧词表的包含关系由 tests/test_reason_vocab_sync.py 锁定。
    "no_rf": {
        "label": "无风险利率 Rf 不可用（yc_cb 无权限且未提供替代值）",
        "remedy": "在数据包 §14 补 Rf 或用 --rf 传入后重跑采集；Rf 缺失是**全局**缺口，整批 ABORT",
    },
    "no_data": {
        "label": "该小节所需的原始数据不足",
        "remedy": "检查 §1–§16 原始数据是否齐备；必要时重跑全量采集",
    },
    "not_applicable": {
        "label": "该小节对本标的不适用（不是数据缺口）",
        "remedy": "无需补救；若出现在 §17.11/§17.14 这类**决策小节**，选股器仍按 "
                  "decision_section_missing 排除（缺同源数字），但排除理由应可区分于数据缺失",
    },
    "negative_basis": {
        "label": "可支配现金结余 AA 为负 ⇒ 派生指标在定义域外",
        "remedy": "AA<0 是真实现金缺口（资本开支超过经营现金流），非数据错误。"
                  "此时「税后回报率」与「按 k 折扣取悲观」均失去定义 ⇒ 无买点可推导；"
                  "选股器按 decision_section_missing 排除，理由记为 negative_basis",
    },
}


def require(code: str) -> str:
    """校验原因码在受控词表内，返回它；未知码**抛异常**（fail-closed）。"""
    if code not in SCREEN_REASONS:
        raise KeyError(
            f"未知的原因码 {code!r}；受控词表为 {tuple(SCREEN_REASONS)}。"
            "新增码必须同时补 label/remedy 与测试 —— 否则排除就不可诊断。"
        )
    return code


def label_of(code: str) -> str:
    return SCREEN_REASONS[require(code)]["label"]


def remedy_of(code: str) -> str:
    return SCREEN_REASONS[require(code)]["remedy"]


#: 影响整批而非单只的码 —— 出现即应 ABORT，而不是"排掉这几只继续跑"
_GLOBAL_CODES = frozenset({
    "rf_missing", "calendar_missing", "market_data_not_ready",
    # `no_rf` 与 `rf_missing` 是同一件事的两个来源名（前者来自 section_contract 词表、
    # 后者是选股器自己的命名）。两者都代表"整批没有无风险利率" ⇒ 都是全局。
    "no_rf",
})


def is_global(code: str) -> bool:
    """该原因码是否代表**全局**输入缺口（影响整批，应 ABORT）。"""
    return require(code) in _GLOBAL_CODES


def describe(codes: Mapping[str, int]) -> str:
    """把 ``{code: count}`` 渲染成人类可读的汇总（用日报的"排除原因分布"）。"""
    if not codes:
        return "（无排除）"
    parts = []
    for code, n in sorted(codes.items(), key=lambda kv: (-kv[1], kv[0])):
        parts.append(f"{code}×{n} — {label_of(code)}")
    return "；".join(parts)
