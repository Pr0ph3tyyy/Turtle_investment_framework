---
name: turtle-analysis
description: |
  Run the full 龟龟投资策略 (Turtle Investment Framework) pipeline on an A-share / HK / US
  stock: market-data refresh, the 11-step penetrating-return-rate calculation (Agent B),
  then valuation and report assembly (Agent C). Requires the qualitative outputs of the
  business-analysis workflow. Use when: the user asks for a 龟龟 / 穿透回报率 analysis, or
  wants {company}_{code}_分析报告.md produced.
---

# turtle-analysis

Read `strategies/turtle/coordinator.md` and follow it. **On DSH there is no slash
command** — read the coordinator markdown directly (see `AGENTS.md`).

## Prerequisite

`output/{code}_{company}/qualitative_report.md` and `data_pack_market.md` must exist
(outputs of the business-analysis workflow). If either is missing, ask the user with
`ask_user_question`: run the 商业分析 workflow first (recommended) or stop.
`data_pack_report.md` is optional — Agent B degrades gracefully without PDF footnote data.

## Pipeline

1. **Step A — market data refresh**
   ```bash
   .venv/bin/python scripts/tushare_collector.py --code <code> \
     --output output/{code}_{company}/data_pack_market.md --refresh-market
   ```
   Refreshes §1/§2/§11/§14 (price/market cap, 52-week range, weekly prices, risk-free
   rate); auto-falls back to full collection when the pack is older than 7 days. If §14
   has no API permission, pass `--rf <pct>` (see the `tushare` skill).
2. **Step 3.1 — Agent B (quantitative)**: read `strategies/turtle/phase3_quantitative.md`.
   Runs Step 0 data validation + accounting anchoring, then the 11-step penetrating
   return-rate calculation. Output: `phase3_quantitative.md`.
3. **Step 3.2 — Agent C (valuation + report)**: read `strategies/turtle/phase3_valuation.md`.
   Consumes `qualitative_report.md` and Agent B's output, then assembles the final report.

## Output

- `output/{code}_{company}/` — all intermediate files
- Final report: `{company}_{code}_分析报告.md`

## Dependencies

- Python venv `.venv/` (created by `bash init.sh`); prefer `.venv/bin/python`
- `TUSHARE_TOKEN` (or `.env`) for the Tushare Pro API
- The scripts are deterministic: the LLM must not redo arithmetic Python already performed
