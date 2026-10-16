# 港股标的 §17.6 / §17.7 / §17.8 缺失诊断报告

> **性质**：纯诊断文档，**未修改任何代码**（本次仅新增本文件）。
> **诊断日期**：2026-10-13
> **样本**：`02669.HK` 中海物业（8/21 旧包 + 10/13 实时复跑）、`00506.HK` 中国食品（8/23）；对照 A 股 `002027` 分众传媒（8/20）、`002984` 森麒麟（8/25）

---

## 一、结论速览

港股标的缺失的不是一个 bug，而是 **5 类根因叠加**，其中 3 类属同一模式：「上游数据缺口 + 人工补写只落 markdown，无法回流 Python」。

| 小节 | 港股状态 | 直接根因 | 性质 | 严重度 |
|---|---|---|---|---|
| §17.6 股价分位 | ✗ 缺失 | `_store["weekly_prices"]` 为空（§11「数据缺失」） | 数据源不可用 | 中 |
| §17.7 控股结构 | ✗ 缺失 | HKFRS 无母公司报表（无 §3P/§4P） | **结构性不适用** | 低（但需标注） |
| §17.8 绝对估值/基准价 | ✗ 缺失 | `basic_info` 无 `close` → 命中 guard | 数据源不可用 | 中 |
| §17.9 / §17.11 / §17.13 | ✗ 缺失 | ① 无风险利率（已由 3-D 修复）② `close`/`weekly` 仍缺 | 双重 | **高** |
| §17.12 G 系数网格 | ✗ 缺失 | §5 D&A 全缺（`any_da=False`） | 数据源缺口 | 中 |

**与 A 股对照**：A 股标的（002027、002984）三项齐备；港股 2/2 全缺 → 与「市场」强相关，而非偶然。

**额外发现（超出原问题，但影响港股可用性）**：

1. **HK `total_market_cap` 单位错误（10⁶ 量级）**——代码假设「百万港元」，Tushare 实际返回**港元**。目前被 `close` 缺失掩盖，属「定时炸弹」（详见第五节）。
2. **§17.9 在 `close` 缺失时会抛 `ZeroDivisionError`**，而 §17.11/17.13 走 `_get_mktcap_shares` 是安全的——同一份数据两套处理逻辑不一致。
3. **静默缺失会导致 Agent 漏报**：02669 报告的风险清单只列了 17.9/17.11/17.12/17.13，**完全没提 17.6/17.7/17.8 也缺失**。

---

## 二、复核方法

1. **旧包静态比对**：4 个数据包的 §17 小节清单、§1/§5/§11/§14 内容。
2. **代码 guard 逐条阅读**：定位每个小节的 `return None` 条件（行号见下）。
3. **实时复跑验证**：2026-10-13 以 `--rf 1.684` 重跑 `02669.HK`，与 8/21 旧包对照（命令见第八节）。
4. **API 原始值取证**：直接调用 `hk_fina_indicator` 核对 `total_market_cap` 量级。

---

## 三、逐项根因

### 3.1 §17.6 股价分位 ← §11 十年周线不可用

- **Guard**：`scripts/tushare_modules/derived_metrics.py:309-312`
  ```python
  wp_df = self._store.get("weekly_prices")
  if wp_df is None or wp_df.empty:
      return None
  ```
- **证据**：02669、00506 数据包 §11 均为「数据缺失」；10/13 复跑仍为「数据缺失」。
- **上游原因**（02669 数据包 §13.2 协调器注原文）：
  > 「§2 市场行情与 §1 当前价格：Tushare hk_daily 接口配额 1次/小时已耗尽、yfinance 被限频，--refresh-market 重取失败……② §11 十年周线行情：数据缺失（同因）。」
- 港股 §11 依赖 yfinance 十年周线，失败后回退 `hk_daily` 重采样；本次两条路都不可用。

### 3.2 §17.7 控股结构 ← HKFRS 无母公司报表（结构性）

