"""Turtle Investment Framework - Tushare field mapping constants.

All *_MAP dicts used by TushareClient mixins live here.
"""

_VIP_MAP = {
    "income": "income_vip",
    "balancesheet": "balancesheet_vip",
    "cashflow": "cashflow_vip",
    "fina_indicator": "fina_indicator_vip",
    "fina_mainbz": "fina_mainbz_vip",
    "forecast": "forecast_vip",
    "express": "express_vip",
}

# ---------------------------------------------------------------------------
# Multi-source field specs (HK/US line-item maps)
#
# A *_MAP value is normally a single ind_name (str). When a single internal
# field has to be assembled from more than one line item, use a spec dict:
#
#     {"names": ["购建固定资产", "购建无形资产及其他资产"], "mode": "sum"}
#
# mode:
#   "sum"   — sum every present name (e.g. HK CapEx = PP&E + intangibles/other)
#   "first" — first non-null in the listed order (fallback chain)
#
# Plain str / list values keep "first" semantics, so all pre-existing 1:1 maps
# behave exactly as before.
# ---------------------------------------------------------------------------


def field_names(spec) -> list:
    """Return the ind_name(s) that a *_MAP entry resolves to."""
    if isinstance(spec, str):
        return [spec]
    if isinstance(spec, dict):
        return [n for n in (spec.get("names") or []) if n]
    if isinstance(spec, (list, tuple)):
        return [n for n in spec if n]
    return []


def field_mode(spec) -> str:
    """Return the aggregation mode for a *_MAP entry ('sum' or 'first')."""
    if isinstance(spec, dict):
        mode = spec.get("mode", "first")
        return mode if mode in ("sum", "first") else "first"
    return "first"


# ---------------------------------------------------------------------------
# 门槛 II —— 全市场统一口径 (known_issues.md P1-12)
#
# **决策口径：所有市场一律 max(5%, Rf + 3%)**（2026-10-13 确定）。
#
# 为什么不按市场分档：视频方法论的原始表述自身矛盾且不完整 ——
#   A股  「最少有 3%」[1:34:10] vs「股息率至少要四个点」[44:06]，而唯一算例
#        （五粮液 4459 亿 = 悲观利润×0.7÷4%）用的是 4%
#   港股  「最少在 4% 以上，最好都在 5%」[1:34:10]
#   美股  未定义
# 因此项目改为统一取「港股最好档 5%」为下限、Rf + 3% 为浮动项，
# 对三个市场一致适用，消除市场间的档位漂移。
#
# 该口径由 constants 单点提供；§17.2 / §17.9 / §17.11 一律经
# TushareClient._get_rf_ii() 取值，不得在别处重写 max() 公式。
# ---------------------------------------------------------------------------
II_FLOOR = 5.0      # 下限（%）
II_SPREAD = 3.0     # 相对无风险利率的浮动项（pct）


def market_key(ts_code) -> str:
    """Map a ts_code to its market bucket (used for labels only)."""
    upper = str(ts_code or "").upper()
    if upper.endswith(".HK"):
        return ".HK"
    if upper.endswith(".US"):
        return ".US"
    return "A"


def ii_threshold(ts_code, rf_pct: float) -> float:
    """Return 门槛 II in percent: ``max(5%, Rf + 3%)`` for every market."""
    return max(II_FLOOR, rf_pct + II_SPREAD)


def ii_label(ts_code, rf_pct: float) -> str:
    """Human-readable derivation of the threshold, for the data pack."""
    market = {"A": "A股", ".HK": "港股", ".US": "美股"}[market_key(ts_code)]
    return f"{market}: max({II_FLOOR}%, {rf_pct:.2f}%+{II_SPREAD}%)"

