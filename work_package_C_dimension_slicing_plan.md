# 包 2 方案（最终版）：平台原生入口 + 上下文切片架构

> 目标平台：DeepSeek Harness ｜ 模型：`deepseek-flash`（V4.1-Flash）｜ 已确认上下文上限：**128K**
> 本文件为方案 + 可行性验证报告。**阶段 0 的样板（turtle-analysis skill）已随平台去耦完成**；
> 切片器与并行改造尚未开工。原型与实验产物在 `/tmp`，未进入仓库。

---

## 0. 一页结论

包 2 现在是**三件事**（比初版多了一件「入口移植」，因为平台去耦遗留了 A2 项）：

| # | 子项 | 状态 |
|---|---|---|
| **2.0** | **入口移植**：4 个 Claude slash command → `.agents/skills/*/SKILL.md` | ✅ **已完成**（5 个 skill：tushare + business-analysis / valuation / turtle-analysis / download-annual-report，均 DSH 实测可发现，`.claude/skills/*` 软链保证单一来源） |
| **2.1** | **确定性切片器** `scripts/dimension_slicer.py` + manifest（两阶段选页） | ✅ **已完成**（20 项测试；三份真实年报实测：6/6 维度落在预算内，填充率 83–99.5%） |
| **2.2** | **维度并行子代理 + 综合**，替换「1M 单 Agent」 | ✅ **已完成**（coordinator/command/AGENTS/skill/assessment 全部改写；`validate_dim_result.py` 硬校验；v3 守卫测试 12 项） |
| **2.3** | **清除 1M 假设**的所有措辞（含 README/AGENTS 同步） | ✅ **已完成**（活跃范围内 `1M context` / `Mode A` / `单 Agent 全量分析` 全部为 0 命中，由测试锁定） |

> **阶段 2（A/B 验收）按用户决定跳过**（2026-10-13）。`experiments/ab_phase2/AB_PLAN.md` 保留为
> 已设计但未执行的方案；若日后需要量化验证，可直接按该 runbook 执行。跳过带来的残余风险已记入其 §9。

**关键可行性结论**

| 维度 | 结论 |
|---|---|
| 上下文预算 | ✅ **已实测**（含一次真实端到端运行）：每维度源码切片填 12,000 字符可达 **93–100%** 填充（175/203/286 页）；含共享前缀后单次调用 **43–45K 字符**（≈26–45K token，128K 窗口余量 2.8–4.9×）。⚠️ 本文早期版本把共享前缀记为 10,785 字符，**漏计了 `data_pack_market.md`(17.0K) 与 `computed_metrics.md`(2.0K)**，故 “≤26K” 的旧结论有误，见 §4.3 的更正 |
| 视觉路径 | ✅ **已验证**：46 个单元格值读取 **0 错误 0 幻觉**，并**正确拒答被裁掉的表格** |
| 选页质量 | ⚠️ **已实测暴露问题并已定策**：四版原型对照见 §5；结论是必须「先定位章节、再段内切分」 |
| 入口形态 | ✅ 路径已确认：DSH 从 `.agents/skills/` 发现技能且**热加载**（turtle-analysis 新建后立即出现在技能目录） |

**实测收益**：单上下文峰值 **245K–617K → 43–45K 字符**（相对最小样本 ↓81.5%、相对最大样本 ↓92.6%）；总输入分散到 7 个独立上下文，且 6 个维度代理的共享前缀逐字节相同（可吃 KV cache）。

---

## 1. 为什么必须改（实测数字）

`/business-analysis` Step 2 现状是「单 Agent + 全文载入」：

| 组成 | 字符数（实测） |
|---|---|
| 6 个参考文件 + `data_pack_market.md` + `computed_metrics.md` | 43,327 |
| 年报正文（5 份真实样本） | 201,678 ～ 573,252 |
| **单上下文峰值** | **245,005 ～ 616,579 字符** |

最小样本已超 128K token 量级，最大样本约 4–5 倍。目标形态：**7 个独立上下文（6 维度 + 1 综合），单上下文 43–45K 字符**（实测值，仍是旧架构的 1/5–1/14）。

---

## 2. 子项 2.0：入口移植（并入自平台去耦）

**现状**：4 个 slash command 仍是 Claude 格式（含 `$ARGUMENTS` 10 处），DSH 无法调用；`AGENTS.md` 提供了「读 coordinator」的手工路径，但入口形态未平台化。

