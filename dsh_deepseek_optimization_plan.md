# 面向 DeepSeek + DeepSeek Harness 的适配与优化方案

> 诊断日期：2026-10-13 ｜ 目标平台：DSH Desktop `0.2.0-rc.2`（Web GUI）｜ 目标模型：`deepseek-flash`（= DeepSeek-V4.1-Flash）
> 本文件为方案文档，**除已完成的缺陷修复外未改动任何代码**。

---

## 〇、已确认的决策与实施状态（2026-10-13 更新）

| 决策项 | 确认结果 | 对方案的影响 |
|---|---|---|
| 上下文上限 | **128K** | 单次子代理输入预算定为 **≤ 25K token**（约窗口 20%，为输出、参考文件与多轮留余量）；包 2 的切片粒度按此反推 |
| 提示词改动 | **允许改正文 + 同步改测试** | 包 2 可以真正重排「1M 单 Agent」，而不只是替换工具名 |
| 编排形态 | **接受 workflow + schema** | 包 3 落地；`output_schema.md` 的 markdown 参数表升级为 JSON Schema |
| 沙箱兼容 | **采用改代码方案** | 包 1.1 见下（已完成） |
| 视觉读年报 | **先做最小实验再定** | 包 4 的视觉路径待实验结论，其余（缓存/错峰）不受影响 |

### ✅ 包 1.1 已实施并验证（沙箱兼容）

**改动**（3 处代码 + 2 处测试契约）：

| 位置 | 改动 |
|---|---|
| `scripts/tushare_collector.py:119-125` | 去掉 `ts.set_token()`，改为 `ts.pro_api(token=token, timeout=30)` |
| `scripts/tushare_collector.py:177-180` | 连接异常重建客户端时同样带上 token（否则会退回读 `~/tk.csv`） |
| `scripts/screener_core.py:116-125`、`:145` | 同上两处 |
| `tests/test_tushare_client.py`（2 个用例） | 断言改为「**不得**调用 set_token」+ 「pro_api 必须收到 token」 |

**端到端验证**（真实 API，**未使用** `HOME` 重定向，即 DSH 默认 `workspace-write` 沙箱下）：

```
$ .venv/bin/python scripts/tushare_collector.py --code 002984 \
      --output /tmp/dsh_refresh_test/pack.md --refresh-market
→ 全量采集完成，无 PermissionError（此前必然崩溃）
→ §17 小节 13/13 全部生成（此前 10/13）
→ §17.2  Rf = 1.6880%（来自 §14）  ← 未传 --rf，由 3-B 从既有数据包回读
→ §14    数据缺失(接口可能无权限) + 明确补救提示
→ 「未生成」告警数 = 0
```

一次运行同时验证了三件事：**沙箱修复生效**、**3-B 回读链路端到端可用**、**3-A 缺失不再静默**。全量测试 **944 passed, 3 skipped**。

---

## 一、直接回答：这个项目依赖指定的 LLM 吗？

**分两层看，结论完全不同：**

| 层 | 是否依赖特定 LLM | 证据 |
|---|---|---|
| **Python 层**（`scripts/` 11.2k 行 + 944 项测试） | **完全不依赖**。任何模型、甚至不用模型都能跑 | 全仓 `scripts/` 中 `openai\|anthropic\|api_key\|chat.completions\|deepseek` 匹配数 **= 0**（实测）。数据采集、预计算网格、估值引擎、质量门全是确定性代码 |
| **提示词 / 编排层**（prompt md + slash command） | **依赖两个隐含前提**：①「1M token 上下文」；② Claude Code 的工具语义与入口形态 | 见下 |

所以：**不依赖模型权重或厂商，但依赖「超长上下文 + Claude Code 工具名」**。这两点在 DSH + DeepSeek 上都不成立，这正是需要适配的地方。

### 1.1 三类具体耦合（可量化）

| # | 耦合 | 规模 | DSH 中的对应物 |
|---|---|---|---|
| 1 | **上下文假设：1M** | `.claude/commands/business-analysis.md:66`「Launch a single Agent with full context (Mode A — leverages 1M context)」、`:186`、`README.md:70,342` | 无对应；必须改为切片式检索 |
| 2 | **Claude Code 工具名** | `WebSearch` ×80、`AskUserQuestion` ×15、`Agent(` ×3、`Skill` ×3 | `web_search` / `ask_user_question` / `subagent`·`subagent_fork`·`workflow` / `skill` |
| 3 | **入口形态：slash command** | `.claude/commands/` 4 个（`/business-analysis`、`/turtle-analysis`、`/valuation`、`/download-annual-report`） | DSH 无 slash command 机制；应转为 skill 或平台中立 runbook |