# HK line-item field mappings: Tushare column name → HK ind_name (Chinese)
HK_INCOME_MAP = {
    "revenue": "营业额",
    "oper_cost": "营运支出",
    "sell_exp": "销售及分销费用",
    "admin_exp": "行政开支",
    "operate_profit": "经营溢利",
    "invest_income": "应占联营公司溢利",
    "int_income": "利息收入",
    "finance_exp": "融资成本",
    "total_profit": "除税前溢利",
    "income_tax": "税项",
    "n_income": "除税后溢利",
    "n_income_attr_p": "股东应占溢利",
    "minority_gain": "少数股东损益",
    "basic_eps": "每股基本盈利",
    "diluted_eps": "每股摊薄盈利",
    # --- 持续经营 / 已终止经营 口径分列（known_issues.md §18）------------------------
    # 上游同时提供两套口径，实测 06862（hk_income，2021，单位千元）：
    #   营业额 / 除税前溢利 / 税项      = 持续经营口径
    #   持续经营业务税后利润            = −3,247,846 = 除税前 − 税项   ← 完全自洽
    #   除税后溢利                      = −4,161,206 = 持续经营 + 终止业务(−913,360)
    #   终止或非持续业务溢利            = −913,360
    #   股东应占溢利                    = −4,163,175（**含已终止经营**）
    # 旧实现把「除税前（持续）+ 税项（持续）+ 除税后（合计）」放进同一列 ⇒ 同年三数不能
    # 自洽（2021 差 913.37、2022 差 264.09 百万元），以除税前/除税后为基础的比率全被污染。
    # 修复**不重绑 n_income**（那会静默改变历史净利口径，见 §18 第 1 条），而是把持续经营
    # 口径单独落字段，交由 §3 分列渲染、I4 按**同口径**勾稽。
    "n_income_cont": "持续经营业务税后利润",
    "discontinued_profit": "终止或非持续业务溢利",
    # 港股按性质列报时员工成本单列（实测覆盖 3/12）。现金流量表没有
    # c_pay_to_staff（该字段 A 股专属），故 W2 必须优先从利润表取。
    "c_pay_to_staff": "薪金福利支出",
}

HK_BALANCE_MAP = {
    "money_cap": "现金及等价物",
    "accounts_receiv": "应收帐款",
    "inventories": "存货",
    "total_cur_assets": "流动资产合计",
    "lt_eqt_invest": "联营公司权益",
    "fix_assets": "物业厂房及设备",
    "cip": "在建工程",
    "intang_assets": "无形资产",
    "total_assets": "总资产",
    "acct_payable": "应付帐款",
    "notes_payable": "应付票据",
    # ⚠️ 上游港股用「合同负债」（06078 实测：`field_absence.json` 的 51 个 ind_name 里有
    # 「合同负债」，年报 PDF p.98「合約負債 50,121 / 54,682 千元」），旧映射只指向
    # 「递延收入(流动)」⇒ 精确匹配失败 ⇒ §4 无该行、§17.3 的 U（合同负债变动）恒按 0 处理，
    # 真实现金收入被少扣 4.56 百万元。与 trad_asset 是同一失败模式，只是又一个变体标签。
    "contract_liab": {"names": ["合同负债", "递延收入(流动)"], "mode": "first"},
    "st_borr": "短期贷款",
    "total_cur_liab": "流动负债合计",
    "lt_borr": "长期贷款",
    "bond_payable": "应付债券",
    # 应付票据(非流动) 与 应付债券 是**两个不同科目**（上游 06862 实测两者并存：
    # 应付债券 2,028,881 千元 / 应付票据另计）。旧映射把 bond_payable 指向"应付票据(非流动)"，
    # 导致真实债券值永远落不进 bond_payable ⇒ §4 无债券行、有息负债漏计 20.29 亿元。
    # 现分别映射，任何一方都不会被静默丢弃。
    "notes_payable_noncur": "应付票据(非流动)",
    "total_liab": "总负债",
    "defer_tax_assets": "递延税项资产",
    "defer_tax_liab": "递延税项负债",
    "total_hldr_eqy_exc_min_int": "股东权益",
    "minority_int": "少数股东权益",
    # --- §17.16 防御性门槛组 + §17.15 防御性净值所需（known_issues.md §3.21 21c/21d）---
    # 上游港股/美股用「交易性金融资产(流动)」「短期投资」等带后缀标签；
    # 精确匹配单一标签会让整行**永远为空**（实测 00696 各年 21.7~60.2 亿的
    # 交易性金融资产从未落表，现金与 §17.15/§17.16 同步低估）——独立审计 P1。
    "trad_asset": {"names": ["交易性金融资产(流动)", "交易性金融资产",
                             "短期投资", "以公允价值计量且其变动计入当期损益的金融资产",
                             # ⚠️ 港股另一套措辞（2026-10-16 实测 06078）：上游返回
                             # 「指定以公允价值记账之金融资产(流动)」，与上一条**用词不同**
                             # ⇒ 精确匹配失败 ⇒ §4「交易性金融资产」整行为空，
                             # §17.15/§17.16 的现金口径同步低估 258,953 千元。
                             "指定以公允价值记账之金融资产(流动)",
                             "指定以公允价值记账之金融资产"],
                   "mode": "first"},
    "st_deposit": "短期存款",
    "lt_deposit": "中长期存款",
    "restricted_cash": "受限制存款及现金",
    "goodwill": "商誉",
    "lease_liab": "融资租赁负债(流动)",
    "lease_liab_noncur": "融资租赁负债(非流动)",
}

