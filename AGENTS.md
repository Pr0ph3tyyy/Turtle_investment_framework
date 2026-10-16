# AGENTS.md — running this repo from DeepSeek Harness (DSH)

Platform-neutral entry point assuming **no Claude Code slash commands**: start any workflow by reading its
coordinator markdown and running shell commands, with DSH tool names (`web_search`, `ask_user_question`,
`skill`, `subagent`, `read`, `edit`, `bash`; the Claude equivalents map one-to-one).

## What this project is

A Chinese A-share / HK / US equity research framework. **Python does all deterministic work** — Tushare Pro API
collection, market-data refresh, pre-computed metric blocks (§17 grids, payout-ratio cross-checks, valuation engine
outputs). **The LLM does qualitative judgement and factor selection** — reading annual reports, filling the
§7/§8/§10 placeholders, scoring the 6 dimensions, choosing scenarios, writing the prose. Prompt rules forbid the LLM
from redoing arithmetic Python already did.

## Conventions

- **One stock = one working directory**: every artifact lives in `output/{code}_{company}/`
  (e.g. `output/600887_伊利股份/`). Create it with `mkdir -p output/{code}_{company}`;
  nothing else in the repo is written at runtime.
- **Environment**: `bash init.sh` creates `.venv/`; prefer `.venv/bin/python`, fall back to
  `python3`. Copy `.env.sample` to `.env` and set `TUSHARE_TOKEN`.
- `.claude/commands/*.md` are **Claude Code slash commands** and are not invokable on DSH —
  read the coordinator markdown listed per workflow instead. The
  `.claude/skills/tushare/SKILL.md` skill documents the collector flags.
- **交付格式：不要生成 `.docx`（Word）。** 用户明确要求过（再次重申于 2026-10-16），
  **阅读版用 markdown 即可**。具体：
  - 「阅读版」= 在原 markdown 旁**另存一份可读的 `.md`**（或 `report_to_html_reading.py`
    产出的**自包含 HTML**），**不要**跑 `scripts/report_to_docx.py`；
  - 不要在用户没点名 Word 时"顺手"生成 `.docx`；一次违规要清理产物，成本高于收益
    （历史遗留：`output/_legacy/00506_中国食品/中国食品_00506_分析报告_阅读版.docx`，
    以及 06078 本轮被删除的 `海吉亚医疗_06078_分析报告_阅读版.docx`）；
  - `scripts/report_to_docx.py` **保留但默认不调用** —— 仅当用户**显式**点名
    「Word / docx」时才用（那时请用工作区自带 Python 运行时，因为 `.venv` 未装 `python-docx`）。
  - 常规分享产物是：`{公司}_{代码}_精华版.md`、`{公司}_{代码}_精华长图.png`（+ 分P）、
    可选的 `_阅读版.html`。

---

## Workflow 1 — 商业分析 / 定性分析 (equivalent of `/business-analysis <code>`)

**Read first:** `shared/qualitative/coordinator.md`
(mirror: `.claude/commands/business-analysis.md`; keep the two in sync).
**Prerequisites:** none — this produces the data pack the other workflows consume.

```bash
mkdir -p output/{code}_{company}
.venv/bin/python scripts/tushare_collector.py --code <code> \
  --output output/{code}_{company}/data_pack_market.md
.venv/bin/python scripts/quality_control.py \
  --input  output/{code}_{company}/data_pack_market.md \
  --output output/{code}_{company}/computed_metrics.md
```

Then follow the coordinator: acquire the annual-report PDF (Workflow 4 or a user-provided path/URL), use
`web_search` as the §7/§8/§10 fallback (`shared/qualitative/data_collection.md`), then run the sliced
parallel analysis:

```bash
# 2.0 deterministic slicing — per-dimension slices + coverage manifest, hard char budget
.venv/bin/python scripts/dimension_slicer.py --code <code> \
  --output-dir output/{code}_{company} --budget-chars 12000 --report-coverage
# scanned PDF (no text layer): add --vision to render the selected pages as images

# 2.1 six dimension subagents in parallel → dims/D1..D6.json, then validate (hard failure):
.venv/bin/python scripts/validate_dim_result.py --input output/{code}_{company}/dims

# 2.2 one synthesis subagent reads ONLY the six JSON files → qualitative_report.md
#     (contract: shared/qualitative/references/dim_result.schema.json;
#      run the Step 2X clean-room recompute in parallel with 2.1)
```