**目标**：每个工作流一个 DSH skill，`turtle-analysis` 已完成的文件即为模板：

```
.agents/skills/
├── tushare/SKILL.md                 ✅ 已完成
├── turtle-analysis/SKILL.md         ✅ 已完成（DSH 实测已发现）
├── business-analysis/SKILL.md       ⬜ 待做（源：.claude/commands/business-analysis.md + shared/qualitative/coordinator.md）
├── valuation/SKILL.md               ⬜ 待做
└── download-annual-report/SKILL.md  ⬜ 待做
```

**模板要素**（照 `turtle-analysis` 的写法）
- YAML front-matter：`name` + 多行 `description`（含 "Use when:"，DSH 技能目录直接展示这段）
- 正文说明 **DSH 无 slash command**，指向 coordinator markdown
- 前置条件、逐步命令（`.venv/bin/python …`）、输出文件名
- 不出现任何 Claude Code 工具名（守卫测试会强制）

**配套**：`.claude/skills/<name>` 保持**符号链接**指向 `.agents/skills/<name>`（单一来源，turtle-analysis 已按此改造）；`.gitignore` 已允许 `.agents/skills/` 入库。

**注意**：`.claude/commands/*.md` **保留**（Claude Code 用户仍可用），只是不再是对 DSH 的主入口。

---

## 3. 子项 2.1：确定性切片器（`scripts/dimension_slicer.py`）

**输入**：`annual_report_*.txt`（含 `===== PDF PAGE n =====` 标记）／`pdf_sections.json`／PDF 本体
**输出**：`output/{code}_{company}/slices/slice_{dim}.md` + `slice_manifest.json`
**CLI（草案）**：`--code --output-dir --budget-chars 12000 --dim all|D1..D6 [--vision] [--report-coverage]`

### 两阶段选择（经 §5 四版实测确定，非拍脑袋）

| 阶段 | 作用 | A股实测 | 港股实测 |
|---|---|---|---|
| **① 定位** | 找「章节」而不是「提及」 | `第X节` 标题页 → 识别 **9 个边界** | `第X节` **0 命中** → 必须用 `董事會報告`/`管理層討論及分析`/`企業管治報告`/`財務報表附註` + 目录页码反查 |
| **② 段内切分** | 段落超预算时二次选页 | MD&A = p10–32 = **31,351 字符（超 12K 预算 2.6×）** | 同类问题 |

**硬约束（均为实测教训）**
1. **必须排除封面/重要提示/目录页** —— 目录命中几乎每个维度的关键词。
2. **段内切分顺序**：段首结论段 → 含小标题页（`一、`/`（一）`）→ 含数字表页（`单位：元`）→ 其余按页序补齐。
3. **manifest + 声明**：切片头必须写明「本次仅提供 N/M 页，未覆盖部分不得推断」，避免把"没给"当"没有"。
4. **叙事型维度优先连续页段**（D4 治理、D5 MD&A）；数字型维度允许分散取页（D1、D6）。
5. **无文本层/表格密集** → 走视觉路径（`--vision`），按 §4 的裁剪规范。

---

## 4. 子项 2.2：维度并行子代理 + 综合

### 4.1 目标架构

```
   Python 确定性层（不变，唯一事实底座）
   data_pack_market.md / computed_metrics.md / dimension_slicer.py → slice_{dim}.md + manifest
                          │  同一份「共享前缀」（逐字节相同、顺序固定 → KV cache 命中）
   ┌──────────┬──────────┬──────────┬──────────┬──────────┬──────────┐
   ▼          ▼          ▼          ▼          ▼          ▼
 D1 子代理   D2 子代理   D3 子代理   D4 子代理   D5 子代理   D6 子代理   ← 并行、上下文隔离
   └──────────┴──────────┴─────┬────┴──────────┴──────────┘
                               ▼
                    综合子代理（只吃 6 份 JSON，~18K 字符）
                               ▼
                 report_consistency.py --gates（审计门不变）
```

### 4.2 维度 → 材料映射

| 维度 | 材料 |
|---|---|
| D1 商业模式与资本特征 | 数据包 §1–§6/§17 + 主营分行业页 + MD&A 经营回顾 |
| D2 护城河 | MD&A 竞争格局 + 分业务收入 + 附注（客户/供应商集中度）+ `framework_guide.md` |
| D3 外部环境 | 数据包 §8 + `web_search` 补充 |
| D4 管理层与治理 | 治理章节 + 董事/审计师变动 + 关联交易附注 |
| D5 MD&A 解读 | MD&A 章节（叙事型，优先连续页段） |
| D6 控股结构（条件触发） | 子公司/长期股权投资附注 + 数据包 §17.7 |

