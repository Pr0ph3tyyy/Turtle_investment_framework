# 每日选股协调器（Daily Screener Coordinator）

> 对应技能：`.agents/skills/turtle-screener/SKILL.md`
> 单条入口：`scripts/screener_daily.py`
> 设计文档：`screener_daily_automation_design.md`
> 上游缺陷台账：`turtle_core_extraction_findings.md`

## 这一段流程干什么

把「全市场 → 前 N 只」这一步做成确定性管线，然后把前 N 只交给
**Workflow 2（龟龟策略全流程）** 做详细分析。排序键是龟龟的决策变量
**安全边际 `GG_pess − II`**，所以筛选结论与个股报告同源。

```
慢层（随采集更新）          快层（每日一次）
产物 claims.json  ──┐
fx.json           ──┼──► financial_snapshot ──┐
股本口径/shares.json┘                         ├──► turtle_core ──► 排序 ──► shortlist.json
                      直连 Yahoo chart 价格 ──┘        （GG_pess − II）
                                                     │
                                                     └──► 协调器按 action 分派 Workflow 2
```

## 前置条件（缺一即停，不得绕过）

| 前置 | 检查 |
|---|---|
| `rf.json` 或 `--rf` / `TUSHARE_RF` | `yc_cb` **实测无权限**，Rf 必须外部供给。缺 ⇒ 整批 ABORT |
| 常备池：产物目录含 `claims.json` | `--scan output` 自动发现，或 `--pool-file` 显式给定 |
| 每个产物目录含 `fx.json`（跨币种标的） | 缺 ⇒ 报表币种未确认，港股会静默差 15.65% |
| 产物含股本（`总股本（百万股）`行或 `shares.json`） | 缺 ⇒ `no_shares`，该股当日不参与排名 |
| 交易日与数据就绪 | A 股 `daily_basic` 约 19:00 后才完整；港股财务约 18:00；美股 16:00 ET |

```bash
# 前置自检
.venv/bin/python scripts/screener_daily.py --scan output --output output --dry-run
# 退出码 2 = 输入缺失；1 = 阻断级；0 = 可交付
```

## 执行步骤

### Step 1 — 慢层快照

**不需要单独命令**：`screener_daily.py` 在启动时从产物目录读取并构造
`FinancialSnapshot`（`turtle_screener/slow_layer.py`）。

它直接读 `claims.json` —— 也就是 canonical 公式链的结构化输出 —— 因此 screener 与
报告的决策数字**在构造上**就是同一批。若某只股的 claim 有字段缺失，会登记受控原因码
（`no_cashflow` / `no_payout` / `no_annual_3y` / `no_shares`），**不会**用 0 兜底。

### Step 2 — 跑当日选股

```bash
.venv/bin/python scripts/screener_daily.py \
  --scan output \
  --output output \
  --n-max 8 \
  --write-price-snapshot output/screener/prices_$(date +%Y%m%d).json
```

三个市场：`--markets A,HK,US`（默认）。美股宇宙的洁净度过滤在 `turtle_screener/universe.py`
（只留 `classify ∈ {EQ, ADR}`；实测混入 PF 385 / GDR 34 / 韩股）。

### Step 3 — 读 `shortlist.json` 并分派

```bash
cat output/screener/$(date +%Y%m%d)/shortlist.json
```

每只股票的 `action` 决定后续深度（已定参数：财报期未变 **且** 价格变动 < 10% ⇒ 只刷新 §17）：

| `action` | 含义 | 协调器动作 |
|---|---|---|
| `refresh_only` | 已有报告，财报期未变、价格变动 <10% | 只刷新 §17 价格相关小节，不重跑全文 |
| `rerun_turtle` | 已有报告，但新财报期或价格变动 ≥10% | 跑完整 Workflow 2 |
| `needs_workflow1` | 无定性报告（池外新股） | **异步慢路径**：先 Workflow 1（需年报 PDF + 6 维度子代理），不阻塞当日交付 |

> `needs_workflow1` 是**慢路径**，这是刻意的：Workflow 1 依赖年报 PDF（靠 `web_search`，
> 不稳定）且要跑 6 个维度子代理 + 综合 + 两条审核。**这是年度事件，不是每日事件** ——
> 因此常备池内的股票应提前完成 Workflow 1，让日报当晚就能交付详细分析。

### Step 4 — 对每只入选股跑 Workflow 2

对 `action ∈ {refresh_only, rerun_turtle}` 的股票，按 `strategies/turtle/coordinator.md`
执行（Step A 市场刷新 → Agent B → 数值审核 → Agent C → 交付门禁）。