After collection, the pack-level **invariant engine** must be clean (it runs automatically at the end of
`tushare_collector.py` and writes `invariants.json`):

```bash
.venv/bin/python scripts/invariants.py --pack output/{code}_{company}/data_pack_market.md \
  --fx output/{code}_{company}/fx.json           # exit 0 = no FAIL
```

It mechanically checks cross-section relations that neither the Python arithmetic nor the report-side
numeric check can see — period labelling on CAGR, per-share currency, §5↔§4 cash tie-out, dividend
integrity, sensitivity direction. **A FAIL means the affected numbers must not be quoted**; fix the data
or stop and report (do not hand-calculate around it).

Invariants only test **internal consistency** — they cannot see a *missing row*. That gap is covered by
the **field-coverage** gate (also run automatically at the end of the collector → `field_coverage.json`):

```bash
.venv/bin/python scripts/field_coverage.py --pack output/{code}_{company}/data_pack_market.md
```

- **C1** required rows per statement (§3/§4/§5) must exist;
- **C2** debt composition must be verifiable: every debt class needs its own §4 row, and a row that is
  **declared but empty in every year** is a FAIL — that is the signature of a mapping label that misses
  the upstream field (real case: `bond_payable` was mapped to `应付票据(非流动)` while upstream carries
  `应付债券` → the 2,028,881k bond never landed → interest-bearing debt understated by 20.29 亿).

**Slice coverage is a hard gate too**: `validate_dim_result.py` now fails when `dropped_pages > 0`
without a `slice_truncated` flag, when the char budget was exhausted without that flag, or when an
`annual_report` citation points to a page **outside** the pages that dimension was given (±1 tolerance
for the slice/printed page offset). *You may only cite what you were handed.*

**Stage hand-off is hash-pinned**: the collector writes `{output_dir}/pack_manifest.json` (md5 + size of
the pack and its sidecars). Downstream stages must verify before they read — **a change is an error, not a
silent pass** (three real incidents: an upstream sidecar edited mid-run made the report self-contradictory;
two half-written reads; one audit that read a stale baseline):

```bash
.venv/bin/python scripts/stage_manifest.py --check output/{code}_{company}/pack_manifest.json
```

**Bare numbers are now counted**: `report_consistency.py --claims --gates` reports bound vs unbound
numeric tokens and the binding-coverage ratio per section (measurement only). Set
`--min-claim-coverage <PCT>` to turn that into a gate.

The synthesis step adjudicates cross-dimension conflicts explicitly and lists every dimension whose
`material_status != "充分"` in the report Warnings. Then the mandatory audit gate:

```bash
.venv/bin/python scripts/report_consistency.py \
  --report output/{code}_{company}/qualitative_report.md \
  --output output/{code}_{company}/consistency_report.md --gates
```

**Outputs:** `qualitative_report.md` (the only deliverable); supporting `data_pack_market.md` and
`data_pack_report.md` (PDF footnotes, feeds Workflows 2–3); internal non-deliverables `slices/**`,
`dims/*.json`, `computed_metrics.md`, `cleanroom_metrics.md`, `consistency_report.md`, `audit.md`.
Optional HTML (`scripts/report_to_html.py --standalone`) only when the user asks.

## Workflow 2 — 龟龟策略 full pipeline (equivalent of `/turtle-analysis <code>`)

**Read first:** `strategies/turtle/coordinator.md`
(mirror: `.claude/commands/turtle-analysis.md`).
**Prerequisites:** `output/{code}_{company}/qualitative_report.md` and
`data_pack_market.md` (Workflow 1). `data_pack_report.md` is optional — the quantitative
agent degrades gracefully without PDF footnote data.

```bash
.venv/bin/python scripts/tushare_collector.py --code <code> \
  --output output/{code}_{company}/data_pack_market.md --refresh-market
```