### 4.3 预算表（全部实测）

| 项 | 字符 | 说明 |
|---|---|---|
| 共享前缀 | **27,812** | `writing_style_rules` 2,038 + `output_schema` 4,897 + `judgment_examples` 1,850 + **`data_pack_market.md` 17,024** + **`computed_metrics.md` 2,003**（后两项是硬数字唯一来源，必须给每个维度代理，不可省） |
| 契约片段 | 435–1,797 | 各维度按需读取 `$defs/paramsD{n}` + 共享 `$defs`（2,841 字符） |
| 维度指令 | 449–2,407 | `qualitative_assessment.md` 各维度节实测（D3 最短、D6 最长） |
| 维度专属参考 | 99 / 3,216 | 行业锚点**单节平均仅 99 字符**（整文件 9,558 是 36 节之和）；D2 另需 `framework_guide.md` 3,216 |
| 源码切片 | 12,000 | 填充率实测 93–100% |

合计：D1 23,986 ／ D2 27,562 ／ D3 23,333 ／ D4 23,308 ／ D5 23,385 ／ D6 25,192 字符。
→ D2/D6 略超 25K 目标，建议切片降至 10,000–11,000。
→ 换算按中文 **1 字符 ≈ 1 token 上界**保守估计；**首次真实运行后须用实际 usage 校准**并写入配置，不要长期依赖 1:1。

### 4.4 JSON 契约（✅ 已定稿并落地，阶段 2 前置条件已满足）

契约本体：`shared/qualitative/references/dim_result.schema.json`（JSON Schema Draft 2020-12）
设计说明与决策记录：`shared/qualitative/references/dim_result_contract.md`
强制校验：`tests/test_dim_result_contract.py`（30 项：schema 合法性 + 3 份正向样例 + 12 个负向用例 + 契约⇄规范同步）

每维度输出 `output/{code}_{company}/dims/{dim}.json`，核心字段：`slice`（材料范围与覆盖声明）、
`verdict.material_status`（充分/不足/不适用 三态）、`narrative`（字数下限结构化）、`params`（40 个参数，
值域照 `output_schema.md` v1.2）、`key_metrics[]`/`evidence[]`（强制 `source`）、`cross_dimension_hooks[]`
（**只投递 L2**）、`uncertainty[]`、`flags[]`。

**收益**：`report_consistency.py` 与审计门可直接消费结构化字段，替代现在「±30 字符窗口 + 关键词就近归类 + 4 位年份」的启发式抓取；术语漂移（如 `moat_rating='中等'`）在产出时刻即被拒绝。

---

## 5. 可行性验证：四版选页策略对照（2.1 的直接依据）

| 版本 | 策略 | A股 002984 | 港股 00506 | 判定 |
|---|---|---|---|---|
| v1 | 锚点首页 + 向后取连续页 | 8/9 锚点命中 | **5/9 锚点未命中** | 港股不可用 |
| v2 | 关键词密度选页 | **D5 选到 p2 封面 + p3 目录** | 6/6 填满预算（93–99%） | 预算填满，内容选错 |
| v2b | v2 + 排除目录/前置页 | D5 只剩 **p10 单页（8% 预算）** | 0 页被排除（港股目录格式不同） | 反向失败：只找到标题页 |
| v3 | 章节边界（`第X节` → 下一节） | 识别 **9 个边界**：MD&A 31,351／治理 22,759／财务报告 105,682 | **0 个边界** | A股可行（需段内切分）；港股需另一套标题体系 |

**四条结论**（已写入 §3）：目录必须先排除；纯关键词会双向失败；章节段普遍超预算，段内切分+声明是必需能力；港股不能套用 A 股规则。

---

## 6. 视觉路径（2.1 的 `--vision` 与表格密集场景）

**实验**：真实年报第 132 页（递延所得税附注，5 列 74 个数字），两条独立真值（pdfplumber 结构化 + 线性化文本），两个**全新上下文**子代理只读图：

