#!/usr/bin/env python3
"""dimension_slicer.py — 包 2 阶段 1：把年报切成「按维度的叙述切片」。

为什么需要它
------------
/business-analysis 现架构把整份年报载入单个 context（实测 245K–617K 字符），在 128K
窗口下不可行。本脚本把年报切成每个维度一份、**预算内**的切片，并输出覆盖率 manifest，
使下游的 6 个维度子代理各只读自己的一份（每份 ≤ budget-chars）。

选择策略：两阶段（由四版原型在真实年报上的实测确定）
----------------------------------------------------
1. **定位章节**（而不是"关键词命中页"）：
   - A 股：``第X节`` 标题页 → 段落 = [标题页, 下一标题页−1]
   - 港股：``董事會報告`` / ``管理層討論及分析`` / ``企業管治報告`` / ``財務報表附註`` 等
     标题页；并优先使用**目录页码反查**（港股年报目录通常写作「董事會報告 ...... 50」）
2. **段内按预算二次切分**：段首结论段 → 含小标题页 → 含数字表页 → 其余按页序补齐。

实测教训（已编码为约束）
------------------------
- **目录页必须排除**：它对几乎每个维度都命中关键词（v2 原型把 D5 选成了封面+目录）。
- **纯关键词选页会双向失败**：要么挑到目录，要么只挑到标题页（v2b 原型只剩 913 字符）。
- **章节段落普遍超预算**：某 A 股 MD&A 实测 31,351 字符 vs 12,000 预算 → 段内二次切分＋
  在切片头声明丢弃范围是必需能力。
- **港股不能套用 A 股规则**：``第X节`` 在港股样本上 0 命中。

CLI::

    python3 scripts/dimension_slicer.py --code 002984.SZ \\
        --output-dir output/002984_森麒麟 --budget-chars 12000 --report-coverage

Exit codes: 0 success · 2 input missing / unparseable
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

PAGE_MARKER_RE = re.compile(r"===== PDF PAGE (\d+) =====")

# --- 章节定位 -------------------------------------------------------------
SECTION_RE_CN = re.compile(r"^第\s*([一二三四五六七八九十百]+)\s*节\s*([^\n]{0,40})", re.M)
SECTION_RE_HEADING = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百]+[章节]\s*)?"
    r"(董事會報告|董事会报告|管理層討論及分析|管理层讨论与分析|經營情況討論與分析|经营情况讨论与分析|"
    r"企業管治報告|企业管治报告|公司治理|財務報表附註|财务报表附注|合併財務報表|合并财务报表|"
    r"主席報告|主席报告|業務回顧|业务回顾|經營回顧|经营回顾|"
    # 英文年报（HKEXnews 的 English 版，如 00696 2025 年报全篇英文）没有中文标题，
    # 只认中文会让 build_sections() 返回空集，6 个维度全部退化为"全文关键词兜底"。
    r"STATEMENT OF THE BOARD|BUSINESS REVIEW|MANAGEMENT DISCUSSION AND ANALYSIS|"
    r"CORPORATE GOVERNANCE REPORT|REPORT OF DIRECTORS|REPORT OF THE SUPERVISORY COMMITTEE|"
    r"AUDITORS’ REPORT|AUDITORS' REPORT|INDEPENDENT AUDITOR|"
    r"NOTES TO THE FINANCIAL STATEMENTS|FINANCIAL HIGHLIGHTS|"
    r"CHAIRMAN’S STATEMENT|CHAIRMAN'S STATEMENT|CORPORATE INFORMATION)\b",
    re.IGNORECASE | re.MULTILINE,
)
TOC_LINE_RE = re.compile(r"^\s*(.{2,40}?)\s*[.·]{3,}\s*(\d{1,3})\s*$", re.M)
# 港股/双语目录无点线，形如「MANAGEMENT DISCUSSION AND ANALYSIS 管理層論析 19」
TOC_PLAIN_LINE_RE = re.compile(r"^(.{2,60}?)\s{1,}(\d{1,3})\s*$", re.M)
TOC_PAGE_HINT_RE = re.compile(r"CONTENTS|目\s*錄|目\s*录")
DOT_LEADER_RE = re.compile(r"[.·]{4,}\s*\d")
SUBHEADING_RE = re.compile(r"^\s*(?:[（(][一二三四五六七八九十0-9]+[）)]|[一二三四五六七八九十]+[、.])", re.M)
TABLE_HINT_RE = re.compile(r"单位：元|單位：元|金额单位|金額單位|单位：人民币")
NUMERIC_CELL_RE = re.compile(r"-?\d[\d,]*\.\d{2}")

# --- 维度规格 -------------------------------------------------------------
# sections: 用于章节定位的标题特征（A股第X节标题 或 港股标题）
# keywords: 段内（或兜底）选页的关键词
# contiguous: True = 叙事型维度，段内优先连续页段；False = 数字型维度，允许分散取页
DIMENSIONS: Dict[str, Dict[str, Any]] = {
    "D1": {
        "label": "商业模式与资本特征",
        "sections": ["管理层讨论与分析", "经营情况讨论与分析", "管理層論析", "管理層討論及分析",
                     "management discussion", "董事會報告", "董事会报告", "主要業務", "主要业务",
                     "principal business", "business review", "statement of the board"],
        "keywords": ["主营业务", "分行业", "分产品", "营业收入", "营业成本", "毛利率", "资本开支",
                     "折旧及摊销", "分部", "主要業務", "分部收入", "revenue", "gross profit"],
        "contiguous": False,
    },
    "D2": {
        "label": "竞争优势与护城河",
        "sections": ["管理层讨论与分析", "经营情况讨论与分析", "管理層論析", "管理層討論及分析",
                     "management discussion", "董事會報告", "董事会报告", "主席函件",
                     "statement from the chairman", "business review", "statement of the board"],
        "keywords": ["竞争", "競爭", "市场份额", "市場份額", "研发", "研發", "专利", "專利",
                     "前五名客户", "前五大客戶", "供应商", "供應商", "行业地位", "行業地位",
                     "competitive", "market share"],
        "contiguous": False,
    },
    "D3": {
        "label": "外部环境",
        "sections": ["行業", "行业", "industry", "政策", "policy",
                     # 英文年报没有独立"行业/风险"章节：外部环境材料落在 Business Review
                     # （宏观/行业/政策）与 Report of Directors（风险与不确定因素）里。
                     "business review", "report of directors", "statement of the board"],
        # 实测教训：不含"风险"关键词时，D3 兜底选页会漏掉年报「公司面临的风险和应对措施」章节，
        # 导致 regulatory_risk 只能靠 web_search 支撑。
        "keywords": ["行业", "行業", "政策", "监管", "監管", "周期", "週期", "宏观", "宏觀", "汇率", "關稅",
                     "风险", "風險", "风险因素", "风险管理", "关税", "贸易", "欧盟", "反倾销",
                     "risk factor", "tariff"],
        "contiguous": False,
    },
    "D4": {
        "label": "管理层与治理",
        "sections": ["公司治理", "企業管治報告", "企业管治报告", "corporate governance",
                     "董事及高級管理層", "directors and senior management",
                     "董事會報告", "董事会报告", "董事、监事、高级管理人员"],
        "keywords": ["董事会", "董事會", "监事会", "監事會", "审计委员会", "審核委員會", "薪酬",
                     "高级管理人员", "高級管理人員", "核数师", "核數師", "关联交易", "關聯交易"],
        "contiguous": False,
    },
    "D5": {
        "label": "MD&A 解读",
        "sections": ["管理层讨论与分析", "经营情况讨论与分析", "管理層論析", "管理層討論及分析",
                     "management discussion", "董事會報告", "董事会报告", "业务回顾", "業務回顧",
                     "主席函件", "statement from the chairman", "董事總經理函件"],
        "keywords": ["经营回顾", "經營回顧", "业务回顾", "業務回顧", "未来发展", "未來發展", "展望"],
        "contiguous": True,
    },
    "D6": {
        "label": "控股结构（条件触发）",
        "sections": ["財務報表附註", "财务报表附注", "notes to the financial statements",
                     "合併財務報表", "合并财务报表", "綜合財務報表", "财务报告", "財務報告",
                     "independent auditor", "獨立核數師報告"],
        # 实测教训：D6 需要附注里"在其他主体中的权益 / 分部报告"那几页（子公司总资产/净资产/净利润），
        # 只靠"子公司/长期股权投资"会选到别处，使 SOTP 只能用合并归母权益做账面替代锚。
        "keywords": ["子公司", "附屬公司", "长期股权投资", "長期股權投資", "合营", "合營",
                     "联营", "聯營", "合并范围", "合併範圍", "控股股东", "控股股東",
                     "在其他主体中的权益", "在其他主體中的權益", "分部报告", "分部報告",
                     "主要子公司", "納入合併範圍", "持股比例", "subsidiaries", "associates",
                     "segment information", "interests in other entities"],
        "contiguous": False,
    },
}


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

def load_pages(text: str) -> Dict[int, str]:
    """把 ``===== PDF PAGE n =====`` 标记的全文解析为 {页码: 正文}。"""
    parts = PAGE_MARKER_RE.split(text)
    pages: Dict[int, str] = {}
    for i in range(1, len(parts), 2):
        pages[int(parts[i])] = parts[i + 1]
    return pages


def is_front_or_toc(text: str) -> bool:
    """前置页／目录页判定 —— 必须在选页前排除（实测教训）。

    目录页会命中几乎每个维度的关键词，且不含任何分析材料。
    封面与分隔页同理：极短且既无数字也无小标题，占了预算却不产出信息。
    """
    t = (text or "").strip()
    if DOT_LEADER_RE.search(t):
        return True
    head = t[:300]
    if "目录" in head or "目 錄" in head or "目錄" in head:
        return True
    # 英文年报（HKEXnews English 版）的目录页首行是 CONTENTS；只认中文会让目录页
    # 混进切片并吃掉预算（00696 实测 D5 的兜底选页把目录页第 3 页选了进来）。
    if re.search(r"^\s*CONTENTS\s*$", head, re.MULTILINE):
        return True
    # 同一页出现多个「第X节」条目 = 目录特征
    if len(re.findall(r"第[一二三四五六七八九十百]+节", t)) >= 3:
        return True
    # 封面／章节分隔页：极短、无金额、无小标题
    if len(t) < 120 and not NUMERIC_CELL_RE.search(t) and not SUBHEADING_RE.search(t):
        return True
    return False


def parse_toc(pages: Dict[int, str]) -> Tuple[Dict[str, int], List[Tuple[int, str]]]:
    """解析目录 → ``({章节名: 页码}, [(页码, 章节名)] 按页码排序)``。

    支持两种真实格式：
    - A 股：点线式 ``第三节 管理层讨论与分析 .......... 10``
    - 港股：双语无点线 ``MANAGEMENT DISCUSSION AND ANALYSIS 管理層論析 19``
      （实测：港股目录**完全不带点线**，早期只认点线的版本因此 0 命中）
    """
    toc: Dict[str, int] = {}
    ordered: List[Tuple[int, str]] = []
    last_page = max(pages)

    for page_no in sorted(pages):
        text = pages[page_no]
        has_leader = bool(DOT_LEADER_RE.search(text))
        is_toc_page = bool(TOC_PAGE_HINT_RE.search(text[:400]))
        if not (has_leader or is_toc_page):
            continue

        entries: List[Tuple[str, int]] = []
        if has_leader:
            for name, num in TOC_LINE_RE.findall(text):
                entries.append((name.strip(" .·　"), int(num)))
        if is_toc_page or len(TOC_PLAIN_LINE_RE.findall(text)) >= 5:
            for name, num in TOC_PLAIN_LINE_RE.findall(text):
                name = name.strip()
                if len(name) < 2 or name.replace(" ", "").isdigit():
                    continue
                entries.append((name, int(num)))

        for name, target in entries:
            if not name or target < 1 or target > last_page:
                continue
            if name not in toc:
                toc[name] = target
                ordered.append((target, name))

    ordered.sort()
    return toc, ordered


def build_sections_from_toc(ordered: List[Tuple[int, str]], last_page: int) -> List[Dict[str, Any]]:
    """用目录条目构造章节区间：第 i 节 = [page_i, page_{i+1} − 1]。

    这是**首选**的定位方式（对 A 股与港股同时有效），标题词表只作兜底。
    """
    sections = []
    for i, (start, name) in enumerate(ordered):
        end = ordered[i + 1][0] - 1 if i + 1 < len(ordered) else last_page
        if end < start:
            end = start
        sections.append({"start": start, "end": end, "title": name, "kind": "toc"})
    return sections


def build_sections(pages: Dict[int, str]) -> List[Dict[str, Any]]:
    """构造章节索引：``第X节``（A 股）与港股标题，返回 [{start, end, title, kind}]。

    仅认「出现在页首附近、且不含目录点线」的标题，避免把正文引用误判为章节起点。
    """
    found: List[Tuple[int, str, str]] = []
    for page_no in sorted(pages):
        text = pages[page_no]
        if is_front_or_toc(text):
            continue
        head = text[:120]
        m = SECTION_RE_CN.search(head)
        title = ""
        kind = ""
        if m:
            title = f"第{m.group(1)}节 {m.group(2).strip()}"
            kind = "cn_section"
        else:
            m2 = SECTION_RE_HEADING.search(head)
            if m2:
                title = m2.group(1)
                kind = "hk_heading"
        if title:
            found.append((page_no, title, kind))

    # 去重：同一章节跨页重复出现页眉
    dedup: List[Tuple[int, str, str]] = []
    seen = set()
    for page_no, title, kind in found:
        key = title[:8]
        if key in seen:
            continue
        seen.add(key)
        dedup.append((page_no, title, kind))

    last_page = max(pages)
    sections = []
    for i, (start, title, kind) in enumerate(dedup):
        end = dedup[i + 1][0] - 1 if i + 1 < len(dedup) else last_page
        if end < start:
            end = start
        sections.append({"start": start, "end": end, "title": title, "kind": kind})
    return sections


# --------------------------------------------------------------------------
# 选择
# --------------------------------------------------------------------------

def _page_priority(text: str, keywords: List[str]) -> Tuple[int, int]:
    """段内选页优先级：小标题页 > 数字表页 > 关键词密度。"""
    score = 0
    if TABLE_HINT_RE.search(text) or len(NUMERIC_CELL_RE.findall(text)) >= 8:
        score += 3
    if len(SUBHEADING_RE.findall(text)) >= 2:
        score += 2
    score += min(sum(text.count(k) for k in keywords), 10)
    return score, -len(text)


def _pick_within_budget(candidates: List[Tuple[int, str]], budget: int,
                        contiguous: bool,
                        allow_truncate: bool = True) -> Tuple[List[int], int, int, Optional[int]]:
    """在预算内选页，返回 ``(选中页, 已用字符, 截断字符数, 被截断的页)``。

    规则：
    - 整页放不下 → **跳过**（保证表格/段落不被截断，尝试后续更小的页）
    - 若**首页本身就超预算**且 ``allow_truncate`` → 截断到预算（预算必须是硬上限，
      否则单页可撑爆上下文）。头/尾分预算调用时关闭该行为，避免出现两处截断
      而 manifest 只能记录一处。
    """
    chosen: List[int] = []
    used = 0
    cut = 0
    truncated_page: Optional[int] = None
    for page_no, text in candidates:
        length = len(text)
        if used + length > budget:
            if chosen:
                continue
            if budget <= 0:
                break
            if not allow_truncate:
                continue
            chosen.append(page_no)
            cut = length - budget
            used = budget
            truncated_page = page_no
            break
        chosen.append(page_no)
        used += length
        if used >= budget * 0.95:
            break
    return sorted(chosen), used, cut, truncated_page


def _match_section(sections: List[Dict[str, Any]], patterns: List[str]) -> Optional[Dict[str, Any]]:
    """按**模式优先级**匹配章节（大小写不敏感，兼容港股双语标题）。

    ``patterns`` 的顺序即优先级：先尝试"管理层讨论与分析/管理層論析"，再退到"董事会报告"等。
    若按文档顺序取最早命中，D4 会被"董事及高級管理層簡介"（人物简历）抢走，
    而真正的治理内容在后面的"企業管治報告"里 —— 因此必须按优先级匹配。

    同一优先级下，**页面标题推导的区间优先于目录推导的区间**：目录里的页码是**印刷页码**，
    而切片渲染用的是 **PDF 页码**；英文年报前置页多（00696 实测偏移 +2），沿用目录页码会让
    整个窗口整体前移、落在相邻章节上。页面标题的区间本就以 PDF 页码为准。
    """
    for pat in patterns:
        needle = pat.lower()
        hits = [s for s in sorted(sections, key=lambda s: (0 if s.get("kind") != "toc" else 1,
                                                           s["start"]))
                if needle in str(s["title"]).lower()]
        if hits:
            return hits[0]
    return None


def slice_dimension(pages: Dict[int, str], sections: List[Dict[str, Any]],
                    toc: Dict[str, int], dim: str, budget: int) -> Dict[str, Any]:
    """为单个维度产出切片信息（不写盘，便于测试）。

    ``sections`` 应优先传入**目录推导**的区间（``build_sections_from_toc``），
    标题词表兜底的区间作为其补充。
    """
    spec = DIMENSIONS[dim]
    keywords: List[str] = spec["keywords"]
    content_pages = {p: t for p, t in pages.items() if not is_front_or_toc(t)}

    anchor_title: Optional[str] = None
    anchor_kind = "keyword_fallback"
    window: List[int] = []

    best = _match_section(sections, spec["sections"])

    # ① 目录/标题定位的章节区间（首选）
    if best:
        anchor_title = best["title"]
        anchor_kind = "toc_section" if best.get("kind") == "toc" else "section_heading"
        window = [p for p in range(best["start"], best["end"] + 1) if p in content_pages]

    # ② 目录页码反查（章节已能由目录区间覆盖，这里作为补充）
    if not window and toc:
        for name, target in sorted(toc.items(), key=lambda kv: kv[1]):
            if any(pat.lower() in name.lower() for pat in spec["sections"]) and target in pages:
                anchor_title = f"{name}（目录反查 p{target}）"
                anchor_kind = "toc_lookup"
                window = [p for p in sorted(content_pages) if p >= target][:40]
                break

    # ③ 兜底：全文关键词选页
    if not window:
        window = sorted(content_pages)

    # 段内排序
    anchor_page = window[0] if window else None
    if spec["contiguous"]:
        # 叙事型维度（如 D5 MD&A）：框架既要"经营结果"也要"展望/风险"，
        # 因此**头+尾各取一段**而不是只取开头。
        # 实测教训：只取开头会让 MD&A 只覆盖 p10–17，把 p18–32 的展望与风险章节整段丢掉，
        # 维度代理只能声明 material_status=不足。
        ordered = list(window)
        if anchor_page is not None and anchor_page in content_pages:
            ordered = [anchor_page] + [p for p in ordered if p != anchor_page]
        head_budget = int(budget * 0.55)
        # 头部允许截断（锚点页本身就是超预算时的兜底），尾部不允许（避免两处截断而
        # manifest 只能记录一处）。
        head, used_head, cut, truncated_page = _pick_within_budget(
            [(p, content_pages[p]) for p in ordered], head_budget, True, allow_truncate=True)
        remaining = max(budget - used_head, 0)
        tail_candidates = [(p, content_pages[p]) for p in reversed(ordered) if p not in head]
        tail, used_tail, _, _ = _pick_within_budget(
            tail_candidates, remaining, True, allow_truncate=False)
        chosen = sorted(set(head) | set(tail))
        used = used_head + used_tail
        if not chosen:
            # 兜底：极端小预算下头尾都放不下任何整页 → 退回"截断首页"，绝不产出空切片
            candidates = [(p, content_pages[p]) for p in ordered]
            chosen, used, cut, truncated_page = _pick_within_budget(candidates, budget, True)
    else:
        ranked = sorted(window, key=lambda p: (_page_priority(content_pages[p], keywords), -p), reverse=True)
        if anchor_page is not None and anchor_page in ranked:
            ranked.remove(anchor_page)
            ranked.insert(0, anchor_page)
        candidates = [(p, content_pages[p]) for p in ranked]
        chosen, used, cut, truncated_page = _pick_within_budget(candidates, budget, False)
    dropped = len(window) - len(chosen)
    section_chars = sum(len(content_pages[p]) for p in window)

    return {
        "dimension": dim,
        "label": spec["label"],
        "anchor": anchor_title or "（无章节锚点，全文关键词兜底）",
        "anchor_kind": anchor_kind,
        "section_pages": [window[0], window[-1]] if window else [],
        "section_chars": section_chars,
        "provided_pages": chosen,
        "used_chars": used,
        "truncated_page": truncated_page,
        "truncated_chars": cut,
        "dropped_pages": max(dropped, 0),
        "strategy": "contiguous_fill" if spec["contiguous"] else "priority_fill",
    }


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

SLICE_HEADER = """# 切片 — {dim} {label}｜{code}

