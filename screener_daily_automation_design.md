# Screener 设计：每日收盘后自动筛选 → 前 N 只自动跑龟龟详细分析

> 终态目标：每天收盘后全自动跑一遍 screener，选出前 N 只，然后对它们跑完整的龟龟策略个股分析
> （Workflow 1 定性 + Workflow 2 定量/报告 + 两条审核 + 四条交付门禁），产出可交付报告。
>
> 本文回答一个问题：**为了这个终态，screener 应该怎么做。**
> 市场范围：A 股 + 港股 + 美股。公式层路线：方案 A（抽 `turtle_core` 纯计算层）。
>
> **状态（2026-10-14）**：§9 的 5 项技术前提已**全部实测完毕**，结论与新发现的 5 个硬约束已并入
> §3/§5/§6。设计可直接进入实现，剩余待拍板参数见 §9.3。

---

## 0. 现状基线（为什么不能直接拿现有 screener 上自动化）

现有 `scripts/screener_core.py` 是「两级 + 自造 5 维评分」的行情代理实现，与 v2/v3 公式链已分叉：

| 分叉 | 位置 | 后果 |
|---|---|---|
| `II = max(3.5, Rf+2.0)`（应为 `max(5.0,Rf+3.0)`） | `screener_core.py:617` | 旧 71 只里 21 只判定改变，22 只 `pass` 掉 10 只 |
| `R = AA×M/市值`（应为 `R=C×M×Q`，`GG=AA×M×Q`） | `screener_core.py:591,716` | 无税、无变体、无 O；实为无税 GG |
| 综合底价对 5 锚点等权平均（canonical 是 ①②③ 底价 + ④⑤ 公允价分列） | `screener_core.py:1010` | canonical 已诊断为 06862 事故并修掉，screener 是该 bug 活副本 |
| 同一指标双数据源（EBITDA/FCF/净负债优先 `fina_indicator.*`） | `screener_core.py:885-911` | **横截面不可比** —— 百分位排名的致命伤 |
| 有息负债内联组件表（未走 `constants.interest_bearing_debt`） | `screener_core.py:819,955` | 值对，单点丢失，会静默漂移 |
| 硬否决 fail-open（缺数据即放行） | `screener_core.py:409,420` | 违反仓库 fail-closed 纪律 |
| 无 FX / 无 Q / 无 claim 账本 / 无 §17 契约 | 全文 | 港股静默单位错；数字全为「裸数字」 |
| 排序用自造 5 维加权（ROE/FCF/R/EV-EBITDA/floor premium） | `screener_core.py:1107` | **与龟龟决策变量不同源** → 筛出来的 top N 与个股报告结论会互相打脸 |

**判断**：骨架（Tier1 取数/过滤、Parquet 分层缓存、限速重试、导出、101 个测试）可复用；
**排序口径与公式层必须重做**。

---

## 1. 从终态倒推出的六条硬约束

这六条是本文的核心，其余都是推论。

### C1 排序键必须与龟龟决策变量同源
若 screener 用 A 口径排名、个股报告用 B 口径结论，自动化每天都会产出「screener 说第一、报告说不可买」
的自相矛盾产物，且 `viewpoint_audit` 会判报告「结论与档位不相容」。

→ **排序键 = 安全边际 `GG_pess − II`（第一键），`目标买入价/现价 − 1`（第二键，人可读）。**
必须由 `turtle_core` 计算，**禁止在 screener 里出现第二个实现**。

### C2 必须拆「慢层/快层」，否则每日全市场重算不可行
全 A 股 5000+ 只 × 每股 6–8 个财务端点 × 0.5s 限速 ≈ 4–5 小时，日报做不完。
但龟龟公式链里**只有分母（市值/价格）是每日变的**：

- 慢层（季度/年度刷新）：`AA` 各变体、`M`（含承诺支付率锚定）、`O`、`Q`、财务质量门槛字段、
  底价锚点 ①②③⑤ 的**报表币种每股量**、`fy_end`、币种、股本口径 → `financial_snapshot.json` / 股
