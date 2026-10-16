#!/usr/bin/env python3
"""把龟龟分析报告 markdown 转成**自包含 HTML 阅读版**（手机/浏览器直接看，可分享）。

与 report_to_docx.py 同一套确定性规则：`<claim …>值</claim>` 只留值、不改写任何数字。
"""
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

CLAIM_RE = re.compile(r'<claim\b[^>]*\bid="[^"]*"[^>]*>(.*?)</claim>', re.S)
# ⚠️ `\bid="…"` 不是装饰，是**必需**（2026-10-16 实测事故）：
# 报告正文会**引用 token 形态本身**做说明，例如反引号里的 `` `<claim>` ``、`` `<claim id>` ``。
# 旧模式 `<claim\b[^>]*>` 会把这类**字面提及**也当成标签，而它们**没有闭标签** ⇒ 非贪婪匹配
# 一路吃到**十几行之后**某个真正标签的 `</claim>`，把中间整段正文替换掉，并留下未闭合的
# `<claim …>`（06078 实测：`行841→行860`、`行884→行931` 两处跨行吞噬）。
# 真正的 claim 一定带 `id="<claim_id>"`，字面提及不带 ⇒ 用 `id=` 作判别即可完全分开。
BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
CODE_RE = re.compile(r"`([^`]+)`")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
TBL_SEP_RE = re.compile(r"^\|?[\s:|-]+\|[\s:|-]*$")


