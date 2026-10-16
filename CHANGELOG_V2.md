# Changelog — 龟龟投资策略 v2

> 本文件记录 v1.1 → v2 → v3 的所有变更。当前版本：**v3**

---

## 2026-10-13 — 已知问题修复第一轮（字段映射 / 市值量纲 / 值域保护 / 文档）

依据 [`known_issues.md`](known_issues.md)（22 项已确证问题，全部为上游继承）。本轮修复 9 项，**未触碰计算路径本身**（P0-1 AA 口径与 P0-2 币种换算仍待办）。

**测试基线：1094 → 1115 passed, 3 skipped**（新增 21 个回归测试）。

### fix(tushare): 港股财报字段映射

- **CapEx 补齐**：`HK_CASHFLOW_MAP` 原先只映射 `购建无形资产及其他资产`，丢掉 CapEx 主体的
  `购建固定资产`。实测低估 1.5–130 倍（06862 FY2024 `10.13` vs 真值 `1,309.18`），
  保利物业（06049）更是完全取不到值（CapEx 恒为 0）——因为它**只有** `购建固定资产`。
- **D&A key 修正**：实际 `ind_name` 是 `加:折旧及摊销`（间接法调整项），原写法 `折旧及摊销`
  在 5/5 港股标的中都不存在，`isin` 精确匹配永远失败。连带后果是 `§17.12 G 系数网格`
  对港股**永久不可用**（`da <= 0` 直接返回 None）——现恢复。
- **`HK_*_MAP` 结构扩展**：值支持多源规格 `{"names": [...], "mode": "sum"|"first"}`，
  新增 `constants.field_names` / `field_mode`。**1:1 映射行为逐字节不变**，US 映射不受影响。
- **W2 三级回退链**：`HK_INCOME_MAP` 补 `薪金福利支出`（港股按性质列报时员工成本单列）。
  `_compute_factor3_step4` 改为 ①利润表薪金福利 → ②现金流量表 `c_pay_to_staff` → ③SGA 兜底（打 †）。
  实测 06862 FY2025：**1,475.5 → 14,072.97 百万**，与年报 `員工成本 (14,072,970)` 千元精确吻合。
- **删除死代码**：`YFinanceMixin._pivot_hk_line_items` 与 `InfrastructureMixin` 的同名方法字节相同，
  但 MRO 中 Infrastructure 在前，**该副本从不执行**——改它不会有任何效果。已删除并加注说明。

### fix(turtle): 港股市值量纲（P0）

- `hk_fina_indicator.total_market_cap` 是**原始货币单位**，不是百万。实测 10/10 港股标的
  （00700 = 3,862,538,856,756 ≈ HK$3.86 万亿；06862 = 53,343,180,000 ≈ HK$533 亿）。
  文档从未标注单位，代码在 **4 处**假设"百万"并无条件 `× 1e6`，导致市值偏大 1e6 倍 ——
  港股 §17.11 的 GG 与目标买入价会被**静默压到 ≈ 0**。
- 新增 `infrastructure._hk_market_cap_raw()` 作为唯一归一化入口：按原始元处理，
  低于 `1e6` 下限时判为旧"百万"形态并 `×1e6` + 记录告警；缺失/非正返回 `None` 让下游降级。
- 修正 4 处调用点：`derived_metrics.py` §17.8（需百万口径，改 `/1e6`）、§17.9、`_get_mktcap_shares`、
  `financials.py` §1 显示（`divider=1` → `1e6`）、`valuation_engine.py` HK 分支。
- **自校验**：`cap / close` 必须复现年报已发行股份数 —— 06862 得 5,574,000,000 股（年报 5,574,000,000）。

### fix(turtle): 负值保护（P0）

- `GG ≤ 0` 时目标买入价 = `现价 × GG/II` 是负数：600941 中国移动曾输出 **−64.79 元**（§17.9 门槛价格同）。
  GG ≤ 0 的语义是"按当前股东现金回报，任何价格都不达门槛"，**无解**。
  现输出 `—` 并在表下说明；顺带删除未使用的 `threshold_price_base`。

### fix(tushare): stuck-DPS 守卫阈值

- `hk_fina_indicator.dps_hkd` 会返回近常量序列。原守卫 `len(set(...)) == 1` 只覆盖"全部相同"，
  而 2026-08 的实际形态是 `{0.7220, 1.2290}` 在 10 行里循环（2 个不同值）→ **未兜住**，
  导致 §17.10 法1 输出 192%–983% 的支付率。现放宽为 `len(set) <= 2`。

### docs: 修正三处接口文档错误 + 两处表述残留

- `tushare_api_reference.md`：
  - 港股金额单位由「港币 HKD (元)」改为「**发行人报告币种**」——多数 A+H/红筹以人民币列示
    （年报原文 `The presentation currency … was Renminbi`）。这是 P0-2 的根因：代码按 `.HK`
    后缀返回 HKD，忠实实现了这份错误文档。
  - `total_market_cap` / `hksk_market_cap` 补单位说明 + 自检方法（与 `close × 总股本`、
    与 `pb_ttm` 双向核对）。
  - 三节 `ind_name 常见取值` 补齐关键项（`购建固定资产`、`加:折旧及摊销`、`薪金福利支出`、
    `商誉`、`收购附属公司`、`回购股份`、`融资租赁负债`…）并加"清单不完整"警告 ——
    实测 `hk_cashflow` 有 47–60 个 `ind_name` 而文档只列 8 个，**照文档写映射必然漏**。
- `derived_metrics.py:425` 注释修正（来源写成 `hk_daily`、单位写成"百万"，两处都错）。
- `README.md`：「否决门（ROE < 8% → 直接否决）」为 v1.1 残留，v2.0 起已明确"无否决门"，已改正。
- `strategies/valuation/coordinator.md`：顶部声明本模块为通用估值参考、不属于龟龟方法论，
  与龟龟报告冲突时以龟龟为准。

### fix(turtle): AA 改为 OCF 锚定 + 计算闸门（P0-1）

- **口径修正**：`AA = 经营现金流 − |资本开支|`。文档因子3 的被减数本就是「经营性现金流」，
  而旧口径 `真实现金收入 − W − 资本开支` 里 `W1 = 利润表营业成本 + ΔAP` **含 D&A 却从未加回**——
  导致重资产公司 AA 系统性为负。
  **实测 600941 中国移动**：AA_2y 由 **−70,808 → +117,865** 百万元，逐年值与 §5 `自由现金流`
  **完全一致**（75,968 / 159,762 / 122,517 / 91,162）；此前 4/4 年符号与 FCF 相反。
- **C3 硬闸门**：AA 的两个输入（`n_cashflow_act` / `c_pay_acq_const_fiolta`）必须同时具备 ≥2 年，
  否则 §17.5 输出「**本节未生成**」并说明原因，而不是拿空值当 0 计算。
- **C4 对账**：§17.5 逐行新增「倒推结余（审计）」与「偏差%」列；偏差 >20% 时输出成因告警。
  实测 06862 8/10 年、600941 7/7 年超过 20%（最大 −479.6%），此前**完全不可见**。
- **λ/CV 改用新 AA 序列**；新增 `AA_latest`（短视主义口径，供后续使用）。
- 倒推口径保留为审计参考，**不再参与任何下游计算**。

### fix(tushare): 港股交易币种 → 报表币种换算（P0-2）

- **问题**：港股以 HKD 交易，但多数 A+H / 红筹发行人（06862 / 00506 / 02669 / 06049）
  的报表币种是 **CNY**（年报原文 `The presentation currency … was Renminbi`）。
  收集器按 ticker 后缀标注币种，于是 `R = 净利润(CNY) / 市值(HKD)` 混用两种币种，
  系统性低估 `1/0.8647 − 1 = 15.65%`。