- 快层（每日）：**1 次全市场行情调用** + join + 纯计算 `R / GG / II / 目标买入价 / 安全边际 / 溢价`

→ 每日成本 ≈ 1 次全市场行情调用 + O(池大小) 的纯运算。**这是日报可行的唯一原因。**

### C3 漏斗必须是四级，且排除必须带 reason code
现有只有 2 级。终态需要：

| 级 | 内容 | 成本 | 失败处理 |
|---|---|---|---|
| T0 宇宙卫生 | ST/退市/停牌/上市年限/流动性/市值/PE 通道 | 秒级（行情） | 直接排除 |
| T1 可达性 | ≥3 年年报、分红历史、关键字段齐、fx 可得、Rf 可得 | 慢层缓存 | **排除并给 reason code**（fail-closed，不静默跳过） |
| T2 canonical 排序 | `turtle_core` → 安全边际 | 纯计算 | 数据不全 ⇒ `UNVERIFIABLE`，不进榜 |
| T3 详细分析 | top N → Workflow 2（必要时先 Workflow 1） | LLM | 按门禁裁决 |

reason code 复用 `scripts/section_contract.py` 的 `REASONS` 受控词表机制
（`no_annual_3y` / `no_dividend_history` / `hk_field_gap` / `rf_missing` / `fx_missing` / `st` / `illiquid` …）。
**没有 reason code 的排除就是不可诊断的排除** —— 自动跑三个月后会没人知道为什么某只票从没出现过。

### C4 门槛制 + N 上限，不是硬取前 N
全市场都不便宜的日子，硬凑 N 只会让下游 LLM 去分析本来不可买的股票。
→ 资格 = `安全边际 > 0` **且** `GG_pess > 0`（否则目标买入价无定义，见 `derived_metrics.py:2030` 注释）；
N 只是**上限**。无标的时日报明确输出「今日 0 只达标」，这是正确结果而非故障。

### C5 稳定性：日报不能每天抖动
每日价格波动会让排名剧烈换手，进而每天重复触发昂贵的 T3。
→ ① 入选需连续 `K` 日满足资格（K 默认 2）或跨越滞回阈值；
② 已分析股票维护「报告有效期」：财报期未变 + 价格变动 < X% ⇒ **只刷新 §17 价格相关小节，不重跑全文**；
③ 每只股票记录 `last_analyzed_at / last_financial_period / last_price / last_verdict`。

### C6 把 Workflow 1 从日报里摘出去（常备池）
Workflow 2 依赖 `qualitative_report.md`，而后者依赖**年报 PDF**：
获取靠 `web_search`（不稳定）、要跑 6 个维度子代理 + 综合 + clean-room + 两条审核。
**这是年度事件，不是每日事件。**

→ 维护**常备池**（建议 200–300 只，A+H+US 混合）：池内股票的 Workflow 1 提前完成，年报季统一刷新。
日报只在池内（+ T0 发现的新候选）做 T2 排序 → top N 直接进 Workflow 2，**当晚可交付**。
池外新进入者走**异步慢路径**（T+1/T+2 补齐 Workflow 1），不阻塞当天交付。

---

## 2. 架构

```
scripts/turtle_core/                    # 新增：纯计算层（方案 A）
  inputs.py      # Tushare/yfinance → 结构化 FinancialSnapshot（fx + shares 归一）
  steps.py       # 步骤1–9 + 10a/10b/10c（AA 变体、M 锚定、Q、O）
  decision.py    # II / R / GG / HH / GG−II / 目标买入价 / GG_pess−II / 仓位矩阵
  snapshot.py    # 慢层快照的读写与版本化（fiscal period 作为版本键）

derived_metrics.py  → 改为 turtle_core 的**渲染器**（行为不变；claims 作 golden 回归护栏）
valuation_engine.py → 同样改走 turtle_core（消除第 3 处重复实现）
screener_core.py    → 只保留 T0/T1 漏斗 + 批量驱动 + 出 manifest
```

