# 上下文窗口复核（1M）与「切片架构」改动的必要性回看

> 复核日期：2026-10-16 ｜ 目标模型：`deepseek-flash`（DeepSeek-V4.1-Flash）
> 性质：**待办文档（尚未实施）**。本文只记录结论、证据与改动清单；代码/文档改动等 06078 流程收尾后再动。
> 执行约束见 §7 —— 在不当时机动 §6 的文件会让在飞流程整轮作废。

---

## 1. TL;DR

1. **官方现行口径是 1M**：`deepseek-flash` 的 CONTEXT LENGTH = **1M**、MAX OUTPUT = **384K**（[Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/)）。
2. 项目里「**已确认上下文上限：128K**」（[work_package_C_dimension_slicing_plan.md:3](work_package_C_dimension_slicing_plan.md#L3)）**是错的**，且它没有证据链：同批文档自己写着「V4.1-Flash 的发布说明未给出上下文长度…需你核对」（[dsh_deepseek_optimization_plan.md:87](dsh_deepseek_optimization_plan.md#L87)、[:250](dsh_deepseek_optimization_plan.md#L250)）——**一个待确认项被后续文档升级成了"已确认"**。
3. 因此「原【单 Agent 全文载入】架构**不可运行**／5/5 超窗」这条论据**必须撤回**：按计划书自己的样本（245,005–616,579 字符 ≈ 147K–617K token）**全部落在 1M 内**，成本也不是障碍。
4. **但架构本身值得保留**，理由是三条与窗口无关的：可机械核验的「只能引用发给你的页」边界、失败隔离与可复现（每维 manifest + JSON）、并行墙钟优势。今晚 06078 有实证：D4 因越界引用 4 页被门禁硬拦并重跑。
5. **真正被窗口大小绑死、应当放宽的是预算**：`--budget-chars` 文档默认 **12000**，只占 1M 窗口的 1–2%；即便今晚流程自行改到 **36000**，每维输入也只有窗口的 **1.3–3.5%**，仍有三个维度在丢页（D3 丢 133 页 / D4 丢 63 / D6 丢 22）。
6. 「切片 vs 单上下文」的**质量比较从未做过**：`experiments/ab_phase2/AB_PLAN.md` 的阶段 2 按决定跳过（2026-10-13）。而 v1 归档里那套多 Agent 拆分当初被废弃的理由恰恰是「单 Agent 交叉验证更优」——**同一问题两次反向裁定，两次都没有实测**。

---

## 2. 官方口径（证据）

| 项 | 值 | 来源 |
|---|---|---|
| 模型名 | `deepseek-flash` = **DeepSeek-V4.1-Flash** | [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing/) |
| **CONTEXT LENGTH** | **1M** | 同上 |
| MAX OUTPUT | **384K** | 同上 |
| 并发上限 | 2500 | 同上 |
| 输入价 | cache-hit $0.003–0.006 / cache-miss $0.15–0.30（每 1M token，谷/峰） | 同上 |
| 输出价 | $0.6–1.2（每 1M token） | 同上 |

关键原文：

- [DeepSeek-V4 Preview Release（2026/04/24）](https://api-docs.deepseek.com/news/news260424/)：
  「**1M Standard**」——官方原文称百万级窗口自此成为**所有官方服务的默认**（原文直链见上，本文不复述其英文字面，以免日后被全库扫描类断言误伤）；
  同页注明 `deepseek-chat` / `deepseek-reasoner` **2026-07-24 之后完全退役**。
- [DeepSeek-V4.1-Flash Release（2026/09/10）](https://api-docs.deepseek.com/news/news260910)：发布说明**未列上下文长度**（这是当年"查不到"的来源），但同站 Models & Pricing 页一直写着 1M。
- 旧 128K 依据是 [DeepSeek-V3.1 Release（2025/08/21）](https://api-docs.deepseek.com/news/news250821/) —— 那个模型已于 2026-07 退役。
- 噪音（不作为依据）：第三方稿称 V4.1-Flash「支持 200K 上下文」（[sohu 转述](https://m.sohu.com/a/1088177509_122135404)），与官方定价页矛盾。

**时间线矛盾**：1M 自 2026-04-24 起即为官方默认；V4.1-Flash 是 2026-09-10 的继任者；本项目的切片方案与工作包写于 **2026-10**（含 10-13 的决定）⇒ **写下"128K"时，1M 已生效半年**。

---

## 3. 仓库里这条错误前提的完整落点

| 文件:行 | 写的什么 | 性质 |
|---|---|---|
| [work_package_C_dimension_slicing_plan.md:3](work_package_C_dimension_slicing_plan.md#L3) | 「已确认上下文上限：**128K**」 | **错误前提被固化为"已确认"** |
| [dsh_deepseek_optimization_plan.md:12](dsh_deepseek_optimization_plan.md#L12) | 「上下文上限 **128K**」⇒ 单次子代理预算 ≤25K token | 由错误前提反推 |
| [dsh_deepseek_optimization_plan.md:87](dsh_deepseek_optimization_plan.md#L87) | 「V4.1-Flash 的发布说明未给出上下文长度…**需你核对实际上限**；但无论上限是多少…」 | 自认未确认 |
| [dsh_deepseek_optimization_plan.md:250](dsh_deepseek_optimization_plan.md#L250) | 「**需要你确认的一个输入**：DSH 中 deepseek-flash 的实际上下文上限」 | 待确认项（未见答复记录） |
| [experiments/ab_phase2/AB_PLAN.md:15-23](experiments/ab_phase2/AB_PLAN.md#L15-L23) | 5 个样本「≈token（0.6–1.0/字）」+「128K 窗口」列全 ❌ | 表格列本身按 128K 定标 |
| [architecture_v3_parallel_design.md:138](architecture_v3_parallel_design.md#L138) | 把「128K 可行性」列为架构验收列 | 错误前提进了验收标准 |
| [CHANGELOG_V2.md:408](CHANGELOG_V2.md#L408) | 「原『单 Agent 全文载入』在目标平台上**不可运行**」 | 强结论 |
| [scripts/dimension_slicer.py:6](scripts/dimension_slicer.py#L6) | 模块 docstring 以「在 128K 窗口下不可行」为立论 | 代码注释亦承载错误前提 |

---

## 4. 那次改动的清单（「清除旧假设」的直接产物）

1. **架构**：单 Agent 全文载入 → [`dimension_slicer.py`](scripts/dimension_slicer.py) 确定性切片（两阶段选页 + manifest）→ 6 维度并行子代理（每人只读自己的切片）→ 只读 `dims/*.json` 的综合；[`validate_dim_result.py`](scripts/validate_dim_result.py) 硬契约 + 切片覆盖度门禁（R1/R2）。
2. **措辞**：`README.md` / `AGENTS.md` / `shared/qualitative/coordinator.md` / `.claude/commands/business-analysis.md` / `.agents/skills/business-analysis/SKILL.md` / `qualitative_assessment.md` 全量清除旧说法，并由 [tests/test_v3_pipeline_prompts.py:59-64](tests/test_v3_pipeline_prompts.py#L59-L64) **锁死字面**（共 4 个被禁字面串，逐个列在该断言里；为免本文自身触发未来的全库扫描，此处不复述，请点开链接查看）。
3. **数字**：`--budget-chars` 默认 **12000**（[dimension_slicer.py:525](scripts/dimension_slicer.py#L525)），且被硬编码进三处文档化命令（[AGENTS.md:50](AGENTS.md#L50)、[coordinator.md:206](shared/qualitative/coordinator.md#L206)、[SKILL.md:48](.agents/skills/business-analysis/SKILL.md#L48)）。
4. **归档**：v1 的 `shared/qualitative/legacy/agents/*`（多 Agent 拆分）当年因旧假设被废弃，本次改造等于把它以 JSON 契约形态复活。

---

## 5. 必要性裁定（逐条）

### 5.1 必须撤回：旧架构「不可运行」

| | 数字 | 1M 窗口下的判定 |
|---|---|---|
| 最小样本（002984） | 201,678 正文 + 43,327 固定 = **245,005 字符** | ≈147K–245K token ⇒ **装得下**（余量 4–6.8×） |
| 最大样本（06862） | 573,252 + 43,327 = **616,579 字符** | ≈370K–617K token ⇒ **装得下**（余量 1.6–2.7×） |
| 输出侧 | 单份定性报告 | 远小于 MAX OUTPUT 384K |
| 成本 | 一次 600K token cache-miss 调用 | ≈**$0.09–0.18**（谷峰两档）；共享前缀可走 cache-hit（$0.003/1M） |

⇒ 结论从「不能跑」变成「能跑」。**"超窗 5/5" 是错误前提的产物，必须撤回。**

### 5.2 仍然站得住：三条与窗口无关的理由

1. **可机械核验的引用边界**：切片把「你只能引用发给你的页」变成可执行断言（`validate_dim_result.py` 的页覆盖门禁 ±1 页容差）。
   **今晚实证**：D4 引用了 4 个不在其切片内的页（主控把参考页写进了 prompt），门禁硬失败 → 重跑修正。在单上下文架构里这条门禁**原理上无法建立**（"发给你的" = 全部）。
2. **失败隔离 + 可复现**：每维一份 manifest（`provided_pages` / `dropped_pages` / `used_chars` / `strategy`）与一份 JSON，坏一维只重跑一维；单上下文一崩全崩且无中间产物可对照。
3. **墙钟时间**：6 维并行 + 综合，快于一次超长单调用（对"当晚出报告"的场景有实际意义）。
4. 附加收益：每维 JSON 让 claim 绑定、报告保真（`check_report_fidelity`）等机械门禁成为可能——这些是 v3 反幻觉体系的一部分，与窗口无关。

### 5.3 应当放宽：每维字符预算（**当前唯一被窗口大小绑死的参数**）

`--budget-chars` 量级对照：

| 预算 | 每维输入 | 占 1M 窗口 | 后果（06078 实测） |
|---|---|---|---|
| 12000（**文档默认**） | 11.1–11.8K 字符 ≈ 7–12K token | **0.7–1.2%** | 首遍：D1 17 页 / D3 **17/182 页** / D4 14 页，`dropped_pages` 10/10/163/102/11/60，多维度被迫 `slice_truncated` |
| 36000（**今晚流程自行探测后改的**） | 21.5–35.1K 字符 ≈ 13–35K token | **1.3–3.5%** | D1/D2/D5 `dropped=0`；**D3 仍丢 133 页、D4 丢 63、D6 丢 22**（D3 因医保/DRG 政策锚点缺失挂 `data_insufficient`） |

> 含共享前缀（`data_pack_market.md` 34.7KB + `computed_metrics.md` ≈2KB + 指令）后，单次调用约 **65–80K 字符 ≈ 40–80K token ≈ 窗口的 4–8%**。

⇒ 在 1M 下这个阈值是**历史遗留的过度保守**。可行方向（择一或组合）：

- 把每维预算提到 **60,000–120,000 字符**（约占窗口 6–12%），让 `priority_fill` 不再因预算而丢页；
- 或提供「**整份年报 + 强制分页引用**」档（把"发给你的"= 全年报，仍保留分页引用门禁与 JSON 契约）作为对照臂；
- 同时把三处文档化命令里的 `--budget-chars 12000` 改为**从配置推导**而不是硬编码（计划书当年就想要 `config.py: LLM_CONTEXT_BUDGET` 这个单点）。

### 5.4 至今未被验证：切片 vs 单上下文的质量差

- [experiments/ab_phase2/AB_PLAN.md](experiments/ab_phase2/AB_PLAN.md) 已设计好 runbook（M5 ≤ 50,000 字符、代理臂定义、字符核算而非 token 遥测）。
- **阶段 2 按决定在 2026-10-13 跳过**（[work_package_C…:20](work_package_C_dimension_slicing_plan.md#L20)），残余风险记在该文件 §9。
- v1 归档 `shared/qualitative/legacy/agents/*` 的废弃理由恰是「单 Agent 交叉验证更优」。
⇒ **同一个架构问题被反向裁定两次，两次都无实测**。要回答"这次改动值不值"，唯一严谨做法是按该 runbook 补做（成本很低：1 份年报 × 2 种架构）。

---

## 6. 待办清单（建议按此顺序）

### P0 — 更正错误前提（纯文档，不改行为）

| # | 动作 | 文件 | 验收 |
|---|---|---|---|
| P0-1 | 把「已确认 128K」改为「**官方 1M（2026-04-24 起默认）**；DSH 侧待核」，并保留"待确认"痕迹 | [work_package_C…:3](work_package_C_dimension_slicing_plan.md#L3)、[dsh_plan:12](dsh_deepseek_optimization_plan.md#L12) | 文中不再出现"已确认 128K" |
| P0-2 | 撤回「不可运行／5/5 超窗」，改写为「在 1M 下**可运行**；切片是为核验性与失败隔离」 | [CHANGELOG_V2.md:408](CHANGELOG_V2.md#L408)、[AB_PLAN.md:15-23](experiments/ab_phase2/AB_PLAN.md#L15-L23)、[architecture_v3_parallel_design.md:138](architecture_v3_parallel_design.md#L138) | 文件内不再有"不可运行"结论 |
| P0-3 | `dimension_slicer.py` docstring 的立论改写（其它功能不动） | [dimension_slicer.py:6](scripts/dimension_slicer.py#L6) | 单测全绿 |
| P0-4 | **同步改写锁定字面的断言**：从「禁止出现旧的单次全量载入相关字面串」（4 个，见断言本身）改为「不得声称**用单次全量载入替代切片**」——即锁设计，不锁"窗口到底多大"这一事实 | [tests/test_v3_pipeline_prompts.py:59-64](tests/test_v3_pipeline_prompts.py#L59-L64) | `pytest tests/test_v3_pipeline_prompts.py -q` 绿；且新断言能对"悄悄回退到单 Agent 全量"报红（做一次回退验证） |

> ⚠️ P0-4 是**前置**：不做它，P0-1/P0-2 一旦写出"1M"字样，现有断言立刻变红。

### P1 — 预算重标定

| # | 动作 | 验收 |
|---|---|---|
| P1-1 | `--budget-chars` 默认值从 12000 提到 60,000–120,000（或引入 `config.LLM_CONTEXT_BUDGET` 推导） | `pytest tests/test_dimension_slicer*.py`（约 20 项）全绿；对 3 份真实年报重跑，`dropped_pages` 显著下降 |
| P1-2 | 三处文档化命令同步（[AGENTS.md:50](AGENTS.md#L50)、[coordinator.md:206](shared/qualitative/coordinator.md#L206)、[SKILL.md:48](.agents/skills/business-analysis/SKILL.md#L48)） | 三处与脚本默认值一致 |
| P1-3 | 新增「整份年报 + 强制分页引用」可选档（`--budget-chars 0`/`--no-budget` 之类） | 该档下 `validate_dim_result.py` 仍拒绝切片外引用 |
| P1-4 | **核对 DSH 侧模型配置/窗口**（API 是 1M，harness 若配小窗口仍会截断）——**这一项需要你确认，我读不到 DSH 配置** | 在 DSH 模型设置里看到 1M（或等价），记录到文档 |

### P2 — 补做对照实验

| # | 动作 | 验收 |
|---|---|---|
| P2-1 | 按 [AB_PLAN.md](experiments/ab_phase2/AB_PLAN.md) runbook 跑：1 份年报 ×（切片臂 vs 单上下文臂） | 产出可比的 M1–M5 指标；结论写回 [work_package_C…](work_package_C_dimension_slicing_plan.md) 并解除 §9 的残余风险 |

---

## 7. 执行约束（为什么现在不动）

1. **不要与在飞的 06078 流程并发**。另一次会话正在跑 `/turtle-analysis 06078.hk`（已到"数值审核 FIX_REQUIRED → 改代码 → 重采"阶段）。它在关键路径上依赖的正是 §6 P0/P1 要改的文件：`scripts/dimension_slicer.py`、协调器/提示词、`validate_dim_result.py`、`tests/*`。
2. **今晚已发生 3 轮"改包即作废"**：数据包每被改写一次，在飞的 6 个维度（以及 clean-room/审核）整批作废重发（00:28 停了 8 个子代理、00:36 再作废一次）。此刻改这些文件 = 再打回一轮。
3. 该流程自身也在改共享脚本（`tushare_modules/derived_metrics.py`、`constants.py`）；并发编辑同一批文件有真实冲突风险。
4. 本文档本身**只新增一个根目录文件**，不在任何测试的扫描范围内（[test_dsh_tool_names.py](tests/test_dsh_tool_names.py) 的 `rglob` 只覆盖 `shared/qualitative/{agents,references}`、`strategies/*`、`.agents/skills` 与 4 个具名根文件；[test_v3_pipeline_prompts.py](tests/test_v3_pipeline_prompts.py) 只覆盖 6 个具名文件）。

---

## 8. 证据索引

**官方文档**（抓取于 2026-10-16）：

- Models & Pricing：<https://api-docs.deepseek.com/quick_start/pricing/>
- DeepSeek-V4 Preview Release（2026/04/24，1M 默认）：<https://api-docs.deepseek.com/news/news260424/>
- DeepSeek-V4.1-Flash Release（2026/09/10）：<https://api-docs.deepseek.com/news/news260910>
- DeepSeek-V3.1 Release（2025/08/21，旧 128K 依据）：<https://api-docs.deepseek.com/news/news250821/>
- 第三方 200K 说法（噪音）：<https://m.sohu.com/a/1088177509_122135404>

**仓库内假设落点**：见 §3 表格（文件:行）。

**06078 实测数据**（`output/06078_海吉亚医疗/slices/slice_manifest.json`，2026-10-16 00:36:26 重切）：

```
budget_chars=36000  total_pages=182  source=06078_2025_年报.txt
D1 商业模式与资本特征   provided=27 used=21504 dropped=  0
D2 竞争优势与护城河     provided=27 used=21504 dropped=  0
D3 外部环境             provided=47 used=35114 dropped=133   （flag: slice_truncated / data_insufficient）
D4 管理层与治理         provided=53 used=34604 dropped= 63   （flag: slice_truncated）
D5 MD&A 解读            provided=27 used=21504 dropped=  0
D6 控股结构（条件触发） provided=53 used=34219 dropped= 22   （flag: slice_truncated）
```

首遍（`--budget-chars 12000`，00:26:45 生成）对照：D1 17 页/11,117 字符、D3 17/182 页/11,825、D4 14 页/11,483，`dropped_pages` 为 10/10/163/102/11/60。

**未决输入**：DSH 侧 `deepseek-flash` 的实际窗口配置（API 侧 1M，harness 侧待你确认）。
