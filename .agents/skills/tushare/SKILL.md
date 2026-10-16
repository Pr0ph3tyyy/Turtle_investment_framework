---
name: tushare
description: |
  Drive this repo's Tushare collector from an agent: scripts/tushare_collector.py
  CLI flags, TUSHARE_TOKEN / .env setup, the §1-§17 structure of
  output/{code}_{company}/data_pack_market.md, and the --rf workaround when the
  yc_cb risk-free-rate endpoint has no permission. Use when collecting, refreshing
  or inspecting a data pack for an A-share / HK / US stock.
---

# tushare — data collection skill for this repo

`scripts/tushare_collector.py` pulls structured financials + market data from the
Tushare Pro API and writes one markdown data pack per stock:
`output/{code}_{company}/data_pack_market.md`. Python does the deterministic
collection; the LLM only interprets the result.

## Token
- `TUSHARE_TOKEN` env var, or a `.env` file at the repo root (`cp .env.sample .env`);
  `scripts/config.py` loads `.env` automatically and raises if it is missing.
- Without a token the collector can only fall back to yfinance for some sections.

## Key flags
| Flag | Meaning |
|------|---------|
| `--code CODE` | **required**; `600887.SH` / `000858.SZ` / `00700.HK` / `AAPL.US` (plain digits normalized by `scripts/config.py`) |
| `--output PATH` | data pack path (default `output/data_pack_market.md`); normally `output/{code}_{company}/data_pack_market.md` |
| `--refresh-market` | refresh only market-sensitive §1/§2/§11/§14 of an existing pack; auto-falls back to full collection when the pack is >7 days old |
| `--no-cache` | bypass the TTL disk cache (financials 7 days, quotes/rates 24h) and fetch live |
| `--cache-refresh` | invalidate this stock's cached financials first — recommended in earnings season (4/8/10 月) |
| `--rf PCT` | explicit 10-year risk-free rate in percent, e.g. `--rf 1.688` (env: `TUSHARE_RF`) |
| `--fx-hkd-cny RATE` | HKD→CNY factor for HK listings, e.g. `--fx-hkd-cny 0.86466` (env: `TUSHARE_FX_HKD_CNY`); converts the trading-currency market cap into the reporting currency |
| `--extra-fields endpoint.field …` | fetch extra Tushare fields into the pack |

## §1–§17 data-pack structure (high level)
- §1 基本信息 · §2 市场行情 · §3 合并利润表 (+§3P 母公司) · §4 合并资产负债表 (+§4P) · §5 现金流量表
- §6 分红历史 · §7 股东与治理(部分) · §8 行业与竞争 · §9 主营业务构成 · §10 MD&A
- §11 十年周线行情 · §12 关键财务指标 · §13 风险警示 (§13.1 自动检测 / §13.2 待补充占位符) · §14 无风险利率
- §15 股票回购 · §16 股权质押 · §17 衍生指标（Python 预计算: 17.1 财务趋势速览 … 17.8 因子4·绝对估值,
  17.10 支付率 M 三重校验, 17.11 穿透回报率网格（税后）, 17.12 G 系数网格, 17.13 收入敏感性）
- §7 / §8 / §10 / §13.2 are only partly filled by the script: they carry LLM-fill
  placeholders — see `shared/qualitative/data_collection.md`.

## Risk-free rate caveat
§14 comes from the Tushare `yc_cb` endpoint. Without that permission it stays empty
and every pre-computed block that depends on the risk-free rate (业绩下滑敏感性 /
穿透回报率网格 / 收入敏感性) **cannot be generated** — the pack says so explicitly.
Pass `--rf <pct>` (or `TUSHARE_RF`) to unblock them; resolution order is
`--rf` → `TUSHARE_RF` → a rate already in the pack's §14, and a live API value wins.

## HK FX caveat
HK listings trade in HKD but **most mainland A+H / red-chip issuers report in CNY**
(their annual reports state "presented in Renminbi"). The collector labels financials by
ticker suffix, so `R = 净利润(CNY) / 市值(HKD)` understates the penetrating return by
`1/0.8647 − 1 = 15.65%`. Provide the factor via `--fx-hkd-cny`, `TUSHARE_FX_HKD_CNY`, or
`{output_dir}/fx.json`; a rate already present in the pack's `*[协调器注 …]*` is reused as
a last resort. Share counts always come from trading-currency cap ÷ price, so only the
return-rate denominator is converted. Missing rate → loud warning, never a silent mix.

## Typical invocations
```bash
.venv/bin/python scripts/tushare_collector.py --code 600887.SH --output output/600887_伊利股份/data_pack_market.md
.venv/bin/python scripts/tushare_collector.py --code 600887.SH --output output/600887_伊利股份/data_pack_market.md --refresh-market
.venv/bin/python scripts/tushare_collector.py --code 002984.SZ --output output/002984_森麒麟/data_pack_market.md --rf 1.688
```