**慢层快照**（`output/.screener_cache/snapshots/{ts_code}.json`，键 = 财报期）：
```json
{"ts_code":"06862.HK","fy_end":"20251231","reporting_currency":"CNY","trading_currency":"HKD",
 "snapshot_version":"1.0","built_at":"2026-10-14T02:00:00+08:00",
 "aa_variants":{"pess":...},"m":{"value":...,"basis":"近3年均值|承诺","source":"..."},
 "o":0.0,"q_default":20.0,
 "gate_fields":{"roe_waa":...,"gross_margin":...,"debt_to_assets":...},
 "floor_anchors":{"nla_per_share_report_ccy":...,"bvps_report_ccy":...,"low10y":...,"fcf_min_5y":...},
 "section_status":{"17.11":"ok","17.6":{"status":"missing","reason":"hk_field_gap"}},
 "invariants":{...},"field_coverage":{...}}
```

**每日快层** → `output/screener/{YYYY-MM-DD}/`：

| 文件 | 内容 |
|---|---|
| `shortlist.json` | top N 的机器可读交接单（见 §4） |
| `screener_results.csv` | 全池结果（CSV 仍产出，给人看） |
| `run_manifest.json` | 宇宙规模、各级漏斗计数、门禁裁决、耗时、缓存命中率、快照版本 |
| `excluded.json` | 被排除股票 + reason code |
| `daily_report.md` | 一页汇总（可选再走 Workflow 5 出长图） |

---

## 3. 四级漏斗与三市场取数

以下均已**实测**（2026-10-14，探针结果见 §9）：

| 级 | A 股 | 港股 | 美股 |
|---|---|---|---|
| 宇宙 | `stock_basic()` → **5574** ✅ | `hk_basic()` → **2788** ✅ | `us_basic()` → 6000/页，**offset 翻页可用** ⇒ 总量 ≥12000 ✅ 但需洁净度过滤 |
| 每日行情 | `daily_basic(trade_date)` → **5561 行，无限频** ✅ 首选 | **Tushare 不可用**（`hk_daily` 批量 1 次/小时）；yfinance 也不可用 ⇒ **直连 Yahoo chart** ✅ | `us_daily` 被限频（1 次/分钟~1 次/小时）+ 单页 6000 < 总量 ⇒ **直连 Yahoo chart** ✅ |
| 日历 | `trade_cal[SSE/SZSE]` ✅ | ❌ **Tushare 无 HKEX 日历（0 行）⇒ 需自建** | `trade_cal[NYSE]` ✅（NASDAQ 复用 NYSE） |
| 财务快照 | Tushare 报表端点 | `hk_fina_indicator` ✅（96 期） | `us_income/balancesheet/cashflow/fina_indicator` 大盘股均有数据 ✅ |
| Rf | ❌ `yc_cb` **无权限** ⇒ 必须外部供给 `rf.json` | 同左 | 同左（同一 `ii_threshold` 函数） |

### 3.1 每日价格只取「池 + 候选集」，不取全市场 ← 实测得出的关键修正

美股 ≥12000 只无法每日全量拉取（`us_daily` 限频 + 单页 6000），港股批量被 `hk_daily` 的
**1 次/小时**锁死。但用户已选池 ≤100 只，因此**每日快层只需 池 + 有界候选集 的价格**：

- 价格源 = 直连 `query1.finance.yahoo.com/v8/finance/chart/{sym}`（仓库已有 `_yahoo_chart_df` 实现）
- 实测吞吐：30 只（20 HK + 10 US）8 并发 **1.82s，30/30 成功，0 失败** ⇒ 300 只外推 **≈18s**
- ⚠️ 港股当日未完成 bar 的 `close[-1]` 可能是 `None`（实测 0700.HK：`close=424.80`、
  `meta.regularMarketPrice=425.2`）⇒ 取最后一个非空值，并优先 `regularMarketPrice`。
  这正是 `diagnosis_hk_section17.md` 记录的港股 close 缺失问题的成因。

**三市场统一**：`II = max(5%, Rf+3%)` 走 `constants.ii_threshold()` 单点（已统一，三市场一致）；
`Q` 按上市地+渠道取 `shared_tables.md` 税率表；fx 与每股量折算走 `fx_utils` + `shares_utils`。

