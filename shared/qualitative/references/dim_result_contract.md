# L1 维度结果契约（`dims/{dim}.json`）· 设计说明与审核清单

> 契约文件：[`dim_result.schema.json`](dim_result.schema.json)（JSON Schema Draft 2020-12，v1.0）
> 参数来源：`output_schema.md` v1.2（**40/40 全覆盖**）｜ 下游兼容：`factor_interface.md`（**14/14 全覆盖**）
> 机械验证：schema 合法性 + 1 个正向样例 + 10 个负向用例，全部符合预期（见 §5）

---

## 1. 四条设计原则

| # | 原则 | 针对的旧问题 |
|---|---|---|
| 1 | **每个事实只有一个权威位置**（禁止重复字段） | v1 的"同一事实在 A/B 两份输出里各写一遍" → 必然漂移 |
| 2 | **所有数字必须带 `source`，且 `source.kind` 决定必须补什么** | v1 的 `[src: ...]` 是自由文本，程序无法校验 |
| 3 | **在"产生点"约束值域**（enum / required），而不是事后由 Summary 统一术语 | v1 的 Summary 职责之一就是"把'较强'统一成'偏强'"——补救而非预防 |
| 4 | **材料不足必须显式声明**，不得用编造内容填充 | v1 无此机制；v2 靠事后审计发现 |

---

## 2. 结构总览

```
dims/{dim}.json
├── schema_version / dimension / stock_code / company_name / generated_at
├── slice                     ← 本维度实际看到的材料范围（由 slicer 生成，agent 只读）
│   ├── provided_pages[] / total_pages / dropped_pages
│   ├── budget_chars / used_chars / source
│   └── declared_incomplete   ← 切片头是否已声明"仅 N/M 页"
├── verdict
│   ├── headline              ← 一句话结论
│   ├── confidence            ← 高/中/低
│   └── insufficient_material ← 材料是否不足（见 §7 决策点 5）
├── narrative                 ← 维度正文；下限 = 交付硬门槛（D1≥600 / D2≥800 / D6≥300）
├── params                    ← 维度专属参数，形状由 dimension 决定（40 个字段）
├── key_metrics[]             ← 关键数字 + 强制 source
├── evidence[]                ← 定性论断 + 原文引文（年报类必带 page + quote）
├── cross_dimension_hooks[]   ← 影响其它维度的事实（**只投递给 L2**）
├── uncertainty[]             ← 显式不确定性清单
├── flags[]                   ← 机器可读的异常标记
└── extensions                ← 预留位（不鼓励使用）
```

---

## 3. 关键机制：来源词表（把反幻觉从"约定"变成"强制"）

`source.kind` 决定必须补齐什么，缺一项即**校验失败**（不是警告）：

| `kind` | 含义 | 强制要求 | `ref` 格式约束 |
|---|---|---|---|
| `computed_metrics` | Python 预计算 | `ref` | `^CM§[1-5]$` |
| `data_pack` | 数据包 §N | `ref` | `^DP§[0-9]{1,2}[A-Z]?(\.[0-9]{1,2})?$` |
| `annual_report` | 年报原文 | `page`（+ 引文时 `quote`） | — |
| `web_search` | 检索补充 | `basis`（检索式） | — |
| `inference` | 模型推断 | `basis`，且 `ref` 必须为 `推断` | `const: 推断` |

**效果**：v1 里那种"引用了一个不存在的来源""把推断写成事实"的做法，在**产出时刻**就被拒绝，而不是等审计门发现。

---

## 4. 参数覆盖（为什么不会丢东西）

- `output_schema.md` v1.2 的 **40 个参数全部落到 `params` 子定义**中：D1=4、D2=21、D3=4、D4=4、D5=4、D6=3。
- `factor_interface.md` 中下游（Agent C）消费的 **14 个定性参数全部存在**且值域一致。
- **值域一律照抄**，例如 `moat_rating ∈ 强/较强/中/弱`（不是"中等"）、`management_rating ∈ 优秀/合格/损害价值/观察期`。
- 下游映射（`强/较强→优质，中→中性，弱→负面`）不变——契约不改语义，只改承载形式。

**被"结构化"进去的既有规则**

| 原机制 | 现位置 |
|---|---|
| D2 四项评级必填（交付硬门槛） | `paramsD2.required` |
| D1≥600 / D2≥800 / D6≥300 字 | `narrative.minLength`（按 dimension 条件应用） |
| "不可得填 null 并注明" | `metric`：`value=null` 时 `note` 必填 |
| D6 不适用时 SOTP 置 null | `paramsD6` 的 `if/then`：`holding_structure=false` ⇒ 两值必须为 `null` |
| 仅强周期才有周期位置 | `paramsD3` 的 `if/then`：非强周期 ⇒ `cycle_position` 必须是 `不适用` |

---

## 5. 验证结果（已固化为仓库测试，30 项）

契约不能只写在文档里——它现在由 [`tests/test_dim_result_contract.py`](../../../tests/test_dim_result_contract.py) 强制执行：