# ---------------------------------------------------------------------------
# 有息负债口径（known_issues.md 2026-10-14：租赁负债必须并入）
#
# 为什么必须并入：HKFRS 16 已把租赁负债**全额资本化到表内**，它是真实的合同义务。
# 旧口径只算 (短期借款 + 长期借款 + 应付债券 + 一年内到期)，把租赁负债留在外面，
# 于是「广义净现金」被系统性高估（实测 06862：租赁负债 3,506.51 百万元不在口径内，
# 净现金 6,203.16 应为 2,696.65）。
#
# 单点定义的原因：这套组合此前在 6 处重复实现（§17.1 / §17.7 / §17.8 / §17.15 /
# §17.16 / screener / valuation_engine），已经有过"同一阈值三处重复实现"的事故（P1-12）。
# ---------------------------------------------------------------------------
IBD_COMPONENTS = ("st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y")
LEASE_COMPONENTS = ("lease_liab", "lease_liab_noncur")
IBD_COMPONENTS_WITH_LEASE = IBD_COMPONENTS + LEASE_COMPONENTS

# ---------------------------------------------------------------------------
# 「科目缺失 = 真的没有」证据表（known_issues.md：C2 假阳性）
#
# 背景：field_coverage 的 C2 抓到 06862 的 P0 缺口（`bond_payable` 映射标签写错 ⇒
# §4 的「应付债券」行声明了却全年度为空 ⇒ 有息负债漏计 20.29 亿）后，把「声明了却
# 全年度为空」一律判 FAIL。但**真的没有该科目**的发行人在结构上无法与它区分：
# 实测 100 个产物中 **40 个**因此 FAIL，其中五粮液（无借款、只有 355 百万元租赁负债）、
# 中国移动、江苏金租等显然正确 —— 假阳性率 40% 让 `--allow-degraded` 变成常态，门禁随即失效。
#
# 判据必须来自上游**科目清单本身**，而不是「值为空」这个结果：
#   · 港股/美股走 `hk_balancesheet`/`us_balancesheet` 的**行项目长表**（ind_name），
#     所以「整张资产负债表的 ind_name 集合里没有这个科目名」是可核验的**缺失证据**
#     —— 06862 的「应付债券」在 ind_name 里**存在**（值 2,028,881 千元），永远不会被误放行；
#   · A 股走 `balancesheet` 宽表字段（st_borr 等），拿不到科目清单，字段全 NaN 只说明
#     「上游没给数」、不能证明「科目不存在该字段里」⇒ 标 `unreported`，**C2 仍判 FAIL**。
#     （否则在 A 股上把 06862 那类映射错位静默洗白。）
#
# 证据由采集器写进 `{output_dir}/field_absence.json`，C2 只有读到 `verified_absent`
# 才降级为披露；sidecar 缺失一律按原语义 FAIL（fail-closed）。
# ---------------------------------------------------------------------------
DEBT_LINE_ITEM_FIELDS = (
    "st_borr", "lt_borr", "bond_payable", "notes_payable_noncur",
    "lease_liab", "lease_liab_noncur",
)

# field → (§4 行标签, 上游科目名)。行标签必须与 financials.py 的 §4 fields 列表逐字一致，
# 否则 C2 的「声明但全空」对不上证据键。上游科目名取自 HK_BALANCE_MAP。
DEBT_ROW_LABELS = {
    "st_borr": ("短期贷款", "短期借款"),
    "lt_borr": ("长期贷款", "长期借款"),
    "bond_payable": ("应付债券",),
    "notes_payable_noncur": ("应付票据(非流动)",),
    "lease_liab": ("融资租赁负债(流动)", "租赁负债"),
    "lease_liab_noncur": ("融资租赁负债(非流动)",),
}



def interest_bearing_debt(row, include_lease: bool = True) -> float:
    """有息负债合计（**默认含租赁负债**，见上）。

    返回 0.0（而不是 None）当所有分量都缺失 —— 调用方通常在累加，None 会污染整条链；
    "科目全缺"由 field_coverage C2 负责报出，不在这里静默处理。
    """
    comps = IBD_COMPONENTS_WITH_LEASE if include_lease else IBD_COMPONENTS
    total = 0.0
    for c in comps:
        try:
            v = row.get(c)
        except AttributeError:
            v = None
        try:
            if v is not None and v == v:      # 排除 NaN
                total += float(v)
        except (TypeError, ValueError):
            continue
    return total