**必须改的一处**：`screener_core.py:_get_latest_trade_date` 现在硬编码 `exchange="SSE"` 且用
`hour<19` 猜数据就绪 —— 三市场下必须换成**按市场的交易日历 + 数据就绪探测**。

---

### 3.2 数据频率：真正需要「每日」的只有两件事，**不需要日线序列**

拆解龟龟决策链对价格的依赖，是判断可行性的关键：

| 龟龟变量 | 公式 | 价格依赖 | 实际变化频率 |
|---|---|---|---|
| `II` 门槛 | `max(5%, Rf+3%)` | 无 | 周/月（Rf 动才动） |
| `R` 粗算 | `C × M × Q / 市值` | **仅分母** | 财报期（季）+ 市值（日） |
| `GG` 精算 | `AA × M × Q / 市值` | **仅分母** | 同上 |
| **`GG_pess − II`（主排序键）** | — | **完全不含价格** | **财报期（季）** |
| `目标买入价` | `现价 × GG / II` | **现价** | 每日 |
| 底价锚点 ③ 10 年最低价 | 近 10 年周收盘最小值 | 长历史**取最小** | 极少变（周更足够） |
| 流动性 / 停牌 | `turnover_rate` | 当日快照 | 每日 |
| 因子4·股价分位（§17.6） | 历史分位数 | 序列 | **可选**（可 `missing` + reason） |

结论：

1. **不需要日线 OHLCV 序列。** 需要的只是**每日一份市值/收盘价快照** —— A 股
   `daily_basic(trade_date)` 一次调用即得全市场（实测 **5561 行、无限频**，字段含
   `close/pe_ttm/pb/total_mv/circ_mv/dv_ttm/turnover_rate`，正是快层所需的全部）。
2. **主排序键不含价格** ⇒ 排名本身是**季度事件**；每日重跑的价值在于「目标买入价 vs 现价」
   这个买点信号 + 流动性/停牌过滤，而不是重算基本面。
3. 唯一的历史序列需求是底价锚点 ③，且它是**取最小值**的统计量 ⇒ 周更即可。

> ⚠️ **顺带发现 screener 的 ③ 口径错误**：`weekly` 不带 `start_date` 返回**全量历史**
> （实测 `600036.SH` 1238 根 = 2002–2026 共 **24 年**；`600941.SH` 243 根 = 上市 4.7 年）。
> 现有 `_extract_floor_price` 直接取 `weekly["close"].min()` ⇒ 实际是「**上市以来**最低价」，
> 而非 canonical / README 明示的「**近 10 年**」：老公司的 ③ 被系统性压低，且**各股口径不一**
> （新股 4.7 年 vs 老股 24 年）⇒ 横截面不可比。
> 修法（实测验证）：`weekly(ts_code, start_date=十年前的今天)` → `600036.SH` 精确返回
> 10 年窗口 550 根。**已并入 P0**。

---

## 4. 下游接口：`shortlist.json`

这是 screener 与龟龟分析之间**唯一的契约**，不是给人读的 CSV。

```json
{"schema_version":"1.0","as_of":"2026-10-14","trade_date":{"A":"20261014","HK":"20261014","US":"20261013"},
 "rf_pct":1.0242,"ii_pct":5.0,"n_max":8,"eligible_count":6,"shortlist":[
  {"rank":1,"ts_code":"000921.SZ","name":"海信家电","market":"A",
   "gg_pess":9.87,"ii":5.0,"margin":4.87,"target_buy_price":18.42,"close":22.25,
   "trading_ccy":"CNY","snapshot_version":"1.0","fy_end":"20251231",
   "coverage":{"score_dims":"5/5","gate":"ok"},
   "gate_status":"deliverable","reason_codes":[],
   "action":"refresh_only","existing_report":"output/000921_海信家电/海信家电_000921_分析报告.md",
   "last_analyzed_at":"2026-10-12","needs_workflow1":false}
]}
```

协调器据 `action` 分派：
- `refresh_only` —— 已有报告且财报期未变、价格变动小 → 只刷新 §17 价格相关小节
- `rerun_turtle` —— 已有 `qualitative_report.md` → 直接 Workflow 2
- `needs_workflow1`（慢路径）—— 池外新股 → 先 Workflow 1（异步，不阻塞当日交付）

