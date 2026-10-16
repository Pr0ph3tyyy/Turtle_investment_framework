---
name: business-analysis
description: |
  Run the 商业分析 / 定性分析 workflow for an A-share / HK / US stock: Tushare data pack →
  deterministic pre-computation (computed_metrics.md) → annual-report PDF → 6-dimension
  qualitative assessment, with a parallel clean-room recompute and a mandatory audit gate.
  Sole deliverable: qualitative_report.md. Use when: the user asks for 商业分析 / 定性分析 /
  护城河评估 / business quality of a stock, or before the turtle-analysis or valuation
  workflows (both consume its output).
---

# business-analysis

Read `shared/qualitative/coordinator.md` and follow it. **On DSH there is no slash
command** — read the coordinator markdown directly (see `AGENTS.md`).

## Prerequisites

None — this workflow produces the data pack the other workflows consume.

## Pipeline

**1A + 1A2 — data pack and deterministic pre-computation**

```bash
mkdir -p output/{code}_{company}
.venv/bin/python scripts/tushare_collector.py --code <code> \
  --output output/{code}_{company}/data_pack_market.md
.venv/bin/python scripts/quality_control.py \
  --input  output/{code}_{company}/data_pack_market.md \
  --output output/{code}_{company}/computed_metrics.md
```

`quality_control.py` emits CM§1–§5 (亿元对照 / 同比 / 多年统计 / 分红支付率 / PE 估值链).
The report must **cite these verbatim** as `[src: CM§N]` and must not recompute them.
If §14 has no API permission, pass `--rf <pct>` (see the `tushare` skill).

**1B — annual report PDF.** Reuse an existing `output/{code}_{company}/*{year}*年报*.pdf`
if present; otherwise use the `download-annual-report` skill. For text-layer PDFs
`scripts/extract_pdf_text.py` turns pages into a page-marked `.txt`; for scanned PDFs use
`scripts/pdf_preprocessor.py` (7 target sections → `pdf_sections.json`).

**Step 2 + Step 2X — slicing, parallel dimensions, synthesis (launch clean-room in parallel)**

- **2.0** slice the annual report deterministically:
  ```bash
  .venv/bin/python scripts/dimension_slicer.py --code <code> \
    --output-dir output/{code}_{company} --budget-chars 12000 --report-coverage
  # → slices/slice_D1..D6.md + slices/slice_manifest.json   (scanned PDF: add --vision)
  ```
- **2.1** six dimension `subagent`s in parallel — each reads the **byte-identical shared prefix**
  (writing_style_rules, output_schema, judgment_examples, data_pack_market.md, computed_metrics.md)
  plus its own dimension section and its own slice, and writes `dims/D{n}.json` conforming to
  `shared/qualitative/references/dim_result.schema.json`
- validate — **hard failure**, retry up to 2 times:
  ```bash
  .venv/bin/python scripts/validate_dim_result.py --input output/{code}_{company}/dims
  # exit 0 all valid · 1 contract failure
  ```
- **2.2** one synthesis `subagent` consuming only the six JSON files → `qualitative_report.md`
  (it adjudicates cross-dimension conflicts explicitly; it does **not** re-read the report)
- **2X** clean-room independent recompute → `cleanroom_metrics.md`
  (reads **only** the data pack + PDF; **forbidden** to read `dims/**`, the draft, or computed_metrics.md —
  this is what prevents same-source anchoring)

Why: real annual reports are 201K–573K chars; with the fixed inputs the single-context design
needs 245K–617K chars, which no longer fits. Slicing brings the per-call peak to ~24K chars.

**Step 3 — audit gate (mandatory, serial)**

```bash
.venv/bin/python scripts/report_consistency.py \
  --report output/{code}_{company}/qualitative_report.md \
  --output output/{code}_{company}/consistency_report.md --gates
# exit 0 = no conflicts & gates pass · 1 = advisory conflicts or gate failures · 2 = file error
```

Then the numeric-audit `subagent` (it must **not** read the PDF) writes `audit.md` ending in
`AUDIT_RESULT: PASS` or `AUDIT_RESULT: FIX_REQUIRED`. On `FIX_REQUIRED`, apply at most **one**
fix pass and re-run the consistency script. Unfixed **critical** findings block delivery.

**Step 4 — HTML (optional, only when the user asks)**

```bash
.venv/bin/python scripts/report_to_html.py \
  --input output/{code}_{company}/qualitative_report.md \
  --output output/{code}_{company}/qualitative_report.html --standalone
```

## Output

**DELIVER ONLY** `qualitative_report.md` (plus `.html` if explicitly requested). Do not
create summary / 摘要 / 略版 derivatives. Internal, non-deliverable artifacts:
`slices/**`, `dims/*.json`, `computed_metrics.md`, `cleanroom_metrics.md`,
`consistency_report.md`, `audit.md`.
Optional downstream input: `data_pack_report.md` (PDF footnotes for the turtle workflow).

## Degradation

No PDF → fall back to `web_search` for §7/§8/§10 and mark the source; scanned PDF → the
preprocessor path; PDF vs Tushare conflict → trust the PDF and record the discrepancy.
Always produce a report even with partial data.
