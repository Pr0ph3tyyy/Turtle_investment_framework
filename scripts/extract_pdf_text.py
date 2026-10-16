#!/usr/bin/env python3
"""Extract full text from a text-based PDF into a .txt file with page markers.

Usage:
    python3 scripts/extract_pdf_text.py --pdf report.pdf [--output report.txt]
"""
from __future__ import annotations

import argparse
import sys

import pdfplumber


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    out = args.output or args.pdf.rsplit(".", 1)[0] + ".txt"
    pages = 0
    with pdfplumber.open(args.pdf) as pdf:
        with open(out, "w", encoding="utf-8") as f:
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                f.write(f"\n===== PDF PAGE {i} =====\n")
                f.write(text)
                f.write("\n")
                pages = i
    print(f"Extracted {pages} pages -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
