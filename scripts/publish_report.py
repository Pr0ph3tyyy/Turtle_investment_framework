#!/usr/bin/env python3
"""
publish_report.py — 将分析报告从 output/ 发布到 docs/reports/

用法：
    python scripts/publish_report.py 600887
    python scripts/publish_report.py 600887 --push        # 发布后自动 git push
    python scripts/publish_report.py 600887 --dry-run     # 预览，不写文件

发布后 GitHub Actions 会自动触发 MkDocs 构建并更新站点。
"""

import argparse
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUTPUT_DIR = ROOT / "output"
DOCS_REPORTS_DIR = ROOT / "docs" / "reports"
INDEX_MD = ROOT / "docs" / "index.md"

REPORTS_START = "<!-- REPORTS_START -->"
REPORTS_END = "<!-- REPORTS_END -->"


def find_report(code: str) -> Path:
    """在 output/ 中查找股票代码对应的报告文件（含子目录）。"""
    code_clean = code.upper().replace(".SH", "").replace(".SZ", "").replace(".HK", "")
    candidates = list(OUTPUT_DIR.glob(f"**/*{code_clean}*分析报告*.md"))
    if not candidates:
        candidates = list(OUTPUT_DIR.glob(f"**/{code_clean}*.md"))
    if not candidates:
        raise FileNotFoundError(
            f"在 output/ 中找不到股票 {code} 的分析报告。\n"
            f"请先运行 /turtle-analysis {code} 完成分析。"
        )
    # 多个文件时取最新的
    return max(candidates, key=lambda p: p.stat().st_mtime)


def extract_company_name(content: str) -> str:
    """从报告 Markdown 中提取公司名称。"""
    match = re.search(r"选股分析报告[：:]\s*(.+?)（", content)
    if match:
        return match.group(1).strip()
    match = re.search(r"^#\s+.+?[··]\s+选股分析报告[：:]?\s*(.+)", content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return ""


def extract_conclusion(content: str) -> str:
    """从报告中提取最终判断（买入/观察/排除）。"""
    match = re.search(r"最终判断[：:]\s*[\*\*]*(.+?)[\*\*]*\n", content)
    if match:
        verdict = match.group(1).strip().strip("*").strip()
        if "买入" in verdict:
            return "买入"
        if "观察" in verdict:
            return "观察"
        if "排除" in verdict:
            return "排除"
    return "—"


def verdict_badge(verdict: str) -> str:
    badges = {
        "买入": "🟢 买入",
        "观察": "🟡 观察",
        "排除": "🔴 排除",
    }
    return badges.get(verdict, verdict)


def update_index(code: str, company: str, verdict: str, report_filename: str, dry_run: bool) -> None:
    """在 docs/index.md 的报告列表中插入或更新当前报告条目。"""
    content = INDEX_MD.read_text(encoding="utf-8")

    start_idx = content.find(REPORTS_START)
    end_idx = content.find(REPORTS_END)
    if start_idx == -1 or end_idx == -1:
        print("  警告：index.md 中未找到 REPORTS_START/END 标记，跳过更新首页。")
        return

    block_start = start_idx + len(REPORTS_START)
    existing_block = content[block_start:end_idx]

    today = date.today().strftime("%Y-%m-%d")
    report_url = f"reports/{report_filename}"
    display_name = f"{company}（{code}）" if company else code
    new_row = f"| [{display_name}]({report_url}) | {verdict_badge(verdict)} | {today} |"

    # 已有表格则追加行，否则新建表格
    if "| 股票 |" in existing_block:
        # 检查是否已存在该股票代码的行，若有则替换
        lines = existing_block.splitlines()
        replaced = False
        new_lines = []
        for line in lines:
            if f"({report_url})" in line or (code in line and "reports/" in line):
                new_lines.append(new_row)
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(new_row)
        new_block = "\n".join(new_lines)
    else:
        header = (
            "\n\n"
            "| 股票 | 结论 | 发布日期 |\n"
            "|------|------|----------|\n"
            f"{new_row}"
        )
        new_block = header

    new_content = (
        content[:block_start]
        + new_block
        + "\n"
        + content[end_idx:]
    )

    if dry_run:
        print(f"  [dry-run] 将更新 docs/index.md，新增条目：{new_row}")
    else:
        INDEX_MD.write_text(new_content, encoding="utf-8")
        print(f"  更新 docs/index.md —— {display_name} {verdict_badge(verdict)}")


def git_push(code: str, company: str, dry_run: bool) -> None:
    display = f"{company}（{code}）" if company else code
    msg = f"docs: publish report for {display}"
    cmds = [
        ["git", "add", "docs/"],
        ["git", "commit", "-m", msg],
        ["git", "push"],
    ]
    for cmd in cmds:
        if dry_run:
            print(f"  [dry-run] {' '.join(cmd)}")
        else:
            result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"  ✗ 命令失败：{' '.join(cmd)}\n{result.stderr.strip()}")
                sys.exit(1)
            print(f"  ✓ {' '.join(cmd)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="将分析报告发布到 GitHub Pages")
    parser.add_argument("code", help="股票代码，如 600887 或 00700.HK")
    parser.add_argument("--push", action="store_true", help="发布后自动执行 git push")
    parser.add_argument("--dry-run", action="store_true", help="预览操作，不实际写文件")
    args = parser.parse_args()

    code = args.code.strip()
    code_clean = code.upper().replace(".SH", "").replace(".SZ", "").replace(".HK", "")

    print(f"\n龟龟投资策略 · 报告发布工具")
    print(f"{'─' * 40}")

    # 1. 查找源报告
    try:
        src = find_report(code)
    except FileNotFoundError as e:
        print(f"✗ {e}")
        sys.exit(1)
    print(f"  源文件：{src.name}")

    # 2. 解析报告内容
    content = src.read_text(encoding="utf-8")
    company = extract_company_name(content)
    verdict = extract_conclusion(content)
    report_filename = f"{code_clean}.md"
    dest = DOCS_REPORTS_DIR / report_filename

    print(f"  公司名称：{company or '（未识别）'}")
    print(f"  最终结论：{verdict_badge(verdict)}")
    print(f"  目标路径：docs/reports/{report_filename}")

    # 3. 复制报告
    if args.dry_run:
        print(f"  [dry-run] 复制 {src.name} → docs/reports/{report_filename}")
    else:
        DOCS_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"  ✓ 已复制报告")

    # 4. 更新首页列表
    update_index(code_clean, company, verdict, report_filename, args.dry_run)

    # 5. 可选：git push
    if args.push:
        print(f"\n  提交并推送到 GitHub…")
        git_push(code_clean, company, args.dry_run)
        print(f"\n  站点将在约 30 秒后更新：")
        print(f"  https://pr0ph3tyyy.github.io/Turtle_investment_framework/reports/{code_clean}/")
    else:
        print(f"\n  完成。手动推送：")
        print(f"  git add docs/ && git commit -m 'docs: {company or code_clean}' && git push")

    print(f"{'─' * 40}\n")


if __name__ == "__main__":
    main()