> ⚠️ **本次仅提供 {n} / {total} 页（占全文 {page_pct:.1f}%，{used_chars:,} 字符）**。未覆盖的部分**不得推断**；
> 若据本切片不足以判断，请在 `verdict.material_status` 标注「不足」并给出 `data_insufficient`。
>
> 锚点：{anchor}｜策略：{strategy}｜段落规模：{section_chars:,} 字符 → 按 {budget:,} 字符预算取页（填充 {fill_pct:.1f}%）
> manifest：`{manifest_path}`

"""


def render_slice(entry: Dict[str, Any], pages: Dict[int, str], code: str,
                 total_pages: int, budget: int, manifest_path: str) -> str:
    chosen = entry["provided_pages"]
    used = entry["used_chars"]
    truncated_page = entry.get("truncated_page")
    out = [SLICE_HEADER.format(
        dim=entry["dimension"], label=entry["label"], code=code,
        n=len(chosen), total=total_pages,
        page_pct=(len(chosen) / total_pages * 100) if total_pages else 0.0,
        used_chars=used,
        fill_pct=(used / budget * 100) if budget else 0.0,
        anchor=entry["anchor"], strategy=entry["strategy"],
        section_chars=entry["section_chars"], budget=budget, manifest_path=manifest_path,
    )]
    for p in chosen:
        text = pages[p]
        if truncated_page == p:
            keep = len(text) - int(entry.get("truncated_chars") or 0)
            text = text[:max(keep, 0)]
            out.append(f"\n> ⚠️ 本页按预算截断（原 {len(pages[p]):,} 字符）\n")
        out.append(f"\n## 页 {p}\n\n{text.strip()}\n")
    return "".join(out)


def _discover_txt(output_dir: str) -> Optional[str]:
    for pattern in ("annual_report*.txt", "*年报*.txt", "*年度报告*.txt"):
        hits = sorted(glob.glob(os.path.join(output_dir, pattern)))
        if hits:
            return hits[0]
    return None


def _discover_pdf(output_dir: str) -> Optional[str]:
    hits = sorted(glob.glob(os.path.join(output_dir, "*.pdf")))
    return hits[0] if hits else None


def render_vision_pngs(pdf_path: str, pages: List[int], out_dir: str, dpi: int = 170) -> List[str]:
    """可选：把选中页渲染为 PNG，供视觉路径（V4.1-Flash 原生多模态）读取。"""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        print("  ⚠️ 未安装 pypdfium2，跳过 --vision 渲染", file=sys.stderr)
        return []
    doc = pdfium.PdfDocument(pdf_path)
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for page_no in pages:
        if page_no - 1 >= len(doc):
            continue
        img = doc[page_no - 1].render(scale=dpi / 72).to_pil()
        path = os.path.join(out_dir, f"page_{page_no:03d}.png")
        img.save(path)
        written.append(path)
    return written


def main() -> int:
    ap = argparse.ArgumentParser(
        description="按维度把年报切成预算内的叙述切片（包 2 阶段 1）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --code 002984.SZ --output-dir output/002984_森麒麟 --report-coverage
  %(prog)s --code 00506.HK --output-dir output/00506_中国食品 --budget-chars 10000 --dim D5
  %(prog)s --code 002984.SZ --output-dir output/002984_森麒麟 --vision
        """,
    )
    ap.add_argument("--code", required=True, help="股票代码，如 002984.SZ / 00506.HK")
    ap.add_argument("--output-dir", required=True, help="该标的的输出目录")
    ap.add_argument("--budget-chars", type=int, default=12000, help="单维度源码预算（默认 12000 字符）")
    ap.add_argument("--dim", default="all", help="all 或逗号分隔的维度，如 D5,D2")
    ap.add_argument("--txt", default=None, help="年报全文 txt（默认在 output-dir 中自动查找）")
    ap.add_argument("--vision", action="store_true", help="额外把选中页渲染为 PNG（视觉路径用）")
    ap.add_argument("--report-coverage", action="store_true", help="打印覆盖率表")
    args = ap.parse_args()

    txt_path = args.txt or _discover_txt(args.output_dir)
    if not txt_path or not os.path.exists(txt_path):
        print(f"Error: 未找到年报全文 txt（查找 {args.output_dir}/annual_report*.txt）。"
              f"可先用 scripts/extract_pdf_text.py 生成。", file=sys.stderr)
        return 2

    pages = load_pages(open(txt_path, encoding="utf-8", errors="ignore").read())
    if not pages:
        print(f"Error: {txt_path} 中未找到 '===== PDF PAGE n =====' 页标记", file=sys.stderr)
        return 2

    dims = list(DIMENSIONS) if args.dim == "all" else [d.strip().upper() for d in args.dim.split(",")]
    bad = [d for d in dims if d not in DIMENSIONS]
    if bad:
        print(f"Error: 未知维度 {bad}（可选 {list(DIMENSIONS)}）", file=sys.stderr)
        return 2

    sections_from_headings = build_sections(pages)
    toc, toc_ordered = parse_toc(pages)
    sections_from_toc = build_sections_from_toc(toc_ordered, max(pages))
    # 目录推导的区间优先；标题词表推导的区间**并列保留**（不再丢弃）。
    #
    # 为什么不再丢弃：旧逻辑把"起点落在任何目录区间内"的标题区间全部剔除，而目录区间
    # 通常首尾相接覆盖全文 ⇒ 标题区间**必然被全部剔除**，等于该兜底路径形同虚设。
    # 目录会漏（英文年报的目录条目会换行折断：00696 的
    # 「Management Discussion and Analysis of Financial / Conditions and Results of
    # Operations 10」只剩后半段，D1/D2/D5 因此全部退化）。标题词表能补上这一刀。
    # `_match_section` 按文档顺序取最早命中，同名者无副作用；故只需去重完全相同的条目。
    seen_pairs = {(s["start"], str(s["title"]).lower()) for s in sections_from_toc}
    sections = sections_from_toc + [
        s for s in sections_from_headings
        if (s["start"], str(s["title"]).lower()) not in seen_pairs
    ]
    front = sum(1 for t in pages.values() if is_front_or_toc(t))
    total_pages = max(pages)

    slices_dir = os.path.join(args.output_dir, "slices")
    os.makedirs(slices_dir, exist_ok=True)
    manifest_path = os.path.join(slices_dir, "slice_manifest.json")

    manifest: Dict[str, Any] = {
        "code": args.code,
        "source": os.path.basename(txt_path),
        "total_pages": total_pages,
        "budget_chars": args.budget_chars,
        "excluded_front_or_toc_pages": front,
        "sections_detected": len(sections),
        "toc_entries": len(toc),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dimensions": {},
    }

    pdf_path = _discover_pdf(args.output_dir) if args.vision else None
    for dim in dims:
        entry = slice_dimension(pages, sections, toc, dim, args.budget_chars)
        manifest["dimensions"][dim] = entry
        text = render_slice(entry, pages, args.code, total_pages,
                            args.budget_chars, os.path.relpath(manifest_path, args.output_dir))
        with open(os.path.join(slices_dir, f"slice_{dim}.md"), "w", encoding="utf-8") as f:
            f.write(text)
        if args.vision:
            if not pdf_path:
                print("  ⚠️ 未找到 PDF，跳过 --vision", file=sys.stderr)
            else:
                pngs = render_vision_pngs(pdf_path, entry["provided_pages"],
                                          os.path.join(slices_dir, f"vision_{dim}"))
                entry["vision_pngs"] = [os.path.relpath(p, args.output_dir) for p in pngs]

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")

    if args.report_coverage:
        print(f"切片完成：{args.code}｜全文 {total_pages} 页｜排除前置/目录 {front} 页｜"
              f"识别章节 {len(sections)} 个｜目录条目 {len(toc)} 条")
        print(f"  {'维度':6s} {'锚点类型':16s} {'选中页':>6s} {'字符':>8s} {'占预算':>7s} {'丢弃页':>6s}  锚点")
        for dim in dims:
            e = manifest["dimensions"][dim]
            fill = e["used_chars"] / args.budget_chars * 100 if args.budget_chars else 0
            print(f"  {dim:6s} {e['anchor_kind']:16s} {len(e['provided_pages']):6d} "
                  f"{e['used_chars']:8,} {fill:6.1f}% {e['dropped_pages']:6d}  {e['anchor'][:28]}")
    else:
        print(f"切片写入 {slices_dir}/（{len(dims)} 个维度，manifest: {manifest_path}）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
