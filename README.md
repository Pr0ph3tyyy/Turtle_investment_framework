# 龟龟投资框架 (Turtle Investment Framework)

AI 辅助的 A 股/港股/美股基本面分析系统。混合架构：Python 脚本完成确定性数据采集，LLM 提示词驱动定性分析与多因子评估。

> **当前版本：v3** — 定性分析改为**切片 + 六维度并行 + 契约化交接**（取代 PDF-first 单 Agent 全文载入）。详见 [CHANGELOG_V2.md](CHANGELOG_V2.md)。

## 核心功能

| 模块 | 名称 | 实现方式 | 说明 |
|------|------|----------|------|
| 龟龟策略 | 穿透回报率 + 估值 | 共享定性 + 专属定量 | 完整四因子分析 → 买入/观察/规避 |
| 商业分析 | 6 维度定性评估 | 切片 + 并行子代理 + 契约 | 按维度切片 → 6 个并行子代理 → 综合；Greenwald 护城河 |
| 估值分析 | 多方法估值 | Python 引擎 + LLM | DCF/DDM/PE Band/PEG/PS 自动分类 |
| 数据采集 | Tushare + web_search | Python + Agent | A/港/美股，24+ API，5 年财务数据 |
| 年报解析 | PDF 章节提取 | Python / pdfplumber | 7 个目标章节 + 附注精提取 |
| 选股器 | 龟龟选股器 | Python / 两级筛选 | Tier 1 批量过滤 + Tier 2 深度分析 |
| 报告输出 | MD + HTML 双输出 | Jinja2 模板 | 暗色模式、KPI 卡片、语义色彩标签 |

## 系统架构 (v3)

v3 沿用 **共享模块 + 策略专属模块** 的分层架构（定性分析可独立运行，也被龟龟/估值策略复用），并把定性分析从「单 Agent 全文载入」改为「**切片 + 六维度并行 + 契约化交接**」。

### 龟龟策略完整流程

```
用户输入 (股票代码 + 年报PDF)
         │
    ┌────▼────┐
    │ Phase 0 │  自动下载年报 (/download-annual-report)
    └────┬────┘
         │
    ┌────▼────┬──────────────┐
    │Phase 1A │  Phase 2A    │  ← 并行运行
    │Tushare  │  PDF预处理    │
    │数据采集  │  (pdfplumber) │
    └────┬────┴──────┬───────┘
         │           │
    ┌────▼────┐      │
    │Phase 1B │      │
    │web_search│      │
    └────┬────┘      │
         │      ┌────▼────┐
         │      │Phase 2B │
         │      │精提取    │
         └──┬───┴────┬────┘
            │        │
       ┌────▼────────▼────┐
       │     Phase 3      │
       │  定性(共享) +     │
       │  定量 + 估值(龟龟) │
       └──────┬───────────┘
              │
     output/{code}_分析报告.md + .html
```

### 独立商业分析流程（切片 + 六维度并行 + 综合）

> Claude Code 用 `/business-analysis {code}`；DeepSeek Harness 用 `business-analysis` skill或直接读
> `shared/qualitative/coordinator.md`（见 [AGENTS.md](AGENTS.md)）。

```
/business-analysis {code}
         │
    ┌────▼────────────────┐
    │ 年报 PDF 下载/缓存   │  glob 检查 → 按需下载
    └────┬────────────────┘
         │
    ┌────▼────────────────┐
    │ Tushare 数据采集     │  历史序列补充（§1–§17）
    └────┬────────────────┘
         │
    ┌────▼────────────────────────┐
    │ 切片 + 6 维度并行 + 综合      │  dimension_slicer → slices/
    │ D1–D6 契约化 JSON → 报告      │  每维 ≤12K 字符，契约校验
    └────┬────────────────────────┘
         │
    report.md + report.html
```

### 各阶段角色