def _split_md_row(line: str) -> list:
    """把一行 markdown 表格拆成单元格，**尊重 `\\|` 转义**。

    为什么必须处理转义（真实缺陷）：报表里有含竖线的文本（如 `|HH|`），
    写入时转义成 `\\|HH\\|`。而朴素的 `line.strip("|").split("|")` 会把它
    拆成 3 个单元格 ⇒ 该行比表头多出列 ⇒ HTML 表格**结构错位**，
    且因为渲染器不校验列数，这个错误是**静默**的（我最初只能靠肉眼比对才发现）。
    """
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    cells, cur, i = [], [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    cells.append("".join(cur).strip())
    return cells


def inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = LINK_RE.sub(lambda m: f'{m.group(1)}<span class="url">（{m.group(2)}）</span>', s)
    s = BOLD_RE.sub(r"<strong>\1</strong>", s)
    s = CODE_RE.sub(r"<code>\1</code>", s)
    return s


def strip_claims(s: str) -> str:
    return CLAIM_RE.sub(lambda m: m.group(1), s)


CSS = """
:root{--paper:#FBF8F3;--ink:#1E1E1E;--muted:#6B6B6B;--acc:#9B4A20;--line:#E3D9CB;}
*{box-sizing:border-box}
body{margin:0;background:#EFE9DF;color:var(--ink);
 font:16px/1.75 "Songti SC","Source Han Serif SC",Georgia,serif;}
.wrap{max-width:860px;margin:0 auto;background:var(--paper);padding:56px 40px 80px;
 box-shadow:0 2px 24px rgba(0,0,0,.06);}
.kicker{color:var(--acc);font-weight:700;letter-spacing:.08em;font-size:13px}
h1{font-size:34px;line-height:1.25;margin:10px 0 6px;
 font-family:"Heiti SC","PingFang SC",sans-serif}
h2{font-size:24px;margin:38px 0 12px;padding-top:14px;border-top:1px solid var(--line);
 font-family:"Heiti SC","PingFang SC",sans-serif}
h3{font-size:19px;margin:28px 0 8px;color:var(--acc);
 font-family:"Heiti SC","PingFang SC",sans-serif}
h4{font-size:16px;margin:20px 0 6px;font-family:"Heiti SC","PingFang SC",sans-serif}
p{margin:10px 0;text-align:justify;text-justify:inter-ideograph}
.sub{color:var(--muted);font-size:14px;margin-bottom:26px}
blockquote{margin:12px 0;padding:8px 16px;border-left:3px solid #C8A27A;color:#544C43;
 background:#F6F1E8;font-size:14.5px}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:13.5px;
 font-family:"PingFang SC","Heiti SC",sans-serif}
th,td{border:1px solid var(--line);padding:7px 9px;text-align:left;vertical-align:top}
th{background:#F2ECE3;font-weight:700}
tr:nth-child(even) td{background:#FCFAF6}
code{font-family:Menlo,Consolas,monospace;font-size:12.5px;color:var(--acc);
 background:#F4EDE4;padding:1px 4px;border-radius:3px}
pre{background:#F6F1E8;border:1px solid var(--line);border-radius:6px;padding:14px 16px;
 overflow-x:auto;font-family:Menlo,Consolas,monospace;font-size:12.5px;line-height:1.5}
pre code{background:none;color:var(--ink);padding:0}
hr{border:none;border-top:1px solid var(--line);margin:26px 0}
ul,ol{margin:10px 0 10px 22px;padding:0}
li{margin:4px 0}
.url{color:var(--muted);font-size:12.5px}
.foot{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);
 color:var(--muted);font-size:12.5px}
@media(max-width:640px){.wrap{padding:30px 18px 56px}h1{font-size:26px}h2{font-size:20px}
 table{font-size:12.5px}th,td{padding:5px 6px}}
"""


def build(md_path: Path, out_path: Path, subtitle: str):
    lines = md_path.read_text(encoding="utf-8").splitlines()
    out = []
    i = 0
    first_h1 = True
    in_code = False
    code_buf: list[str] = []
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()

        if s.startswith("```"):
            if in_code:
                out.append("<pre><code>" + html.escape(strip_claims("\n".join(code_buf))) + "</code></pre>")
                code_buf, in_code = [], False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(raw)
            i += 1
            continue

        if s.startswith("|") and i + 1 < len(lines) and TBL_SEP_RE.match(lines[i + 1].strip()):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cur = lines[i].strip()
                if not TBL_SEP_RE.match(cur):
                    rows.append(_split_md_row(cur))
                i += 1
            if rows:
                head, body = rows[0], rows[1:]
                out.append("<table><thead><tr>" +
                           "".join(f"<th>{inline(strip_claims(c))}</th>" for c in head) +
                           "</tr></thead><tbody>" +
                           "".join("<tr>" + "".join(f"<td>{inline(strip_claims(c))}</td>"
                                                    for c in r) + "</tr>" for r in body) +
                           "</tbody></table>")
            continue

        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", s):
            out.append("<hr>")
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            lvl = min(len(m.group(1)), 4)
            txt = strip_claims(BOLD_RE.sub(r"\1", m.group(2)).strip())
            if lvl == 1 and first_h1:
                out.append(f'<div class="kicker">龟龟投资策略 · 投资分析</div>'
                           f"<h1>{inline(txt)}</h1>"
                           f'<div class="sub">{html.escape(subtitle)}</div>')
                first_h1 = False
            else:
                out.append(f"<h{lvl}>{inline(txt)}</h{lvl}>")
            i += 1
            continue

        if s.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            body = " ".join(x for x in buf if x)
            out.append(f"<blockquote>{inline(strip_claims(body))}</blockquote>")
            continue

        m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", raw)
        if m:
            ordered = m.group(2) not in "-*+"
            tag = "ol" if ordered else "ul"
            items = []
            while i < len(lines):
                mm = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", lines[i])
                if not mm:
                    break
                items.append(f"<li>{inline(strip_claims(mm.group(3)))}</li>")
                i += 1
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue

        if not s:
            i += 1
            continue

        out.append(f"<p>{inline(strip_claims(s))}</p>")
        i += 1

    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>中国食品 00506.HK · 龟龟投资策略阅读版</title>
<style>{CSS}</style></head>
<body><div class="wrap">
{chr(10).join(out)}
<div class="foot">本文由龟龟投资策略框架按既定规则机械产出，非投资建议。<br>
claim 标记已剥离为纯文本，数值未做任何改写。</div>
</div></body></html>"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--subtitle", default="阅读版")
    a = ap.parse_args()
    p = build(Path(a.md), Path(a.out), a.subtitle)
    print(f"✅ {p}  ({p.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