---

## 5. 调度与长跑（DSH 原生，已实测）

DSH 内置 Host Schedule 服务，工具在本会话**实测可用**：`schedule_create` / `schedule_list` /
`schedule_update` / `schedule_delete`；时间选择器 `after_seconds` / `at` / `every_seconds` /
`daily` / `weekly` / `cron`（**恰好一个**，多给即 `invalid_selector` 拒绝）。**不需要 launchd。**

实测：`daily {time:"19:00:00", time_zone:"Asia/Shanghai"}` → `scheduledAt = 2026-10-14T11:00:00.000Z`
（= 19:00 CST ✅ 时区语义正确）；`weekly {weekdays:[1..5], 19:00, Asia/Shanghai}` 同样正确。

**已定方案：分两次**（A 股/港股 15:00/16:00 收盘，美股 16:00 ET = 次日 04:00–05:00 CST）：

| # | 覆盖 | 建议写法 | 说明 |
|---|---|---|---|
| 1 | A 股 + 港股 | `weekly`, `19:00:00`, `Asia/Shanghai`, weekdays `[1,2,3,4,5]` | 定 19:00 而非收盘即刻：`daily_basic` 当日数据 19:00 前后才完整（`_get_latest_trade_date` 的 `hour<19` 启发式），港股 fina 约 18:00 更新 |
| 2 | 美股 | `weekly`, `06:30:00`, `Asia/Shanghai`, weekdays `[2,3,4,5,6]` | 覆盖前一夜美股收盘，ISO 周几顺延一位 |

**必须纳入设计的四条交付语义**（来自 `schedule_create` 契约，非猜测）：

1. **任务绑定本会话**（"in the current session"）⇒ 日报会话必须长期存活，不能依赖临时会话。
2. **错过不补跑**："After downtime, a recurring reminder delivers only its latest missed occurrence"
   ⇒ 机器关机/休眠导致某天漏跑，**不会**事后补跑，也不会积压。可接受，但需在运行清单里体现日期连续性。
3. **投递可能重复**："Delivery can repeat after a crash" ⇒ **日报必须按 `trade_date` 幂等**
   （重跑覆盖而非追加），这是 §6 幂等要求的机械理由。
4. 不存在的本地时刻被跳过（DST 安全）。

**长跑**：一次全池 T2 + top N 的 Workflow 2 可能数小时 ⇒ 用 `create_goal`（跨轮续跑）+
后台 job（`job_output` 收集）+ `present` 交付成品。scheduled prompt 只负责「唤醒协调器」。

---

## 6. 门禁与失败模式（fail-closed）

每日运行本身要有裁决，写进 `run_manifest.json`，复用 `scripts/gate_policy.py` 的
critical/major ⇒ blocking、minor ⇒ disclosure 单点：

| 失败模式 | 处置 |
|---|---|
| 非交易日 / `daily_basic` 空 / 数据未就绪 | **ABORT**，不出残缺清单（绝不用昨天数据假装今天） |
| Rf 不可得（`yc_cb` **已实测无权限**，`rf.json` 缺失） | **ABORT** —— `II` 全错会污染全市场排名，不得用 0/默认值兜底 |
| 同一 `trade_date` 被重复投递（schedule 契约明示 "delivery can repeat after a crash"） | **按 `trade_date` 幂等**：重跑覆盖同一 `output/screener/{date}/`，绝不追加第二份 |
| 港股日历不可得（`trade_cal` 无 HKEX） | 用自建港股节假日表；缺失时该市场整体 ABORT，不猜交易日 |
| fx 不可得（港股/美股跨币种） | 该股 `fx_missing` 排除，**不得**混币种硬算 |
| 快照过期（新财报期已出，慢层未刷新） | 标记 `stale_snapshot`，该股不进榜（避免用旧盈利排名） |
| 单只股票 T2 数据不全 | `UNVERIFIABLE` + reason code，不进榜，不静默补数 |
| 某只 T3 报告未过四条交付门禁 | 该股票**不交付**；当日其余标的照常交付（不因一只失败而全案作废） |