> **采集现在是 fail-closed（2026-10-14）**：采集结束时会跑不变式 + 字段覆盖，按 FAIL 分级裁决，
> 结果写入 `{output_dir}/gate_status.json`（`deliverable` / `degraded` / `blocking[]`），
> **存在阻断级失败时退出码为 1**。阻断级 = 会改变结论数字的那几类（`C1/C2` 与
> `I1/I2/I4/I7/I8/I9`）；`I3/I5/I6/I10` 属披露级（相关数字不得引用，但不阻断）。
> 确知影响面而仍要交付时，用 `--allow-degraded` 显式降级 —— 它会把 `degraded: true`
> 与失败清单写进 `gate_status.json`，**必须随报告披露**。旧行为（只 print、永远退出 0）
> 已废止：实测 `invariants` FAIL 3 + `field_coverage` FAIL 1 的产物照样交付。

Then follow the coordinator: Step 3.1 quantitative `subagent` (reads `strategies/turtle/phase3_quantitative.md`),
then Step 3.2 valuation/report `subagent` (reads `strategies/turtle/phase3_valuation.md`).

**审核一（数值，报告之前）**：Agent B 跑完、Agent C 动笔**之前**必须先过数值审核 ——
`numeric_audit.py` 按 `shared_tables.md` **独立重写**一遍公式（不 import `derived_metrics`），
只读数据包 §1–§16 的表格与 §17 对账，再交子代理逐条裁定：

```bash
.venv/bin/python scripts/numeric_audit.py \
  --pack output/{code}_{company}/data_pack_market.md --output-dir output/{code}_{company}
# → numeric_audit.md（裁定工作单）+ numeric_audit.json；再按
#   shared/audit/numeric_audit_agent.md 跑裁定 → numeric_audit_verdict.md
#   末行 NUMERIC_AUDIT_RESULT: PASS | FIX_REQUIRED
```

**审核二（观点，报告之后）**：报告写完、交付之前，检查结论是否由它自己引用的数字按文档规则
得出、是否自洽、有没有被明令禁止的读数、上游限制有没有带进结论：

```bash
.venv/bin/python scripts/viewpoint_audit.py \
  --report "output/{code}_{company}/{company}_{code}_分析报告.md" \
  --output-dir output/{code}_{company} --strict
# → viewpoint_audit.md + viewpoint_audit.json；再按 shared/audit/viewpoint_audit_agent.md
#   跑裁定 → viewpoint_audit_verdict.md（含论证质量六类）→ 末行 VIEWPOINT_AUDIT_RESULT
```

Then the **交付门禁**（四条命令，全部为交付前置；任一条非零即不得交付）：

```bash
# ① 声明绑定：报告里每个 §17 数字必须机械对上 claims.json
#    （--strict 会启用默认覆盖率下限 20% + UNEVALUATED 计失败）
.venv/bin/python scripts/report_consistency.py \
  --report "output/{code}_{company}/{company}_{code}_分析报告.md" \
  --claims output/{code}_{company}/claims.json --gates --strict

# ② 报告级不变式：保真（I8）+ 规则序（I9）
#    I9 在给了 --claims 时**从账本取值**，不再从正文正则抓（正文可被两处同时改写洗白）
.venv/bin/python scripts/check_report_fidelity.py \
  --report "output/{code}_{company}/{company}_{code}_分析报告.md" \
  --dims   output/{code}_{company}/dims \
  --claims output/{code}_{company}/claims.json --strict

# ③ 上游冻结：确认数据包与 sidecar 未被中途改动
.venv/bin/python scripts/stage_manifest.py --check output/{code}_{company}/pack_manifest.json

# ④ 观点审核（结论自洽性 + 引用合规 + 禁令 + 披露闭环）
.venv/bin/python scripts/viewpoint_audit.py \
  --report "output/{code}_{company}/{company}_{code}_分析报告.md" \
  --output-dir output/{code}_{company} --strict
```

**Outputs:** `phase3_quantitative.md` (intermediate) and the final `{company}_{code}_分析报告.md`.

## Workflow 3 — 估值 (equivalent of `/valuation <code>`)

**Read first:** `strategies/valuation/coordinator.md`
(mirror: `.claude/commands/valuation.md`).
**Prerequisites:** `output/{code}_{company}/qualitative_report.md` and
`data_pack_market.md` (Workflow 1 — the coordinator stops if either is missing).

```bash
.venv/bin/python scripts/valuation_engine.py --code <code> \
  --output-dir output/{code}_{company}
```