### 1.2 顺带发现的既有缺陷（与平台迁移直接相关）

- **引用的 skill 不存在**：`turtle-analysis.md:16`、`strategies/turtle/coordinator.md:60` 让 Agent「invoke the `business-analysis` skill」，但仓库里没有这个 skill（只有 `.claude/commands/business-analysis.md`）。
- **软链断链**：`.claude/skills/tushare -> ../../.agents/skills/tushare`，而 `.agents/` **不存在** → tushare skill 实际缺失。
- **DSH 看不到项目自带 skill**：`.claude/skills/` 下的 `turtle-analysis` 不在 DSH 技能目录中。**已实测确认原因**：DSH 从 **`.agents/skills/<name>/SKILL.md`** 发现技能（且支持热加载）——见第七节第 3 条。
- **沙箱不兼容（会被直接挡住）**：`ts.set_token()` 会写 `~/tk.csv`，在 DSH 默认 `workspace-write` 策略下抛 `PermissionError` —— 我已实际复现，采集脚本**完全无法运行**。详见包 1。

---

## 二、为什么「1M 单 Agent」在你的模型上必然失效（实测数字）

对 `/business-analysis` Step 2 的实际输入做字符统计：

| 输入 | 字符数 |
|---|---|
| 6 个参考文件（框架/判断锚点/写作规范/行业锚点/schema） | 8,477 + 1,850 + 3,216 + 2,038 + 3,822 + 4,897 = **24,300** |
| `data_pack_market.md`（案例 002984） | 17,024 |
| `computed_metrics.md`（案例） | 2,003 |
| **固定输入小计** | **43,327** |
| 年报正文（5 个真实样本） | 201,678 / 410,807 / 422,695 / 533,182 / **573,252** |

**单次定性分析的上下文下限 = 245,005 ～ 616,579 字符 ≈ 150K ～ 620K token**（中文 1 字符 ≈ 0.6–1.0 token）。