**长跑**：全池 + 前 N 只的完整流程可能数小时 ⇒
用 `create_goal` 跨轮续跑 + 后台 job（`job_output` 收集）+ `present` 交付成品。

### Step 5 — 交付

主产物是 `output/screener/{trade_date}/` 下的四个清单 + 各入选股的
`{company}_{code}_分析报告.md`。可选：`plugins/dsh-share-longimage`（Workflow 5）出长图。

## 门禁（fail-closed）

裁决由 `scripts/gate_policy.py` **单点**给出。

| 情形 | 分级 | 处置 |
|---|---|---|
| `rf_missing` / `calendar_missing` / `market_data_not_ready` | **blocking** | 整批 ABORT；只写 `run_manifest.json` + `ABORTED.txt`，**不产出残缺清单** |
| 逐股数据缺口（`no_cashflow` / `no_price` / `stale_snapshot` / `fx_missing` …） | disclosure | 该股带原因码进 `excluded.json`，不阻断当日其余交付 |
| 安全边际未达门槛 | 非错误 | `below_threshold`；全市场都不便宜时 **0 只达标是正确结果**，不硬凑 N |
| 某只股的报告未过四条交付门禁 | blocking（对该股） | 该股不交付，其余照常 |

`data_gap_ratio` 会在 `run_manifest.json` 里报出；占比过高是需要人看的信号，
但**不由脚本擅自升级为阻断** —— 那是交付政策，要改就改 `gate_policy.py`。

## 调度（DSH 原生 `schedule_create`）

三个收盘时点不能一次覆盖，因此两条（已确认方案）：

| # | 覆盖 | 写法 |
|---|---|---|
| 1 | A 股 + 港股 | `weekly`，`19:00:00`，`Asia/Shanghai`，weekdays `[1,2,3,4,5]` |
| 2 | 美股 | `weekly`，`06:30:00`，`Asia/Shanghai`，weekdays `[2,3,4,5,6]`（ISO 周几顺延一位） |

实测确认（2026-10-14）：`daily 19:00 Asia/Shanghai` → `scheduledAt=2026-10-14T11:00:00.000Z` ✅

比收盘**晚**而不是收盘即跑：A 股 `daily_basic` 当日数据约 19:00 后才完整，
港股财务约 18:00 更新。这也是 `_get_latest_trade_date` 里 `hour < 19` 启发式的由来。

**四条交付语义**（来自 `schedule_create` 工具契约，必须纳入设计）：

1. 任务绑定**本会话**（"in the current session"）⇒ 日报会话必须长期存活
2. **错过不补跑**（"only its latest missed occurrence"）⇒ 关机漏一天不会事后积压
3. **投递可能重复**（"can repeat after a crash"）⇒ 日报**必须按 `trade_date` 幂等**
4. 不存在的本地时刻被跳过（DST 安全）

## 幂等与复跑

```bash
# 用当日价格快照离线复跑 —— 结果与首次逐字节一致（已实测 MD5 相同）
.venv/bin/python scripts/screener_daily.py --scan output --output output \
  --offline-prices output/screener/prices_20261014.json
```

价格必须先落快照再跑，否则 Yahoo 实时报价会让重跑不幂等。

## 故障处置

| 症状 | 处置 |
|---|---|
| `rf_missing` | 补 `output/rf.json`（`{"rf_pct":…,"date":…,"source":…}`）后重跑 |
| 大面积 `no_shares` | 产物是旧版（无「总股本（百万股）」行）⇒ 补 `shares.json` 或重跑采集 |
| 大面积 `no_cashflow` | 上游现金流量表缺口 ⇒ 见该股 `section17_status.json` 的 reason |
| 港股 `calendar_missing` | 港股日历需自建（`turtle_screener/calendar.py` 的节假日表） |
| 全部 `below_threshold` | 不是故障；门槛制下这是"今日没有便宜货" |
| 价格大批失败 | Yahoo 限频 ⇒ 降 `--workers` 或改用 `--offline-prices` 复跑 |

## 已知前置缺陷（会阻断验收标准第 1 条）

见 `turtle_core_extraction_findings.md`：

- **D-10** 产物 `市值 ≠ 股本 × 现价 × fx`（00696 实测 +10.32%）
  ⇒ screener 与报告 §17 的 GG 不相等。
  检查：`turtle_screener.engine.check_market_cap_consistency()`
- **D-1** §17.9「门槛价格」分母用股本、§17.14 用市值，且前者币种错标
  ⇒ `numeric_audit` X1/X2 报 critical 阻断交付
