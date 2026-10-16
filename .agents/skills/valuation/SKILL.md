---
name: valuation
description: |
  Run the 估值 analysis for an A-share / HK / US stock: scripts/valuation_engine.py computes
  the company classification, WACC and every valuation method (DCF, DDM, PE Band, PEG, PS)
  with 5×5 sensitivity tables, then the LLM applies the D1–D6 qualitative adjustments — with
  no arithmetic of its own — and assembles {company}_{code}_估值报告.md. Requires the
  qualitative_report.md and data_pack_market.md produced by the business-analysis workflow.
  Use when: the user asks for 估值 / 内在价值 / 安全边际 / DCF valuation of a stock.
---

# valuation

Read `strategies/valuation/coordinator.md` and follow it. **On DSH there is no slash
command** — read the coordinator markdown directly (see `AGENTS.md`).

## Prerequisites (hard gate)

Both files must exist in `output/{code}_{company}/`:

- `qualitative_report.md` — the D1–D6 parameters drive the qualitative adjustments
- `data_pack_market.md` — market data and §17 pre-computed grids

If either is missing, **stop** and tell the user to run the `business-analysis` skill first.
Unlike the turtle workflow there is no degraded mode here.

## Step 1 — deterministic valuation (Python)

```bash
.venv/bin/python scripts/valuation_engine.py --code <code> --output-dir output/{code}_{company}
# add --no-cache to bypass the TTL cache for financial endpoints
```

Output `output/{code}_{company}/valuation_computed.md` contains: the company classification
(蓝筹价值型 / 成长型 / 混合型), WACC, each applicable method's result plus its **5×5
sensitivity tables**, cross-validation, reverse valuation and the assumptions list.
Classification is threshold-driven (`ROE>15` / `payout>30` / `revenue CAGR<20` /
`profit CAGR>25`); an ambiguous case falls back to 混合型. A method that cannot be computed is
skipped and its weight redistributed.

## Step 2 — qualitative adjustment + report (LLM, no arithmetic)

Read, in this order:

- `strategies/valuation/phase2_valuation.md` — the adjustment instructions
- `strategies/valuation/references/valuation_methods.md` — methodology reference
- `strategies/valuation/references/report_template.md` — output format
- `output/{code}_{company}/qualitative_report.md` — D1–D6 insights
- `output/{code}_{company}/valuation_computed.md` — all computed numbers

**Never compute a value yourself**: every number is either quoted from
`valuation_computed.md` or selected from one of its sensitivity tables. The qualitative
adjustments map dimensions onto valuation inputs, e.g. D1 core operating-profit growth → FCF
growth; D2 moat rating → terminal growth; D3 cycle position → scenario weights; D4
governance red flags → ERP / governance discount. Each adjustment must cite the dimension it
came from.

Output: `output/{code}_{company}/{company}_{code}_估值报告.md`

## Error recovery

- Missing prerequisites → stop (see above), do not improvise
- `valuation_engine.py` fails → check `TUSHARE_TOKEN` / `.env` and retry
- Classification ambiguous → the engine already defaults to 混合型; state this in the report
- A method missing → use the remaining methods and say which were skipped
- `qualitative_report.md` has no structured parameter table → skip the qualitative
  adjustments and use the engine defaults, and say so