- **新增 `scripts/fx_utils.py`**：解析 `fx.json` / 数据包协调器注；优先级
  `--fx-hkd-cny` → `TUSHARE_FX_HKD_CNY` → `{output_dir}/fx.json` → 包内注记。
- **新增 `InfrastructureMixin._fx_to_reporting()`**；4 处消费点（§17.8 / §17.9 /
  `_get_mktcap_shares` / `valuation_engine`）改为**交易币种取股数、报表币种作分母**。
- **collector 新增 `--fx-hkd-cny`**（含 dry-run 与启动横幅提示）。
- **无汇率时不静默**：照常按未换算计算，但写入 `_store["_fx_warnings"]` 并在启动时打印警告。
- 已为 5 只港股 pack 落盘 `fx.json`（`factor_to_reporting` / `date` / `source` / 币种声明）。
- **实测 06862**：GG 由 5.61% → **6.48%**，目标买入价 14.56 → **16.84** 港元。

### 测试

- 新增 `tests/test_fx_utils.py`（17 例）；`TestSensitivityBase` 重写为 12 例（覆盖 OCF 口径、
  闸门、审计对照、`AA_latest`）；`TestHKMarketCapUnit` +4 例覆盖汇率换算。
- **1094 → 1147 passed, 3 skipped**。

### fix(turtle): 门槛 II 单点化 + 消除报告内口径矛盾（P1-8 / P1-10 / P1-11 / P1-12）

- **P1-12 门槛 II 单点化**：`max(floor, Rf+spread)` 原先在 `_compute_factor2_inputs`、
  `_compute_factor4_sensitivity`、`_get_rf_ii` **三处各写一遍**，且美股档与
  `shared_tables.md` 的 `max(5%, Rf+3%)` 矛盾（代码是 `max(4%, Rf+2%)`）。
  现集中到 `constants.II_THRESHOLDS` / `ii_threshold()` / `ii_label()`，三处全部改调
  `_get_rf_ii()`。**项目档为决策档**（沿用既有行为，避免结论静默翻转），
  文档档作为 §17.11 表头的「门槛对照」行输出，差值显式可见。
  方法论来源本身自相矛盾（A股 3% vs 4% vs 算例 4%；美股未定义），两档均已写明出处。
- **P1-10 §17.9 对齐 §17.11**：§17.9 原先用 §17.1 的 3 年均值且**不含 (1−Q)**，
  与 §17.11 的 `M_rec` + 默认 Q 并存于同一报告，**港股下相差 25%**，而
  `phase3_valuation.md` 又指示 Agent 引用 §17.9 的「门槛价格」。
  现两表同源，并有不变式测试断言二者基准 GG 必须相等。
- **P1-8 港股 §17.10 法1**：`dps_hkd` 是「按公告的每股股息快照」，同一年度的中期与
  年度两行不是可累加事件。旧实现无条件求和，把 06862 FY2024 算成
  `(0.7220 + 1.2290) / 0.87 = 224.25%`。现每年取年度（1231）单一值、无年度行取最大值；
  **A 股中期+末期仍正常累加**（那是不同的现金分派事件）。并新增告警说明该字段本身
  与年报不符（06862 实测 0.7220/1.2290 vs 年报 0.898），港股 M 应以法2 现金流口径为准。
- **P1-11 §17.3 AR 规则**：提示词写 `S − T净增 − U净减`（AR 净减少时**加回**），
  而 Python 实现是 `S − max(0,T) − max(0,−U)`（**不加回**）。同一条规则在提示词与代码里
  相反，走降级路径手算的 Agent 会得到与网格不同的数。现提示词对齐代码的保守口径。
- 顺带：v1 存档 `prompts/references/shared_tables.md` 顶部加 superseded 标记，
  指明其美股档位已过期（避免再次被误读为权威）。

### 测试

- **1140 → 1147 passed, 3 skipped**（新增 7 例：§17.10 港股法1 三例、门槛单点三例、§17.9/§17.11 港股默认列对齐一例）。

### feat(turtle): P2-21 方法论保守性 —— 5 个子项全部落地

文档要求"不可能使用正常的利润，要打八折"，而代码一直用中性口径。经复核该问题应拆为
**5 个性质完全不同的子项**（`known_issues.md` §3.21），现已全部实现。

**21e — AA 收紧为下界**（`HK_CASHFLOW_MAP` +5 映射，§17.5 新增扣减列）
文档因子3 要求扣除记在投资/融资表、**不在 OCF 内**的真实现金流出。利息按分类区分：
`已付利息(融资)` 需扣（06862/02669），`已付利息(经营)` 已在 OCF 内**不得重复扣**（06049/00506）。
符号约定为「流出为正、净流入为负」（06862 租赁 +1,023.77 vs 06049 收购 −5.94），
故只扣 `max(0, v)` —— 用 `abs()` 会把净流入误当流出。
**实测 06862 的 AA_2y 由 6,606 → 4,110 百万（−38%）**。
A 股标准字段集没有这几项的独立条目，故 A 股 AA 仍是上界（已在 §17.5 表头标注）。

**21a — 新增 §17.14 悲观基准网格（k 折扣）**
`k ∈ {1.00 … 0.70}` 七档 × 表A（精算 AA×k）/ 表B（粗算 C×k），★ 标 k=0.80（文档算例档），
统一使用 **M_pess**（§17.10 各法3年均值最低者 —— 文档明确否定均值外推
`[1:16:14]`「不能说取个平均数」）。
**这是纯输入端手段**：Agent B 选一行 + 说明理由，**输出范式（§17.11 / 仓位矩阵）不变**。
**实测 06862：k=1.0 → KK +0.78（观察档）；k=0.80★ → KK −0.37（不建仓）。**

**21b — 承诺优先 M（新增 P14 提取目标）**
文档的第一优先级是"公开文件规定的最低股息支付率"（`[1:32:56]`、`[1:09:42]`「一票成立」），
但此前 `pdf_preprocessor.py` 的分红政策关键词命中数为 **0** —— 该优先级**根本没有数据路径**。
现新增 **P14**（16 个简/繁关键词 + 区块偏好 + 提取参数），`phase2_PDF解析.md` 给出提取规格与
`dividend_commitment.json` 契约，collector 新增 `_seed_dividend_commitment()`，
§17.10 裁定链改为**承诺优先**：书面公告 → 直接锚定 M；口头 → 须 `occurrences ≥ 2`；
金额承诺 → 按最新年归母净利折算为等价支付率。

**21c — 新增 §17.16 防御性门槛组**
文档的六条下行保护判据（现金>短债 `[56:28]` / 现金>流动负债 `[56:41]` /
现金储备占市值 `[1:32:31]` / 剔净现金后可支配现金 ≥40% `[42:08]` / 净现金>市值 `[53:43]` /
现金流兜住派息 `[1:32:23]`）。**输出为档位/标记，明确不参与买入/观察/排除裁决** ——
文档自称"极端保守主义"，做成硬否决会清空选股器。
另修复一个数据陷阱：Tushare 偶发把「现金及等价物」与「短期存款」返回成同一个数
（实测 06862 FY2025 均为 6,602.3），相加会双计现金 → 已自动识别并只计一次。
**实测 06862 通过 2/6 项。**

**21d — 新增 §17.15 防御性净值（A/B 双口径）**
文档因子3 `[18:47]`「商誉无形都是假资产，我会把它扣除掉」。
**但文档自身存在张力**：因子2 `[32:45]` 用「路权拿不到 → 必须高分红」解释高速公路，
而**路权本身就是无形资产** —— 全扣会推翻该论点。
故同时输出 **A 严口径**（扣商誉 + 全部无形资产）与 **B 宽口径**（只扣商誉），
并在表内写明张力与选择规则（经营必需型用 B，收购溢价型用 A）。