Then read `strategies/valuation/phase2_valuation.md` (with `references/valuation_methods.md` and
`references/report_template.md`) and apply the qualitative adjustments — no arithmetic needed; the engine already
emitted the sensitivity tables.

**Outputs:** `valuation_computed.md` and the final `{company}_{code}_估值报告.md`.

## Workflow 4 — annual-report download (used by Workflows 1–3)

**Read first:** `.claude/commands/download-annual-report.md` — it defines the PDF search
priority (巨潮资讯网 cninfo → 雪球 stockn.xueqiu → 同花顺 10jqka) and the exclusion
keywords. Use `web_search` to find a direct PDF link, then:

```bash
.venv/bin/python scripts/download_report.py \
  --url "<PDF_URL>" --stock-code "<SH600887|00700>" \
  --report-type "年报" --year "<year>" --save-dir output/{code}_{company}
```

**Output:** `output/{code}_{company}/{code}_{year}_{report_type}.pdf`. If no PDF can be
obtained, Workflow 1 falls back to `web_search` and Workflows 2–3 use degraded mode.

## Workflow 5 — 精华长图 (B 站分享，插件 `share_longimage`)

把某个个股产物目录里**已审计的成果文件**渲染成一张中文精选长图 + 分P图片。版式为 Claude
文档风格；刻度按手机看长图定标（1080 宽下正文 30px，约合 11–12pt），因此**不是**桌面网页字号。

插件源码在 `plugins/dsh-share-longimage/`，作为 bundle `dsh-plugin-share-longimage`
装进当前 profile（`enabled` 已选；**宿主插件改代码后需要重启 DSH**，本进程按 bundle 名缓存模块）。

```bash
# 通过工具调用（正常路径）
share_longimage(code="06862.HK")
share_longimage(dir="output/600941_中国移动", theme="ink", panelHeight=1600)
share_longimage(dir="output/06862_海底捞", brief="output/06862_海底捞/brief.json")

# 或者绕过 DSH 直接跑渲染脚本
.venv/bin/python plugins/dsh-share-longimage/assets/render_longimage.py --dir output/06862_海底捞
```

**产出**（都在个股产物目录里）：

- `{公司}_{代码}_精华长图.png` — 无缝长图（主产物）
- `{公司}_{代码}_精华长图_分P/*.png` — 按**内容块边界**切好的分P，适配 B 站动态最多 9 图；切分只落在区块边界，不会截断一行字
- `_share_longimage.html` — 中间 HTML，版式可复核、可手动改

**纪律**：内容由 Python 从报告与 sidecar 文件机械摘录（claim 标记剥离为纯文本），
**不做任何新计算、不推断、不改写数值**；「可信度」区块直接读 `invariants.json` /
`section17_status.json` / `claims.json` / `dims/` / `consistency_report.md`，不自行判定。
缺料整块略去（例如没有 dims 就没有六维速览），缺 Chrome 时如实报错并保留 HTML，不伪造图片。
`brief.json` 可选，只覆盖它声明了的字段（空模板见插件 `assets/brief.example.json`）。

**测试**：`node --test plugins/dsh-share-longimage/tests/host.test.mjs`（注册契约、
JSON Schema 子集、参数 fail-closed、结果键投影）与
`.venv/bin/python -m pytest tests/test_share_longimage.py`（抽取、分P 装箱、中文排版、sidecar 门禁）。

## Workflow 6 — 每日选股（龟龟选股器 → 前 N 只跑 Workflow 2）

**读 first:** `strategies/screener/coordinator.md`
**技能:** `turtle-screener`（`.agents/skills/turtle-screener/SKILL.md`）
**设计依据:** `screener_daily_automation_design.md`；上游缺陷台账 `turtle_core_extraction_findings.md`

把「全市场 → 前 N 只」做成确定性管线，再把前 N 只交给 Workflow 2 做详细分析。
**排序键是龟龟的决策变量：安全边际 `GG_pess − II`** —— 不是自造评分，
因此筛选结论与个股报告同源（这是验收标准第 1 条的结构性保证）。

`scripts/turtle_core/` 是**公式单点**（纯计算，不取数不渲染）；
`scripts/turtle_screener/` 负责取数编排（宇宙/日历/价格/快照/清单）。