```
① schema 合法性（Draft 2020-12 check_schema）                      ✅
② 正向样例 3 份（覆盖三种材料状态）
     D2_golden.json          material_status=充分（正文 924 字）
     D6_not_applicable.json  material_status=不适用（holding_structure=false）
     D1_insufficient.json    material_status=不足（正文豁免但留痕）   ✅ 全部通过
③ 负向用例 12 项，全部按预期被拒（含 D6 ↔ 材料状态一致性、D3 非周期等）  ✅
④ 契约 ⇄ 规范同步（防止悄悄改松）
     output_schema.md 定义的参数必须全部存在于契约
     契约不得存在 output_schema.md 未定义的参数
     factor_interface.md 的 14 个下游消费参数必须齐全
     moat_rating 值域必须仍是 强/较强/中/弱（下游映射的来源）          ✅
```

**被负向用例拦住的典型故障**（都是 v1 真实发生过的）：

| 负向用例 | 拦下的问题 |
|---|---|
| `moat_rating='中等'` | **术语漂移**（值域应为"中"）——v1 靠 Summary 事后统一 |
| `inference` 缺 `basis` | 把推断写成事实 |
| `annual_report` 缺 `page` | 无法复核的引用 |
| `value=null` 缺 `note` | 留空而不注明 |
| D2 正文不足 800 字 | 敷衍交付 |
| D1 `material_status=充分` 但正文过短 | **材料不足却不说** |
| `dropped_pages>0` 无 `slice_truncated` | 切片截断不声明 |
| 多塞未登记参数 | 绕开 schema 私下加字段 |

---

## 6. 它**不能**约束什么（诚实边界）

| 不能约束 | 原因 | 现有兜底 |
|---|---|---|
| 判断本身是否正确 | schema 管**形式**，不管**内容真伪** | clean-room 独立重算 + 审计门 + 行业锚点 |
| 引文是否真的在第 N 页 | schema 不读 PDF | clean-room 抽查 10 条 `[src:]` |
| 数字之间是否自洽 | schema 不做跨字段算术 | `report_consistency.py` 5% 容差扫描 |
| 跨维度判断冲突 | 需裁决，非校验 | L2 综合代理显式裁决 + 记录理由 |

---

## 7. 决策记录（已定稿并落地）

| # | 决策 | 结论 | 落地位置 |
|---|---|---|---|
| 1 | 布尔无法表达"不适用" | ✅ **改为三态** `verdict.material_status ∈ 充分/不足/不适用` | `properties.verdict`；并加 **D6 ↔ 材料状态一致性**规则：`holding_structure=false` ⇒ 必须"不适用"，`=true` ⇒ 不得"不适用" |
| 2 | `narrative` 放 JSON 还是单独 md | ✅ **放 JSON**（单一来源，字数门槛可机械校验；自报字数不可信） | `properties.narrative` |
| 3 | D3/D4/D5 是否设字数下限 | ✅ **设为 400 字**（仅当 `material_status=充分`） | `allOf` 中新增 3 个 `if/then` |
| 4 | hook 是否只投递 L2 | ✅ **只投递给 L2**，禁止 agent 点对点（否则重新引入 v1 协作问题） | `$defs.hook.target` + 文档说明 |
| 5 | 校验失败策略 | ✅ **硬失败 + 最多 2 次重试**；仍失败则该维度标 `critical`，由 L2 决定是否降级出报告 | 策略（由包 2 阶段 4 的 workflow 实现；schema 层已保证"不合格进不了下一步"） |
| 6 | `unit` 枚举扩展 | ✅ 增补 `万元`、`元/股` | `$defs.metric.properties.unit`（现 11 个值） |

**门槛汇总（`material_status=充分` 时生效）**：D1≥600、D2≥800、D3/D4/D5≥400、D6≥300（且仅当控股结构适用）。

---

## 8. 与包 2 阶段的衔接

| 阶段 | 本契约的作用 |
|---|---|
| 阶段 2（切片模式 + A/B） | 6 个维度子代理按本契约产出 `dims/{dim}.json`；A/B 的"字段完整率"直接由 schema 校验结果得出 |
| 阶段 3（转默认） | `report_consistency.py --gates` 增加"读 JSON"路径：字数与必填项由 schema 负责，脚本只管跨维度数值冲突 |
| 阶段 4（workflow + schema） | 本文件直接作为 `agent(prompt, {schema})` 的 `schema` 参数（Draft 2020-12 兼容） |
| 附加收益 | `output_schema.md` 从"给人看的规范"降级为**人类可读镜像**，权威性转移到本契约；同步由测试 ④ 强制 |

---

## 9. 下一步

契约与测试已就位（阶段 2 的前置条件满足）。接下来是**包 2 阶段 0 + 阶段 1**：

1. **阶段 0**：补齐另外 3 个 skill（`business-analysis` / `valuation` / `download-annual-report`）到 `.agents/skills/`；
2. **阶段 1**：新增 `scripts/dimension_slicer.py` + manifest（`slice` 字段的形状已由本契约定死），**不接入现有链路**，仅产出切片供人工抽查。

*本文件为设计说明；契约本体为 `dim_result.schema.json`，强制校验见 `tests/test_dim_result_contract.py`。*