HK_CASHFLOW_MAP = {
    "n_cashflow_act": "经营业务现金净额",
    "n_cashflow_inv_act": "投资业务现金净额",
    "n_cash_flows_fnc_act": "融资业务现金净额",
    # CapEx: Tushare 港股把「购建固定资产」与「购建无形资产及其他资产」拆成两个
    # 独立 ind_name。只映射后者会低估 CapEx 1.5–130 倍（实测 06862/00506/06049），
    # 保利物业更是只有前者、完全取不到值。故必须求和。
    "c_pay_acq_const_fiolta": {
        "names": ["购建固定资产", "购建无形资产及其他资产"],
        "mode": "sum",
    },
    # D&A: 港股现金流量表里带「加:」前缀（间接法调整项）。
    # 实测无前缀的「折旧及摊销」在 5/5 标的中都不存在，原写法永远匹配不上。
    "depr_fa_coga_dpba": "加:折旧及摊销",
    "c_pay_dist_dpcp_int_exp": "已付股息(融资)",
    "c_paid_for_taxes": "已付税项",
    "c_recp_return_invest": "收回投资所得现金",
    # --- 文档因子3 要求的额外扣减项（known_issues.md §3.21 21e）---
    # `AA = OCF − Capex` 原本是**可支配现金的上界**；这四项按文档要求一并扣除。
    #   「收购附属公司」「偿还融资租赁」「购买子公司少数股权」记在投资/融资表，
    #   不包含在 OCF 内，故必须单独扣除（文档 [26:27] / [20:40] / [26:35]）。
    "c_pay_acq_sub": "收购附属公司",
    "c_pay_fin_lease": "偿还融资租赁",
    "c_pay_minority": "购买子公司少数股权而支付的现金",
    # 利息按**分类**区分，不能一律扣：
    #   `已付利息(融资)` 不在 OCF 内 → 需另扣（06862 / 02669 属此）
    #   `已付利息(经营)` 已在 OCF 内 → 扣除会重复（06049 / 00506 属此）
    "c_pay_int_finance": "已付利息(融资)",
    "c_pay_int_operating": "已付利息(经营)",
}

# US line-item field mappings: Tushare column name → US ind_name (Chinese)
US_INCOME_MAP = {
    "revenue": "营业收入",
    "oper_cost": "营业成本",
    "gross_profit": "毛利",
    "sell_exp": "营销费用",
    "rd_exp": "研发费用",
    "operate_profit": "经营利润",
    "n_income": "净利润",
    "n_income_attr_p": "归属于母公司净利润",
    "basic_eps": "基本每股收益",
    "diluted_eps": "稀释每股收益",
}

US_BALANCE_MAP = {
    "money_cap": "现金及等价物",
    "accounts_receiv": "应收帐款",
    "inventories": "存货",
    "total_cur_assets": "流动资产合计",
    "fix_assets": "固定资产",
    "intang_assets": "无形资产",
    "total_assets": "总资产",
    "acct_payable": "应付帐款",
    "st_borr": "短期贷款",
    "total_cur_liab": "流动负债合计",
    "lt_borr": "长期贷款",
    "total_liab": "总负债",
    "defer_tax_assets": "递延税项资产",
    "defer_tax_liab": "递延税项负债",
    "total_hldr_eqy_exc_min_int": "股东权益",
    "minority_int": "少数股东权益",
    "goodwill": "商誉",
    # 上游港股/美股用「交易性金融资产(流动)」「短期投资」等带后缀标签；
    # 精确匹配单一标签会让整行**永远为空**（实测 00696 各年 21.7~60.2 亿的
    # 交易性金融资产从未落表，现金与 §17.15/§17.16 同步低估）——独立审计 P1。
    "trad_asset": {"names": ["交易性金融资产(流动)", "交易性金融资产",
                             "短期投资", "以公允价值计量且其变动计入当期损益的金融资产",
                             # ⚠️ 港股另一套措辞（2026-10-16 实测 06078）：上游返回的是
                             # 「指定以公允价值记账之金融资产(流动)」，与上面那条**用词不同**
                             # ⇒ 精确匹配失败 ⇒ §4「交易性金融资产」整行为空，
                             # 而 §17.15「现金 = 货币资金 + 交易性金融资产」与 §17.16 判据
                             # 同步低估现金（06078：258,953 千元 = 2.59 亿从未落表）。
                             # 与 00696 那次是**同一失败模式**，只是又一个变体标签。
                             "指定以公允价值记账之金融资产(流动)",
                             "指定以公允价值记账之金融资产"],
                   "mode": "first"},
}