```bash
# 前置：Rf 必须外部供给（yc_cb 实测无权限），否则整批 ABORT
.venv/bin/python -c "
import sys; sys.path.insert(0,'scripts')
from turtle_core import rf
rf.write_rf_json('output', 1.0242, date='2026-10-14', source='中债10年期')"

# 跑当日选股（--scan 自动发现 output/*/claims.json 作为常备池）
.venv/bin/python scripts/screener_daily.py --scan output --output output --n-max 8 \
  --write-price-snapshot output/screener/prices_$(date +%Y%m%d).json

# 幂等复跑（用当日价格快照，不联网）
.venv/bin/python scripts/screener_daily.py --scan output --output output \
  --offline-prices output/screener/prices_20261014.json
```

退出码：`0` 可交付 · `1` 阻断级（如 `rf_missing`）· `2` 用法/输入错误。

**产出**：`output/screener/{YYYYMMDD}/` 下 `shortlist.json`（**与龟龟分析之间唯一的契约**，
每只带 `action` 与 `needs_workflow1`）· `run_manifest.json`（门禁裁决）· `excluded.json`
（排除明细 + 原因分布）· `screener_results.csv`；整批中止时只写 `ABORTED.txt` + manifest，
**不产出残缺清单**。

**分派**（已定参数：财报期未变 **且** 价格变动 <10% ⇒ 只刷新 §17）：
`refresh_only` → 只刷价格相关小节；`rerun_turtle` → 完整 Workflow 2；
`needs_workflow1` → **异步慢路径**（池外新股，需年报 PDF + 6 维度子代理，不阻塞当日交付）。

> 常备池存在的理由：Workflow 1 依赖年报 PDF（`web_search`，不稳定）+ 6 维度子代理 + 两条审核，
> **是年度事件而非每日事件**。池内股票提前完成 Workflow 1，日报当晚即可交付详细分析。

**门禁**由 `scripts/gate_policy.py` 单点裁决：只有 `rf_missing` / `calendar_missing` /
`market_data_not_ready` 三个**全局**缺口是 blocking；逐股数据缺口是「合格地排除」
（进 `excluded.json` 带受控原因码），不阻断当日交付。`data_gap_ratio` 在 manifest 里报出。

**调度**（DSH 原生 `schedule_create`，已实测）：A+H `weekly 19:00 Asia/Shanghai` weekdays `[1-5]`；
美股 `weekly 06:30 Asia/Shanghai` weekdays `[2-6]`（ISO 周几顺延）。比收盘晚是因为
A 股 `daily_basic` 约 19:00 后才完整。**投递可能重复**（工具契约原文 "can repeat after a crash"）
⇒ 必须按 `trade_date` 幂等。

**验收（必须先过）**：`turtle_core_selfcheck.py` 用账本反算 canonical 决策数字
（00696 实测 20/20 复现）+ screener 与既有 Workflow 2 报告的 §17 claim **逐字段相等**。
⚠️ 后者被 **D-10**（产物 `市值 ≠ 股本×现价×fx`，00696 实测 +10.32%）卡住，
检查用 `turtle_screener.engine.check_market_cap_consistency()`。

## 两个审核步骤（三条流程共用）

设计取舍：**Python 做确定性预检，LLM 只做裁定**。理由是历史证据 ——
纯 LLM 审计会自证通过（06862 上一轮"六段全部改写却声明逐字保留"），
纯脚本审计抓不到口径与提取问题（脚本没有年报）。

| | 审核一（数值） | 审核二（观点） |
|---|---|---|
| 时点 | 计算完成**之后**、最终报告**之前** | 报告生成**之后** |
| 脚本 | `scripts/numeric_audit.py` | `scripts/viewpoint_audit.py` |
| 子代理契约 | `shared/audit/numeric_audit_agent.md` | `shared/audit/viewpoint_audit_agent.md` |
| 独立第二实现 | **是** —— 按 `shared_tables.md` 重写公式，不 import `derived_metrics` | 不适用 |
| 机械检查 | §17←§1–§16 派生对账、输入可溯性（§17 用到的值必须在 §1–§16 有可见来源行）、跨表恒等（§17.9 门槛价格 ≡ §17.11/§17.14 目标买入价） | 安全边际=GG_pess−II、仓位矩阵代入、买点触发、跨段一致、禁令、ES↔正文、上游限制是否带进结论 |
| 子代理负责 | 逐条裁定（真实错误/口径差异/脚本误判）+ 需要年报的提取核查 | 逐条裁定 + **论证质量六类**（理由有无数据支撑、评级与事实矛盾、推断写成事实、披露是否影响结论、替代值是否诚实、结论与档位相容） |
| 结论行 | `NUMERIC_AUDIT_RESULT: PASS|FIX_REQUIRED` | `VIEWPOINT_AUDIT_RESULT: PASS|FIX_REQUIRED` |
| 已接入流程 | 龟龟 / 估值 / 定性 | 龟龟（全量）｜估值·定性（`--profile valuation|qualitative`，跳过龟龟专有的仓位矩阵/买点检查） |