---

## 7. 分期实施

| 阶段 | 内容 | 工期 |
|---|---|---|
| P0 止血 | II 走 `ii_threshold`；IBD 走 `interest_bearing_debt`；底价拆 ①②③/④⑤；补 Q/O；接 `fx_utils`；同步改 `tests/test_screener.py:838` 等断言。~~③ 改近 10 年窗口~~ **核实后撤回**：采集器三市场**都已**传 10 年窗口（A 股 `start_date`、港/美 `range=10y`，`financials.py:1301-1306`），产物 §11 的「10年最低 …(20161021)」即印证 —— 我此前据 API 默认值推断，属无效推断 | 0.5–1 d |
| P1 抽层 | `turtle_core` 纯计算层 + `derived_metrics`/`valuation_engine` 改走它；claims equivalence 快照回归（对 `output/_baselines/06862_海底捞` 等） | 2–4 d |
| P2 决策与契约 | 排序换 `GG_pess − II`；`snapshot.py` 慢层 + 每日快层；`shortlist.json` / `run_manifest.json` / reason codes；接 `gate_policy` | 2–3 d |
| P3 三市场 | 三套日历（**港股日历需自建**，Tushare 无 HKEX）；数据就绪探测；港股/美股每日价格改**直连 Yahoo chart**（池+候选集，8 并发）；美股宇宙 offset 翻页 + `classify` 洁净度过滤；**`rf.json` 契约**（`yc_cb` 无权限）。~~先修港股市值 10⁶ 单位炸弹~~ **已完成**（见下） | 2–3 d |
| P4 调度与集成 | AGENTS.md Workflow 6 + `.agents/skills/turtle-screener/` + coordinator；`schedule_create` 两条定时（A+H `weekly` 19:00 CST / 美股 `weekly` 06:30 CST）；长跑 goal/后台 job/present | 1–2 d |
| P5 常备池 bootstrap | 批量预跑 **≤100 只**的 Workflow 1（**运营成本，非开发成本**），年报季刷新 | 运营 |

**开发合计 ≈ 8–13 天。**

### 7.1 实施期对原计划的修正（2026-10-14 复核代码后）