- **Phase 0** — 调用 `/download-annual-report`（或 `download-annual-report` skill）自动搜索并下载最新年报 PDF
- **Phase 1A** — `tushare_collector.py` 通过 Tushare Pro API 采集结构化数据，输出 `data_pack_market.md`（顶层 §1–§17；§8/§10 为待补占位符）
- **Phase 1B** — Agent 执行 web_search 补 §8 行业与竞争 / §10 管理层讨论等非结构化信息
- **Phase 2A** — `pdf_preprocessor.py` 使用关键词匹配定位年报 7 个目标章节，输出 `pdf_sections.json`
- **Phase 2B** — Agent 从 PDF 章节中精确提取附注数据
- **Step 2** — `dimension_slicer.py` 按目录/章节切片（预算硬上限）→ 6 个维度子代理并行产出契约化 JSON（`validate_dim_result.py` 硬校验）→ 综合代理只读 JSON 组装报告
- **Phase 3** — 定性分析（切片并行）+ 定量分析（含 Pre-flight Step 0）+ 估值（龟龟专属），输出 MD + HTML 双格式报告

## 快速开始

### 环境要求

- Python >= 3.10
- [Tushare Pro](https://tushare.pro/) 账号及 API Token
- （可选）pdfplumber 用于 PDF 解析
- （内置）`/download-annual-report` 命令（DSH 下为 `download-annual-report` skill）用于自动下载年报

### 安装

**首次安装：**

```bash
git clone https://github.com/terancejiang/Turtle_investment_framework.git
cd Turtle_investment_framework

# 一键初始化（创建 venv、安装依赖、验证环境）
bash init.sh
```

**更新已有项目：**

```bash
cd Turtle_investment_framework
git pull

# 重新安装依赖（确保新增的包被安装）
bash init.sh --force-install
```

`init.sh` 会自动完成：
1. 查找系统中 Python >= 3.10，创建 `.venv`
2. 安装 `requirements.txt` 中的依赖
3. 检查 `TUSHARE_TOKEN` 环境变量
4. 运行测试验证环境

### 配置 Tushare Token

```bash
cp .env.sample .env
# 编辑 .env，填入你的 Token
# TUSHARE_TOKEN=your_token_here
```

或者直接设置环境变量：

```bash
export TUSHARE_TOKEN='your_token_here'
```

## 使用方法

### 单股分析

**DeepSeek Harness（推荐）**：读 [AGENTS.md](AGENTS.md) 按流程执行，或直接调用同名 skill
（`turtle-analysis` / `business-analysis` / `valuation` / `tushare` / `download-annual-report`）。

**Claude Code**：使用 slash command（与 coordinator.md 保持同步）：

```
/turtle-analysis 600887          # 完整龟龟策略（定性 + 定量 + 估值）
/business-analysis 600887        # 独立商业分析（切片 + 六维度并行）
/valuation 600887                # 独立估值分析（自动分类 + 多方法估值）
```

`/turtle-analysis` 自动执行 Phase 0 → 1A → 1B → 2A → 2B → 3 完整流程。
`/business-analysis` 切片 + 六维度并行定性分析（年报切片 + Tushare 数据 + 契约校验）。
`/valuation` 独立估值分析，需先运行 `/business-analysis`（依赖定性报告 + 市场数据）。

### 数据采集（仅 Phase 1A）

```bash
# 基本用法
.venv/bin/python scripts/tushare_collector.py --code 600887.SH

# 指定输出路径
.venv/bin/python scripts/tushare_collector.py --code 600887.SH \
  --output output/600887_伊利股份/data_pack_market.md

# 附加额外字段
.venv/bin/python scripts/tushare_collector.py --code 00700.HK --extra-fields balancesheet.defer_tax_assets

# 仅刷新行情相关小节（保留人工补充的 §14）
.venv/bin/python scripts/tushare_collector.py --code 600887.SH --refresh-market

# 无 yc_cb 权限时显式指定无风险利率（或设 TUSHARE_RF）
.venv/bin/python scripts/tushare_collector.py --code 600887.SH --rf 1.688

# 试运行（不调用 API）
.venv/bin/python scripts/tushare_collector.py --code 600887 --dry-run
```

输出 Markdown 包含以下数据段：

| 段号 | 内容 | 来源 |
|------|------|------|
| §1 | 基本信息 | stock_basic + daily_basic |
| §2 | 市场行情（52 周范围） | pro_bar weekly |
| §3 | 合并利润表（5 年） | income |
| §3P | 母公司利润表 | income (report_type=6) |
| §4 | 合并资产负债表（5 年） | balancesheet |
| §4P | 母公司资产负债表 | balancesheet (report_type=6) |
| §5 | 现金流量表 + FCF | cashflow |
| §6 | 分红历史 | dividend |
| §7 | 股东与治理（部分） | top10_holders |
| §8 | 行业与竞争 | **占位符 → Phase 1B（web_search）** |
| §9 | 主营业务构成 | fina_mainbz |
| §10 | 管理层讨论与分析 | **占位符 → Phase 1B（web_search）** |
| §11 | 十年周线行情 | weekly |
| §12 | 关键财务指标（ROE/毛利率等） | fina_indicator |
| §13 | 风险警示 | 自动检测 + Agent 补充 |
| §14 | 无风险利率 | yc_cb（无权限时用 `--rf` / `TUSHARE_RF` / 回读） |
| §15 | 股份回购 | repurchase |
| §16 | 股权质押 | pledge_stat |
| §17 | 衍生指标预计算（Python，含 17.x 小节） | compute_derived_metrics |

### 年报解析（仅 Phase 2A）

```bash
# 基本用法
.venv/bin/python scripts/pdf_preprocessor.py --pdf 伊利股份_2024_年报.pdf

# 指定输出 + 详细日志
.venv/bin/python scripts/pdf_preprocessor.py --pdf report.pdf --output output/pdf_sections.json --verbose

# 使用 TOC hints 覆盖关键词匹配
.venv/bin/python scripts/pdf_preprocessor.py --pdf report.pdf --hints toc_hints.json
```

提取的 7 个目标章节：

| 缩写 | 章节 | 说明 |
|------|------|------|
| P2 | 公司治理 | 公司治理结构 |
| P3 | 会计政策 | 重要会计政策和会计估计 |
| P4 | 应收账款 | 应收账款/票据附注 |
| P6 | 合并报表 | 合并财务报表附注 |
| P13 | 风险 | 风险提示/重大事项 |
| MDA | 管理层讨论 | 经营情况讨论与分析 |
| SUB | 子公司 | 主要子公司/长期股权投资明细 |

### 批量选股（龟龟选股器）

```bash
# 完整流程（Tier 1 + Tier 2）
.venv/bin/python scripts/screener_core.py

# 仅 Tier 1 快速筛选
.venv/bin/python scripts/screener_core.py --tier1-only

# 限制 Tier 2 分析数量
.venv/bin/python scripts/screener_core.py --tier2-limit 50

# 自定义阈值
.venv/bin/python scripts/screener_core.py --min-roe 10 --max-pe 30 --min-gross-margin 20

# 导出结果
.venv/bin/python scripts/screener_core.py --csv output/screener.csv --html output/screener.html

# 刷新缓存
.venv/bin/python scripts/screener_core.py --cache-refresh          # 全部刷新
.venv/bin/python scripts/screener_core.py --cache-tier2-refresh    # 仅刷新 Tier 2
```

### Jupyter Notebook

```bash
cd notebooks
jupyter notebook screener.ipynb
```

`screener.ipynb`（17 个 Cell）：初始化 → Tier 1 过滤 → 排名 → Tier 2 分析 → 评分 → 导出 → 个股详情，附 matplotlib 可视化；`screener_hk.ipynb` 为港股版本。

## 项目结构

```
Turtle_investment_framework/
├── AGENTS.md                       # 平台中立入口（DSH：读 coordinator + 跑脚本）
├── .agents/skills/                 # DSH 技能（DSH 从此路径发现）
│   ├── tushare/                    #   Tushare 采集用法 / §1–§17 / --rf
│   ├── business-analysis/          #   切片 → 六维度并行 → 综合
│   ├── valuation/                  #   估值引擎 + 定性调整
│   ├── turtle-analysis/            #   龟龟策略全流程
│   └── download-annual-report/     #   年报 PDF 搜索与下载
├── .claude/                        # Claude Code 兼容层
│   ├── commands/                   #   slash commands（与 coordinator.md 同步）
│   └── skills/                     #   → ../../.agents/skills/* 软链（单一来源）
├── shared/                         # 共享模块
│   └── qualitative/                #   通用定性分析模块
│       ├── coordinator.md          #     v3 入口（切片 + 六维度并行 + 综合）
│       ├── qualitative_assessment.md  #  v3 6 维度框架（按维度分段消费）
│       ├── data_collection.md      #     数据采集指令（web_search 兜底）
│       ├── agents/                 #     cleanroom_audit / numeric_audit / writing_style
│       ├── references/             #     output_schema · dim_result.schema.json + contract ·
│       │                           #     writing_style_rules · judgment_examples ·
│       │                           #     framework_guide / framework_scope ·
│       │                           #     industry_metrics_lookup · market_rules_hk/us
│       └── templates/dashboard.html
├── strategies/                     # 策略专属模块
│   ├── turtle/                     #   龟龟策略
│   │   ├── coordinator.md          #     入口
│   │   ├── phase2_PDF解析.md        #     PDF 附注提取规范
│   │   ├── phase3_quantitative.md  #     Pre-flight Step 0 + 穿透回报率（11 步）
│   │   ├── phase3_valuation.md     #     估值 + 报告组装
│   │   ├── legacy/                 #     v1 归档提示词
│   │   └── references/             #     shared_tables · factor_interface · judgment_examples_turtle
│   └── valuation/                  #   独立估值模块
│       ├── coordinator.md          #     入口
│       ├── phase2_valuation.md     #     估值方法 + 报告组装
│       └── references/             #     classification_rules · valuation_methods ·
│                                   #     valuation_examples · report_template
├── scripts/                        # Python 脚本（确定性计算，零 LLM 调用）
│   ├── tushare_collector.py        #   采集门面（--rf / --refresh-market / --dry-run）
│   ├── tushare_modules/            #   模块化实现（constants / infrastructure / financials /
│   │                               #   other_data / derived_metrics / yfinance_integration / assembly）
│   ├── quality_control.py          #   CM§1–§5 确定性预算指标
│   ├── dimension_slicer.py         #   按维度切片 + 覆盖 manifest（Step 2.0）
│   ├── validate_dim_result.py      #   L1 契约校验（硬失败 + ≤2 重试）
│   ├── report_consistency.py       #   跨段一致性 + 硬门槛（Step 3A）
│   ├── extract_pdf_text.py         #   PDF → 带页标文本（切片/审计用）
│   ├── pdf_preprocessor.py         #   年报章节定位（7 个目标章节）
│   ├── valuation_engine.py         #   估值引擎（DCF/DDM/PEG/PE Band/PS）
│   ├── report_to_html.py           #   MD → HTML（--standalone）
│   ├── download_report.py          #   年报 PDF 下载
│   ├── rf_utils.py                 #   §14 无风险利率解析与回读
│   ├── screener_config.py / screener_core.py    # 选股器（两级筛选）
│   └── cache_utils.py / config.py / format_utils.py / generate_available_fields.py
├── prompts/                        # v1 提示词（只读遗留）
├── ciguttprepare/                  # 烟蒂策略准备（独立模块）
├── financial-report-minesweeper/   # 财报排雷（独立模块）
├── experiments/ab_phase2/          # 阶段 2 A/B 方案（已设计，未执行）
├── notebooks/                      # screener.ipynb（17 cells）· screener_hk.ipynb
├── tests/                          # 测试套件（29 个文件 / 1094 passed, 3 skipped）
├── output/                         # 输出目录（gitignored）
├── init.sh · requirements.txt · skills-lock.json · CHANGELOG_V2.md
```

## 分析框架详解

### 定性分析（共享模块，6 维度）

v3 的定性分析采用 **切片 + 六维度并行 + 综合** 架构：`dimension_slicer.py` 按目录/章节把年报切成每个维度一份的切片（预算硬上限、排除封面与目录页），6 个维度子代理各自读取**逐字节相同的共享前缀**（数据包 + CM + 写作规范 + schema）与**自己的切片**，产出符合 `dim_result.schema.json` 的契约化 JSON，再由综合代理只读这 6 份 JSON 组装报告并显式裁决跨维度冲突。

> 为什么不再用"单 Agent + 全文载入"：真实年报正文 201K–573K 字符，叠加固定输入后达 245K–617K 字符，**超出上下文窗口**。切片后单次调用峰值实测 **43–45K 字符**（含必须下发的 data pack 与 CM；相对最小样本 ↓81.5%），窗口余量仍有 2.8–4.9×。

| 维度 | 内容 | 说明 |
|------|------|------|
| D1 商业模式 | 轻/重资产判别、利润结构、资产质量 | 核心维度 |
| D2 护城河 | Greenwald 三维框架（供给侧/需求侧/规模经济）+ 竞对对比 | 6步结构化分析 |
| D3 外部环境 | 行业周期、政策、竞争格局 | — |
| D4 管理层 | 治理结构、激励机制、历史诚信 | — |
| D5 MD&A | 管理层讨论分析、战略一致性 | — |
| D6 控股结构 | 母/子公司关系、关联交易 | 条件触发 |

**D2 护城河分析（v2.0 重大升级）**：6 步结构化分析 — 行业地图 → 量化验证（ROE 门槛 8/15/25%）→ 双框架分析（非技术+技术 × 供给/需求/规模）→ 虚假优势辨析 → 竞对对比表 → 可持续性监控锚点

### 四因子模型（龟龟策略专属）

在定性分析基础上，龟龟策略增加穿透回报率计算和估值：

**因子 2：穿透回报率粗算**
- 从 §17.2 读取 C/B/M/N/OE 等参数 + OE 纠偏
- 分母用市值，R% 与门槛 II 比较（`max(3.5%, Rf+2%)` A股 / `max(5%, Rf+3%)` 港股）
- 计算穿透回报率 R% = M × (1 − 分红税率) × OE
- **无否决门**：v2.0 起所有因子只输出客观评估，由 Agent C 统一裁决（v1.1 的 9 个否决门已移除）

**因子 3：穿透回报率精算**
- 真实现金收入（S/T/U、保守基础、收款比率）
- 经营性流出（W1–W4：供应商/员工/税/利息）
- 基础盈余、AA（含/不含资本化）、CV、λ 系数
- 分配意愿（M）、可预测性
- 输出：精算后 R% + 可靠性标签

**因子 4：估值与安全边际**
- 相对估值（PE/PB 分位数、历史对比）
- 绝对估值指标（EV/EBITDA、现金调整 PE、FCF 收益率等 11 项）
- 基准价（5 种方法取算术平均）+ 溢价分析
- 输出：买入/观察/规避 评级

### 龟龟选股器

两级筛选系统，从全 A 股 5000+ 只中筛选优质标的：

**Tier 1 — 批量过滤（仅市场数据，~5 秒）**

| 过滤条件 | 默认阈值 |
|----------|----------|
| 排除 ST/PT/退市整理 | — |
| 上市年限 | ≥ 3 年 |
| 市值 | ≥ 5 亿元 |
| 日换手率 | ≥ 0.1% |
| PB | 0 < PB ≤ 10 |
| 股息率 | > 0 |
| PE（主通道） | 0 < PE_TTM ≤ 50 |
| PE（观察通道） | PE_TTM < 0，按市值取前 50 |

排名公式：`Score = 0.4 × dv_ttm + 0.3 × (1/PE) + 0.3 × (1/PB)`

主通道取前 150 名 + 观察通道 50 名 → 共 200 只进入 Tier 2。

**Tier 2 — 深度分析（逐只调用 API）**

- 硬否决：质押比 > 70%、非标审计意见
- 财务质量：ROE ≥ 8%、毛利率 ≥ 15%、资产负债率 ≤ 70%
- 因子 2 指标：分配意愿 M、穿透率 R、Threshold II
- 因子 4 指标：EV/EBITDA、现金调整 PE、FCF 收益率、商誉占比
- 基准价：5 种方法（净流动资产/BVPS/10 年低点/股息隐含/悲观 FCF）取算术平均

**底价（Floor Price）— 5 种方法取算术平均**

底价是多维度安全边际锚点，综合物理资产、历史价格、分红能力和现金流生成能力：

| # | 方法 | 公式 | 说明 |
|---|------|------|------|
| 1 | 净流动资产/股 | (现金 + 交易资产 − 有息负债) / 总股数 | 清算视角的底线价值 |
| 2 | 每股净资产 (BVPS) | 归属股东权益(不含少数) / 总股数 | 账面价值锚点 |
| 3 | 10 年历史最低价 | 过去 10 年周收盘价最小值 | 历史极端情绪底部 |
| 4 | 分红折现价 | 近 3 年平均每股分红 / max(Rf, 3%) | 股息率等于折现率时的价格 |
| 5 | 悲观 FCF 资本化价 | 近 5 年最小 FCF / Rf% / 总股数 | 仅当 5 年 FCF 全为正时有效 |

复合基准取有效方法值的 **算术平均**。底价溢价率 = (当前价 / 基准价 − 1) × 100%。

| 溢价率 | 区间含义 |
|--------|----------|
| ≤ 0% | "买入就是胜利"区间 — 当前价低于底价 |
| 0–30% | 安全边际充足 |
| 30–80% | 合理溢价，需成长验证 |
| > 80% | 高溢价，需强成长预期支撑 |

**综合评分权重**

| 维度 | 权重 |
|------|------|
| ROE | 20% |
| FCF 收益率 | 20% |
| 穿透率 R | 25% |
| EV/EBITDA（逆序） | 15% |
| 基准价溢价（逆序） | 20% |

## 测试

```bash
# 运行全部测试（1094 passed, 3 skipped）
.venv/bin/python -m pytest tests/ -v

# 运行单个测试文件
.venv/bin/python -m pytest tests/test_screener.py -v

# 快速模式（遇到失败即停止）
.venv/bin/python -m pytest tests/ -x -q

# 查看覆盖率
.venv/bin/python -m pytest tests/ --cov=scripts --cov-report=term-missing
```

测试覆盖范围（29 个测试文件，含 L1 契约 / 切片器 / 精度回归 / v3 流水线守卫 / DSH 工具名守卫）：
- 配置与工具函数（`test_config.py`, `test_format_utils.py`）
- Tushare 数据采集 + 衍生指标（`test_tushare_client.py`, `test_derived_metrics.py`）
- PDF 预处理（`test_pdf_preprocessor.py`）
- LLM 提示词验证（`test_phase1b_prompt.py`, `test_phase2b_prompt.py`, `test_phase3_prompt.py`）
- 协调器与集成测试（`test_coordinator.py`, `test_integration.py`）
- 输出格式验证（`test_output_format.py`）
- 年报下载（`test_download_report.py`）
- 选股器全流程（`test_screener.py`）
- 增量刷新模式（`test_refresh_market.py`）
- v3 专项：`test_dim_result_contract.py`（契约）、`test_dimension_slicer.py`（切片）、`test_validate_dim_result.py`（硬校验）、`test_v3_pipeline_prompts.py`（流水线接线）、`test_report_consistency_precision.py`（冲突识别精度）、`test_dsh_tool_names.py`（平台去耦）

所有测试使用 Mock 数据，不需要 Tushare Token 即可运行。

## 技术细节

### 数据单位约定

数据包与结构化参数表的金额统一为 **百万元（RMB）**：Tushare 返回的原始数据（元）在采集时自动除以 1,000,000，并使用千分位格式化。

**定性报告正文为可读性统一用亿元**（取自 `computed_metrics.md` 的 CM§1 亿元对照表），引用时标 `[src: CM§1]`；两套单位并存但各有唯一来源，LLM 不做换算。

### 缓存策略

选股器使用 Parquet 格式的分层缓存：

| 数据类型 | 缓存 TTL |
|----------|----------|
| stock_basic（全量股票列表） | 7 天 |
| daily_basic（每日行情） | 当日有效 |
| Tier 2 财务数据（年报类） | 168 小时（7 天） |
| Tier 2 行情数据（周线） | 24 小时 |
| 全局数据（无风险利率） | 24 小时 |

缓存目录：`output/.screener_cache/`

### 速率限制

Tushare API 调用默认每次间隔 **0.5 秒**（环境变量 `TUSHARE_RATE_DELAY` 可调，设为 `0` 关闭限速）；失败重试按 `2.0s × 尝试次数` 递增退避。

## 开发指南

### 功能开发流程

1. 先读 [AGENTS.md](AGENTS.md) 与相关 `coordinator.md`，确认改动落在哪一层
   （**Python 做确定性计算**、**提示词做定性判断**，不要把算术搬回 LLM）
2. 改 Python 必须同步改测试（不允许跳过测试）；改提示词必须同步对应断言
   （`test_*_prompt*.py` 以及 `tests/test_v3_pipeline_prompts.py`）
3. 跨平台措辞只用中立工具名（`web_search` / `subagent` / `bash` / `read` / `edit` …），
   `tests/test_dsh_tool_names.py` 会拦截 Claude 专有名称
4. 运行 `.venv/bin/python -m pytest tests/ -q` 必须全绿；改动交付契约时同时更新
   `shared/qualitative/references/dim_result.schema.json` 与 `dim_result_contract.md`
5. 在 [CHANGELOG_V2.md](CHANGELOG_V2.md) 记录变更（含实测数据，便于日后复核）

### 提交规范

```
feat(scope): description
fix(scope): description
test(scope): description
```

Scope 用模块名：`tushare` / `qualitative` / `valuation` / `screener` / `pipeline` / `docs` 等。

### 里程碑

| 标签 | 范围 | 状态 |
|------|------|------|
| v1.0-alpha | 基础设施 (#1–#8) | 已完成 |
| v1.0-beta | 全部脚本 (#1–#47) | 已完成 |
| v1.0 | 完整 pipeline + 提示词 v1 | 已完成 |
| v1.1 | 17 improvements, HK/US support, shared_tables | 已完成 |
| v2.0-alpha | 模块化拆分 + Greenwald 护城河 + Agent Team + HTML 仪表盘 | 已完成 |
| v2.0-beta | PDF-first 单 Agent + 估值模块 + Pre-flight 合并 + 实战验证 | 已完成 |
| **v3** | **定性分析改切片 + 六维度并行 + 契约化交接（`dim_result.schema.json`）+ 平台去耦（DSH）** | **当前** |
| v3.1 | 烟蒂策略接入 shared 模块 + 端到端调优 | 计划中 |

## 依赖

```
tushare>=1.2.89       # A 股数据接口
pandas>=1.5.0         # 数据处理
pdfplumber>=0.9.0     # PDF 文本提取
requests>=2.28.0      # HTTP 请求
pytest>=7.0.0         # 测试框架
pyarrow>=10.0.0       # Parquet 缓存
matplotlib>=3.5.0     # 可视化（Notebook）
tqdm>=4.60.0          # 进度条
jupyter>=1.0.0        # Notebook 运行环境
jinja2>=3.0.0         # HTML 模板（报告 + 选股器导出）
jsonschema>=4.0.0     # L1 维度结果契约校验（validate_dim_result.py）
markdown>=3.4.0       # MD → HTML 转换（report_to_html.py）
yfinance>=0.2.0       # 港股/美股行情回退
```

## License

MIT