- **测试 1146 → 1178 passed, 3 skipped**（新增 32 例）。
- `phase3_quantitative.md` 约束 #9 扩展至 §17.14；步骤10 增加 k 选择块；
  `phase3_valuation.md` 步骤3 改用 `GG_pess` 为决策口径并注明矩阵不重标定；
  `factor_interface.md` 新增 4 个字段（`k_selected` / `GG_pess_pct` /
  `target_buy_price_pess` / `M_pess_pct`）。

### change(turtle): 门槛 II 改为全市场统一 `max(5%, Rf + 3%)`

- 原先按市场分档（A股 `max(3.5%, Rf+2%)` / 港股 `max(5%, Rf+3%)` / 美股 `max(4%, Rf+2%)`），
  且美股档与 `shared_tables.md` 的 `max(5%, Rf+3%)` 矛盾。
- 现统一为 **`II = max(5%, Rf + 3%)`**，由 `constants.II_FLOOR` / `II_SPREAD` 单点提供。
- **这是一次刻意的收紧**：方法论来源自身矛盾（A股 3% vs 4% vs 算例 4%；港股 4% vs 5%；美股未定义），
  故取「港股最好档 5%」为下限、`Rf+3%` 为浮动项，三市场一致适用。
- 影响：600941（Rf 1.67%）门槛由 3.67% → **5.00%**；06862（Rf 1.688%）保持 5.00%。
  `GG − II` 整体更保守。若要放宽，只改两个常量。
- 同时移除 §17.11 表头的"文档档对照"行（已有单一决策口径，无需再并列），
  出处与理由记入 `shared_tables.md`。
- 同步修正 `prompts/references/shared_tables.md`（v1 存档）的 superseded 标记内容。

### docs: P2-21 复核 —— 更正一处错误结论并拆分为 5 个子项

- **更正**：首轮方案中「悲观折扣会导致仓位矩阵退化为永不建仓、必须重新标定阈值」的结论是
  **未经计算得出的，实测不成立**。用真实数据（II=5%，AA×0.85，M_pess 取三法最低）：
  06862 KK 由 +2.20 → **+1.12**（仍落在"50% 仓位"档）；600941 由 −0.94 → −1.55（判定不变）。
  降幅恒为 `≈15% × GG ≈ 0.6–1.1 pct`，不会把宽裕标的翻成不可投。
- **正确框架**：k 折扣是**输入端**选择，仓位矩阵是**输出端**约定，两者独立；叠用只是
  "重复保守"的**政策取舍**，不构成实施阻塞。矩阵阈值本身（KK ≥ 1.5/0.5/0）系上游继承、
  无标定依据说明 —— 要判断重复保守是否过度，应先补依据而非先改阈值。
- **拆分**（`known_issues.md` §3.21，每项已逐项核实）：
  `21a` 悲观折扣（输入端）· `21b` 承诺优先 M（**数据采集缺口**：分红政策类 PDF 关键词命中数为 0）·
  `21c` 防御门槛组（新判据，须做档位而非硬否决）· `21d` 商誉/无形剔除（**文档自相矛盾**：
  全扣无形资产会推翻"路权"论点）· `21e` X1/租赁/少数股东股息（**纯映射补齐**，
  四个字段 `hk_cashflow` 实测已存在）。

### 测试

- **1140 → 1146 passed, 3 skipped**（门槛统一导致 8 处断言更新；测试数因两个用例合并为
  "全市场统一"不变式用例而净减 1）。

### feat(§17): 小节状态契约 + 币种拆分（P2-20 改动 4 与层1）

**根因不是一个，是三个。** 调研（含对首轮建议的更正）见 `known_issues.md` §3.20。

**改动 4 —— §17 状态契约**（新增 `scripts/section_contract.py`）
§17 的 16 个小节都是可选的，而缺席过去只能靠"标题不出现"推断 —— 于是
"没算"与"算出来是 0"无法区分，提示词被迫写了 **4 条手算降级路径**
（§17.10/11/12/13），直接违反核心不变量「LLM 不做算术」。现：

- 落盘 `{output_dir}/section17_status.json`：每节 `status`（`ok`/`degraded`/`missing`）
  + 受控词表的 `reason` + 可执行的 `remedy`
- **fail-closed**：未登记状态的小节一律记为 `missing`，不是"没登记就算通过"
- **在产生点约束值域**：越界编号 / 词表外原因 / `ok` 带原因 → 硬错误
- 新不变式（`check_pack_consistency()` 机器校验）：
  **`### 17.N` 标题存在 ⟺ 该节 `status == ok`**。取代旧契约
  "缺失通知里不得出现小节号"——旧契约靠字符串不出现来推断缺席，新契约显式枚举
- **提示词删掉 35 行手算公式**，改为「状态非 `ok` → 停止、报告、**禁止手算**」

> **这是一次设计取舍的反转**：旧契约宁可让 LLM 手算也要给出一个数；
> 新契约宁可没有数 + 显式说明缺失原因。理由：手算会绕过 M 裁定 / AA 口径 /
> 悲观档，口径漂移已有实际事故（P1-11）。
> 对应测试 `test_degradation_paths_preserved` 已反转为
> `test_hand_calculation_paths_are_gone`。

**层1 —— 币种拆分（P0-2 的真正根因）**
`_detect_currency` 的 docstring 字面写着 *"Detect reporting currency"*，
实际按 ticker 后缀返回**交易币种**，而同一个值被用于给三张财务报表贴标签：

- 更名 **`_detect_trading_currency`**（保留旧名作别名）
- 新增 **`_reporting_currency()`**：港股**只在显式确认时断言**（`fx.json` /
  `--fx-hkd-cny`），未确认返回 `None` —— **不猜**；A 股=CNY / 美股=USD 按市场定
- `_unit_label()`（金额）改用报表币种；港股未确认时输出 `百万（币种未确认）`
- `_price_unit()`（价格）仍用交易币种
- `units` 契约块同时声明两者，使不一致**可见**

**外部调研结论（修正了我首轮的建议）**：JSON 相比 markdown 表格在数值检索上
**买不到准确率**（52.3% vs 51.9%，CI 重叠），真正高 8.4 pct 的是 KV 呈现方式。
JSON 的价值在"作为机器校验的底稿"（PCN 协议）与"强制类型/单位"（XBRL/FRED 惯例），
不在让 LLM 读得更准。故后续改动 1–3 按此重排。

**测试 1178 → 1207 passed, 3 skipped**（新增 `tests/test_section_contract.py` 23 例
+ 币种拆分 5 例 + 状态契约断言改写 5 例）。

### feat(§17): 声明账本 —— 数值可校验（P2-20 改动 1/2/3）

**问题**：§17 的数值以 markdown 表格交给 LLM，LLM 在报告里**重新打一遍**。
重打没有任何机械校验 —— 数字可能被误抄、按错单位换算、或被编造，而报告读起来完全正常。

