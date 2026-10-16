# turtle_core 抽取期发现的上游缺陷（2026-10-14）

抽 `turtle_core` 纯计算层时，对 `derived_metrics.py`（2682 行）做了逐函数规格提取，
顺带发现下列**既有缺陷**。它们不是待做工作，而是**当前就影响交付**的问题，
按 `AGENTS.md` 纪律 3「脚本误判要回灌脚本」处理。

---

## D-1（严重）§17.9「门槛价格」与 §17.14「目标买入价」分母不同源 + 币种错标

**断言**（`scripts/numeric_audit.py:954-994` X1、`:996-1011` X2）：
``§17.9 门槛价格 == §17.14 目标买入价``（同一 k），MISMATCH ⇒ **critical** ⇒ blocking。
其 docstring 称二者「在同一 k 上是同一个数」。

**两侧的真实算法**：

| 侧 | 公式 | 分母 | 币种 | 代码 |
|---|---|---|---|---|
| §17.9 门槛价格 | `AA×k×M_rec×(1−Q) ÷ (II/100 × **总股本**)` | **总股本** | **报表币种**（却渲染在「门槛价格（港元）」表头下） | [derived_metrics.py:1011](scripts/tushare_modules/derived_metrics.py#L1011) |
| §17.14 目标买入价 | `现价 × GG_pess / II`，GG 用 **市值** | **市值** | 交易币种（正确） | [derived_metrics.py:2501](scripts/tushare_modules/derived_metrics.py#L2501) |

⇒ 二者相等**当且仅当**三个条件同时成立：
`mkt_cap == 总股本 × 现价 × fx` **且** `M_pess == M_rec` **且** 币种标注正确。

**这三条在真实产物里都不成立，实测（00696.HK 新采集，2026-10-14）**：

```
§17.14 k=0.80 目标买入价 = 2.5263 港元/股   （turtle_core 重算，与产物 2.53 一致）
§17.9  −20%  门槛价格   = 1.9637 人民币/股（产物渲染为 1.96 港元 —— 币种错标）
自洽股本应为 2926.21 百万股，产物用了 3228.18 百万股（kind=implied_period_end，产物自己标了 ⚠️降级）
⇒ 市值 22,533.63 百万 ≠ 股本×现价×fx = 24,858.99 百万，偏差 +10.32%
用自洽股本重算 §17.9：2.1664 CNY → ÷fx = 2.5263 港元/股 —— 与 §17.14 **精确一致**
```

**⚠️ 修正我此前的判断**：我最初把 X1 记为"对正确产物的误报"。这个结论**只对了一半**，
必须分开陈述：

1. **X1 的前提确实不完整** —— 它忽略了 `M_pess/M_rec` 与分母同源两个条件。
   在 A 股 fixture（fx=1、股本与市值自洽）上，仅 `M_pess/M_rec = 54.202/58.081 = 0.93322`
   就足以让 X1 误报（实测 tp9=11.97、tbp14=11.17，比值恰为 0.93322）。
   `tests/test_audits.py:213-235` 把两格硬编码成同一个 8.67，**编码了前提而非推导它**。
2. **但 00696 上的 X1/X2 是真正的阳性** —— 它们抓到了两个真缺陷：
   - **股本与市值不同源（+10.32%）**：股本是"期末市值÷期末价"反推的降级值，
     而市值是当日值；§17.9 用股本、§17.11/§17.14 用市值，于是同一物理量在两节里不同。
   - **§17.9 的币种错标**：以报表币种（CNY）计算却渲染在「门槛价格（港元）」表头下。
     canonical 因此在跨币种时把该 claim **强制置 None**（[derived_metrics.py:1057-1090](scripts/tushare_modules/derived_metrics.py#L1057)），
     但**表格单元格仍照常打印** —— 审计读的是单元格，所以照样报 critical。
   X1 自己的成因候选文本给出的是"币种错"与"M 口径"，两者都与实测比值 0.7747 不符，
   于是输出「均未命中 ⇒ 需人工查证」—— 它诚实地承认了自己解释不了。

**修法（三条都要）**：
1. §17.9 的门槛价格改为与 §17.14 同式（`现价 × GG/II`），或至少除以 `mkt_cap/现价`
   得到的自洽股本，**不要**直接用 `total_shares`；
2. 跨币种时把单元格也置 `—`（当前只置了 claim，正文照印）；
3. X1 改成比例恒等：断言 `tbp14 == tp9 × M_pess/(M_rec × fx) × (自洽股本/产物股本)`，
   或更简单 —— **断言两侧都由 turtle_core 计算**（同源即恒等，无需跨表猜）。
   X2 同理（它的前提"两侧都用 M_rec"是对的，但仍受分母不同源影响）。


---

## D-2（严重）M_rec 若是承诺值，则 §17.11/§17.14 **完全看不到它**

`_compute_penetration_grid` 构造 M 候选时**只**取 `m1/m2/m3`
（[derived_metrics.py:1966-1993](scripts/tushare_modules/derived_metrics.py#L1966)）。
当 `m_rec_label ∈ {承诺(支付率), 承诺(金额折算)}` 时，[:1984-1985](scripts/tushare_modules/derived_metrics.py#L1984)
的守卫**静默回退到法1**，★ 随之移动。

后果：
- §17.9 与 §17.13 **用** `pc["m_rec"]`（含承诺）；
- §17.11 与 §17.14 **不用**（§17.14 用 m_pess，同样与承诺无关）；
- ⇒ 同一份包里 §17.9 的基准 GG 与 §17.11 的 `AA_2y×★` 行**在存在承诺时必然不一致**，
  而 [:2143-2144](scripts/tushare_modules/derived_metrics.py#L2143) 的正文却称二者可对照。

`_method_slug` 里的 `commitpct` / `commitamt` 两个 slug 是**死代码**（没有任何 claim id 用它）。

**修法**：把承诺值作为一个显式 M 候选注入 §17.11/§17.14 的候选集（带 label），
或反过来让 §17.9 也只用三法 —— 但必须二选一并写进 `shared_tables.md`。

---

## D-3（中）O（注销型回购）**根本没有被计算**，§15 的成果无人消费

- §17.9 / §17.11 硬编码 `o_val = 0.0`（[:998](scripts/tushare_modules/derived_metrics.py#L998)、[:2009](scripts/tushare_modules/derived_metrics.py#L2009)）；
- §17.14 **连 O 项都没有**（[:2500](scripts/tushare_modules/derived_metrics.py#L2500)）；
- §17.2 恒定渲染 `0.00 百万`（[:388-393](scripts/tushare_modules/derived_metrics.py#L388)）；
- 唯一真实的回购聚合在 §15（[other_data.py:418-447](scripts/tushare_modules/other_data.py#L418)，含去重与 `年均回购金额`），
  **但没有任何地方读回它**。

**修法**：`O` 在 `turtle_core` 里做成显式输入（默认 `0.0`，单位百万报表币种），
并从 §15 的聚合回填；§17.14 补 `+O` 项（当前 `GG_pess` 少一项，与 §17.11 口径不一致）。

---

## D-4（中）加回 O 的"加法修正"只存在于 §17.11，且 §17.14 缺失

§17.11 渲染 `o_increment = 100/mkt_cap_mm × 100` pct/百万（[:2010-2011](scripts/tushare_modules/derived_metrics.py#L2010)、[:2023-2024](scripts/tushare_modules/derived_metrics.py#L2023)），
§17.14 无此说明。同一份报告里两处对 O 的处置不一致。

---

## D-5（中）`cm17.2.m` 与 `cm17.10.m3` 是同一个数，却是两个 claim id

两者都来自 `_mean_first3(_get_payout_by_year(), years)`。
且 `cm17.2.m` 的 basis 文本写「§6 DPS÷§3 EPS」，而取值实际走 `_get_payout_by_year()`
（港股下会优先 `divi_ratio` 并与 DPS/EPS 交叉验证）—— **港股下 basis 文本是错的**。

---

## D-6（中）canonical 忽略 `fina_indicator.*`，screener 却优先用 ⇒ 横截面不可比

- canonical：EBITDA/净负债/FCF 全部自行从报表算，`fina_indicator` 只作 `· 对照` 行渲染
  （[:645-675](scripts/tushare_modules/derived_metrics.py#L645)）。docstring 明确
  `netdebt ≠ 有息负债−货币资金`、`fcff ≠ FCF`；002984 事故即写入 **+2.88%** FCF 收益率，
  真实为 **−231.85 百万元（−1.59%）**。
- screener：**优先**取 `fina_indicator.ebitda / netdebt / fcff`
  （[screener_core.py:885-911](scripts/screener_core.py#L885)）。
- ⇒ 同一指标在不同股票间来源漂移，对**百分位排名**是致命的。

---

## D-7（低）`aa_excl` 的实现与文档不符

实现是"剔除负值年份"（[:1376-1377](scripts/tushare_modules/derived_metrics.py#L1376)），
文档写的是"剔除非经营性主导年份"。claim basis 里已登记该差异（[:1494-1498](scripts/tushare_modules/derived_metrics.py#L1494)）。

---

## D-8（低）`constants.interest_bearing_debt` 在穿透回报率链路上**一次都没用**

[:595-603](scripts/tushare_modules/derived_metrics.py#L595) 的 `_ibd_yuan` 内联了
`IBD_COMPONENTS_WITH_LEASE`（为保留明细）。单点存在于 §17.1/§17.7/§17.8，
但 §17.9/§17.11/§17.14 的 AA→GG 链路不涉及 IBD。
⇒ `known_issues.md:1491` 声称的"7 处调用点全部改走单点"对穿透回报链路不适用。

---

## D-9（低）§17.14 缺 `no_rf` reason code

`assembly.py:281-285` 的 `_RF_DEPENDENT_LABELS` **不含**「悲观基准网格」，
所以 Rf 缺失时 §17.14 的缺失原因被推断为 `no_data`（或 `no_price`）而非 `no_rf`，
掩盖了真实成因。

---

## D-10（严重）§17.9 与 §17.11/§17.14 的**分母不同源**：一个用股数、一个用市值

实测 00696.HK 新采集产物：

```
§17.8 总市值（报表币种）                    = 22,533.63 百万
股本 3,228.18 百万股 × 现价 8.98 HKD × fx  = 24,858.99 百万      差 +10.32%
```

**成因（已核算，不是"股本算错"）**：

```
市值隐含价 = 22,533.63 / (3,228.18 × 0.85753) = 8.1400
§2 当日价                                      = 8.98
8.98 / 8.14 = 1.1032   ← 恰好就是观测到的 +10.32%
```

⇒ **HK 的市值取 `hk_fina_indicator` 的期末口径，而 §2 的现价是当日值**；
§17.9 的门槛价格以**股数**为分母、§17.11/§17.14 的目标买入价以**市值**为分母，
于是**价格一变动，同一物理量在两节里必然不同**。

> ⚠️ **我最初把这条记为「股本口径降级 / 产物市值缺陷」，并据此设计了不变式 I11
> （`市值 ≡ 股本 × 现价`，阻断级）。核算上面三行数字后撤回 —— 那是错的**：
> 该"恒等式"在价格变动后**本就不该成立**，做成包级不变式会在**几乎所有港股包**上
> 系统性误报（正是本仓库反复批评的假阳性形态）。
> I11 已从 `invariants.py` 与 `gate_policy.py` 移除。

**为什么仍然是严重问题**：§17.9 与 §17.11/§17.14 的正文**声称口径一致、可直接比较**
（`derived_metrics.py:352` 一类），而实际上它们在**任何价格变动的标的上**都会给出不同的
"同一个数"。这才是缺陷 —— 是**口径声明与实现不符**，不是某个值算错。

**已落地的处置**：
1. `numeric_audit` 的 **X1/X2** 改为**前提守卫**：先判 `M_rec == M_pess` 与分母是否同源；
   **仅在能证明前提不成立时**判 `UNVERIFIABLE`（披露级，附完整归因），
   **不再无条件断言两格相等** —— 旧版会在正确产物上报 critical 并阻断交付。
2. 新增 **X3**：§17.9 正文**自己声明的 M** 必须等于 §17.10 的 `M_rec`。
   实测 06862：声明 `M = 89.70%（§17.1 3年均值）`，而 `M_rec = 81.11%`，
   紧邻一行却写「与 §17.11 表B 一致（同一 M_rec）」—— **包内相邻两行自相矛盾**，判 critical。
3. **§17.9 门槛价格列改为单元格级 fail-closed**（`derived_metrics._compute_factor4_sensitivity`）：
   跨币种时**单元格也置 `—`**，并把「两表数值可直接比较」改为条件声明。
   - 修前：只把该列的 **claim** 置 null，**单元格照印** —— 报告与审计读的是单元格，
     「已封锁」在纸面上成立、在产物里不成立。
   - 修后实测（00696.HK 重新采集）：表头仍是「门槛价格（港元）」，表体该列 7 行全为 `—`，
     并打印「门槛价格列已整体置 — …不可引用」。
   - **决策数字零影响**（已核对）：61 项决策类 claim 里只有 11 项 `.tbp` 变化，
     而那 11 项全部是**价格依赖**项（现价 8.98 → 9.25 所致）；`GG / AA / M / II / 市值` 全未变。
     `turtle_core_selfcheck` 仍 **20/20 复现**，全量测试 **1960 passed**。
   - A 股（fx=1）不受影响 —— 测试同时锁了 `fx=1` 与 `fx≠1` 两条路径。

**仍待修（需你确认，因为要动市值口径，影响面大）**：
让 §17.9 的门槛价格与 §17.14 的目标买入价**同用市值口径**（§17.9 现用股数），
使两节在**任何价格**下都给出同一个数。当前 §17.9 的价格列已封锁，
所以**残留风险是"少一个读数"而不是"多一个错读数"** —— 这是刻意的取舍。


## D-11（严重）`gate_policy` 把 `WARN` 计入阻断 ⇒ **全部 A 股产物不可交付**

**现象**：池内每一只 A 股产物的 `gate_status.json` 都 `deliverable: False`，
阻断项里都有 `I4`，而 **I4 的实际 status 是 `WARN`**：

```
000651 格力电器  blocking: [('I4', 'WARN', '§3 同年口径混用…')]
601318 中国平安  blocking: [('I4', 'WARN', …)]
```
而 I4 的 evidence 显示逐年 `除税前 − 税项 = 除税后` 差 **0.00**（合计口径完全自洽），
detail 是「上游未提供「持续经营业务税后利润」行 ⇒ **无法确认**」——
这是**咨询性**状态，不是"算错了"。

**根因**：`gate_policy._STATUS_NEEDS_TIER` 含 `WARN` ⇒ 阻断级 code（I4）只要返回 WARN 就阻断。

**为什么这是"仓库自己预言过的问题"**：`invariants.py` 的 I4 docstring 明确写着目标是
「否则 I4 对**每一只 A 股**都返回 UNEVALUATED，而 I4 属阻断级 ⇒ 全部 A 股包不可交付」。
别名修复让它从 `UNEVALUATED` 变成 `WARN`，**但两者都在 `_STATUS_NEEDS_TIER` 里**
⇒ 问题根本没解决，只是换了个状态名。

**修法**（已落地）：把 `WARN` 从 `_STATUS_NEEDS_TIER` 移除。
判据：`WARN` 按定义是咨询性（"需确认/无法确认"）；真正要阻断的发现必须由不变式
返回 `FAIL`/`MISMATCH` 等，而不是 `WARN`。

**实测效果（同一批产物）**：

| 产物 | 修前 | 修后 |
|---|---|---|
| 000651 格力电器 | 阻断（I4-WARN） | **可交付**（I4 降为披露） |
| 601318 中国平安 | 阻断（I4-WARN） | **可交付** |
| 601166 兴业银行 | 阻断（I4-WARN + I9/C2-FAIL） | **仍阻断**（I9/C2 是真 FAIL）|
| 601668 中国建筑 | 阻断（I4-WARN + C2-FAIL） | **仍阻断**（C2 是真 FAIL）|

⇒ 修的是**假阳性**，真缺陷的检测**未削弱**。

**配套**：`slow_layer` 现在**按当前 `gate_policy` 重算**产物的门禁裁决，而不是直接信任
`gate_status.json` 里记录的 `deliverable`（那是**采集时刻**的政策算出来的）。
政策是单点，消费者按当前口径重算才正确 —— 否则政策修正无法对既有产物生效。

---

## D-12（中）screener 曾完全无视产物自身的门禁裁决

`gate_status.json` 里 `deliverable: False` 的产物，采集器的原话是
「data pack 已写出但**不得作为分析输入**」。而 screener 首版只看 `claims.json` 存在与否，
照样用这些产物的数字排名（第一批短名单里的格力电器就是这么被选出来的）。

**修法**（已落地）：
1. `reasons.py` 新增受控码 `upstream_gate_failed`；
2. `slow_layer` 读 `gate_status.json` 并按当前政策重算裁决；
3. `engine.screen_one` **默认排除**此类产物；`--allow-degraded` 才纳入，
   且**始终披露**该原因码；
4. `screener_pool --bootstrap` 按裁决分级报 `OK / DEGRADED / BLOCKED[码]`，
   不再把「有 claims.json」当成成功。

---

## D-13（中）`valuation_engine` 把**建模假设**标成实测值「§14」

**现象**：`ValuationEngine.compute_wacc()` 的 Rf 解析是

```python
rf = store["risk_free_rate"].yield          # 有就用
if rf is None:
    rf = self.params["rf_default"]          # A 2.5 / HK 4.0 / US 4.0
```

而 WACC 表里这一行**无论走哪条路径**都标 `"§14"`：

```python
["Rf (无风险利率)", self._fmt_pct(w["rf"]), "§14"],
```

**为什么这是活跃缺陷而不是理论问题**：`yc_cb` 在本仓库**实测无权限**
（`AGENTS.md` 专门为此写了 `--rf` 变通），所以 `risk_free_rate` 为空是**常态** ⇒
`rf_default` 是**实际生效的路径**。于是 WACC、以及下游每一个 DCF 终值，
都建立在一个**编出来的利率**上，而报告把它的来源写成「§14」——
读者/审计无法分辨。

**性质**：这不是"数值错了"（按市场默认利率做估值是正当的建模选择），
而是**溯源与披露不实** —— 与本仓库「不得静默补数」的纪律直接冲突。
它与本次修的其他几项是同一类：`WARN` 被当成 FAIL（D-11）、
screener 无视上游门禁（D-12）、§17.9 把 CNY 印在港元表头下（D-2）。

**修法（已落地）**：`compute_wacc()` 返回值新增 `rf_source`，
走 fallback 时写成
`默认值（§14 不可用：yc_cb 无权限且未传 --rf）⚠️ **非实测**，是 {市场} 市场建模假设`；
WACC 表读它。`tests/test_valuation_wacc_rf.py`（6 项）锁住两条路径**标注必须不同**。

> ⚠️ 该模块此前**没有任何测试**（`tests/` 下无 valuation 文件），
> 所以这一改动若不做断言就只能靠静态确认 —— 已补上最小回归。

**未做（需你决定）**：是否要把「Rf 缺失」升级为**阻断级**（即 §14 不可用时
拒绝输出 DCF 数字），而不是仅如实标注。那会改变 HK/US 估值产物的可交付性。

---

## D-9（中）§17.14 缺 Rf 时被标成 `no_data` 而不是 `no_rf`

**实证**（00696.HK，**不传** `--rf`，2026-10-14）：

```
§17.9  missing  no_rf
§17.11 missing  no_rf
§17.13 missing  no_rf
§17.14 missing  no_data    ← 错
```

四节缺席的原因是**同一个**（缺 Rf ⇒ 缺 II），因为 §17.14 的目标买入价
`= 现价 × GG_pess / II`、`vs 门槛 = GG_pess − II`，与 §17.11 同依赖。
根因：`assembly.AssemblyMixin._RF_DEPENDENT_LABELS` 漏登记 `"悲观基准网格"`。

**为什么值得修**：`no_rf` 的补救是「传 `--rf`」，`no_data` 的补救是「去找上游数据」——
**标错会把读者引向完全错误的修法**。这与 D-11/D-12/D-13 同属「声明与实现不符」一类。

**修法（已落地）**：补登记该标签；`tests/test_section_contract.py::TestRfDependentSections`
（7 项）锁住四节**分组一致**，并断言 `no_rf` 在 `section_contract.REASONS` 内、§17.14 在
`_LABEL_TO_SECTION` 内。

> 顺带澄清一处易混：**存在两套原因码词表** ——
> §17 小节用 `section_contract.REASONS`（`no_rf`），screener 排除用
> `turtle_core.reasons.SCREEN_REASONS`（`rf_missing`）。**同一个物理条件在两个词表里名字不同。**
> 我第一版测试混用了两套，立刻被断言抓到。目前设计上是有意分开的（消费者不同），
> 但写测试/读代码时必须分清。

### D-6 结案：对新管线**不适用**（架构上已消除）

原判断：「canonical 忽略 `fina_indicator.*`，而 screener 偏爱它 ⇒ 可能不同源」。
**核实结论：对新 screener 不成立**，且是**结构性**的而非巧合：

```
$ grep -rn "fina_indicator" scripts/turtle_screener/ scripts/turtle_core/
（无输出）

$ grep -c "fina_indicator" scripts/screener_core.py
8        ← 只在 legacy 里
```

新链路的决策输入**只**来自 `claims.json` —— 那正是 canonical 公式链的结构化输出，
经 `slow_layer.CLAIM_MAP` 逐字段取出（`slow_layer.py` 顶部即注明「**天然同源**」）。
`fina_indicator` 的直读只存在于 `screener_core.py`（**legacy，设计上不参与新流水线**）。

⇒ 验收标准①（screener ↔ 报告 §17 逐字段同源）**由架构保证**，
不是靠人工对账维持的。这也解释了为什么 `screener_consistency.py` 的 (b) 口径
（产物市值）在每一只真实产物上都逐字段相等 —— 它是**读同一份账本**。

**残余边界（诚实声明）**：`(a)` 自洽市值口径仍会因 D-10（HK 市值取期末口径）而不等；
那不是同源性问题，而是**同一份账本内部两节口径不一致**。

## D-3（中）O（注销型回购）**全链路恒为 0** —— 设计里的人工步骤从未接线

**实测状态**（`derived_metrics.py`）：

```python
# O = buyback annual average (cancellation-type only)
# Tushare does not provide repurchase purpose; default to 0.
# Phase 3 should determine cancellation amount from annual report.
rep_df = self._store.get("repurchase")
if rep_df is not None and not rep_df.empty:
    summary_rows.append(["O（年均回购金额）", "0.00 百万",
                         "默认0（无法区分注销型），Phase 3 从年报确认后填入"])
```

`o_val = 0.0` 出现在 **§17.9（:1014）· §17.11（:2053）· §17.14（`o=0.0`）** 三处；
`claims.json` 里**没有任何 O 的 claim**。

**两半要分开评价**：

- ✅ **诚实的一半**：Tushare 确实无法区分**注销型**与库存股回购，而龟龟的 O
  只认**真正减少股本**的那一类 ⇒ 默认 0 并注明，是 fail-closed 的正确姿态。
- ❌ **缺口的一半**：设计写明「Phase 3 should determine cancellation amount from annual report」，
  即把「从年报确认注销型金额」当成一个**人工/LLM 步骤**，但**这一步从未接线** ——
  代码里没有接收它的入口。所以 `turtle_core.decision.penetration_return(..., o=...)`
  的 O 项在本实现里是**死参数**。

**⚠️ 为什么不能只接一半**：screener 已支持 `o_yuan`，但若只让 screener 传非 0 而
canonical 仍发 0，就会**制造**报告与筛选的不同源 —— 正是验收①要防的事。
**必须 canonical 先接线，screener 经 claims 继承。**

**实施方案（沿用仓库既有的 sidecar 模式，与 `fx.json` / `dividend_commitment.json` 同构）**：

1. 新增 `{product_dir}/o.json`：
   ```json
   {"o_annual_mm": 123.4, "years": 5, "source": "2025年报 p.56 注销型回购",
    "confirmed_by": "human|llm", "as_of": "2026-10-14"}
   ```
2. `derived_metrics` 读它：缺文件 ⇒ **保持现状**（O=0 + 现有说明），
   有文件 ⇒ O 取该值并**在 §15 摘要行如实标注来源**（不得只写 "0.00"）。
3. 三节把 `o_val` 用同一个值（现在是三个独立的 `0.0` 字面量 —— 应提为**单点**）。
4. 新增 claim `cm17.x.o`，使 screener 能经 `CLAIM_MAP` 继承；
   `slow_layer.CLAIM_MAP` 加 `o` 字段。
5. 新增不变式/门禁：`o_annual_mm > 0` 时**必须**有 `source` 与 `confirmed_by`
   （防止 LLM 自行编一个数填进去 —— 那比 0 更糟）。

**为什么默认不改数值**：对**无 `o.json` 的全部既有产物**，行为逐字节不变 ——
所以这是一个**纯增量**能力，可安全实施。**待你确认后执行**（因为它会让填了 sidecar
的产物数值变化，进而影响短名单）。

## D-3b（**已解决**）O 不生效的根因：**10⁶ 单位错配**（百万 vs 元）

**根因**（插桩实测，一步定位）：

```
### DBG §17.11: o_val=123.4   mkt_cap=22,533,626,329.52   aa=2,126,605,006.91   q=28.0
                  ↑ 百万              ↑ 元                      ↑ 元
```

`o.json`/`cm17.11.o` 的单位是**百万**（与 §15 摘要、`UNIT_ENUM` 一致），
而 canonical 内部量纲是**元**。不换算时 O 的贡献
= `123.4 / 22,533,626,329 × 100` = **0.00000055 pct** ⇒ 效果等同"没接通"，
而 claim 却如实写着 123.4 ⇒ **产物内部自相矛盾**。

**我的排查弯路（值得记）**：我先前用**claim 值**（百万口径）反推算术，
得出「预期含 O = 2.3058，观测 1.76」的矛盾，并据此排除了"单位错"这一可能 ——
**那一步是错的**：claims 是百万口径，内部计算是元口径，**两套尺子混用才制造了假矛盾**。
插桩打印真实内部值后，一分钟就定位了。

**修法（已落地）**：三处调用点全部 `×1e6`（§17.9 `o_val`、§17.11 `o_val`、
§17.14 `pessimistic_grid(o=...)`）。实测：

| claim | 修前 | 修后 | 手算校验 |
|---|---|---|---|
| `cm17.9.base.gg` | 1.76 | **2.31** | 1.76 + 123.4/22533.63×100 = 1.76+0.5476 = 2.3076 ✓ |
| `cm17.11.aa2y.m2.gg` | 1.76 | **2.31** | 同上 ✓ |
| `cm17.14.k080.ggpess` | 1.41 | **1.95** | ✓ |

**⚠️ 这暴露了一个新的真实不同源（未解决）**：

```
报告 §17.14 GG_pess = 1.95   （O 已生效）
筛选 §17.14 GG_pess = 1.4067 （O 未生效）
```

`slow_layer` 的 `snap.o = 123400000.0`（元，**正确**）已进快照，
但 `engine.screen_one` 没把它用进 `penetration_return`。
**已重新加回 fail-closed 闸**：声明非零 O 的产物**排除出排名**（`o_not_applied`），
宁可少一只也不产出与报告不一致的数。

**已解决（2026-10-14）**。真正的原因是**同一概念存在两个字段**：

```python
# snapshot.py
o_yuan: float = 0.0     # 陈旧字段，默认恒为 0（旧注释"canonical 硬编码 0"）
o: Optional[float] = None   # 本次新增，由 `_MM_KEYS` 归一到元

# screener_consistency.py:135
gg = penetration_return(aa*adopted_k, m_pess, q, snap.o_yuan, cap)   # ← 读了恒为 0 的那个
# engine.py:200
o = snapshot.o_yuan if o_yuan is None else o_yuan                     # ← 同上
```

`load_product` 填的是 `o`，而**检查器与引擎都读 `o_yuan`（恒 0）** ⇒
日报路径（引擎 L212 会覆盖）与检查器路径取值不同，于是报出 1.735 vs 1.4067 的"不同源"。
**这不是公式问题，是两个字段的分工被违反。**

**修法**：三处改为读 `snap.o`；`o_yuan` 标注弃用（保留仅为兼容旧调用方）。
实测（00696.HK，`o.json` O=123.4 百万）：

| 口径 | 报告 | 筛选 | 判定 |
|---|---|---|---|
| (b) 产物市值 GG_pess | 1.9500 | **1.9543** | ✅ |
| (b) 产物市值 目标买入价 | 3.5700 | **3.5764** | ✅ |
| (a) 自洽市值 GG_pess | 1.9500 | 1.7386 | ❌ 纯市值口径差 = **D-10** |

1.9543 与手算 `(2126.61e6×0.8×0.2329×0.8 + 123.4e6)/22,533.63e6×100 = 1.954` **逐位吻合**。
⇒ **含 O 的公式已同源**；残余差异**全部归于 D-10**，与 O 无关。

**我的两次误判（务必牢记）**：
1. R22 我据「报告 1.95 vs 筛选 1.4067」判定"筛选侧没接 O"并加了排除闸 —— **误诊**，
   差异其实是两个字段的分工被违反。基于误诊的闸会无谓排除带 `o.json` 的股票。
2. R20 我据「量纲审计通过 + claim 值正确」就拆了闸 —— **验证了中间产物却没验证端到端效果**。
   正确顺序：**先证明"改输入会改变输出"，再拆闸。**

---

## D-3b 原始记录（保留作排查过程的证据）

**状态：未隔离。** 这是当前最高优先级的未决项，证据如下（00696.HK，`o.json` 声明 O=123.4 百万）。

### 事实 1：代码路径正确

```
L2084  o_val = self._o_input().o_annual_mm      # = 123.4（百万，与 mkt_cap 同scale）
L2090  _o_in = self._o_input()                  # 同一缓存对象
L2140  _r_def = _tc_dec.penetration_return(c, m, q_default, o_val, mkt_cap)
L2165  gg     = _tc_dec.penetration_return(aa, m, q, o_val, mkt_cap)
```
AST 确认 `o_val` 与所有 `penetration_return` 调用**在同一方法** `_compute_penetration_grid` 内；
`penetration_return` 本身对 O 生效（实测差 0.5476 pct）；签名与实参顺序正确；
`_output_path` 由 `tushare_collector.py:491` 设置，`Path(path).parent` 即产物目录。

### 事实 2：claim 正确，但数字没跟着动

```json
"cm17.11.o": 123.4          ← claim 说 O = 123.4 百万
"cm17.11.aa2y.m2.gg": 1.76  ← 但 GG 是"O=0"的值
```

反推（单位均为**百万**：AA=2126.61, M=23.2885%, Q≈20%, cap=22533.63）：

```
gg（不含 O）= 2126.61 × 0.232885 × 0.8 / 22533.63 × 100 = 1.758  ≈ 1.76  ← 观测值
gg（含 O）  = (2126.61 × 0.232885 × 0.8 + 123.4) / 22533.63 × 100 = 2.3058
```

⇒ **同一份 `claims.json` 里，声称 O=123.4 的那个 claim 与受 O 影响的 GG claim 互相矛盾。**

### 我的假设（**未验证**，仅作排查起点）

`_o_input()` 的结果被缓存在 `self._store["_o_input_cache"]`。
`_compute_factor2_inputs` 更早调用它（L423，写 §15 的 O 行）。
若**第一次**调用发生在 `_output_path` 被赋值之前，缓存里就是默认值 0，
后续 §17.9/§17.11/§17.14 全部读到 0 —— 而 claim 的 123.4 就无法解释。
**所以这个假设与"claim=123.4"矛盾**，尚未成立。

### 已排除

- ❌ 单位错（`snap.o`=123400000 元、AA 与 mkt_cap 同尺度，量纲审计通过）
- ❌ 函数不生效（直接调用证明生效，差 0.5476 pct）
- ❌ 签名/实参顺序（`(base, m_pct, q_pct, o, mkt_cap)` 正确）
- ❌ 变量作用域（AST 确认同一方法）

### 影响评估

**当前无实际危害**：所有真实产物 O=0，报告与筛选一致且同源（都取 `cm17.11.o`）。
即「O 项仍未真正接通」，而非「不同源」。**但 `cm17.11.o` claim 目前是误导性的** ——
它声称一个不参与计算的输入。在隔离之前，**不得给任何产物加 `o.json`**。

### 建议的下一步

写一个**机械对账**：从 `ct=1的 claim`（AA/M/Q/O/cap）重算 GG，与登记的 GG claim 比对，
不一致即 FAIL。这正是 `numeric_audit` / `turtle_core_selfcheck` 的形态，
能一次性把这类"claim 与数字脱钩"的问题钉死 —— 比继续读代码猜测有效得多。

## D-14（运营级）A 股常备池 59% 被上游门禁阻断 —— 两个数据缺口

**实测**（池 100 只，61 只已采集）：

```
可交付 25 只 | 上游门禁阻断 36 只
阻断原因分布: I9 × 24  ·  C2 × 23  ·  I2 × 1  ·  I7 × 1
```

### I9 × 24 —— 包内「股数」有两种口径（与 D-10 同源）

`601166 兴业银行` 的 I9 证据：

```
路径A 市值÷价         = 384,105.99/18.15 = 21,162.86 百万股   ← 当日口径（daily_basic）
路径B 权益÷每股净资产 = 23,941.21 百万股（2026H1）              ← 期末口径
相对差 11.60%
§1 股本 = 21,162.86 百万股（**降级值：反推**）
```

**I9 不是假阳性** —— 它正确地说「我无法判断哪个股数是对的，请给我申报值」。
`bootstrap` 未传 `--shares` ⇒ 每只都落到反推降级值 ⇒ I9 必 FAIL。
**修法（框架已在证据里写明）**：传年报已发行股数（`--shares` / `{output_dir}/shares.json`）。

> 与 D-10 的关系：**同一个病根** —— 「当日口径」与「期末口径」在同一份产物里混用。
> D-10 表现在市值（§17.9 vs §17.11/§17.14），I9 表现在股数。**修一个不会自动修另一个**，
> 但正确的方向是同一个：**明确每个量属于哪个日期口径**。

### C2 × 23 —— 债务构成不可核验或声明行全空

`field_coverage` 的 C2：每一类债务都要有自己的 §4 行，且**声明了却在所有年份都为空**的行是 FAIL ——
那是「映射标签没命中上游字段」的特征（真实案例：`bond_payable` 映射到 `应付票据(非流动)`
而上游是 `应付债券` ⇒ 20.29 亿债券从未落地，有息负债被低估）。这是**上游字段映射缺陷**，
需要在采集层修。

### 运营结论

**在修掉这两个缺口之前，A 股池无法产出「未降级」的短名单。**
这不是 screener 的问题 —— 门禁在正确地阻止使用有缺陷的包。
`--allow-degraded` 可显式放行并全程披露，但**不应成为常态**（那会让门禁形同虚设）。

## D-15（**重大**）I9 路径 B 把两个**不可通约**的权益口径相除 ⇒ 误阻断 31 只

### 现象

常备池 100 只中，**31 只被阻断级不变式 I9 挡下**（占已采集的 30%），
行业分布直指问题：

```
银行 10 · 证券 7 · 火力发电 5 · 建筑工程 3 · 家居用品 2 · 其他 4
```

**前四类（25/31）正是「其他权益工具」（优先股 / 永续债）的重度发行人。**

### 证实的根因（`601166 兴业银行` 实测）

```
平衡表申报股数 (balancesheet.total_share)      = 21,163.00 百万股   ← 权威
daily_basic.total_share                        = 21,162.86 百万股   ← 与申报值一致（差 0.0007%）✓
I9 路径A 市值÷价                                = 21,162.86 百万股   ✓ 与申报值一致
I9 路径B 权益÷每股净资产                        = 23,941.21 百万股   ✗ 偏离 +13.1%

归母权益 948,790 百万元 ÷ 申报股数 21,163 = 44.83 元/股
Tushare `fina_indicator.bps`             = 39.63 元/股
产物 §17.8 ② 每股净资产                    = 42.61 元/股
差额 948,790 − 42.61×21,163 = 47,040 百万元 ≈ 470 亿
                            ↑ 正是兴业银行的优先股 + 永续债
```

**路径 B 是 `归母权益（**含**其他权益工具） ÷ 每股净资产（**不含**其他权益工具）`** ——
两个不同权益基数的量相除，**商根本不是股数**。

### 性质

**这是 I9 自身的规格缺陷，不是数据缺陷。** 它把"两个独立路径必须一致"这个正确原则，
实现成了对不可通约量的比较。后果是**把一批完全正常的银行/券商/电力股挡在交付之外** ——
与 D-11（WARN 被当 FAIL 阻断全部 A 股）是同一类错误：**假阳性阻断**。

### 修法（下一轮执行，三者取一，推荐 B）

- **A**：路径 B 改用 `归母权益 − 其他权益工具` 作为分子（与 BPS 同基数）。
  要求 §4 有「其他权益工具」行；缺失时回落到 B。
- **B（推荐）**：路径 B 直接改用 **`balancesheet.total_share` 申报股数**，
  与路径 A（市值÷价）构成**真正独立**的两条路径，彻底绕开 BPS 的权益基数歧义。
  实测该字段可得（格力 5,601,406,000 股、兴业 21,163,000,000 股，均为期末申报值）。
- **C**：无法取得申报股数时，判 `UNEVALUATED`（而非 FAIL）—— 不知道 ≠ 不一致。

### 附带发现（另一个单位标签缺陷）

产物 §4 有 `| 总股本 (万股) | 211.63 |` —— 而真实值是 **21,163 百万股 = 211.63 亿股**。
**单位标签写错（亿股标成万股）**，差 10⁴ 倍。与 D-2（CNY 印在港元表头下）、
D-9（`no_data` 冒充 `no_rf`）同属"标签与实现不符"。
**I9 的修法 B 若要用申报值，须先修这个标签。**

## 附：等价回归的可复现配方（本次实测得出）

`claims.json` 的逐字段复现**必须**把输入 sidecar 一并复制 —— 否则 M 锚定会走不同分支，
GG 可差 2 倍以上而不报任何错。

```bash
D=output/00696_中国民航信息网络           # 任取一个完整产物目录
E=/tmp/eq/00696_中国民航信息网络
mkdir -p "$E"
cp "$D"/fx.json "$D"/dividend_commitment.json "$E"/    # ← 输入 sidecar，缺一不可
.venv/bin/python scripts/tushare_collector.py --code 00696.HK \
  --output "$E"/data_pack_market.md --rf 1.67
# 然后逐字段比对 "$E"/claims.json 与 "$D"/claims.json
```

**实测教训（三次迭代）**：
1. 缺 `fx.json` ⇒ `reporting_currency=null`、`unit_label=百万·币种未确认`，**GG 从 3.85 腰斩到 1.76**；
2. 补 `fx.json` 但缺 `dividend_commitment.json` ⇒ M 从「§17.1 3年均值 51.04%」变成「法2 23.29%」，
   GG 再变一次（比值恰为 51.04/23.29 = 2.19）；
3. 两个 sidecar 都补齐后，余下的值差异来自 `derived_metrics.py` 的非提交改动
   （工作树 +1454/−535，mtime 晚于既有产物）—— 即**既有产物早于当前代码**。

⇒ 结论：等价基准必须以「**当前代码 + 完整 sidecar** 的一次全新采集」为准，
不能拿历史产物当 golden。

---

## 抽取进度

| 模块 | 状态 | 测试 |
|---|---|---|
| `turtle_core/__init__.py` | ✅ | — |
| `turtle_core/units.py`（金额/币种单一换算入口） | ✅ | 16 |
| `turtle_core/floors.py`（①②③④⑤ + 综合底价/参考公允价 + 溢价档位） | ✅ | 36 |
| `turtle_core/decision.py`（II/Q/R/GG/HH/GG−II/目标买入价/门槛价格/AA/M） | ✅ | 59 |
| `turtle_core/pessimistic.py`（AA_pess/GG_pess/R_pess/安全边际/仓位矩阵） | ✅ | 46 |
| `turtle_core/reasons.py`（受控原因码，fail-closed） | ✅ | 12 |
| `turtle_core/snapshot.py`（慢层快照，原子写 + 版本化 + 过期判定） | ✅ | 19 |
| `turtle_core/rf.py`（`rf.json` 契约；`yc_cb` 无权限的唯一外部输入） | ✅ | 27 |
| `scripts/turtle_core_selfcheck.py`（账本 ↔ turtle_core 同源自检） | ✅ | 5 |
| **turtle_core 小计** | **1501 行 / 8 模块** | **220** |
| `turtle_screener/prices.py`（直连 Yahoo chart；8 并发实测 8/8 成功 0.51s） | ✅ | 28 |
| `turtle_screener/calendar.py`（三市场日历 + 就绪判定；港股自建） | ✅ | 27 |
| `turtle_screener/universe.py`（三市场宇宙 + 美股 classify 洁净度 + offset 翻页） | ✅ | 23 |
| `turtle_screener/engine.py`（每日快层；排序键 = 安全边际；含 D-10 一致性检查） | ✅ | 41 |
| `turtle_screener/manifest.py`（shortlist/run_manifest/excluded + 刷新策略 + 门禁） | ✅ | 30 |
| `turtle_screener/slow_layer.py`（**从已审计产物**读快照 ⇒ 与报告天然同源） | ✅ | 22 |
| `scripts/screener_daily.py`（单条 CLI，串起全链路） | ✅ | 9（集成） |
| **turtle_screener 小计** | **~1550 行 / 6 模块** | **180** |
| `scripts/gate_policy.py` 扩展（选股器 3 个全局阻断码） | ✅ | — |
| P4 集成：`strategies/screener/coordinator.md` · `.agents/skills/turtle-screener/SKILL.md`（已注册）· `AGENTS.md` Workflow 6 | ✅ | — |
| P4 调度：两条 `schedule_create`（A+H `weekly 19:00 CST` [1-5] · 美股 `weekly 06:30 CST` [2-6]） | ✅ | — |
| `output/rf.json`（Rf=1.665%，10年期国债 2026-10-12，新华财经） | ✅ | — |

全量测试：**1907 passed, 3 skipped**（改动未破坏任何既有测试）。

### 三条验收标准的实测状态

| 验收 | 状态 |
|---|---|
| ① 同源一致性（screener ↔ 报告 §17 逐字段） | **检查器已建成**：`scripts/screener_consistency.py`。实测 00696：**(b) 产物市值下逐字段相等 ⇒ 公式同源 ✅**；**(a) 自洽市值下不等 ⇒ 归因 D-10（价格日期不同源）**，exit 1 并打印完整归因。旧产物缺字段则老实报 `UNVERIFIABLE`（exit 2，不凑数） |
| ② 幂等（同一 trade_date 重跑逐字节一致） | ✅ **已实测**：两次运行 `shortlist.json`/CSV 的 MD5 相同，日志一致，目录未追加 |
| ③ 门禁（缺 Rf/fx/快照 ⇒ ABORT 或 reason code，不出残缺清单） | ✅ **已实测**：Rf 缺失 ⇒ exit 1，只写 `ABORTED.txt` + manifest；逐股缺口 ⇒ `excluded.json` + 原因码，不阻断 |

### 本轮对 `numeric_audit` 的修正（findings D-1）

X 组旧版**无条件断言** `§17.9 门槛价格 == §17.14 目标买入价`，其前提
（`M_rec == M_pess` 且 分母同源）在真实产物里经常不成立
⇒ 在**正确**产物上报 critical 并**阻断交付**。改为**前提三态守卫**：

| 前提 | 处置 |
|---|---|
| `True`（可证明成立） | 断言相等；不等 ⇒ `MISMATCH/critical` |
| `False`（**已证明**不成立） | `UNVERIFIABLE`（披露级）+ 点名缺哪个前提 |
| `None`（算不了） | **仍按包的自身声明断言相等** —— 算不了不等于不成立，不得静默放过 |

> 只有**证明**前提不成立时才降级。这是本轮的关键取舍：早先写成
> "算不了就 UNVERIFIABLE"，会静默放过真缺陷。

**新增 X3**：§17.9 正文声明的 M 必须等于 `M_rec`。实测 06862 抓到
**包内相邻两行自相矛盾**（声明 `M = 89.70%（§17.1 3年均值）` vs `M_rec = 81.11%`）。

**实测三态结果**：

```
00696（当前代码，正确）: X1=UNVERIFIABLE  X2=UNVERIFIABLE  X3=MATCH        ← 无误报阻断
06862（旧产物，真缺陷）: X1=MISMATCH/crit  X2=MISMATCH/crit  X3=MISMATCH/crit ← 检测保全
```

**⚠️ 一次错误与撤回**：我曾把 D-10 记为「股本口径降级」并据此新增阻断级不变式
**I11**（`市值 ≡ 股本 × 现价`）。核算 00696 的数字后发现该"恒等式"在价格变动后
**本就不该成立**（差额恰好等于 `8.98/8.14 = 1.1032`），做成包级不变式会在
**几乎所有港股包**上系统性误报。I11 已从 `invariants.py` / `gate_policy.py` / 测试中
全部撤回，D-10 的定性已按核算结果重写。

**端到端冒烟（真实数据）已通过**：两个真实产物（00696.HK / 06862.HK）的 `claims.json`+`fx.json`
→ 慢层快照 → 真实 Yahoo 价格 → 引擎 → 清单落盘。结果符合预期且 fail-closed：

```
00696.HK  AA 存在，GG_pess=1.238% < II=5.0%  → below_threshold（算得出来但不达标）
06862.HK  旧产物缺 AA_2y                     → no_cashflow，GG_pess=None（**不是 0**）
deliverable=True  blocking=0  data_gap_ratio=0.5
```

**⚠️ 端到端冒烟暴露的依赖**：00696 的 screener GG_pess=1.238% 与产物 §17.14 的 1.41% 不等，
偏差来自 D-10（产物股本 3228.18 vs 自洽 2926.21 百万股 = +10.32%）。
→ **验收标准第 1 条在 D-10 修好之前必然失败**。已加 `check_market_cap_consistency()`
把这条依赖变成机械检查（`engine.py`）。

**验收标准第 1 条的机械证明已达成**：对 00696.HK 的**全新采集**产物，
`turtle_core_selfcheck.py` 从 claim 账本复现了**全部 20 项** canonical 决策数字
（R 三法、GG 三变体、GG_pess 七档、10 个 `*.tbp` 的隐含现价散度仅 0.32%）：

```
检查 20 项，复现 20 项，不可复现 0 项（容差 ±0.015）
```

## 尚未开始

- **慢层构建器**：把采集器的产物固化成 `financial_snapshot.json`（当前冒烟测试是从
  `claims.json` 临时构造的，生产路径应由 collector 侧产出）
- 池管理（常备池 ≤100 只的入选口径与 bootstrap）
- 单条 CLI 把上面串起来（`scripts/screener_daily.py`）
- `derived_metrics` 逐节迁到 turtle_core（P1c）—— 待在飞改动落地
- Workflow 6 + 技能 + coordinator + 两条定时（P4）
- **修 D-1（§17.9 门槛价格分母/币种）与 D-10（股本↔市值不等式）** —— 后者是
  验收标准第 1 条的前置