- **Guard**：`derived_metrics.py:362-364`
  ```python
  if bs_df.empty or bs_parent_df.empty:
      return None
  ```
- **证据**：`grep -c "^## 3P\.\|^## 4P\." 02669/data_pack_market.md` → **0**（两个港股包都没有母公司报表）；数据包 §13.1 亦自述「§3P/§4P 母公司报表在 HKFRS 体系下不适用」。
- **性质**：这不是缺陷，而是**港股的固有差异**。问题在于它与「数据缺失」在输出上不可区分，容易被误判为抓取失败。

### 3.3 §17.8 绝对估值与基准价 ← `basic_info.close` 缺失

- **Guard**：`derived_metrics.py:427-428`（港股分支）
  ```python
  total_market_cap = self._safe_float(bi.get("total_market_cap"))
  if not close or not total_market_cap:
      return None
  ```
- **`close` 来源**：港股 §1 只有在 yfinance 取价成功时才会写入该列（`scripts/tushare_modules/financials.py:101-106`）：
  ```python
  yf_data = self._yf_hk_market_data(ts_code)
  if yf_data and yf_data.get("close"):
      fina_copy["close"] = yf_data["close"]
      self._store["basic_info"] = fina_copy
  ```
- **证据**：
  - 00506 §1「当前价格 (HKD) | —」→ 无 `close` → §17.8 缺失；
  - 10/13 复跑 02669 §1「当前价格 (HKD) | —」→ 同样命中；
  - 合成验证：同一港股 `_store`，含 `close` → 生成；去掉 `close` → `return None`。
- **结论**：港股 §17.8 的可用性 = yfinance/hk_daily 取价是否成功。

### 3.4 §17.9 / §17.11 / §17.13 ← Rf（已修）叠加 `close` 缺失

两层依赖，本次已解除第一层、暴露第二层：

| 依赖 | 旧包（8/21） | 10/13 复跑（`--rf 1.684`） |
|---|---|---|
| 无风险利率 | ✗（§17.2 Rf = 「—」） | **✓ Rf = 1.6840%，港股 II = 5.00%** |
| `basic_info.close` | ✗ | ✗（§1 价格仍为「—」） |
| §17.9/17.11/17.13 | 全部缺失 | **仍全部缺失** |

- §17.11/17.13 经 `_get_mktcap_shares` 取数，缺 `close` 时**安全返回 None**（`derived_metrics.py:1238-1240`）。
- §17.9 走自己的分支且写成 `total_shares = mkt_cap / close if close else 0`（`derived_metrics.py:695`），随后 `threshold_price` 除以 `total_shares` → **`ZeroDivisionError`**。
- **实测证据**：10/13 复跑包第 256 行出现
  ```
  *<lambda> 计算失败: float division by zero*
  ```
  而 8/21 旧包**没有**这一行——因为旧包在更早的 Rf guard（`derived_metrics.py:706-707`）就返回了 None，**根本走不到**除法。即：修好 Rf 后，港股的下一层缺口才暴露出来。
- 这也是判定 §17.9 属「高严重度」的原因：它是唯一会**抛异常**（被外层 `except` 捕获）而非静默返回的分支。

### 3.5 §17.12 G 系数网格 ← §5 D&A 全缺

- **Guard**：`derived_metrics.py:1618-1619`
  ```python
  if not any_da or da <= 0:
      return None
  ```
- **证据**：02669、00506 的 §5 中「折旧及摊销 (D&A)」五年全为「—」；02669 报告亦自述「§5 D&A 缺失（已由年报补充 113.47 百万元）」——即**人工从年报补的数只在 markdown/报告里，没进 `_store`**。

---

## 四、共性根因（本次最重要的发现）

> **人工/WebSearch 补救只写进 markdown，永远进不了 Python `_store`。**

港股这一轮所有缺失，本质是**同一个上游故障**（hk_daily 配额耗尽 + yfinance 限频）的三处表现：