**外部依据**：[PCN](https://ar5iv.labs.arxiv.org/html/2509.06902) 把这类失效定性为
**呈现层问题**：检索与引用都无法保证保真，必须由渲染器机械校验并 **fail-closed**。
论文并指出 "*fabricated numbers can still appear structurally 'valid' without being
faithful*" —— 即"只把格式换成 JSON"并不解决问题。

**新增 `scripts/claim_ledger.py`**，声明形状 `⟨claim_id, indicator, value, unit,
time, basis, policy⟩`：

- **值级类型化（改动 3）**：`unit` 必须是枚举；`value is None` **强制**带 `note`
  （null ≠ 0）；`policy` 受控
- **决策单元格 KV 化（改动 1）**：§17.11 表B 的 ★ 行、§17.14 全部 7 档 k、
  §17.10 的 `M_rec`/`M_pess` 以 `key: value` + `claim_id` 渲染（宽表格保留供人读）。
  依据：Markdown-KV 数值检索准确率 60.7% > markdown 表格 51.9% ≈ JSON 52.3%
- **PCN 式校验（改动 2）**：落盘 `claims.json`；
  `report_consistency.py --gates --claims` 校验每个
  `<claim id="…">值</claim>`；策略 `exact` / `round:<d>` /
  `alias[:<tol>]`（**降精度必须显式给容差**）/ `tolerance:<abs>,<rel>`（须带限定词）
- **fail-closed**：未知 id / 解析失败 / 单位不符 / 值对不上 → unverified；
  裸数字按契约**默认不可信**；非空失败 → 退出码 1

**实测**（mock 全流程）：登记 24 条声明；2 个正确 token 通过、1 个篡改值
（`7.20%` vs 声明 `2.89%`）与 1 个未知 id 被拦下，退出码 1。

**提示词**：`phase3_quantitative.md` / `phase3_valuation.md` 新增
「§17 数值引用契约（claim 绑定 · 强制）」；`factor_interface.md` 新增 `claim_id`。

**测试 1207 → 1237+ passed, 3 skipped**（`tests/test_claim_ledger.py` 30 例 +
`TestClaimRegistration` 7 例 + `TestClaimGate` 7 例）。

### fix: 真实 API 运行暴露的 3 个问题（yfinance 回退全失效 / 市值与收盘价绑死）

此前全部验证都跑在**缓存数据**上。首次 `--no-cache` 走真实 Tushare API 后立刻暴露：

**① yfinance 回退在生产环境 100% 失效（严重）**
`_yf()` 读 `sys.modules["tushare_collector"].yf`；以 `python scripts/tushare_collector.py`
运行时模块名是 **`__main__`**，该键不存在 → `KeyError`。实测日志：
`[yfinance] 06862.HK income: fallback failed after 2 retries: 'tushare_collector'`。
**测试从未发现**：测试里 `import tushare_collector` 恰好创建了生产环境缺失的那个键。
修法：三级回退 `tushare_collector.yf` → `__main__.yf` → 直接 `import yfinance`。

**② 市值与收盘价绑死，一个缺失拖死整组网格（重要）**
`_get_mktcap_shares` 旧实现在检查市值**之前**就 `if not close: return None`。
`hk_daily` 配额（5次/天）用尽 → 收盘价缺失 → **§17.11/§17.13/§17.14 全部消失**，
而市值来自 `total_market_cap`、本来就拿到了，`GG`/`R`/`GG−II` 也只需市值 ——
只有目标买入价需要收盘价。
修法：`close` 缺失时返回 `(mkt_cap, None, None)`，目标买入价降级为 `—`。

**③ 缺失原因不精确**：同场景下 §17.6/17.8 被归因为 `hk_field_gap`，
真实原因是收盘价不可用，补救动作指错方向。新增受控原因码 **`no_price`**。

**修复效果（同一标的、连续两轮真实采集）**

| | 修复前 | 修复后 |
|---|---|---|
| §17 小节 ok | 10 / 16 | **13 / 16** |
| pack | 395 行 / 20,671 B | **647 行 / 29,737 B** |
| §17.11 / 17.13 / 17.14 | 全部缺失 | 全部生成 |
| 声明条数 | 2 | **28** |

**④ 顺带修掉我自己的一处假阳性**：`GG−II` 声明单位为 `pct点`，但 `display` 渲染成 `0.78%`，
照抄该值的报告被判"单位不符"。新增 `units_compatible()`：`%`/`pct`/`pct点` 量纲相同视为兼容，
而 `百万 ↔ 亿` 是尺度变化，仍须显式 `alias` 放行。假阳性比漏报更危险 —— 它会让 Agent 不信任闸门。

**⑤ `reporting_currency` 在 seed 时被丢弃**：`fx.json` 写了 `reporting_currency: "CNY"`，
但 `make_fx_store_entry()` 只返回 `factor_to_reporting`/`date`/`source` —— 确认的币种
在写入 `_store` 时丢掉，`_unit_label()` 又退回"未确认"。已改为全链路传递。
教训：**修链路时要检查每一跳是否真的把新字段带过去**。

**⑥ 三张报表仍硬编码「百万港元」**：`financials.py` 6 处写死表头/脚注，
与 §17 的 `_unit_label()` 无关；pack 头部写的是 `报表币种: HKD`（实为**交易**币种），
且 `assemble_data_pack` 与 `_build_header` 各有一份独立头部构造。
EPS/BPS 亦同（`(HKD)` / `港元/股`，而 EPS 来自 CNY 报表）。
已全部改用 `_unit_label()`，新增 `_per_share_unit()` 区分"价格单位"与"每股财务指标单位"，
头部同时声明 `交易币种` 与 `报表币种`（未确认时明说）。

最终真实 pack 头部：
```
*金额单位: 百万元 (除特殊标注)*
*交易币种: HKD（价格与市值口径）*
*报表币种: CNY（三张财务报表口径）*
```

**方法论结论**：**缓存既是测试的组成部分，也是盲区。** 上述四点在缓存数据上都不可能复现
（① 只在脚本身份下触发；②③ 只在行情接口失败时触发；④ 只在真的照抄 KV 值去校验时触发）。
契约类改动**必须在真实 API 路径上验证一次**。

**测试 1252 → 1269 passed, 3 skipped**；PCN 闸门在真实 pack 上退出码 1（故意篡改被拦下）。

### 端到端验证（缓存真实数据离线复跑 06862.HK）

| 指标 | 修复前 | 修复后 |
|---|---:|---:|
| `§17.2 D 折旧与摊销` | `—` | 2,198.38 |
| `§17.2 E 资本开支` | 75.82 | 1,748.38 |
| `§17.2 F（Capex/D&A 中位数）` | 数据不足 | 1.50 |
| `§17.4 W2 员工` | 1,475.5 | 14,072.97 |
| `§17.5 AA_2y` | 22,426 | 8,317.91 |
| `§17.11 市值` | 1e6 偏大 → GG≈0 | 53,343.18 百万港元 |
| `§17.11 目标买入价` | ≈0 / 负 | 10.52 – 37.49 港元 |
| `§17.12 G 网格` | 永久不可用 | 正常生成 12 行 |

> ⚠️ 仍未修的两项 P0：**P0-1**（AA 仍走"营业收入倒推"，未满足设计前提，应改为
> `AA = OCF − Capex`）与 **P0-2 的汇率换算层**（港股 R/GG 仍被低估约 15.65%）。

---

## v3 (2026-10-13) — 定性分析重构 + 平台去耦

### 跨小节不变式与定义有效性（第二轮，由 06862 全量复查驱动）

**动因**：第一轮修的是「单小节内部」的错误；06862 复查发现**每处单看都对、放在一起才错**的问题，
现有三层校验（Python 算式、报告内数字一致性、§17 状态契约）都看不见。

**新增两个 fail-closed 闸门**

- `scripts/invariants.py` —— 数据包级不变式引擎（判不了记 `UNEVALUATED`，绝不默认通过；`--strict` 可计为失败）：
  I1 CAGR 期间标注 · I2 每股价币种 · I3 §5↔§4 现金勾稽 · I5 分红数据完整性 · I6 敏感性方向。
  **由 `tushare_collector.py` 在生成数据包后自动运行并写出 `invariants.json`。**
- `scripts/check_report_fidelity.py` —— 报告级：I8 内嵌叙述保真（逐字 + 数字多重集）· I9 规则序
  （`综合基准价 ≤ 采纳档目标买入价`）。

**七项缺陷与修复**

| # | 缺陷 | 级别 | 修复 |
|---|---|---|---|
| N1 | §17.1 CAGR 静默换基年：净利润「5年CAGR 43.36%」实为 2022–2025 **3 期**（2021 亏损年被过滤），营收行 4 期 | P0 | `_cagr` 重写：正基数渲染**基年+期数**；负基数输出 `N/A（基年负）`+原因+标注期数的替代口径 |
| N2 | §17.8 基准价混币种：①1.11 / ②1.80 是**报表币种**（元/股，源自 §12「每股净资产 (元)」），却与港元锚点 ③④ 求均值 | P0 | ①②⑤ 按 `1/_fx_to_reporting` 折算 + 表下币种说明；综合值 9.34 → **9.46** |
| N3 | §5 三节合计与 §4 现金及等价物变动**4/4 年不勾稽**（差额 408–2,521 百万，占实际变动 50–165%） | P0 | 新增 I3 机械把关（容差 max(5百万,1%)）；**根因待查**，修复前不得据此下净额结论 |
| N4 | §6 分红表同年度重复行 + `dps_hkd` 恒定，却用于推导派息率（法1 101.47% 不可复现） | P1 | 新增 I5；决策口径仍用法2（M_rec 81.11%），法1/法3 标注为不可复现对照 |
| N5 | §17.13 λ 方向性错误 fail-open：λ<0 时收入越降结余越高（10,415/16,721/23,026），旧实现附警告后照常出厂 | P0 | 改为 **fail-closed**：λ ≤ 0 ⇒ 标记 `17.13 = missing / input_inconsistent`（新增受控 reason 码）且整节不输出；出表后另有单调性自检。可靠性警告（λ 序列符号不稳）**不**触发封锁，只压低可信度评级 |
| N6 | 报告称六维叙述「逐字原样保留、未作任何改动」，实测**六段全部被改写**（D1/D2/D5 新增数字、D3 删 9.35 并改口径名、D4 插入整段、D6 删 −8.80 且自承改了措辞） | P0 | 新增 I8：按**维度小节**切片逐字 + 数字多重集机械比对；取消无法自证的声明式保证 |
| N7 | 规则序自相矛盾：步骤4 规定买点=悲观档目标价，步骤5 又把「溢价 ≤0%」标为「买入就是胜利」；06862 综合 9.34 > 8.92 | P0 | `phase3_valuation.md` 写明**买点只由步骤4 决定** + 集合包含不变式；I9 机械把关；并要求基准价构成同币种、不得把「股息资本化公允价」与「清算/账面底价」等权平均 |

**测试**：新增 `tests/test_invariants.py`（24）· `tests/test_report_fidelity.py`（16）· `tests/test_definition_validity.py`（13）。
两条**真实产物回归**：06862 数据包必须恰好报出 I1/I2/I3/I5/I6；真实报告必须报出 I8（逐维点名 D1–D6）与 I9。
测试规模 **1269 → 1322 passed, 3 skipped**。

**已知未闭环（登记在 `known_issues.md` §十一）**：I3/I5 的**根因未修**（只检测）；N7 的**基准价构成缺陷未修**
（折算后 9.46 仍 > 8.92，I9 仍 FAIL，需改构成或改规则语义）；§17.1/§17.8 尚未纳入 `claims.json` 注册；
`--strict` 未接入流程，采集器不因 FAIL 中断。


### 架构：PDF-first 单 Agent → 切片 + 六维度并行 + 契约化交接

**动因（实测）**：年报正文 201K–573K 字符，叠加固定输入后达 **245K–617K 字符**，5/5 真实样本超出
128K 上下文（即便按 0.6 token/字 的乐观系数）。原「单 Agent 全文载入」在目标平台上**不可运行**。

- 新增 `scripts/dimension_slicer.py`：**两阶段选页**（目录/章节定位 → 段内按预算二次切分），
  排除封面与目录页，预算为**硬上限**（单页超限则截断并标注），产出 `slices/slice_D1..D6.md`
  与 `slice_manifest.json`（含覆盖声明「仅提供 N/M 页，未覆盖不得推断」）
- Step 2 拆为 **2.0 切片 → 2.1 六个维度子代理并行（共享前缀逐字节相同）→ 2.2 综合代理只读 6 份 JSON**
- 单次调用峰值从 245K–617K 字符降到 **≈24K 字符**（A股/港股/286 页三份真实年报实测：6/6 维度落在预算内，填充率 83–99.5%）
- **港股适配**：实测港股目录完全不带点线（`MANAGEMENT DISCUSSION AND ANALYSIS 管理層論析 19`），
  且无 `第X节` 体系 → 改用**目录驱动**定位（A股/港股通用），标题词表仅作兜底

### 契约：L1 维度结果契约（`dim_result.schema.json`，JSON Schema 2020-12）

- 40 个参数（照 `output_schema.md` v1.2 全覆盖）、来源词表强制（`computed_metrics`/`data_pack`/
  `annual_report`/`web_search`/`inference` 各自必填 `ref`/`page`/`basis`）、三态 `material_status`
  （充分/不足/不适用）、正文字数下限结构化（D1≥600 / D2≥800 / D3-D5≥400 / D6≥300）
- 新增 `scripts/validate_dim_result.py`：**硬失败 + 最多 2 次重试**（决策 5）；
  契约失败不得静默继续，否则退化为 v1 的"散文交接"
- 设计说明与 6 项决策记录：`shared/qualitative/references/dim_result_contract.md`

### 平台去耦（DeepSeek Harness）

- 工具名映射：`WebSearch`→`web_search`、`AskUserQuestion`→`ask_user_question`、
  `Agent(`/`Task(`→`subagent(`、`Bash(`→`bash(`、`Skill 工具`→`skill 工具`
- **5 个 skill 落位 `.agents/skills/`**（DSH 从此路径发现，实测热加载）：
  `tushare` / `business-analysis` / `valuation` / `turtle-analysis` / `download-annual-report`；
  `.claude/skills/<name>` 为软链（单一来源）
- 新增 **`AGENTS.md`**（平台中立入口，无 slash command）、`scripts/rf_utils.py`
- **沙箱兼容**：去掉 `ts.set_token()`（它写 `~/tk.csv`，在 DSH 默认 workspace-write 下 `PermissionError`），
  改为 `ts.pro_api(token=...)`，重试路径同样带 token

### §17 网格与 §14 解耦（缺陷修复）

- 无风险利率缺失时，依赖它的三个小节（业绩下滑敏感性 / 穿透回报率网格 / 收入敏感性）**不再静默消失**：
  输出显式「未生成清单 + 原因 + 补救指引」
- `--refresh-market` 不再覆盖人工补充的 §14（原会被"数据缺失"文本替换）
- 新增 `--rf` / `TUSHARE_RF`，并从既有数据包 §14 **回读**（实测：不传 `--rf` 也能生成 13/13 个 §17 小节）
- 占位符改为工具名无关形式（`待Agent补充`），避免再次因平台改名而返工

### 选股器缺失值处理（缺陷修复）

- 修复 `rank(na_option=...)` 让 **NaN 拿到最高分位**的缺陷；缺失维度按可用维度**权重重归一化**；
  质量门关键字段缺失**不再视为通过**，改标记 `data_incomplete` 并单独列出（不给分）
- 历史结果重算逐值一致（数据完整时行为零变化）

### 端到端验证（2026-10-13，真实标的 002984 森麒麟）

首次在真实标的上跑通完整 v3 链路（切片 → 6 维度并行 → 契约校验 → 综合 → 审计门 → 修复环）：

- 6/6 维度 JSON 通过契约校验；`material_status` 4×充分 / 2×不足（D5、D6 因切片截断如实自报）
- 综合报告：12 个必备章节齐备、40 个参数 0 空格、153 行溯源汇总；`report_consistency --gates` **硬门槛 0 失败**
- clean-room 独立重算 9 项、**8/8 与数据包偏差 <1%**；两条独立路径互相印证
- 审计：critical 0 / major 1 / minor 11；40 参数与维度 JSON **逐字符一致**（综合层无静默改数）；未登记算术 0 处

**验证暴露并已修复的 5 个缺陷**（前 4 个是链路自身的，第 5 个是分析错误）：

1. **切片器·叙事维度只取段首**：D5 仅覆盖 p10–17，把 p18–32 的展望/风险章节整段丢掉，维度代理只能报「不足」→ 改为**头+尾分预算**（修复后覆盖 p10–14 + p28/30/31/32）
2. **切片器·关键词漏项**：D3 漏年报「面临的风险」章节、D6 漏「在其他主体中的权益/分部报告」→ 补关键词
3. **切片器·空切片**：修复 1 引入的回归——极小预算下头尾都放不下任何整页 → 加兜底（截断首页，绝不产出空切片）
4. **审计环节·误审陈旧副本**：3B 审计读到了**放在被审目录内的基线备份**，整份审计（1 major + 13 minor）引用的原文全部只存在于基线里，`FIX_REQUIRED` 完全无效 → 新增：禁读 `baseline/_v2/backup` 类文件、审计须在 audit.md 首部自证被审文件的**路径+字节+md5**、基线统一移出到 `output/_baselines/{code}_{company}/`。重跑后审计才真正针对交付物
5. **证据归属错标（已修）**：D4 把「审计意见 / 审计费用」标为 `DP§7`，但数据包把 `### 审计意见` 放在 `## 16. 股权质押` 之下（§7 是股东与治理），
   正确引用是 **DP§16**。已在**源头 `dims/D4.json`（4 处）+ 报告正文与溯源表（3 处）**一并修正，契约校验仍通过。
6. **分析错误（审计发现并修复）**：2024 年两笔回购合计被写成 2.07 亿元（DP§15：150.00 + 76.89 = 226.89 百万元 = **2.27 亿元**）。根因在 `dims/D5.json` 的 `distribution_signal`，被综合层原样复制进正文与参数表；已在**源头 JSON + 3 处引用**一并修正并重跑契约与门槛

**审计 8 项 minor 也全部修掉（源头 JSON + 报告两侧同步）**

审计在最终版本上列出 8 项 minor（标签/口径/引用类，非数字错误）。因为审计同时验证了「40 参数表 ↔ 维度 JSON 逐键一致」与「溯源表 ↔ key_metrics 双向一致」，
所以每项都**先改拥有该文本的 `dims/*.json`，再改报告**，以保住这两条不变式：

| # | 问题 | 修正 |
|---|---|---|
| 1 | 裁定理由称宽口径"无科目级明细支撑"，与 DP§13.2⑤ 矛盾 | 承认该注列出科目构成（22.42+1.47+23.21=47.10 亿），但仍按 §17.7 口径裁定 |
| 2 | 净现金唯一未括注口径处 | 补「（货币资金口径）」 |
| 3 | 「三项合计占 99.01%」实为 96.12%（99.01% 属前五名） | 改为"前三名合计 96.12%（前五名合计 99.01%）" |
| 4 | 把加权 ROE 写成 8.24%（加权 8.41%） | 改为"加权 8.41%、平均权益口径 8.24%"，与参数表一致 |
| 5 | 「海外份额 22%」与溯源表「全球 22%」标签冲突 | 统一为"全球份额约 22%、其中半钢约 15%"（叙述+evidence+uncertainty 三处） |
| 6 | 「分红加回购超过 5.8 亿元」跨期混用 | 改为"5.90 亿元（2025 分红 3.63 + 2024 回购 2.27，跨年度）" |
| 7 | 研发投入 248.98 百万元未标与 DP§3 研发费用 209.59 百万元的口径差 | 补口径注（JSON note + 溯源两处） |
| 8 | 有息负债率 14.80% 未标账面/面值口径 | 补"（账面口径；年报带息债务面值口径 15.12%）"（JSON 叙述 + 报告正文与表格） |

改后：契约 6/6 通过、`report_consistency --gates` 硬门槛 0 失败、报告 ↔ JSON 逐字一致（6 项定向核验全通过），并**重跑 3B 审计认证新字节流**。

**最终认证（3B 复审）**：重跑审计 → **`AUDIT_RESULT: PASS`**，critical 0 / major 0。八项修正逐条复核「真修复」，40/40 参数与维度 JSON 一致、153 行溯源双向闭合、
clean-room 9 项 0 项偏差 >1%、未登记算术 0 项、净现金采用口径全文一致（宽口径 5 处均标注为被拒备选）。6 行 advisory 裁定为 **0 真错 / 4 口径差异 / 2 工具误报**。
审计另提 2 项**可选**标注建议（L149「前三名 vs 前五名」主体、L261 引文括注），已按其给出的措辞一并应用——审计已明确声明这两处不影响其认证结论。

> 认证严格对应当时的字节流（报告 md5 `815266ce…` / 95,736 B ＋ 六个 dims 各自 md5）；此后仅按审计自身建议改动了上述两处标注文本（现为 `64048426…` / 95,776 B），未触及任何数值。


**验证后修复的工具层缺陷（2 项，均已实测复验）**

1. **`data_pack_market.md §17.8` 口径不自洽（已修）**：旧代码在 `fina_indicator` 可用时用 Tushare 同名字段**覆盖**自算值，而两者定义不同——
   `netdebt` ≠「有息负债−货币资金」（造成 §17.8 与 §17.7 出现第三种现金口径：EV 反解现金 2,101.73 vs 货币资金 2,242.01）；
   `fcff` 是**企业自由现金流**而非 FCF（把 −231.85 百万元的 FCF 报成 **+2.88%** 的收益率，**符号相反**）；
   `ebitda` 也与标签所写的「营业利润+财务费用+D&A」不符（1,926.79 vs 1,879.95）。
   现改为：自算口径为唯一权威（与 §17.7 一致），Tushare 值仅作为**带「· 对照」前缀的对照行**出现，并在表下加口径说明。
   真实 API 复验（002984 重新采集）：EV = 市值 13,471.73 + 净负债 477.25、FCF收益率 **−1.72%**、净负债/EBITDA **0.25x**，对照行正确标注。新增 `TestFactor4EVBaselineCaliber` 覆盖——**原测试夹具从未写入 `fina_indicators`，所以这条覆盖路径一直没被测到**。
2. **`report_consistency.py` 冲突识别精度过低（已修）**：002984 上 12 处 advisory 中审计判定 **8 处纯属解析伪影**（精度 33%）。逐类修复：
   区间写法 `8%-15%` 被读成 `-15`；阈值 `<18%`／`30% 以上` 被当成观测值；`同比下降 11.65 个百分点` 的**变化量**与水平值混比；
   数值**后置**的指标名偷走左侧同比序列；溯源表**备注列的年份**被继承给本行主值（`| 毛利率 | 21.28% | 2024 年 32.84% |`）；
   逐年序列被折叠进同一年；`归母净利润` 被读成 `净利润`（关键词后缀更"近"）。
   改为：区间/阈值/变化量过滤 + 前置关键词优先（距离按关键词末尾计、同距取更具体者）+ **年份中心**归属（右侧优先，表格行不做继承，变化动词后不继承）。
   实测 **12 → 6**，且**四类真实口径差异全部保留**（母/合并营收、母/合并净利润、加权/平均 ROE、分部/合并毛利率）。
   残余 6 处仍混有「归母权益 vs 归母净利润」「5 年均值 vs 单年」「派生差额」等同类不同科目的值——属启发式期间归属的固有限制，
   **不宜针对单一报告继续调参（会过拟合）**；工具定位仍是 advisory，最终由 3B 审计裁定。新增 14 项精度回归测试。

### 其他

- 交付硬门槛新增 D3/D4/D5 正文下限 400 字
- 依赖补齐：`markdown`（`report_to_html.py` 硬依赖）、`jsonschema`（契约校验）
- `tests/test_coordinator.py` 从 v1 归档改指**活跃 coordinator**（原断言钉在 `prompts/` 归档上，守卫失效）
- 新增守卫测试：`test_dsh_tool_names.py`、`test_dim_result_contract.py`、`test_dimension_slicer.py`、
  `test_validate_dim_result.py`、`test_v3_pipeline_prompts.py`、`test_rf_manual_input.py`
- 测试规模：**1094 passed, 3 skipped**

---

## v2.1-beta (2026-07-04) — 全面优化轮

一轮跨四轨的速度/精度/流程优化（计划与审计由 Fable 主持，实现由 Opus 子代理完成）。

### 速度：数据管线（feat(perf) b1b83d3）

- 财报类接口（income/balancesheet/cashflow/dividend/fina_indicator 等 20 个）新增 TTL 磁盘缓存：财务 7 天、weekly/yc_cb 24 小时；daily/daily_basic/\*_basic 永不缓存。缓存实现抽取为共享 `scripts/cache_utils.py`（ScreenerCache），筛选器 re-export 兼容
- §13 警告区块改读 `_store`，消除 A 股路径 5 次重复 API 调用
- 限速改为 `TUSHARE_RATE_DELAY` env 可配置（默认 0.5s 不变）；缓存命中不触发限速 sleep
- 新 CLI：`--no-cache` / `--cache-refresh`（财报季 4/8/10 月建议刷新）；valuation_engine 镜像 `--no-cache`
- **实测 600887.SH：冷跑 25.9s → 暖跑 5.7s**，输出逐字节一致（仅时间戳）

### 精度：VIP 中继行序防御（fix(tushare) 258fd8e）

- 发现券商 VIP 中继返回价格行为**最旧在前**（标准 Tushare 为最新在前），导致 §1 显示 1996 年上市首日股价/市值、§2 "最新收盘价"为一年前数据，污染所有市值依赖指标
- `get_basic_info` / `get_market_data`（A股/HK/US 四处）防御性 newest-first 排序 + 回归测试

### 精度：龟龟 §17.10-13 预计算网格（feat(turtle) b3641be）

- §17.10 支付率 M 三重交叉校验（法1 DPS/EPS、法2 现金流、法3 §17.1）+ Python 内裁定 M_rec
- §17.11 税后 R/GG 网格（M候选 × 税率情景 × AA变体），含 HH、GG−II、**目标买入价**列
- §17.12 G 系数网格（0.7-1.8 × 12 档，H/OE 逐行预算）；§17.13 λ 收入敏感性 + 临界收入倍数
- Agent B（phase3_quantitative.md）改为**查表选行 + 说明理由**，新约束 #9"有网格时不做数学"；手算公式保留为降级路径
- factor_interface 新增 target_buy_price / M_source / GG_source

### 精度：定性模块 11 杠杆 P0+P1（feat(qualitative) 1a3c275）

- 杠杆 1：`scripts/quality_control.py` → `computed_metrics.md`（CM§1-5：亿元对照/同比/多年统计/分红率/PE 估值链），LLM 只引用不重算
- 杠杆 2：`[src: ...]` 溯源标注语法 + 材料性规则 + 必备 `## 数字溯源汇总` 节
- 杠杆 5：clean-room 独立重算 agent（与分析并行、禁读草稿、防锚定 + 防注入）
- 杠杆 6：`scripts/report_consistency.py` 跨段数值冲突扫描 + `--gates` 硬门槛校验
- 杠杆 3：numeric_audit 审计门（severity 评级、原文→修正对、AUDIT_RESULT 门控、修复环 max 1 次）
- 杠杆 7：output_schema v1.2 交付硬门槛 + DELIVER ONLY 禁项
- 杠杆 8：`writing_style_rules.md` 权威化（lead-with-numbers、模糊量化词禁用表、**正文单位切换为亿元**、参数表保持百万元）
- 杠杆 9：`industry_metrics_lookup.md` 36 行业关键指标锚点
- 杠杆 4/10/11（P2）遗留未实施

### 流程：v1 存档 + 引用修复（refactor(v2) e69f26d）

- v1 定性模块（agent-team 并行架构 + narrative 变体 + split_data_pack.py）→ `shared/qualitative/legacy/`
- v2 转正改名：coordinator_v2.md → coordinator.md、qualitative_assessment_v2.md → qualitative_assessment.md
- turtle 孤儿归档：phase3_preflight.md、phase1_数据采集.md → `strategies/turtle/legacy/`
- 修复 stale 引用：turtle-analysis 命令 Step 3.0、SKILL.md 入口与流程、PDF 文件名约定统一

### 测试

- 时间炸弹修复：`other_data.py` 抽出可冻结的 `_now()`，TestRepurchase 冻结时钟（fixture 永不过期）
- 测试规模：597 → **920 全绿**（新增缓存/网格/质量脚本/行序防御/prompt 断言共 ~130 项）

---

## v2_beta (2026-04-05)

### PDF-first 架构升级

business-analysis 从 WebSearch-first 升级到 PDF-first 数据流：

- 年报 PDF 作为主数据源，直接载入 1M context，Tushare 仅作历史序列补充
- 单 Agent 模式取代 v2_alpha 的多 Agent 并行拆分（Agent A/B/C + Summary）。实测单 Agent 在交叉验证上更优，所有维度共享同一 context 消除信息孤岛
- 新增 PDF 本地缓存检查：下载前先 glob `output/{code}_{company}/` 中已有 PDF，避免重复下载
- 新增 `coordinator_v2.md` + `qualitative_assessment_v2.md`

### Turtle 策略 Pre-flight 合并

将独立的 `phase3_preflight.md`（数据校验+口径锚定）合并进 `phase3_quantitative.md` 的 Step 0：

- Pipeline 从 3 个串行 Agent（Pre-flight → Agent B → Agent C）缩减为 2 个（Agent B 含 Step 0 → Agent C）
- 每次分析预计节省 2-3 分钟（消除 1 次 Agent 启动 + 9 次文件读取 + 1 次文件写入）
- `phase3_valuation.md` 不再依赖独立的 `phase3_preflight.md`，从 Agent B 输出读取基础信息
- `phase3_preflight.md` 保留为文档参考，不再被 pipeline 调用

### 新增独立估值模块

- `/valuation` slash command：独立于龟龟策略的通用估值分析
- `scripts/valuation_engine.py`：DCF / DDM / 可比估值 / Graham 四种方法
- `strategies/valuation/`：coordinator + phase2_valuation + 参考文件（方法论/分类规则/模板/示例）

### Tushare 模块增强

- `assembly.py`：新增 §17 衍生指标预计算（Factor 2/3/4 加速）
- `--refresh-market` 模式：仅刷新 §1/§2/§11/§14（增量更新，<5秒）
- `financials.py` / `infrastructure.py`：minor fixes
- `report_to_html.py`：支持 `--standalone` 内嵌 CSS 模式
- `tests/test_refresh_market.py`：新增 refresh-market 测试覆盖

### 测试

- 新增 `test_refresh_market.py`：refresh-market 模式测试覆盖
- 总测试数：792（v2_alpha 时为 769）

### 实战验证

完成两只标的的端到端全流程（business-analysis + turtle-analysis）：

| 标的 | 结论 | 关键发现 |
|------|------|---------|
| 美的集团 (000333) | Observe 30% | Gross R 5.69% > 门槛，GG 3.53% < 门槛（SGA proxy 驱动的保守偏差），KUKA 商誉 34.3B 为潜在风险 |
| 海尔智家 (600690) | Observe 50% | 股价低于地板价 7.4%，零价值陷阱信号，但毛利率连降 5ppt + AR 增速远超营收需警惕 |

---

## v2_alpha (2026-03-31)

### 架构重构：模块化拆分

**从 `prompts_v2/` 到 `shared/` + `strategies/`**

v1.x 所有内容（数据采集、preflight、定性、定量、估值）耦合在 `prompts_v2/` 中。v2 将定性分析独立为通用模块：

- `shared/qualitative/` — 通用定性分析模块（6维度商业质量评估）
  - 可独立运行（`/business-analysis` 命令）
  - 可被龟龟策略调用（替代原 phase3_qualitative.md）
  - 可被烟蒂策略、未来其他投资框架调用
- `strategies/turtle/` — 龟龟策略专属模块
  - 数据采集（phase1/phase2）
  - Preflight 数据校验
  - 穿透回报率计算（phase3_quantitative.md）
  - 估值与报告组装（phase3_valuation.md）
- `prompts_v2/` 已删除，文件迁移至上述两个目录
- `prompts/` 保留为 v1 只读遗留

**新增文件**：
| 文件 | 用途 |
|------|------|
| `shared/qualitative/coordinator.md` | 独立定性分析入口 |
| `shared/qualitative/qualitative_assessment.md` | 6维度分析 prompt |
| `shared/qualitative/data_collection.md` | 轻量级 WebSearch 指令 |
| `shared/qualitative/references/output_schema.md` | 结构化参数输出 schema (v1.1) |
| `shared/qualitative/references/framework_guide.md` | 框架说明固定附录 |
| `.claude/commands/business-analysis.md` | `/business-analysis` 命令 |

**变更文件**：
| 文件 | 变更 |
|------|------|
| `strategies/turtle/coordinator.md` | 路径引用从 `{prompts_v2_dir}` 改为 `{shared_dir}` + `{strategy_dir}` |
| `strategies/turtle/references/factor_interface.md` | 新增 shared output_schema 引用和 moat_rating 映射说明 |
| `.claude/commands/turtle-analysis.md` | 入口从 prompts/coordinator.md 改为 strategies/turtle/ |
| `CLAUDE.md` | 反映新目录结构 |

---

### 护城河分析框架升级

**维度二（D2）重大扩展**，灵感来源：太阳纸业护城河深度分析 + Greenwald 竞争优势理论

v1 的 D2 仅有定性的非技术/技术双层护城河评估。v2 扩展为6步结构化分析：

| 步骤 | 新增内容 | 来源灵感 |
|------|---------|---------|
| 2.1 行业地图 | 先定义竞争战场：细分市场、进入壁垒表（5类）、CR4 | Greenwald |
| 2.2 量化验证 | ROE vs 门槛（8/15/25%）、份额稳定性、低谷韧性 | Greenwald |
| 2.3 双框架分析 | 框架A（非技术+技术双层）+ 框架B（供给侧/需求侧/规模经济） | 新增 Greenwald 三维 |
| 2.4 虚假优势辨析 | 品牌≠护城河、运营效率≠结构壁垒等 | 太阳纸业案例 |
| 2.5 竞争对手对比 | vs 前2-3名对手逐维度对比表 + 差距可持续性 | 太阳纸业案例 |
| 2.6 可持续性与监控 | 护城河监控锚点（3个KPI，含当前值和警戒线） | 太阳纸业案例 |

**新增结构化参数**：14个（market_cr4, entry_barrier, roe_5y_avg, moat_existence, moat_framework_primary, supply/demand/scale_ratings, false_advantages, competitor_ranking, advantage_gap_sustainability, moat_sustainability, moat_monitor_kpis）

**judgment_examples.md 拆分**：
- 通用锚点（护城河、MD&A、管理层 + Greenwald 三维示例 + 虚假优势辨析）→ `shared/qualitative/references/`
- 龟龟专属锚点（G系数、分配意愿、λ可靠性）→ `strategies/turtle/references/`

---

### 报告可读性与输出格式

**可读性改进**：
- 新增 **执行摘要**（报告开头半页，结论先行 + 关键指标表）
- 新增 **深度总结与投资启示**（报告末尾2-3页：商业模式本质、优劣势归因表、竞争定位、投资者启示、监控锚点展开、催化剂与风险事件、一句话最终结论）
- 写作风格指令：结论先行、重点加粗、通俗表达、去重、逻辑过渡
- 每个维度开头用加粗一句话结论引导

**HTML 仪表盘报告**：
- `scripts/report_to_html.py`：MD → HTML 转换（Markdown + Jinja2）
- `shared/qualitative/templates/dashboard.html`：IBM Plex 字体、暖色调浅底 + 自动暗色模式、语义色彩标签（绿/琥珀/红）、KPI 卡片、Verdict 横幅、折叠式附录
- 双输出：MD（策略消费）+ HTML（人类阅读 / 打印 PDF）

**固定附录**：
- `shared/qualitative/references/framework_guide.md`：Greenwald 框架说明、飞轮护城河概念、评级标准定义表、量化验证门槛

---

### Agent Team 并行架构

将单 Agent 串行改为多 Agent 并行，提升效率并降低 context 压力：

```
Step 3.0: split_data_pack.py → 按维度切割数据子集 + D6 触发检查
Step 3.1: 并行执行
  Agent A: D1(商业模式) + D2(护城河)  ← 最重的维度，合并消除依赖
  Agent B: D3(外部环境) + D4(管理层) + D5(MD&A)  ← 天然独立
  Agent C: D6(控股结构)  ← 条件触发，大概率跳过
Step 3.2: Summary Agent → 执行摘要 + 深度总结 + 报告组装
Step 4: report_to_html.py → HTML 仪表盘
```

**新增文件**：
| 文件 | 用途 |
|------|------|
| `scripts/split_data_pack.py` | 确定性数据预分发 + D6 触发检查 |
| `shared/qualitative/agents/agent_a_d1d2.md` | Agent A prompt（D1+D2） |
| `shared/qualitative/agents/agent_b_d3d4d5.md` | Agent B prompt（D3+D4+D5） |
| `shared/qualitative/agents/agent_summary.md` | Summary Agent prompt |
| `shared/qualitative/agents/writing_style.md` | 共享写作风格前置指令 |

---

## 待完成（v2_rc 路线图）

### 定性模块
- [x] ~~Agent Team 端到端测试与调优~~ → 改为单 Agent 模式（v2_beta）
- [ ] D2 护城河分析的边界案例处理（如 platform 型企业的框架选择）
- [ ] HTML 模板增加 bar chart（利润率趋势可视化）和 signal dots（风险信号矩阵）

### 龟龟策略（strategies/turtle/）
- [x] coordinator.md 调度改为引用 shared 定性模块（v2_beta 已完成）
- [x] phase3_preflight 合并进 phase3_quantitative Step 0（v2_beta）
- [x] 端到端 `/turtle-analysis` 测试 — 美的集团 + 海尔智家（v2_beta）
- [ ] W2 员工支出 SGA proxy 偏差优化（当前驱动 HH 偏差过大）
- [ ] Agent B + Agent C 进一步合并可行性评估

### 估值模块（strategies/valuation/）
- [x] 新增独立 `/valuation` 命令 + valuation_engine.py（v2_beta）
- [ ] 端到端测试与调优

### 烟蒂策略（strategies/cigarbutt/）
- [ ] 创建 cigarbutt coordinator + 策略专属 prompt
- [ ] 接入 shared 定性模块
- [ ] 端到端 `/cigarbutt-analysis` 测试

---

## 版本历史

| 版本 | 日期 | 主要变更 |
|------|------|---------|
| v1.0 | — | 初始版本：6-phase pipeline, 4 factors |
| v1.1 | — | 17 improvements across 9 files, shared_tables, HK/US support |
| v2.0-alpha | 2026-03-31 | 模块化拆分 + Greenwald 护城河框架 + HTML 仪表盘 + Agent Team |
| v2.0-beta | 2026-04-05 | PDF-first + 单Agent + Pre-flight合并 + 估值模块 + 实战验证 |
| v2.1-beta | 2026-07-04 | TTL缓存(冷25.9s→暖5.7s) + §17.10-13网格 + 定性审计门(P0+P1杠杆) + v1存档 + VIP行序防御 + 920测试全绿 |