| 实验 | 结果 |
|---|---|
| 整页 1406×1989 | 10/10 目标正确；主动多读的 8 个数字全对；**正确判定空单元格为"空白"** |
| 仅裁剪表（真盲测） | 8/8 目标正确；**主动多读 20 个数字全对**；**拒绝回答被裁掉的 Q4** |
| **合计** | **46 个单元格值，0 错误、0 幻觉、1 次正确拒答** |

**模型自发行为**（应显式指令化）：它自己做了自洽校验——「四行明细相加正好等于合计」「各负债 = 对应差异 ×15%」。

**操作规范**：逐表裁剪并包含表题 + 8px 边距；小字号子表必须裁剪（整页会判"模糊"）；空白格必须答"空白"、禁止补全。

---

## 7. 子项 2.3：清除 1M 假设

| 位置 | 现状 | 改为 |
|---|---|---|
| `.claude/commands/business-analysis.md:66` | 「Launch a single Agent with full context (Mode A — leverages 1M context …)」 | 「并行 6 个维度子代理；每个只读自己的切片（≤25K token）」 |
| 同文件 `:184` | 「Single Agent mode (recommended): Uses 1M context for all 6 dimensions」 | 同上，并说明综合子代理只消费 JSON |
| `shared/qualitative/coordinator.md` | Step 2 的单 Agent 指令 | 改为并行维度调度 + 综合 |
| `README.md:70,342` | 「PDF 直接载入 1M context」 | 与 README 整体重写一轮一起处理（README 还停在 v2.0-beta） |

> v1 归档里那套 Agent Team 拆分（`shared/qualitative/legacy/agents/agent_a_d1d2.md` 等）**正是本方案要的形态** —— 当年因「1M 单 Agent 交叉验证更优」而废弃，在 128K 窗口上结论要反过来：**复活其结构，但由 DSH 并行工具 + JSON 契约驱动**。

---

## 8. 风险与缓解

| # | 风险 | 证据 | 缓解 |
|---|---|---|---|
| 1 | 选页语义偏差 | §5 四版实测 | 两阶段策略 + 目录排除 + A/B 验收 |
| 2 | 章节段超预算 | MD&A 31,351 字符 | 段内二次切分 + manifest 声明 |
| 3 | 港股规则不同 | `第X节` 0 命中 | 港版标题词 + 目录页码反查 |
| 4 | 跨维度一致性下降（原靠单上下文） | 架构固有 | 共享前缀用同一份数据包数字 + 综合阶段跨维度冲突检查 + 保留 clean-room/审计门 |
| 5 | 切片遗漏 → 以偏概全 | 抽样本质 | 切片头注入「仅 N/M 页」声明；manifest 可审计 |
| 6 | 提示词测试被钉住 | 7 个文件 ~245 assert | 分阶段：新链路并存 → A/B 达标后再切默认 |
| 7 | 成本上升（6× 调用） | — | 单上下文 ↓81–93% + 共享前缀缓存命中 + 批量错峰（官方 5 折） |
| 8 | token 估算偏差 | 1:1 为保守代理 | 首次真实运行后用 usage 校准写入配置 |

---

## 9. 分阶段落地与回滚

| 阶段 | 内容 | 工作量 | 风险 | 可回滚 |
|---|---|---|---|---|
| **0** | 入口移植 2.0：5 个 skill（`.agents/skills/*`）+ `.claude/skills/*` 软链 | 0.5 天 | 极低 | ✅ **已完成** |
| **1** | 切片器 2.1 + manifest（**未接入任何链路**） | 0.5 天 | 极低 | ✅ **已完成** |
| **2** | 加「切片模式」开关（**默认关**），跑 A/B 验收 | 0.5 天 | 低 | 关开关 |
| **3** | A/B 达标 → 切片模式转默认；清除 1M 措辞（2.3）；同步 7 个提示词测试 | 1 天 | 中 | 切回默认关 |
| **4** | （原包 3）迁移到 `workflow` + JSON Schema 落地 | 1–2 天 | 中 | 保留 md 编排兜底 |

### 阶段 0/1 完成记录（实测）

**阶段 0**：`.agents/skills/` 下 5 个 skill 均被 DSH 热加载发现（tushare、turtle-analysis、business-analysis、valuation、download-annual-report）；`.claude/skills/<name>` 全部为指向 `.agents/skills/<name>` 的软链（单一来源）；守卫测试自动纳入新文件。

**阶段 1**：`scripts/dimension_slicer.py`（两阶段选页 + 预算硬上限 + manifest + 可选 `--vision` 渲染）。
三份真实年报实测结果：