| 缺失数据 | 人工补救落点 | 能否回流 Python | 后果 |
|---|---|---|---|
| §14 无风险利率 | 协调器 WebSearch 写入 §14 | **现已可以**（3-D 已修：`--rf` + 回读） | §17.9/11/13 的 Rf 依赖解除 |
| §1 当前价格 | 腾讯行情写入 §1 文本 | ✗ 无通道 | §17.8 缺失；§17.9 crash；§17.11/13 返回 None |
| §11 十年周线 | 未补（标注数据缺失） | ✗ 无通道 | §17.6 缺失 |
| §5 D&A | 写进分析报告正文 | ✗ 无通道 | §17.12 缺失 |

**判断**：§14 的修复模式（显式 `--rf` + 从数据包回读）已被验证有效，**同样的模式应复用到价格/周线/D&A**，否则港股永远只能得到 17.1–17.5 + 17.10 这半套预计算，而 v2.1 主打特性「有网格时不做数学」（`strategies/turtle/phase3_quantitative.md:24` 约束 #9）在港股上无法成立，Agent 只能退回手算——正是质量体系要消灭的算术路径。

---

## 五、附带发现（超出原问题范围）

### 5.1 HK `total_market_cap` 单位错误（10⁶ 量级）

**API 实测**（2026-10-13，`pro.hk_fina_indicator(ts_code="02669.HK")`）：

| 字段 | 值 |
|---|---|
| `total_market_cap` | `1.098485e+10` |
| `hksk_market_cap` | `1.098485e+10` |

中海物业实际市值约 **109.8 亿港元** → 即接口返回的是**港元原值**，不是「百万港元」。

**但代码四处按「百万港元」处理**：

| 位置 | 代码 | 后果 |
|---|---|---|
| `derived_metrics.py:429-431` | `mkt_cap = total_market_cap` / `mkt_cap_yuan = mkt_cap * 1e6` | §17.8 市值放大 10⁶ |
| `derived_metrics.py:691-695` | `mkt_cap = total_market_cap * 1e6` | §17.9 市值放大 10⁶ |
| `derived_metrics.py:1248-1251` | `mkt_cap = total_market_cap * 1e6` | §17.11/17.13 市值放大 10⁶ |
| `valuation_engine.py:106-108` | `total_mv = total_market_cap  # 百万港元`；`mkt_cap_mm = total_mv` | **港股估值整体失真** |
| `financials.py:111` | `format_number(..., divider=1)` 显示为「总市值 (百万港元)」 | §1 展示标签错误 |

**当前为何没爆**：港股分支都先要求 `close`，而 `close` 恰好缺失 → 全部提前 `return None`。**一旦价格源恢复（`close` 可用），港股市值会瞬间被放大 10⁶**，穿透回报率/估值会被压到近乎 0。属高优先级「定时炸弹」。

**另一处不一致**：8/21 的 02669 包中该字段显示为 `11,050.53`（= 11,050.53 百万港元，量级正确），与 API 原始口径不符——结合 §13.2 的「已用腾讯行情接口恢复……总市值 110.51 亿港元」，可判断这是**人工改写进 markdown 的值**。也就是说：同一字段在「API 原始值」与「人工补写值」之间发生了单位漂移，这本身也说明需要单位归一化与断言。

### 5.2 同一数据、两套处理逻辑

- `_get_mktcap_shares`（`derived_metrics.py:1227-1260`）：缺 `close` → 返回 `None`（安全）。
- `_compute_factor4_sensitivity`（§17.9，`derived_metrics.py:683-695`）：缺 `close` → `total_shares = 0` → **除零**（不安全）。

建议 §17.9 统一改走 `_get_mktcap_shares`。

### 5.3 静默缺失会造成 Agent 漏报

02669 分析报告的风险清单（`output/02669_中海物业/中海物业_02669_分析报告.md:17`）只写了：

> 「⑤ §17.9/17.11/17.12/17.13 预计算缺失（GG/敏感性按降级路径手算）」