> `--profile` 存在的理由：`V1`（安全边际=GG_pess−II、仓位矩阵、买点触发）**与 `V2`（claim 绑定）
> 都是龟龟专有语义** —— claim 账本覆盖 §17 决策单元格，而估值/定性的 coordinator 里没有"claim"
> 这个概念。对这两类报告跑它们只会得到一片 `UNVERIFIABLE`，而 `--strict` 下无 severity 的
> `UNVERIFIABLE` 会阻断交付（实测：一份完全正常的定性报告因 `V2a 绑定 0 个 claim`(critical)
> 被阻断）—— 等于把共享工具变成龟龟专用工具。
> 两个 profile 下仍会**如实声明**「V1/V2 未检查（不是通过）」，带 MINOR severity ⇒ 披露级、不阻断。

**分级与退出码**由 `scripts/gate_policy.py` **单点**提供（与采集器共用）：
critical/major ⇒ blocking（阻断交付），minor ⇒ disclosure；`--allow-degraded` 是唯一逃生口，
它把 `degraded: true` 写进裁决，必须随报告披露。

**三条纪律**：
1. **输入不全 ⇒ `UNVERIFIABLE`，不凑数** —— 审核脚本在拿不到输入时必须点名缺什么，
   而不是算一个数去对账（假阳性与真错误同样有害：它会让 `--allow-degraded` 变成常态）。
2. **审计不得被下游结论锚定** —— 数值审核禁读 `phase3_quantitative.md` 的结论段与最终报告；
   观点审核禁读 `audit.md` 与 `output/_baselines/**`（非预期版本会让整份审计作废）。
3. **脚本误判要回灌脚本** —— 裁定为"脚本误判"时必须给出脚本应如何改，否则同一假阳性会反复出现。

## §17 section status contract (`section17_status.json`)

Every §17 sub-section is **optional**, and absence used to be inferable only from a
heading *not* appearing — which made "not computed" indistinguishable from "computed
as zero" and pushed the prompts into 4 hand-calculation fallbacks (a direct violation
of "LLM 不做算术"; see `known_issues.md` P2-20).

The collector now writes `{output_dir}/section17_status.json` next to the pack:

```json
{"schema_version": "1.0", "stock_code": "06862.HK",
 "units": {"trading_currency": "HKD", "reporting_currency": "CNY",
           "amount_unit": "百万", "unit_label": "百万元"},
 "sections": {"17.11": {"title": "穿透回报率网格（税后）", "status": "ok"},
              "17.6":  {"title": "因子4·股价分位", "status": "missing",
                        "reason": "hk_field_gap",
                        "reason_label": "港股接口缺少该小节所需字段（上游数据缺口）",
                        "remedy": "见 diagnosis_hk_section17.md；…"}}}
```

Contract (`scripts/section_contract.py`):

- **fail-closed** — a section with no registered status is reported as `missing`,
  never assumed fine
- `status != ok` **requires** a `reason` from the controlled vocabulary `REASONS`
  (an out-of-vocabulary or missing code is a hard error)
- invariant, machine-checked by `check_pack_consistency()`:
  **a `### 17.N` heading exists ⟺ that section's status is `ok`**
  (the in-pack status table references sections as `§17.N`, so it cannot be
  mistaken for a heading)
- **`status != ok` means stop and report — never hand-calculate.** The old
  "降级路径（§17.X 缺失时）" formula blocks were deleted; the design now prefers
  *no number plus an explicit reason* over a hand-derived number.

