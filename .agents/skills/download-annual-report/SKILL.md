---
name: download-annual-report
description: |
  Search for and download an annual / interim report PDF (A-share or HK) by trying
  cninfo (巨潮资讯网) → xueqiu (stockn.xueqiu.com) → 10jqka (notice.10jqka.com.cn) with
  web_search, filtering out 摘要 / 审计报告 / ESG / 公告 results, then fetching the PDF with
  scripts/download_report.py. Use when: a workflow needs the annual-report PDF, or the user
  asks to download 年报 / 中报 / 季度报告 for a stock.
---

# download-annual-report

Search-and-download runbook. On DSH there is no slash command — follow the steps below.
Search priority and exclusion keywords are also documented in
`.claude/commands/download-annual-report.md`.

## Step 0 — parse the input and format the code

- **stock_code** (required), **year** (optional, default = latest available),
  **report_type** (optional, default 年报)

| Input pattern | Market | Formatted code |
|---|---|---|
| 6-digit starting with `6` | Shanghai A-share | `SH600887` |
| 6-digit starting with `0` or `3` | Shenzhen A-share | `SZ300750` |
| 1–5 digits | Hong Kong | zero-pad to 5: `700` → `00700` |
| already prefixed `SH`/`SZ` | use as-is | — |

Report types: 年报 / 中报 / 一季报 / 三季报 — **HK supports only 年报 and 中报**.
Annual reports for fiscal year N are published Jan–Apr of N+1; when no year is given search
`current_year − 1` first, then fall back one more year.

## Step 1 — search (try each round until a PDF is found)

Use `web_search`:

1. **巨潮资讯网（官方，优先）**: `site:cninfo.com.cn {company_name} {year} 年度报告`
   (company name works better than the code; fall back to the code if unknown)
2. **雪球**: A-share `site:stockn.xueqiu.com {formatted_code} 年度报告 {year}`;
   HK `site:stockn.xueqiu.com {formatted_code} annual report {year}`
3. **同花顺**: `site:notice.10jqka.com.cn {company_name} {year} {search_keyword}`
4. **不限域（兜底）**: `{company_name} {formatted_code} {year} 年度报告 PDF`

## Step 2 — pick the right PDF

Accept direct PDF links only from the supported hosts:
`static.cninfo.com.cn` (uppercase `.PDF` is possible), `stockn.xueqiu.com`,
`notice.10jqka.com.cn`.

**Exclude** results whose title contains: 摘要 · 审计报告 · 公告 · 利润分配 · 可持续发展 ·
股东大会 · ESG · summary · auditor · dividend · 更正 · 补充 · 意见 · 内部控制。
Prefer the title that matches the report keyword **without** 摘要, then the URL closest to
the expected publish date. If nothing survives, tell the user and ask them to verify the
code / year / report type.

## Step 3 — download

```bash
.venv/bin/python scripts/download_report.py \
  --url "<PDF_URL>" \
  --stock-code "<SH600887|00700>" \
  --report-type "<年报|中报|一季报|三季报>" \
  --year "<year>" \
  --save-dir output/{code}_{company}
```

The script prints a structured block between `---RESULT---` and `---END---` with
`status` (SUCCESS / FAILED), `filepath`, `filesize` and `message`. Parse those fields; the
file lands as `output/{code}_{company}/{code}_{year}_{report_type}.pdf`.
(`--max-retries` controls retry attempts; PDFs are validated by magic bytes.)

## Step 4 — report back

On success: file path, human-readable size, stock code, year, report type. On failure: the
error message plus suggestions (URL may have expired, retry later, verify code/report type).

## If no PDF can be obtained

The business-analysis workflow falls back to `web_search` for §7/§8/§10 and marks the
source; the turtle and valuation workflows then run in degraded mode (no PDF footnotes).
Do not fabricate a report — record the gap.