**未提及 §17.6/17.7/17.8 同样缺失**。原因是这三节此前既不生成告警、也不在报告模板的检查项里。本次 3-D 已加入「未生成清单」显式告警（实测新包会列出全部 7 个缺失小节），但**报告侧的自检**仍建议补一条。

---

## 六、影响面与优先级

| 优先级 | 问题 | 影响 |
|---|---|---|
| **P0** | HK `total_market_cap` 单位（10⁶） | 一旦价格可用，港股 §17.8/17.9/17.11/17.13 与 `valuation_engine` 全部失真 |
| **P1** | §17.9 缺 `close` 时除零 | 预计算崩溃（已可见于新包），并掩盖真实原因 |
| **P1** | 港股 `close` / §11 周线 无显式输入通道 | §17.6/17.8/17.9/17.11/17.13 在港股持续不可用 → 退回手算 |
| P2 | §17.7 对 HK 的「结构性不适用」未标注 | 易被误判为抓取失败，浪费排查时间 |
| P2 | 报告侧未自检 §17 缺失小节 | Agent 漏报数据缺口（5.3 实例） |
| P3 | §1「总市值 (百万港元)」标签错误 | 人工阅读误导 |

---

## 七、建议方案（**未实施**，供选择）

| # | 方案 | 内容 | 工作量 | 风险 |
|---|---|---|---|---|
| A | **单位归一化 + 断言**（建议优先） | 在写入 `basic_info` 时把 HK `total_market_cap` 统一换算为百万港元（或统一为原值 HKD），并加量级合理性断言/告警；同步修正 `financials.py:111` 标签与 4 处消费点 | 中 | 中：需确认历史数据口径，可能影响既有报告可比性 |
| B | **§17.9 走 `_get_mktcap_shares`** | 消除除零，与 §17.11/17.13 逻辑统一；缺 `close` 时安全返回 None | 小 | 低 |
| C | **扩展显式输入通道（复用 3-D 模式）** | 新增 `--price` / `--weekly-csv` / `--da` 或从数据包回读 §1 价格与 §11 周线，使人工补写可回流 `_store` | 中大 | 中：解析规则需覆盖多来源写法 |
| D | **HK 结构性差异显式标注** | §17.7 对 `.HK` 直接输出「HKFRS 不适用母公司口径」而非静默跳过；纳入「未生成清单」的原因分类 | 小 | 低 |
| E | **报告侧自检** | 在 `report_consistency.py --gates` 或交付 checklist 中加入「§17 缺失小节必须在报告 Warnings 中列出」 | 小 | 低 |

**组合建议**：`B`（低成本止血）→ `A`（消除定时炸弹）→ `D`+`E`（防漏报）→ `C`（根治港股数据链）。

---

## 八、复现命令

```bash
# 实时复跑（HOME 重定向：tushare set_token 会写 ~/tk.csv，沙箱不允许写工作区外）
mkdir -p /tmp/dsh_home /tmp/dsh_hk_verify
HOME=/tmp/dsh_home .venv/bin/python scripts/tushare_collector.py \
    --code 02669 --rf 1.684 --output /tmp/dsh_hk_verify/data_pack_market.md

# 查看 §17 小节清单（对照 output/02669_中海物业/data_pack_market.md）
grep -o '^### 17\.[0-9]*' /tmp/dsh_hk_verify/data_pack_market.md

# 查看被捕获的异常（Rf 修复后才暴露的除零）
grep -n '计算失败' /tmp/dsh_hk_verify/data_pack_market.md

# 核对 HK 市值单位（返回 ≈1.0985e10 = 港元原值）
HOME=/tmp/dsh_home .venv/bin/python -c "
import sys; sys.path.insert(0,'scripts')
import tushare as ts; ts.set_token(__import__('config').get_token())
print(ts.pro_api(timeout=30).hk_fina_indicator(ts_code='02669.HK',
      fields='ts_code,end_date,total_market_cap,hksk_market_cap').head(2))"
```

---

*本报告为只读诊断产物，不包含任何代码改动。*
