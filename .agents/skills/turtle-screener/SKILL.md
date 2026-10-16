---
name: turtle-screener
description: |
  Run this repo's daily 龟龟选股 (Turtle screener): build the slow-layer financial
  snapshots from audited product dirs, fetch the day's prices (direct Yahoo chart,
  pool + candidates, three markets A/HK/US), compute the turtle decision numbers via
  the single-source scripts/turtle_core, rank by 安全边际 = GG_pess − II (threshold-gated,
  N-capped), and write output/screener/{trade_date}/shortlist.json + run_manifest.json +
  excluded.json + CSV. Use when screening a universe down to the top-N stocks that the
  full 龟龟 strategy pipeline (turtle-analysis) should then analyze in depth, or when
  asked to run/refresh the daily screener.
---

# turtle-screener — 每日选股技能

单条入口：`scripts/screener_daily.py`。

**分工（不可混淆）**

| 层 | 模块 | 职责 |
|---|---|---|
| 公式 | `scripts/turtle_core/` | 纯计算，无取数、无渲染。**公式单点** |
| 取数编排 | `scripts/turtle_screener/` | 宇宙、日历、价格、快照、清单 |
| 入口 | `scripts/screener_daily.py` | 把上面串成一次运行 |

排序键是 **安全边际 `GG_pess − II`**（龟龟的决策变量），不是自造评分 ——
因此 screener 的结论与个股报告天然同源（验收标准第 1 条）。

## 快速开始

```bash
# 1) 无风险利率（yc_cb 无权限，必须外部供给）
cp .env.sample .env            # 或直接
.venv/bin/python -c "
import sys; sys.path.insert(0,'scripts')
from turtle_core import rf
rf.write_rf_json('output', 1.0242, date='2026-10-14', source='中债10年期')"

# 2) 跑当日选股（自动发现 output/*/claims.json 作为常备池）
.venv/bin/python scripts/screener_daily.py --scan output --output output --n-max 8

# 3) 离线复跑（幂等；用当日价格快照，不联网）
.venv/bin/python scripts/screener_daily.py --scan output --output output \
  --offline-prices output/screener/prices_20261014.json
```

## 关键参数

| 参数 | 含义 |
|---|---|
| `--scan DIR` | 自动发现含 `claims.json` 的产物目录（常备池的来源） |
| `--pool-file F` | 显式常备池 JSON：`{"pool":[{"ts_code":"00696.HK","dir":"output/00696_...","name":"..."}]}` |
| `--rf PCT` | 无风险利率（百分数）。解析顺序 `--rf` → `TUSHARE_RF` → `{output}/rf.json` |
| `--n-max N` | 入选**上限**（门槛制：`安全边际 > 0` 且 `GG_pess > 0` 才有资格，不硬凑 N） |
| `--markets` | 市场范围，默认 `A,HK,US`（用于日历/就绪判定） |
| `--offline-prices F` | 读当日价格快照 ⇒ 幂等复跑、不联网 |
| `--write-price-snapshot F` | 把本次抓到的价格落盘（次日复跑/审计） |
| `--allow-degraded` | 显式接受带缺陷交付（会把 `degraded: true` 写进裁决，必须随报告披露） |

退出码：`0` 可交付 · `1` 不可交付（阻断级，如 Rf 缺失）· `2` 用法/输入错误。

## 产出

`output/screener/{YYYYMMDD}/`

| 文件 | 内容 |
|---|---|
| `shortlist.json` | **与龟龟分析之间唯一的契约**：每只带 `action`（`refresh_only`/`rerun_turtle`/`needs_workflow1`）、`needs_workflow1`、安全边际、目标买入价、原因码 |
| `run_manifest.json` | 宇宙规模、各漏斗计数、原因分布、`gate_policy` 裁决、`deliverable` |
| `excluded.json` | 因原因码被排除的明细 + 分类计数（含 `qualified_not_selected`） |
| `screener_results.csv` | 全量结果（含不合格股，供人工排查） |
| `ABORTED.txt` | 仅在整批中止时出现；此时**不写** shortlist/excluded/CSV |

## 纪律（改代码前必读）

1. **fail-closed**：数据不全 ⇒ 登记受控原因码（`scripts/turtle_core/reasons.py`），
   **绝不** `or 0` 把缺失变成零值（冒烟测试踩过：`or 0` 让 GG_pess 变成 0.0 而非 None）。
2. **只登记全局缺口为阻断**（`rf_missing`/`calendar_missing`/`market_data_not_ready`）；
   逐股数据缺口是「合格地排除」，写进 `excluded.json` 即可，不应阻断当日交付。
3. **按 `trade_date` 幂等**：重跑覆盖同一目录，绝不追加。依据是 DSH schedule 的工具契约原文
   —— "Delivery can repeat after a crash"。
4. **价格必须落当日快照**，否则 Yahoo 实时报价会让重跑不幂等。
5. **港股日历需自建**（Tushare `trade_cal` 无 HKEX，实测 0 行）。
6. **每日价格只取「池 + 候选集」**，不取全市场（美股 ≥12000 无法全量日更；
   `us_daily`/`hk_daily`/`yfinance` 实测均被限频，只有直连 Yahoo chart 可用）。

## 已知前置缺陷（会阻断验收）

`turtle_core_extraction_findings.md` 记录了 10 项上游缺陷，其中两项直接卡住本工具：

- **D-10**：产物 `市值 ≠ 股本 × 现价 × fx`（00696 实测 +10.32%）⇒
  screener（自算市值）与报告 §17（用产物市值）的 GG 不相等。
  用 `turtle_screener.engine.check_market_cap_consistency()` 机械检查。
- **D-1**：§17.9「门槛价格」分母用股本、§17.14 用市值，且前者币种错标
  ⇒ `numeric_audit` X1/X2 报 critical 阻断交付。

## 测试

```bash
.venv/bin/python -m pytest tests/test_turtle_core_*.py tests/test_turtle_screener_*.py \
  tests/test_screener_daily_cli.py -q
```