1. **港股市值 10⁶ 单位炸弹已修，无需再做。** `diagnosis_hk_section17.md` 是修复前的诊断；
   现由 [infrastructure.py:124](scripts/tushare_modules/infrastructure.py#L124) 的
   `_hk_market_cap_raw()`（带 `_HK_MKT_CAP_RAW_FLOOR` 下限校验）统一归一化，
   `known_issues.md` P0-3 记录「10/10 港股实测」。P3 因此从 3–4 d 降为 2–3 d。
2. **执行顺序调整为「turtle_core 先行、derived_metrics 逐步迁移」**（原 P0→P1 会把公式写两遍）：
   - **阶段 1**：`turtle_core` 实现全部决策公式（新代码 + 单测），**screener 独占使用** —
     这一步同时完成 P0 的六项止血与 P2 的排序口径。
   - **阶段 2**：`derived_metrics` 按小节逐个改为调 `turtle_core`（factor2 → factor3 → 穿透网格
     → 悲观网格 → 底价），**每迁一节就跑一次 claims equivalence**，不通过则不合并。
     太纠缠的小节允许暂留，但必须由验收第 1 条（screener ↔ 报告 §17 claim 同源）机械兜住。
   - 这样 P0 的价值（立刻可用的正确 screener）不受 P1 工期影响。
3. **顺带修掉一个真实 CLI bug**：`tushare_collector.py --help` 因 argparse help 串里的
   `2.79%）。`（`%` 紧跟全角右括号）触发 `unsupported format character` 崩溃，
   已改为 `%%`。此前任何 `--help` 调用都会抛异常。

---

## 8. 验收

1. **同源一致性**（最重要）：对已有个股报告的股票（002027 / 002984 / 600941 / 601398 / 06862 等），
   screener 的 `GG_pess / II / 目标买入价 / 安全边际` 必须与 Workflow 2 报告的 §17 claim **逐字段相等**。
   不等则禁止上线 —— 这是 C1 的机械证明。
2. **幂等性**：同一 `trade_date` 重跑两次结果一致。前提是**价格为当日快照**（首次抓取即落盘
   `prices_{trade_date}.parquet`，重跑读快照而非重新联网）—— 否则 Yahoo 的实时报价会让重跑不幂等。
3. **门禁**：人为断掉 Rf / fx / 快照刷新，运行必须 ABORT 或带 reason code 排除，**不得**产出残缺清单。
4. **测试**：现有 101 个 + turtle_core 与旧 `claims.json` 的 equivalence 快照 + 枯竭场景（全市场 0 达标）。

---

## 9. 技术前提验证结果（2026-10-14 实测）

全部 5 项已实测，**无需再假设**。探针均为只读，脚本在 `/tmp/probe*.py`（未污染仓库）。

| # | 待验证项 | 结论 | 实测证据 |
|---|---|---|---|
| 1 | `hk_basic` / `us_basic` 无参是否返全量 | ✅ **通过** | `stock_basic()` 5574 · `hk_basic()` **2788** · `us_basic()` 6000/页且 **offset 翻页可用**（`offset=6000` 返回下一批）⇒ 美股总量 ≥12000 |
| 2 | yfinance 批量港股价格 | ❌ **不可用** | 批量 30 只 42.5s → **30/30 `YFRateLimitError`**；单只 `yf.Ticker("0700.HK").history()` 同样失败 |
| 2b | 替代港股价格源 | ✅ **直连 Yahoo chart 可用** | `query1.finance.yahoo.com/v8/finance/chart/` 30 只（20HK+10US）8 并发 **1.82s / 30 成功 / 0 失败** ⇒ 300 只 ≈18s |
| 3 | `yc_cb` 权限 | ❌ **无权限** | 明确报「您没有接口(yc_cb)访问权限」⇒ **Rf 必须外部供给**（见下） |
| 4 | DSH `schedule_create` 可用性 | ✅ **实测通过** | `daily 19:00 Asia/Shanghai` → `scheduledAt=2026-10-14T11:00:00.000Z`；`weekly [1-5]` 同样正确；多参数被 `invalid_selector` 拒绝；探针已全部删除（`schedule_list` → `[]`） |
| 5 | 美股宇宙与财报覆盖 | ✅ **通过（带条件）** | 财报：`us_income[AAPL]` 2680 / `us_balancesheet` 2903 / `us_cashflow` 2625 / `us_fina_indicator` 106 行；AAPL/MSFT/NVDA/KO 均有数据。**ts_code 必须是裸 ticker**（`AAPL` ✅ / `AAPL.O` ❌0 行） |

### 9.1 验证过程中新发现的 5 个硬约束（已并入上文设计）

1. **`hk_daily` 批量 = 1 次/小时、`us_daily` = 1 次/分钟~1 次/小时**（实测文案两种都出现过），
   且 `us_daily` 单页上限 6000 < 美股 ≥12000 ⇒ **Tushare 港股/美股日线不能做每日批量**，
   必须走直连 Yahoo chart，且每日价格**只取池 + 候选集**（§3.1）。
2. **`trade_cal` 没有 HKEX 日历**（`trade_cal[HKEX]` → 0 行；`NYSE` ✅ 44 行，NASDAQ 复用 NYSE）
   ⇒ 港股交易日历需自建（静态节假日表，或从 Yahoo chart 返回的 bars 日期反推）。
3. **`yc_cb` 无权限** ⇒ 新增 `rf.json` 契约（照 `fx.json` 的 option-A 模式）：
   `{"rf_pct":1.0242,"date":"2026-10-14","source":"..."}`，协调器每周更新；
   **缺失即 ABORT**，不得用 0 或默认值（`II` 全错会污染全市场排名）。
4. **美股宇宙需洁净度过滤**：`us_basic.classify` 分布（前 6000） EQ 5026 / ADR 555 / PF 385 / GDR 34，
   且实测含**非美股**（`035420`=NAVER 韩股、`036570`）与权证/单位（`D06P59`）
   ⇒ 只保留 `EQ`（可选 `ADR`），剔除 `PF`/`GDR`/非美票，否则 T0 会被垃圾标的淹没。
5. **美股财报是 line-item pivot 格式**（`ind_name`/`ind_value`/`report_type`），与 A 股/港股口径不同
   ⇒ `turtle_core` 的 US 字段映射必须单独走一遍 equivalence 验证，不能假定与 A 股同构。

### 9.2 已确认的设计参数（用户决策）

| 参数 | 取值 |
|---|---|
| 公式层路线 | **方案 A**（抽 `turtle_core` 纯计算层） |
| 市场范围 | A 股 + 港股 + 美股 |
| 调度 | **分两次**：A+H `weekly` 19:00 CST（周一–五）；美股 `weekly` 06:30 CST（周二–六） |
| 常备池 | **≤100 只**，先跑通再扩 |
| 报告刷新 | 财报期未变 **且** 价格变动 < 10% ⇒ 只刷新 §17 价格相关小节 |
| 下一步 | 先验证技术前提（本文档即产出），再动手实现 |

### 9.3 仍未定、实现前需拍板的参数

`N` 上限（建议 5–8，受下游 LLM 预算约束）· `K` 连续确认天数（建议 2）· 常备池的**入选口径**
（建议：先按 A 股 T0 硬门槛 + 流动性 + 市值取前若干，再按行业分散约束）· `rf.json` 的更新责任人/频率。

### 9.4 当前 token 的权限清单（实测，2026-10-14）

结论：**目标可用当前权限实现，唯一缺口是 Rf。**

| 能力 | 当前权限 | 实测 | 方案 |
|---|---|---|---|
| A 股全市场每日快照 | ✅ | `daily_basic(trade_date)` **5561 行 / 无限频** | 每日快层直接用（含 close/pe_ttm/pb/total_mv/dv_ttm/turnover_rate） |
| A 股财报链 | ✅ | `income` 36 · `balancesheet` 32 · `cashflow` 38 · `dividend` 26 · `fina_indicator` 40 · `fina_audit` 9 · `pledge_stat` 235 | 直接用 |
| A 股周线（10 年低点） | ✅ | `weekly(600036.SH)` 1238 根（2002–2026） | 传 `start_date=10年前` |
| A 股日线（备用） | ✅ | `daily(trade_date=20261013)` **5561 行** | 需要历史分位时才用 |
| 港股财报链 | ✅ | `hk_income` 566 · `hk_balancesheet` 957 · `hk_cashflow` 912 · `hk_fina_indicator` 21 | 直接用 |
| 美股财报链 | ✅ | `us_income` 2680 · `us_balancesheet` 2903 · `us_cashflow` 2625 · `us_fina_indicator` 106 | 直接用（裸 ticker，line-item pivot 格式） |
| 三市场宇宙 | ✅ | `stock_basic` 5574 · `hk_basic` **2788** · `us_basic` 6000/页 + offset 翻页 | 直接用（美股需 `classify` 过滤） |
| 三市场长历史价格 | ✅ | Yahoo chart `10y/1wk`：HK 524 · US 524 · A股 524 根 | 底价 ③ 与股价分位；A 股 ticker 用 `.SS/.SZ`（`.SH` → 404） |
| 港股/美股每日报价 | ⚠️ 限频 | `hk_daily` 1 次/小时 · `us_daily` 1 次/分钟~1 次/小时 | Yahoo chart 池内批量（30 只 8 并发 1.82s） |
| 港股交易日历 | ❌ | `trade_cal[HKEX]` → 0 行 | 自建节假日表或由 Yahoo bars 反推 |
| **无风险利率 Rf** | ❌ | `yc_cb` 明确「没有接口访问权限」 | **唯一外部输入**：`rf.json`，每周更新，缺失即 ABORT |

> 注：`yc_cb` 之外，**港股/美股财报链、以及三市场长历史价格都不受限频影响**（实测每股约 1s）；
> 被限频的只有 `hk_daily`/`us_daily` 这两个日线端点，而它们恰好是设计里**已决定不用**的路径。