而按 DeepSeek 官方文档，V3.1 时代的 `deepseek-chat` / `deepseek-reasoner` 均为 **128K 上下文**（[DeepSeek-V3.1 Release](https://api-docs.deepseek.com/news/news250821/)）。V4.1-Flash 的发布说明未给出上下文长度（[DeepSeek-V4.1-Flash Release](https://api-docs.deepseek.com/news/news260910/)），**需你在 DSH 模型设置里核对实际上限**；但无论上限是多少：

- **最小**的一份年报（002984，201,678 字符）叠加固定输入就已 **245K 字符**，远超 128K token 量级；
- **最大**的一份（海底捞 573,252 字符）约 4–5 倍；
- 即使窗口够大，把 20 页 ×N 次的全文堆进一个 context，也会**稀释注意力、拉高时延与费用**，与 Flash 档「快而省」的定位相悖。

> 结论：不是「能不能塞进去」的问题，而是这个架构不该继续用。

---

## 三、你的平台与模型的 5 个可利用优势（方案的地基）

| # | 优势 | 对本项目的具体价值 | 落地动作 |
|---|---|---|---|
| 1 | **V4.1-Flash 原生视觉理解**（[发布说明](https://api-docs.deepseek.com/news/news260910/)：「native visual understanding」） | 年报 PDF **可以直接按页读图**，绕过 `pdf_preprocessor.py` 关键词定位的脆弱性与扫描件降级 | PDF 页面截图/图像喂给维度 Agent，表格与附注保真度显著提升 |
| 2 | **成本低 + KV cache 压缩（1/4 HBM）**，且官方明确指出「cache-hit 常占 agent 成本很大比重」 | ① 大量小上下文并行子代理在经济上可行；② **提示词前缀稳定化**能直接省钱 | 固定参考文件前置且顺序恒定、变量（公司/数据）后置，最大化缓存命中 |
| 3 | **DSH `workflow` 工具**（`pipeline` / `parallel` / `agent(prompt,{schema})` / 阶段可视化） | 把「让 LLM 读 prose 自己决定怎么编排」变成**可测试、可重试、可并行的脚本** | 龟龟 Agent B/C 串行链、BA 三路并行全部下移到 workflow |
| 4 | **DSH `subagent` / `subagent_fork`（上下文隔离）** | 维度级并行天然成立；clean-room 审计本就需要「看不见草稿」的独立上下文 | 6 维度各一个子代理；审计/重算各一个 |
| 5 | **DSH 长任务能力**：`create_goal`（跨轮续跑）、后台 job + `job_output`、`todo_write`、`present` | 批量选股 → 逐股分析可以长跑数小时；报告以文件卡交付（Web GUI 体验好） | 批量任务用 goal + 后台 job；成品用 `present` 交付 |

**补充一条成本工程**：官方对 V4.1-Flash 明确「off-peak rates are 50% of peak」（[同上](https://api-docs.deepseek.com/news/news260910/)）→ 选股器全量跑、批量分析应尽量排在错峰时段。

---

## 四、优化方案：四个工作包（按性价比排序）

### 包 1 ｜P0：先让项目在 DSH 上跑得起来（约 30 分钟，风险≈0）

**1.1 修沙箱不兼容（硬阻断，必须最先做）— ✅ 已完成，见第〇节**

现状（实测报错）：
```
File ".../tushare/util/upass.py", line 20, in set_token
PermissionError: [Errno 1] Operation not permitted: '/Users/polygon/tk.csv'
```
`ts.set_token()` 的唯一作用是写 `~/tk.csv`；而 DSH 默认 `workspace-write` 不允许写工作区外。**但该调用完全多余**——我实测 `ts.pro_api(token=...)` 直接可用：

```
pro_api(token=...) 直接可用: True | 返回行数: 13     ← 未写 ~/tk.csv
```

需改的 3 处（**重试路径必须一并修**，否则连接异常后会退回去读 `~/tk.csv`）：

| 文件:行 | 现状 | 改为 |
|---|---|---|
| `scripts/tushare_collector.py:120-121` | `ts.set_token(token)` + `ts.pro_api(timeout=30)` | `ts.pro_api(token=token, timeout=30)` |
| `scripts/tushare_collector.py:173` | `self.pro = ts.pro_api(timeout=30)` | `ts.pro_api(token=self.token, timeout=30)` |
| `scripts/screener_core.py:120-121, 145` | 同上 | 同上 |

保留 `_DataApi__token/__http_url` 的 VIP 中继赋值（那是给券商中继用的，与 set_token 无关）。

**1.2 平台工具名映射**（把 Claude Code 语义换成 DSH 语义）
`WebSearch`→`web_search`、`AskUserQuestion`→`ask_user_question`、`Agent(...)`→`subagent`/`workflow`、`Skill`→`skill`。
涉及文件：`.claude/commands/business-analysis.md`、`turtle-analysis.md`、`download-annual-report.md`、`strategies/*/coordinator.md`、`shared/qualitative/data_collection.md`、`shared/qualitative/references/market_rules_{hk,us}.md`。

**1.3 入口与 skill 归位**
- 修断链：把 `.claude/skills/tushare` 指向真实目录（或删除该软链）。
- 新增 **`AGENTS.md`**（平台中立入口）：把 4 个 slash command 的内容改成「可被任何 agent 直接 read 的 runbook」，DSH 里只要说「按 AGENTS.md 分析 600887」即可，不依赖 slash command 机制。
- **技能发现路径实验**（需要你配合，见第七节）。

### 包 2 ｜P0/P1：把「1M 单 Agent」换成「确定性切片 + 维度并行 + 综合」（1–2 天）

这是**收益最大**的一步，也正好对上 DeepSeek 的强项（便宜、可高并发、KV cache 友好）：

```
现状：1 个 Agent  ──▶ 读全文 245K–617K 字符  ──▶ 输出 6 维度报告
改后：Python 切片 ──▶ 6 个维度子代理（各 15–30K 字符，并行）──▶ 6 份结构化 JSON
                     └─▶ 1 个综合 Agent 只吃 6 份 JSON（几 KB）
```

**维度 → 材料映射（关键设计）**

| 维度 | 需要的材料 | 来源 |
|---|---|---|
| D1 商业模式/资本特征 | 利润表+现金流（已在 `data_pack_market.md`）、MD&A 中的经营回顾 | 数据包 + MD&A 页段 |
| D2 护城河 | MD&A 竞争格局、分业务收入、附注（分部/客户集中度） | MD&A + 附注页段 |
| D3 外部环境 | 行业数据、政策 | 数据包 §8 + `web_search` |
| D4 管理层与治理 | 公司治理章节、董事/审计师变更、关联交易 | 治理页段 + 附注 P4 |
| D5 MD&A 解读 | MD&A 全章 | MD&A 页段 |
| D6 控股结构（条件触发） | 子公司/母公司报表、长期股权投资附注 | 附注 SUB + §17.7 |

**落地要点**
- 以 `pdf_preprocessor.py`（已存在，输出 `pdf_sections.json`）为主路径，**把「全文载入」降级为兜底**；目前 5 个样本里只有 1 个（00506/002027 等）留了 `pdf_sections.json`，说明实际执行走了全文路径。
- 每个维度子代理只拿「自己那段 + 固定参考文件」，输入压到 15–30K 字符。
- **架构不是新发明**：仓库里 `shared/qualitative/legacy/agents/agent_a_d1d2.md`、`agent_b_d3d4d5.md`、`agent_summary.md` 就是这套拆分，v2 为了「1M 单 Agent 交叉验证更优」而废弃。**在小窗口模型上，那个结论要反过来**——应复活该结构，但改用 DSH `workflow` 驱动而非 prose 描述。

### 包 3 ｜P1：编排从 prose 迁到 `workflow` 脚本 + schema 化输出（2–3 天）

- 把龟龟 `Agent B → Agent C` 串行链、BA 的「定性 ∥ 附注提取 ∥ clean-room」三路并行，写成 `workflow` JS（`pipeline`/`parallel`/`agent(prompt,{schema})`）。
- **schema 化**收益最大的一处：现在 `output_schema.md` 要求 LLM 在 markdown 里输出「结构化参数表」，再由 `report_consistency.py` 用 **±30 字符窗口 + 关键词就近归类 + 4 位年份** 的启发式去抓数字。改成 JSON Schema 强约束后：
  - 审计门直接吃结构化字段，误判率下降；
  - 不再依赖「LLM 记得填表」；
  - clean-room 对账可做逐字段 diff 而非全文正则。
- 附带好处：workflow 的失败重试与阶段可见性，替代当前「coordinator.md 里写 16 分钟超时规则」的软约束。

### 包 4 ｜P2：多模态 + 成本工程（可与包 2/3 并行）

1. **PDF 读图**：用 V4.1-Flash 的视觉能力读年报页面（表格、附注、扫描件），替代/补充 pdfplumber。对「扫描件 → 人工兜底」这条现在的死路是质变。
2. **缓存命中工程**：固定前缀（框架 + 写作规范 + schema）在前且**顺序恒定**，变量在后；跨股票的公共前缀可复用。
3. **成本调度**：选股器全量、批量分析排到错峰时段（官方 50% 折扣）。
4. **长任务**：`create_goal` 跟踪「筛 200 只 → 深度分析前 50 只」这类跨轮任务；采集（冷跑 50s）与筛选用后台 job；成品 HTML/MD/CSV 用 `present` 交付。

---

## 五、**不建议**改的部分（这些恰恰是 DeepSeek 的加分项）

| 保留项 | 理由 |
|---|---|
| Python 确定性层（11.2k 行 + 944 测试） | 与模型完全解耦，且是「小上下文/低成本模型」的最佳搭档 |
| 「Python 算、LLM 选」的网格化（§17.10–13） | 模型越小越容易算错，这个设计应**加强**而非削弱 |
| 反幻觉体系：`[src:]` 溯源、`computed_metrics.md`、clean-room 重算、`AUDIT_RESULT` 门控、字数硬门槛 | 与模型无关，且能补偿小模型在长指令遵循上的弱点 |
| 中文优先的写作规范与报告模板 | DeepSeek 中文能力强，这是顺风项 |

---

## 六、改提示词的隐形成本：测试钉住了文本

改动提示词必须同步改测试，否则 944 全绿会立刻变红：

| 测试文件 | assert 数 |
|---|---|
| `tests/test_phase3_prompt.py` | 93 |
| `tests/test_qualitative_prompts.py` | 36 |
| `tests/test_phase1b_prompt.py` | 31 |
| `tests/test_output_format.py` | 28 |
| `tests/test_phase2b_prompt.py` | 25 |
| `tests/test_phase3_prompt_grids.py` | 17 |
| `tests/test_coordinator.py` | 15 |

**这是回归网，不是障碍**——包 2/3 的每次提示词改动都应先跑这几个文件。

---

## 七、验证清单（可执行）

```bash
# 1) 包 1.1 沙箱修复验证（应打印 True 且不产生 ~/tk.csv）
.venv/bin/python -c "
import sys; sys.path.insert(0,'scripts')
import tushare as ts, os
from config import get_token
pro = ts.pro_api(token=get_token(), timeout=30)
print('可用:', not pro.trade_cal(exchange='SSE', start_date='20261001', end_date='20261013').empty)"

# 2) 上下文预算测量（每次分析的输入字符数，建议阈值 ≤ 30K/调用）
.venv/bin/python - <<'PY'
import glob, os
files = ["shared/qualitative/qualitative_assessment.md",
         "output/002984_森麒麟/data_pack_market.md"]
print(sum(len(open(f,encoding='utf-8').read()) for f in files), "字符（固定输入示例）")
for f in glob.glob("output/*/annual_report*.txt"):
    print(f, len(open(f,encoding='utf-8').read()))
PY

# 3) 技能发现路径 —— ✅ 已实测确认（2026-10-13）
#    结论：**DSH 从 `.agents/skills/<name>/SKILL.md` 发现技能**，且**热加载**——
#    新建 .agents/skills/tushare/SKILL.md 后，无需重启会话，它立刻出现在本会话的技能目录里。
#    这也解释了为什么 .claude/skills/ 下的 turtle-analysis 一直不被 DSH 看见。
#    → 移植建议：把 4 个 slash command 改写成
#      .agents/skills/{business-analysis,turtle-analysis,valuation,download-annual-report}/SKILL.md
#    （本机 ~/.dsh/skills、.dsh/skills 不存在，非 DSH 的发现路径）
```

**需要你确认的一个输入**：DSH 中 `deepseek-flash`（V4.1-Flash）的**实际上下文上限**。官方发布说明未列出该数值；确认后我把它写进配置（例如 `config.py` 的 `LLM_CONTEXT_BUDGET`），切片阈值按它推导，而不是硬编码。

---

## 八、优先级与预期收益

| 包 | 工作量 | 收益 | 风险 | 建议 |
|---|---|---|---|---|
| **1 沙箱 + 平台去耦** | ~30 分钟 | **项目从「跑不起来」变成「能跑」**；消除 3 处平台工具名/入口错配 | 极低（1 行核心改动） | **立刻做** |
| **2 切片 + 维度并行** | 1–2 天 | 单次上下文 ↓ 约 80–90%（245K–617K → 15–30K/调用）；成本与时延同比例下降；交叉验证由结构化字段保证 | 中（提示词重写，需同步测试） | **强烈建议** |
| **3 workflow + schema** | 2–3 天 | 编排可测试/可重试；审计门从正则启发式升级为结构化校验 | 中 | 建议 |
| **4 多模态 + 成本** | 1–2 天 | 扫描件/表格保真；缓存命中与错峰进一步降本 | 低 | 随包 2/3 并行 |

---

## 九、一句话总结

项目**不依赖任何特定模型**——它的 Python 层和反幻觉设计是模型无关的资产；真正耦合的是提示词层的**「1M 上下文单 Agent」假设**与**Claude Code 的工具/入口语义**。
面向 DeepSeek + DSH 的正确改法不是「找更长的窗口」，而是**顺着 DeepSeek 的强项重排架构**：用**便宜可并行的短上下文子代理**取代巨型单上下文，用 **DSH `workflow` + schema** 取代 prose 编排，用**原生视觉**读年报，用**固定前缀**吃缓存红利，用**后台 job + goal** 跑批量。同时把沙箱兼容（1 行）先修掉——否则现在连采集都跑不起来。

---

### 外部资料来源

- [DeepSeek-V3.1 Release（128K 上下文 / deepseek-chat 与 deepseek-reasoner）](https://api-docs.deepseek.com/news/news250821/)
- [DeepSeek-V4.1-Flash Release（原生视觉、KV cache 1/4、错峰 5 折）](https://api-docs.deepseek.com/news/news260910/)

*（本文件为方案文档；诊断结论基于本仓库实测与本机 DSH 配置读取，不含任何未经核实的外部断言。）*