US_CASHFLOW_MAP = {
    "n_cashflow_act": "经营活动现金净额",
    "n_cashflow_inv_act": "投资活动现金净额",
    "n_cash_flows_fnc_act": "筹资活动现金净额",
    "c_pay_acq_const_fiolta": "资本支出",
    "depr_fa_coga_dpba": "折旧及摊销",
    "c_pay_dist_dpcp_int_exp": "已付股息",
}

# yfinance field mappings: yfinance index name → Tushare column name
# Both CamelCase and space-separated variants included (format varies by yfinance version)
_YF_INCOME_MAP = {
    "Total Revenue": "revenue", "TotalRevenue": "revenue",
    "Cost Of Revenue": "oper_cost", "CostOfRevenue": "oper_cost",
    "Selling General And Administration": "admin_exp",
    "SellingGeneralAndAdministration": "admin_exp",
    "Operating Income": "operate_profit", "OperatingIncome": "operate_profit",
    "Interest Expense": "finance_exp", "InterestExpense": "finance_exp",
    "Pretax Income": "total_profit", "PretaxIncome": "total_profit",
    "Tax Provision": "income_tax", "TaxProvision": "income_tax",
    "Net Income": "n_income", "NetIncome": "n_income",
    "Net Income Common Stockholders": "n_income_attr_p",
    "NetIncomeCommonStockholders": "n_income_attr_p",
    "Basic EPS": "basic_eps", "BasicEPS": "basic_eps",
    "Diluted EPS": "diluted_eps", "DilutedEPS": "diluted_eps",
    "Gross Profit": "gross_profit", "GrossProfit": "gross_profit",
    "Research And Development": "rd_exp", "ResearchAndDevelopment": "rd_exp",
}

_YF_BALANCE_MAP = {
    "Cash And Cash Equivalents": "money_cap",
    "CashAndCashEquivalents": "money_cap",
    "Accounts Receivable": "accounts_receiv", "AccountsReceivable": "accounts_receiv",
    "Inventory": "inventories",
    "Current Assets": "total_cur_assets", "CurrentAssets": "total_cur_assets",
    "Investments And Advances": "lt_eqt_invest",
    "Net PPE": "fix_assets", "NetPPE": "fix_assets",
    "Goodwill And Other Intangible Assets": "intang_assets",
    "Total Assets": "total_assets", "TotalAssets": "total_assets",
    "Accounts Payable": "acct_payable", "AccountsPayable": "acct_payable",
    "Current Debt": "st_borr", "CurrentDebt": "st_borr",
    "Current Liabilities": "total_cur_liab", "CurrentLiabilities": "total_cur_liab",
    "Long Term Debt": "lt_borr", "LongTermDebt": "lt_borr",
    "Total Liabilities Net Minority Interest": "total_liab",
    "Stockholders Equity": "total_hldr_eqy_exc_min_int",
    "StockholdersEquity": "total_hldr_eqy_exc_min_int",
    "Minority Interest": "minority_int", "MinorityInterest": "minority_int",
    "Goodwill": "goodwill",
    "Other Short Term Investments": "trad_asset", "OtherShortTermInvestments": "trad_asset",
}

_YF_CASHFLOW_MAP = {
    "Operating Cash Flow": "n_cashflow_act", "OperatingCashFlow": "n_cashflow_act",
    "Investing Cash Flow": "n_cashflow_inv_act", "InvestingCashFlow": "n_cashflow_inv_act",
    "Financing Cash Flow": "n_cash_flows_fnc_act", "FinancingCashFlow": "n_cash_flows_fnc_act",
    "Capital Expenditure": "c_pay_acq_const_fiolta", "CapitalExpenditure": "c_pay_acq_const_fiolta",
    "Depreciation And Amortization": "depr_fa_coga_dpba",
    "DepreciationAndAmortization": "depr_fa_coga_dpba",
    "Common Stock Dividend Paid": "c_pay_dist_dpcp_int_exp",
    "Income Tax Paid Supplemental Data": "c_paid_for_taxes",
    "Sale Of Investment": "c_recp_return_invest",
}