## §17 claim binding (`claims.json`) — numeric verifiability

Every §17 value the agent may quote is registered as a **claim** with
`(indicator, value, unit, time, basis, policy)`. Decision cells are rendered twice:
a human-readable wide table, plus a `key: value` block carrying the `claim_id`
(KV is measurably better for numeric retrieval than wide tables).

When a report quotes a §17 value it must bind it:

```
穿透回报率为 <claim id="cm17.11.aa2y.m3.gg" policy="round:2">2.89%</claim>，
目标买入价 <claim id="cm17.11.aa2y.m3.tbp">14.96</claim>。
```

The `id` must be copied verbatim from a `claim:` line in the pack or from
`{output_dir}/claims.json` — never invented. Then verify:

```bash
.venv/bin/python scripts/report_consistency.py \
  --report output/{code}_{company}/qualitative_report.md \
  --claims output/{code}_{company}/claims.json --gates
```

Policies (`scripts/claim_ledger.py`): `exact` · `round:<d>` · `alias[:<tol>]`
(sanctioned unit conversions such as 百万↔亿; a *precision loss* needs an explicit
tolerance) · `tolerance:<abs>,<rel>` (the quote must carry a qualifier like 约/≈).

**Fail-closed** (per [PCN](https://ar5iv.labs.arxiv.org/html/2509.06902)): an
unknown id, an unparsable number, a unit mismatch or a value that fails its policy
is reported as a gate failure and the command exits 1. A **bare number is untrusted
by default** — trust is earned by a mechanical match, never assumed.

## Risk-free rate (`yc_cb` has no permission)

§14 (无风险利率) comes from Tushare's `yc_cb` endpoint. Without that permission the risk-free-dependent
pre-computed blocks (业绩下滑敏感性 / 穿透回报率网格 / 收入敏感性) cannot be generated. Pass `--rf` explicitly:

```bash
.venv/bin/python scripts/tushare_collector.py --code <code> \
  --output output/{code}_{company}/data_pack_market.md --rf <pct>   # e.g. --rf 1.688
```

Resolution order: `--rf` → `TUSHARE_RF` → a rate already present in the pack's §14; a live
API value still wins when the permission is available.

## HK FX rate (`--fx-hkd-cny`)

HK listings trade in HKD but **most mainland A+H / red-chip issuers report in CNY**
(their annual reports say "presented in Renminbi"). The collector labels financials by
ticker suffix, so without conversion `R = 净利润(CNY) / 市值(HKD)` understates the
penetrating return by `1/0.8647 − 1 = 15.65%`. Pass the factor explicitly:

```bash
.venv/bin/python scripts/tushare_collector.py --code 06862.HK \
  --output output/06862_海底捞/data_pack_market.md --fx-hkd-cny 0.86466
```

Resolution order: `--fx-hkd-cny` → `TUSHARE_FX_HKD_CNY` → `{output_dir}/fx.json` → a rate
already recorded in the pack's `*[协调器注 …]*`. The option-A contract is `fx.json`:

```json
{"factor_to_reporting": 0.86466, "date": "2026-08-21",
 "source": "CFETS中间价(新华社)", "reporting_currency": "CNY", "trading_currency": "HKD"}
```

`factor_to_reporting` means "multiply a trading-currency amount by this to get the
reporting currency"; use `1.0` (or `"reporting_currency": "HKD"`) for genuine HKD
reporters such as `00001.HK`. **Share counts are always derived from the
trading-currency cap ÷ price.**

**Scope (widened 2026-10-13):** the conversion is *not* limited to the return-rate denominator.
**Every per-share amount that will be compared or averaged with a price must be converted too** —
EPS/BPS come off statements in the reporting currency (`_per_share_unit()`), while prices and every
row of the §17.8 baseline table are in the trading currency (`_price_unit()`). Averaging the two
without conversion is a silent unit error (06862: ① 1.11 / ② 1.80 were CNY per share sitting in a
"基准价（港元）" column, making the composite 9.34 instead of 9.46). `invariants.py` I2 fails a pack
whose baseline table mixes currencies without declaring the conversion.
When no rate is available the collector degrades **audibly** — it never silently mixes
currencies. See `scripts/fx_utils.py` and `known_issues.md` P0-2.