| 样本 | 页数 | 目录条目 | D1 | D2 | D4 | D5 | D6 | D3 |
|---|---|---|---|---|---|---|---|---|
| 002984.SZ | 175 | 10 | MD&A 95% | MD&A 96% | 治理 96% | MD&A 99% | 财务报告 99% | 兜底 97% |
| 00506.HK | 203 | 17 | MD&A 96% | MD&A 98% | 管治报告 93% | MD&A 100% | 报表附注 96% | 兜底 98% |
| 06862.HK | 286 | 14 | MD&A 94% | MD&A 87% | 管治报告 99% | MD&A 83% | 报表附注 98% | 兜底 100% |

（百分比 = 预算填充率；除 D3 外全部由**目录区间**定位，D3 因年报无独立"外部环境"章节而走关键词兜底——这正是设计预期。）

**实现过程中被测试抓出的三个真实缺陷**（已修复并回归）：
1. 港股目录**完全不带点线**（`MANAGEMENT DISCUSSION AND ANALYSIS 管理層論析 19`）→ 早期版本 0 命中；已支持双语无点线格式。
2. `_match_section` 原按**文档顺序**取最早命中，导致 D4 被"董事及高級管理層簡介"（人物简历）抢走 → 改为**按模式优先级**匹配，D4 正确落在"企業管治報告"。
3. 预算可被**单页超限**打破（首页大于预算时被整页收下）→ 改为硬上限：首页超预算则按预算截断并在切片中标注。


**A/B 验收标准（阶段 2，建议 002984 A股 + 00506 港股）**

| # | 指标 | 通过线 |
|---|---|---|
| 1 | 结构化参数字段完整率 | ≥ 现有全量报告 |
| 2 | `report_consistency.py --gates` 冲突数 | ≤ 现有报告 |
| 3 | 审计门 critical 数 | = 0 |
| 4 | 关键事实覆盖（人工预置 10 条必现清单） | ≥ 9/10 |
| 5 | 单上下文峰值字符数 | ≤ 50,000（按实测 43–45K 加余量；旧稿的 26K 系漏计共享前缀所致） |
| 6 | 总耗时 | ≤ 现有 1.5× |

未达标回退：① 改用章节边界选择；② 对失败维度放宽预算；③ D4/D5 强制连续页段。

---

## 10. 验收命令（可执行）

```bash
# 阶段 0：技能是否被 DSH 发现（新建 skill 后本会话技能目录应立即出现）
ls .agents/skills/*/SKILL.md
readlink .claude/skills/turtle-analysis        # → ../../.agents/skills/turtle-analysis

# 阶段 1：预算与覆盖率自检
.venv/bin/python scripts/dimension_slicer.py --code 002984 \
    --output-dir output/002984_森麒麟 --budget-chars 12000 --report-coverage

# 切片体积核查
.venv/bin/python - <<'PY'
import glob, os
for f in sorted(glob.glob("output/002984_森麒麟/slices/slice_*.md")):
    print(f"{os.path.basename(f):18s} {len(open(f,encoding='utf-8').read()):>7,} 字符")
PY

# 阶段 2：A/B 对照（现有 vs 切片）
.venv/bin/python scripts/report_consistency.py \
    --report output/002984_森麒麟/qualitative_report.md --gates

# 回归网（守卫测试必须保持绿）
.venv/bin/python -m pytest tests/test_dsh_tool_names.py tests/test_coordinator.py -q
```

---

## 附：已完成的前置工作（不在包 2 范围内，但为其铺路）

| 项 | 状态 |
|---|---|
| 沙箱兼容（`ts.set_token` → `pro_api(token=)`） | ✅ 已完成，真实 API 端到端验证 |
| 平台去耦（活跃提示词层工具名映射 + 守卫测试 35 项） | ✅ 已完成（988 passed, 3 skipped） |
| `tushare` / `turtle-analysis` skill 落位 `.agents/skills/` + 软链 | ✅ 已完成，DSH 实测可发现 |
| `AGENTS.md` 平台中立入口 | ✅ 已完成 |
| 视觉可行性验证 | ✅ 已完成（46 格 0 错误） |
| 切片预算与选页策略验证 | ✅ 已完成（本文件 §4/§5） |

*本文件为方案与验证报告；阶段 0 的 3 个 skill、阶段 1–4 均未开工。*
