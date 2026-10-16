#!/usr/bin/env python3
"""龟龟投资框架 · 精华长图渲染器（供 DSH 插件 `share_longimage` 调用）。

把 ``output/{code}_{company}/`` 里已经审计过的成果文件，机械地抽取成一张
适合分享到 B 站的中文长图，并附带按内容块切分的分P图片。

设计约束（与仓库 AGENTS.md 一致）
--------------------------------
* **零新算术**：本脚本只做「读取 / 摘录 / 排版」，不计算、不推断、不改写任何
  数值。所有数字都原样来自报告或 sidecar 文件；claim 标记被剥离为纯文本。
* **零第三方依赖**：只用标准库。渲染走本机 Chrome 的无头模式
  （``--dump-dom`` 量高度、``--screenshot`` 出图），不依赖 Pillow / Playwright。
* **可降级**：缺少 Chrome 时仍然产出 HTML 并如实报错，不静默伪造图片。
* **可选文案覆盖**：``--brief brief.json`` 允许 agent 提供人工精选的标题与解说词，
  覆盖自动抽取的对应区块；没有 brief 时全自动出图。

用法::

    python3 render_longimage.py --dir output/06862_海底捞
    python3 render_longimage.py --report <report.md> --brief <brief.json> --json

排版取向：Claude 文档风格的暖纸底 + 衬线大标题 + 发丝分隔线 + 克制的陶土色强调；
字号按「手机上看长图」定标（正文 30px @ 1080 宽），而不是按桌面网页定标。
"""
from __future__ import annotations

import argparse
import html as _html
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

RESULT_MARKER = "DSH_SHARE_LONGIMAGE_RESULT "

# --------------------------------------------------------------------------- #
# 主题（Claude 版式：暖纸、低对比墨色、发丝线、单一强调色）
# --------------------------------------------------------------------------- #

THEMES: dict[str, dict[str, str]] = {
    "paper": {
        "paper": "#FAF9F5",
        "card": "#FFFFFF",
        "ink": "#181715",
        "ink2": "#4F4C46",
        "muted": "#8B857A",
        "hair": "#E8E4D9",
        "clay": "#C15F3C",
        "clay_bg": "#F6EAE4",
        "green": "#3F6B45",
        "amber": "#946A12",
        "shadow": "0 1px 2px rgba(24,23,21,.04)",
    },
    "ink": {
        "paper": "#1C1B18",
        "card": "#242320",
        "ink": "#F5F3EE",
        "ink2": "#C6C1B7",
        "muted": "#8E8980",
        "hair": "#332F2A",
        "clay": "#E08A63",
        "clay_bg": "#2E2620",
        "green": "#8FBF94",
        "amber": "#D9AB55",
        "shadow": "none",
    },
}

# 若本机装了这些字体家族就优先用，否则逐级回退。
SERIF_STACK = ('"Songti SC","Songti TC","STSong","Source Han Serif SC",'
               '"Noto Serif CJK SC","Times New Roman",serif')
SANS_STACK = ('"PingFang SC","Hiragino Sans GB","Heiti SC","STHeiti",'
              '"Microsoft YaHei","Noto Sans CJK SC",sans-serif')

DIM_NAMES = {
    "D1": "商业模式与资本特征",
    "D2": "竞争优势与护城河",
    "D3": "外部环境",
    "D4": "管理层与公司治理",
    "D5": "MD&A 解读",
    "D6": "控股结构",
}

DIM_ORDER = ["D1", "D2", "D3", "D4", "D5", "D6"]


# --------------------------------------------------------------------------- #
# Markdown 工具
# --------------------------------------------------------------------------- #

CLAIM_RE = re.compile(r"</?claim\b[^>]*>")


def strip_claims(text: str) -> str:
    """把 <claim id="…" policy="…">4.66%</claim> 还原为 4.66%。"""
    return CLAIM_RE.sub("", text)


def norm(text: str) -> str:
    """折叠空白，去掉 markdown 装饰，用于长度判断与 JSON 输出。"""
    t = strip_claims(text)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"(?<!\w)[*_](?!\s)", "", t)
    t = t.replace("`", "")
    t = re.sub(r"\[src:[^\]]*\]", "", t)
    t = re.sub(r"\s+", " ", t)
    return cjk_tighten(t).strip()


CJK_TIGHTEN = [
    # 中文排版：行内换行折叠成空格后，标点前/括号后不该留空格
    (re.compile(r"\s+(?=[，。、；：！？）］｝」』】…—])"), ""),
    (re.compile(r"(?<=[（［｛「『【])\s+"), ""),
    # 汉字之间的空格是 PDF 抽取/折行的痕迹，中文不用空格分词；只删「汉字↔汉字」，
    # 因此「5 年」「A 股」这类中英/数字边界上的空格会保留
    (re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])"), ""),
]


def cjk_tighten(text: str) -> str:
    for rx, repl in CJK_TIGHTEN:
        text = rx.sub(repl, text)
    return text


def esc(text: str) -> str:
    return _html.escape(text, quote=True)


def inline(text: str) -> str:
    """极简 inline markdown → HTML（表格/正文共用）。"""
    t = esc(strip_claims(text))
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[src:[^\]]*\]", "", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"(?<![\w<])_([^_]+)_(?![\w>])", r"<i>\1</i>", t)
    return cjk_tighten(t)


def clip(text: str, limit: int, ellipsis: str = "…") -> str:
    t = norm(text)
    if len(t) <= limit:
        return t
    return t[: max(1, limit - 1)].rstrip(" ，。、；：,;:") + ellipsis


def first_sentence(text: str, limit: int = 160) -> str:
    """取第一句（中英文句末标点都算），过长则按 limit 截断。"""
    t = norm(text)
    m = re.search(r"[。！？；!?;]", t)
    if m and m.end() <= limit + 60:
        out = t[: m.end()]
        return out[:-1] + "。" if out.endswith(("；", ";")) else out
    return clip(t, limit)


def short_value(text: str, limit: int = 26) -> str:
    """卡片 / KV 的取值：取第一个测量值短语，丢掉冗长的括号说明。

    「KK = −0.34 pct（决策口径 = GG_pess − II）」→「KK = −0.34 pct」；
    「高（5 项判据中 2 项「存在」）」→「高」。括号里的口径说明另有 note 承载。
    """
    t = norm(text)
    if not t:
        return ""
    head = re.split(r"[（(]", t, maxsplit=1)[0].strip(" ，。、；：,;:·")
    if head:
        return head if len(head) <= limit else clip(head, limit)
    return clip(t, limit)


@dataclass
class Section:
    level: int
    title: str
    body: str

    @property
    def key(self) -> str:
        return norm(self.title)


def split_sections(md: str, max_level: int = 3) -> list[Section]:
    """按 ATX 标题切分（跳过 fenced code 内部的 '#' 行）。"""
    out: list[Section] = []
    cur: Section | None = None
    fence = False
    for line in md.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = not fence
        if not fence:
            m = re.match(r"^(#{1,%d})\s+(.*?)\s*$" % max_level, line)
            if m:
                if cur is not None:
                    out.append(cur)
                cur = Section(len(m.group(1)), m.group(2), "")
                continue
        if cur is None:
            cur = Section(0, "", "")
        cur.body += line + "\n"
    if cur is not None:
        out.append(cur)
    return out


def find_section(sections: Sequence[Section], *patterns: str) -> Section | None:
    for pat in patterns:
        rx = re.compile(pat)
        for s in sections:
            if s.level and rx.search(s.key):
                return s
    return None


def find_sections(sections: Sequence[Section], *patterns: str) -> list[Section]:
    rx = re.compile("|".join("(?:%s)" % p for p in patterns))
    return [s for s in sections if s.level and rx.search(s.key)]


def table_rows(block: str) -> list[list[str]]:
    """解析第一个 markdown 表格 → [[cell,…], …]（不含表头分隔行）。"""
    rows: list[list[str]] = []
    for line in block.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            if rows:
                break
            continue
        if re.fullmatch(r"\|[\s:|-]+\|", s):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        rows.append(cells)
    return rows


def list_items(block: str, limit: int = 0) -> list[str]:
    """抽取 -/•/数字 列表项（合并续行）。"""
    items: list[str] = []
    buf: list[str] = []
    for line in block.splitlines():
        m = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$", line)
        if m:
            if buf:
                items.append("\n".join(buf))
            buf = [m.group(1)]
        elif buf and line.strip() and not line.lstrip().startswith(("#", "|", ">", "```")):
            buf.append(line.strip())
        elif buf and not line.strip():
            items.append("\n".join(buf))
            buf = []
    if buf:
        items.append("\n".join(buf))
    out = [norm(i) for i in items if norm(i)]
    return out[:limit] if limit else out


def paragraphs(block: str, skip_quote: bool = True) -> list[str]:
    """按空行切段；默认跳过引用块与代码块。"""
    out: list[str] = []
    fence = False
    for chunk in re.split(r"\n\s*\n", block):
        c = chunk.strip()
        if not c:
            continue
        if c.startswith("```") or c.startswith("~~~"):
            fence = not fence
            continue
        if fence:
            continue
        if c.startswith("|") or re.match(r"^#{1,6}\s", c):
            continue
        if skip_quote and all(l.strip().startswith(">") for l in c.splitlines() if l.strip()):
            continue
        out.append(c)
    return out


def kv_from_box(block: str) -> dict[str, str]:
    """从「综合结论」的 ASCII 框里抽 `键：值` 行。"""
    out: dict[str, str] = {}
    for line in block.splitlines():
        m = re.match(r"^\s*([\u4e00-\u9fff][^：:=＝]{1,14}?)\s*[：:=＝]\s*(.+?)\s*$", line)
        if m:
            out.setdefault(norm(m.group(1)), norm(m.group(2)))
    return out


def meta_table(sections: Sequence[Section]) -> dict[str, str]:
    """把「报告元信息」这类 2 列表格折成 dict。"""
    out: dict[str, str] = {}
    for sec in sections:
        if sec.level > 2:
            continue
        for row in table_rows(sec.body):
            if len(row) >= 2:
                out.setdefault(norm(row[0]), norm(row[1]))
    return out


# --------------------------------------------------------------------------- #
# 报告模型
# --------------------------------------------------------------------------- #

@dataclass
class Block:
    """一个长图区块：渲染成 HTML + 参与分P切分的粒度。"""

    kind: str
    html: str


@dataclass
class Doc:
    company: str = ""
    short_name: str = ""
    code: str = ""
    framework: str = "龟龟投资策略"
    kind_label: str = "投资分析"
    kind: str = "analysis"
    meta_line: str = ""
    verdict_word: str = ""
    verdict_tone: str = "neutral"
    verdict_line: str = ""
    verdict_extra: str = ""
    verdict_kv: list[dict[str, str]] = field(default_factory=list)
    kpis: list[dict[str, str]] = field(default_factory=list)
    notes: list[dict[str, str]] = field(default_factory=list)
    dims: list[dict[str, str]] = field(default_factory=list)
    table: dict[str, Any] | None = None
    items: list[dict[str, str]] = field(default_factory=list)
    thesis: dict[str, Any] | None = None
    risks: list[str] = field(default_factory=list)
    gates: list[dict[str, str]] = field(default_factory=list)
    limits: list[str] = field(default_factory=list)
    footer_lines: list[str] = field(default_factory=list)
    footer_tags: list[str] = field(default_factory=list)
    brief_used: bool = False
    sources: list[str] = field(default_factory=list)


# 只在「仓位建议」列取不到动作词时，才拿它去一句话结论里找回退候选
ACTION_WORD_RX = r"建仓|买入|可买|增持|减仓|清仓|持有|观察|等待|排除|回避|推荐|通过|不参与|放弃"

TONE_MAP = [
    (r"不建仓|不可买|排除|减仓|回避|卖出|不建议", "negative"),
    (r"建仓|买入|可买|增持|推荐|通过|成立", "positive"),
    (r"观察|等待|持有|中性|暂缓|不适用", "neutral"),
]


def tone_of(word: str) -> str:
    for rx, tone in TONE_MAP:
        if re.search(rx, word):
            return tone
    return "neutral"


def detect_kind(path: Path, md: str) -> tuple[str, str]:
    """先看文件名，再看正文首段。

    顺序很重要：不少「分析报告」的正文里会出现「定性分析」字样（它引用上游的
    qualitative_report），若先匹配正文关键词，整份报告就会被误判成定性档而掉进
    降级抽取路径。
    """
    name = path.name
    head = md[:4000]
    if "估值报告" in name:
        return "valuation", "估值分析"
    if "_分析报告" in name or "分析报告" in name:
        return "analysis", "投资分析"
    if "qualitative" in name:
        return "qualitative", "定性分析"
    if re.search(r"^#.*估值报告", head, re.M):
        return "valuation", "估值分析"
    if re.search(r"^#.*分析报告", head, re.M):
        return "analysis", "投资分析"
    if re.search(r"^#.*(定性分析|商业模式与护城河)", head, re.M):
        return "qualitative", "定性分析"
    return "analysis", "投资分析"


HERO_RE = [
    re.compile(r"^#\s*([^：:\n]+)[：:]\s*(.+?)\s*[（(]([A-Za-z0-9.]+)[)）]"),
    re.compile(r"^#\s*(.+?)\s*[（(]([A-Za-z0-9.]+)[)）]"),
    re.compile(r"^#\s*(.+)$"),
]


def parse_hero(md: str, doc: Doc) -> None:
    for line in md.splitlines():
        if not line.startswith("# "):
            continue
        raw = line[2:].strip()
        for i, rx in enumerate(HERO_RE):
            m = rx.match("# " + raw)
            if not m:
                continue
            groups = m.groups()
            if len(groups) == 3:
                # 「龟龟投资策略 · 分析报告」→ 只留框架名，档位由 kind_label 承载
                fw = norm(groups[0])
                fw = re.sub(r"\s*[·・]\s*(分析报告|估值报告|定性分析|研究报告)\s*$", "", fw)
                doc.framework = fw.strip(" ·・") or "龟龟投资策略"
                doc.company = norm(groups[1])
                doc.code = groups[2]
            elif len(groups) == 2:
                doc.company = norm(groups[0])
                doc.code = groups[1]
            else:
                title = norm(groups[0])
                m2 = re.search(r"[（(]([A-Za-z0-9.]+)[)）]", title)
                if m2:
                    doc.code = m2.group(1)
                doc.company = re.sub(r"[（(].*?[)）]", "", title).strip(" —-·")
            break
        break


def parse_meta(meta: dict[str, str], doc: Doc, extra: dict[str, str] | None = None) -> None:
    def get(*keys: str) -> str:
        for k in keys:
            for mk, mv in meta.items():
                if k in mk:
                    return mv
        return ""

    date = get("分析日期")
    price = short_value(get("最新股价", "当前价"), 30)
    asof = get("数据截止", "报告期")
    bits = []
    if asof:
        bits.append("数据截止 " + clip(asof, 34))
    if price:
        bits.append("最新股价 " + clip(price, 30))
    if date:
        bits.append("分析日期 " + clip(date, 22))
    doc.meta_line = " ｜ ".join(bits)


def derive_short_name(out_dir: Path, doc: Doc) -> str:
    """output/{code}_{company}/ → 用目录里的短名做文件名主干。"""
    m = re.match(r"^[0-9A-Za-z.]+_(.+)$", out_dir.name)
    if m:
        return m.group(1).strip()
    full = re.sub(r"(股份有限公司|有限公司|国际控股|控股|集团|公司)$", "", doc.company).strip()
    return full or out_dir.name


# --------------------------------------------------------------------------- #
# analysis（龟龟策略分析报告）抽取
# --------------------------------------------------------------------------- #

# 估值推导表只放「卡片之外」的推导参数，避免与结论区重复。
EXEC_TABLE_KEEP = ["Owner Earnings", "悲观档", "目标买入价", "门槛值", "分配比率"]
HYP_KEEP = r"系数 G$|支付率锚定|悲观折扣 k|股息税率 Q|基准值选择"


def build_analysis(md: str, doc: Doc, out_dir: Path, sections: list[Section]) -> None:
    meta = {}
    meta_sec = find_section(sections, r"报告元信息")
    if meta_sec:
        meta = meta_table([meta_sec])
    parse_meta(meta, doc)

    exec_sec = find_section(sections, r"Executive Summary|执行摘要|核心结论")
    exec_rows = table_rows(exec_sec.body) if exec_sec else []
    exec_map: dict[str, list[str]] = {}
    for row in exec_rows:
        if len(row) >= 2:
            exec_map.setdefault(norm(row[0]), row)

    # ---- 一句话结论（同时留一份未归一化的原文，供后面取加粗动作词）----
    verdict_line = ""
    verdict_line_raw = ""
    if exec_sec:
        for p in paragraphs(exec_sec.body):
            if "一句话结论" in p or "核心结论" in p:
                verdict_line_raw = re.sub(
                    r"^\**\s*(一句话结论|核心结论)\**\s*[：:]\s*", "", p).strip()
                verdict_line = norm(verdict_line_raw)
                break
        if not verdict_line:
            for p in paragraphs(exec_sec.body):
                if len(norm(p)) > 30:
                    verdict_line = first_sentence(p, 180)
                    break

    # ---- 仓位建议 / 最终判断 ----
    word = ""
    for key, row in exec_map.items():
        if ("仓位" in key or "建议" in key) and len(row) >= 2:
            word = norm(row[1])
            break
    box = find_section(sections, r"综合结论")
    box_kv = kv_from_box(box.body) if box else {}
    if not word:
        word = box_kv.get("仓位建议", "")
    word = re.sub(r"^按.*?[：:]", "", word).strip()
    word = short_value(word, 16)
    # 「仓位建议」列有时只给百分比（如「70%」），此时从一句话结论的加粗片段里取动作词
    raw_word = word
    if not re.search(ACTION_WORD_RX, word):
        for seg in re.findall(r"\*\*(.+?)\*\*", strip_claims(verdict_line_raw or verdict_line)):
            candidate = short_value(seg, 16)
            if re.search(ACTION_WORD_RX, candidate):
                word = candidate
                break
    word = re.split(r"\s*[/／、；;]\s*", word)[0].strip()
    doc.verdict_word = short_value(word, 12)
    doc.verdict_tone = tone_of(word)
    doc.verdict_line = clip(verdict_line, 220)
    # 原始读数带仓位比例等细节时，作为小注保留，不丢掉信息
    if raw_word and raw_word != word and re.search(r"\d", raw_word):
        doc.verdict_extra = clip(raw_word, 34)

    # ---- 结论 KV：买点 / 现价 / 安全边际 ----
    price = ""
    for row in exec_rows:
        if len(row) >= 2 and re.search(r"最新股价|当前价", norm(row[0])):
            price = short_value(row[1], 30)
    if not price:
        for k, v in meta.items():
            if "股价" in k or "现价" in k:
                price = short_value(v, 30)
    tbp = ""
    for row in exec_rows:
        if len(row) >= 2 and "目标买入价" in norm(row[1]):
            tbp = short_value(row[1], 30)
    if not tbp:
        m = re.search(r"目标买入价[^\d\-]{0,24}([\d,]+(?:\.\d+)?\s*港元)", md)
        if m:
            tbp = m.group(1)
    kk = ""
    for key, row in exec_map.items():
        if "安全边际" in key and len(row) >= 2:
            # 标签已写明是哪一项，取值里再重复一次「KK =」只会把行撑断
            kk = re.sub(r"^[A-Za-z_]{1,8}\s*[=＝]\s*", "", short_value(row[1], 26))
    for label, value, note in (
        ("目标买入价", clip(tbp, 30), "§17.14 悲观档唯一买点"),
        ("最新股价", clip(price, 30), "高于买点即不构成买点"),
        ("安全边际 KK", clip(kk, 26), "GG_pess − II"),
    ):
        if value:
            doc.verdict_kv.append({"label": label, "value": value, "note": note})
    doc.verdict_kv = doc.verdict_kv[:3]

    # ---- KPI 卡片 ----
    # 决策口径是「悲观档穿透回报率 GG_pess」，它通常只出现在「安全边际」行的说明列，
    # 所以先专门把它挖出来 —— 不能误用粗算 R 或中性档 GG 当结论。
    def exec_cell(*key_rx: str, col: int = 1) -> str:
        for key, row in exec_map.items():
            if len(row) > col and any(re.search(rx, key) for rx in key_rx):
                return strip_claims(row[col])
        return ""

    def exec_note(*key_rx: str) -> str:
        return exec_cell(*key_rx, col=2)

    def find_in_exec(*pats: str) -> str:
        blob = " ".join(strip_claims(" ".join(r)) for r in exec_rows)
        for pat in pats:
            m = re.search(pat, blob)
            if m:
                return m.group(1)
        return ""

    def num_only(text: str) -> str:
        return re.sub(r"^[A-Za-z_]{1,8}\s*[=＝]\s*", "", text).strip()

    gg_pess = find_in_exec(r"GG_?pess\s*[=＝]?\s*([−\-+]?[\d.]+%)",
                           r"悲观[^。；]{0,14}?([\d.]+%)")
    gg_mid = find_in_exec(r"精算穿透回报率\s*GG\s*[=＝]?\s*([−\-+]?[\d.]+%)",
                          r"(?<!pess\s)GG\s*[=＝]\s*([−\-+]?[\d.]+%)")
    ii = num_only(short_value(exec_cell(r"门槛值|门槛"), 18))
    kk = num_only(short_value(exec_cell(r"安全边际"), 18))
    pitfall = short_value(exec_cell(r"价值陷阱"), 18)
    conf = short_value(exec_cell(r"外推可信度"), 18)

    kpi_specs: list[tuple[str, str, str, str]] = [
        ("穿透回报率 · 悲观档（决策口径）", gg_pess,
         ("门槛 II = " + ii + "；低于门槛即不满足最低回报要求") if ii
         else "决策口径：低于门槛即不满足最低回报要求", "negative"),
        ("穿透回报率 · 中性档（对照）", gg_mid, "仅作对照，不参与买点判定", "neutral"),
        ("门槛值 II", ii, "max(5%, 无风险利率 + 3%)", "neutral"),
        ("安全边际 KK", kk, "决策口径 GG_pess − II", "negative"),
        ("价值陷阱风险", pitfall, exec_note(r"价值陷阱"), "amber"),
        ("外推可信度", conf, exec_note(r"外推可信度"), "amber"),
    ]
    for label, value, note, tone in kpi_specs:
        if not value:
            continue
        if label.startswith("安全边际") and not re.search(r"[-−]", value):
            tone = "positive"
        if label.startswith("价值陷阱"):
            tone = "amber" if re.search(r"高|是|存在", value) else "green"
        doc.kpis.append({
            "label": label,
            "value": clip(value, 22),
            "note": clip(note, 84),
            "tone": tone,
        })

    # ---- 最大优势 / 最大风险 ----
    adv = risk = ""
    if exec_sec:
        for p in paragraphs(exec_sec.body):
            t = norm(p)
            if t.startswith("最大优势"):
                adv = re.sub(r"^最大优势\**\s*[：:]\s*", "", t)
            elif t.startswith("最大风险"):
                risk = re.sub(r"^最大风险\**\s*[：:]\s*", "", t)
    if not adv:
        adv = box_kv.get("最大优势", "")
    if not risk:
        risk = box_kv.get("最大风险", "")
    if adv:
        doc.notes.append({"label": "最大优势", "text": clip(adv, 190), "tone": "green"})
    if risk:
        doc.notes.append({"label": "最大风险", "text": clip(risk, 190), "tone": "clay"})

    # ---- 六维速览（优先 dims/*.json，回退到报告正文的维度小节）----
    doc.dims = collect_dims(out_dir, sections)

    # ---- 估值推导表（与 KPI 卡片刻意不重叠）----
    rows: list[list[str]] = []
    for key, row in exec_map.items():
        if any(k in key for k in EXEC_TABLE_KEEP) and len(row) >= 2:
            rows.append([short_value(row[0], 26), short_value(row[1], 52)])
    hyp = find_section(sections, r"关键假设|假设汇总")
    if hyp:
        for row in table_rows(hyp.body):
            if len(row) >= 3 and re.search(HYP_KEEP, norm(row[1])):
                rows.append([short_value(row[1], 26), short_value(row[2], 52)])
    seen: set[str] = set()
    dedup: list[list[str]] = []
    for r in rows:
        if not r[0] or not r[1] or r[0] in seen:
            continue
        seen.add(r[0])
        dedup.append(r)
    doc.table = {"columns": ["参数 / 指标", "取值"], "rows": dedup[:9]} if dedup else None

    # ---- 投资论点卡 ----
    thesis_sec = find_section(sections, r"投资论点卡", r"Thesis Card")
    core = ""
    bullets: list[str] = []
    if thesis_sec:
        for p in paragraphs(thesis_sec.body):
            t = norm(p)
            if t.startswith("核心论点"):
                core = re.sub(r"^核心论点\**\s*[：:]\s*", "", t)
                break
        for p in paragraphs(thesis_sec.body):
            if "多头逻辑" in p:
                bullets = list_items(p, limit=6)
                break
        if not bullets:
            bullets = list_items(thesis_sec.body, limit=4)
    if core or bullets:
        doc.thesis = {"core": clip(core, 320), "bullets": [clip(b, 170) for b in bullets[:4]]}

    # ---- 风险提示 ----
    risk_sec = find_section(sections, r"风险提示|风险与不确定性|不确定性清单")
    if risk_sec:
        sub = [s for s in split_sections(risk_sec.body, 3) if s.level == 3]
        pool: list[str] = []
        for s in sub:
            pool.extend(list_items(s.body))
        if not pool:
            pool = list_items(risk_sec.body)
        doc.risks = [clip(x, 160) for x in pool[:5]]

    # ---- 局限：口径缺口与 Warnings ----
    gap_sec = find_section(sections, r"口径缺口与限定|口径缺口")
    if gap_sec:
        for row in table_rows(gap_sec.body):
            if len(row) >= 2 and row[0].strip():
                doc.limits.append(clip(row[1], 150))
    doc.limits = [x for x in doc.limits if x][:4]
    doc.gates = collect_gates(out_dir, md)
    doc.footer_lines = collect_footer(sections, out_dir)
    doc.footer_tags = collect_tags(out_dir)


def collect_dims(out_dir: Path, sections: list[Section]) -> list[dict[str, str]]:
    dims: list[dict[str, str]] = []
    dim_dir = out_dir / "dims"
    if dim_dir.is_dir():
        for did in DIM_ORDER:
            p = dim_dir / f"{did}.json"
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            verdict = data.get("verdict") or {}
            headline = verdict.get("headline") or ""
            if not headline:
                headline = first_sentence(data.get("narrative") or "", 150)
            dims.append({
                "id": did,
                "name": DIM_NAMES.get(did, did),
                "headline": clip(headline, 150),
                "confidence": norm(str(verdict.get("confidence") or "")),
                "status": norm(str(verdict.get("material_status") or "")),
            })
    if dims:
        return dims
    for s in find_sections(sections, r"维度[一二三四五六]", r"^D[1-6][\s:：]"):
        m = re.search(r"^\s*#*\s*(维度[一二三四五六]|D[1-6])[：:、\s]*(.*)$", s.title)
        name = norm(m.group(2)) if m else norm(s.title)
        para = next((p for p in paragraphs(s.body) if len(norm(p)) > 40), "")
        dims.append({
            "id": "D%d" % (len(dims) + 1),
            "name": name or "维度",
            "headline": first_sentence(para, 150),
            "confidence": "",
            "status": "",
        })
    return dims[:6]


def collect_gates(out_dir: Path, md: str) -> list[dict[str, str]]:
    """从 sidecar 文件机械汇总可信度门禁，不做任何推断。"""
    gates: list[dict[str, str]] = []

    inv = out_dir / "invariants.json"
    if inv.is_file():
        try:
            findings = json.loads(inv.read_text(encoding="utf-8")).get("findings") or []
        except (OSError, ValueError):
            findings = []
        if findings:
            bad = [f for f in findings if str(f.get("status", "")).upper() not in ("PASS", "OK")]
            codes = "、".join(str(f.get("code")) for f in bad[:5])
            gates.append({
                "label": "机构不变量",
                "value": "%d / %d PASS" % (len(findings) - len(bad), len(findings)),
                "note": ("未通过：" + codes) if bad else "全部通过；FAIL 项的数字在正文均已被限定",
                "tone": "amber" if bad else "green",
            })

    s17 = out_dir / "section17_status.json"
    if s17.is_file():
        try:
            secs = (json.loads(s17.read_text(encoding="utf-8")).get("sections") or {})
        except (OSError, ValueError):
            secs = {}
        if secs:
            ok = sum(1 for v in secs.values() if v.get("status") == "ok")
            miss = [k for k, v in secs.items() if v.get("status") != "ok"]
            gates.append({
                "label": "§17 小节状态",
                "value": "%d / %d ok" % (ok, len(secs)),
                "note": ("fail-closed 记录：" + "、".join("§" + m for m in sorted(miss)[:5])) if miss
                        else "全部小节已计算并登记 claim",
                "tone": "amber" if miss else "green",
            })

    claims = out_dir / "claims.json"
    if claims.is_file():
        try:
            data = json.loads(claims.read_text(encoding="utf-8"))
            n = len(data.get("claims") or data) if isinstance(data, (dict, list)) else 0
        except (OSError, ValueError):
            n = 0
        if n:
            gates.append({
                "label": "claim 账本",
                "value": "%d 条已登记" % n,
                "note": "报告中引用的每个数值都必须绑定 claim id 才能落笔",
                "tone": "green",
            })

    dim_dir = out_dir / "dims"
    if dim_dir.is_dir():
        n = len(sorted(dim_dir.glob("D*.json")))
        if n:
            gates.append({
                "label": "维度契约",
                "value": "%d / 6 份" % n,
                "note": "validate_dim_result.py 硬校验：越界引用即失败重跑",
                "tone": "green" if n == 6 else "amber",
            })

    cons = out_dir / "consistency_report.md"
    if cons.is_file():
        try:
            text = cons.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text:
            failed = bool(re.search(r"❌|FAIL|不通过", text))
            hits = re.search(r"发现\s*\**(\d+)\**\s*处潜在冲突", text)
            gates.append({
                "label": "报告级门禁",
                "value": "FAIL" if failed else "PASS",
                "note": ("发现 %s 处跨段数值冲突（advisory）" % hits.group(1)) if hits
                        else "叙述保真 + 规则序 + 数值绑定均通过",
                "tone": "amber" if (failed or hits) else "green",
            })

    if not gates and "硬门槛" in md:
        gates.append({"label": "报告级门禁", "value": "见正文", "note": "", "tone": "neutral"})
    return gates


def collect_footer(sections: list[Section], out_dir: Path) -> list[str]:
    lines: list[str] = []
    src = find_section(sections, r"数据来源|数据来源与免责")
    if src:
        rows = table_rows(src.body)
        tools = []
        for row in rows:
            if len(row) >= 3 and row[2].strip() and row[2].strip() not in tools:
                tools.append(norm(row[2]))
        if tools:
            lines.append("数据来源：" + "、".join(tools[:6]))
    lines.append("所有数字由程序计算或带 [src:] 溯源；语言模型只做定性判断与参数选择，不做算术。")
    if (out_dir / "claims.json").is_file():
        lines.append("关键引用值均绑定 claim id，可机械复现。")
    return lines


def collect_tags(out_dir: Path) -> list[str]:
    tags = ["Tushare Pro", "年报溯源", "claim 绑定", "fail-closed 门禁"]
    if (out_dir / "cleanroom_metrics.md").is_file():
        tags.append("clean-room 复核")
    if (out_dir / "invariants.json").is_file():
        tags.append("机构不变量")
    return tags[:6]


# --------------------------------------------------------------------------- #
# 通用（估值 / 定性）抽取 —— 按标题别名降级
# --------------------------------------------------------------------------- #

def build_generic(md: str, doc: Doc, out_dir: Path, sections: list[Section]) -> None:
    meta = {}
    meta_sec = find_section(sections, r"元信息", r"执行摘要", r"摘要")
    if meta_sec:
        meta = meta_table([meta_sec])
    parse_meta(meta, doc)

    summary = find_section(sections, r"执行摘要", r"Executive Summary", r"核心结论", r"结论强度")
    line = ""
    if summary:
        for p in paragraphs(summary.body):
            if len(norm(p)) > 40:
                line = first_sentence(p, 200)
                break
    doc.verdict_line = line
    word = ""
    for key, val in meta.items():
        if "仓位" in key or "结论" in key or "评级" in key:
            word = val
    if summary:
        for row in table_rows(summary.body):
            if len(row) >= 2 and re.search(r"仓位|建议|结论|评级", norm(row[0])):
                word = norm(row[-1])
    doc.verdict_word = clip(word, 14)
    doc.verdict_tone = tone_of(doc.verdict_word) if doc.verdict_word else "neutral"

    if summary:
        for row in table_rows(summary.body)[:6]:
            if len(row) >= 2:
                doc.kpis.append({
                    "label": clip(row[0], 26),
                    "value": clip(row[1], 42),
                    "note": clip(row[2], 90) if len(row) > 2 else "",
                    "tone": tone_of(row[1]),
                })
    doc.kpis = doc.kpis[:4]

    doc.dims = collect_dims(out_dir, sections)

    tbl = find_section(sections, r"估值|参数|关键假设|假设汇总")
    if tbl:
        rows = [[clip(norm(r[0]), 34), clip(norm(r[1]), 72)] for r in table_rows(tbl.body) if len(r) >= 2]
        doc.table = {"columns": ["参数 / 指标", "取值"], "rows": rows[:8]} if rows else None

    thesis = find_section(sections, r"投资逻辑", r"核心逻辑", r"论点")
    if thesis:
        core = next((norm(p) for p in paragraphs(thesis.body) if len(norm(p)) > 40), "")
        bullets = list_items(thesis.body, limit=4)
        if core or bullets:
            doc.thesis = {"core": clip(core, 320), "bullets": [clip(b, 170) for b in bullets]}

    risk_sec = find_section(sections, r"风险", r"不确定性", r"Warnings")
    if risk_sec:
        pool: list[str] = []
        for s in split_sections(risk_sec.body, 3):
            if s.level == 3:
                pool.extend(list_items(s.body))
        if not pool:
            pool = list_items(risk_sec.body)
        if not pool:
            pool = [first_sentence(p, 150) for p in paragraphs(risk_sec.body)]
        doc.risks = [clip(x, 160) for x in pool[:5]]

    doc.gates = collect_gates(out_dir, md)
    doc.footer_lines = collect_footer(sections, out_dir)
    doc.footer_tags = collect_tags(out_dir)


# --------------------------------------------------------------------------- #
# brief.json 覆盖
# --------------------------------------------------------------------------- #

def apply_brief(doc: Doc, brief: dict[str, Any]) -> list[str]:
    """返回被 brief 覆盖的字段名，便于在结果里如实报告。"""
    touched: list[str] = []
    for key, attr in (
        ("framework", "framework"), ("title", "company"), ("code", "code"),
        ("subtitle_kind", "kind_label"), ("meta", "meta_line"),
        ("verdict_word", "verdict_word"), ("verdict_line", "verdict_line"),
    ):
        if brief.get(key):
            setattr(doc, attr, str(brief[key]))
            touched.append(key)
    if brief.get("verdict_word"):
        doc.verdict_tone = str(brief.get("verdict_tone") or tone_of(doc.verdict_word))
    if brief.get("verdict_kv"):
        doc.verdict_kv = [
            {"label": str(x.get("label", "")), "value": str(x.get("value", "")),
             "note": str(x.get("note", ""))}
            for x in brief["verdict_kv"]
        ][:3]
        touched.append("verdict_kv")
    if brief.get("kpis"):
        doc.kpis = [
            {"label": str(x.get("label", "")), "value": str(x.get("value", "")),
             "note": str(x.get("note", "")), "tone": str(x.get("tone") or "neutral")}
            for x in brief["kpis"]
        ][:4]
        touched.append("kpis")
    if brief.get("notes"):
        doc.notes = [
            {"label": str(x.get("label", "")), "text": str(x.get("text", "")),
             "tone": str(x.get("tone") or "neutral")}
            for x in brief["notes"]
        ][:4]
        touched.append("notes")
    if brief.get("dimensions"):
        doc.dims = [
            {"id": str(x.get("id", "")), "name": str(x.get("name", "")),
             "headline": str(x.get("headline", "")),
             "confidence": str(x.get("confidence", "")), "status": str(x.get("status", ""))}
            for x in brief["dimensions"]
        ][:6]
        touched.append("dimensions")
    if brief.get("table"):
        t = brief["table"]
        doc.table = {
            "columns": [str(c) for c in (t.get("columns") or ["参数 / 指标", "取值"])][:3],
            "rows": [[str(c) for c in row] for row in (t.get("rows") or [])][:12],
        }
        touched.append("table")
    if brief.get("thesis"):
        t = brief["thesis"]
        doc.thesis = {
            "core": str(t.get("core", "")),
            "bullets": [str(b) for b in (t.get("bullets") or [])][:6],
        }
        touched.append("thesis")
    if brief.get("risks"):
        doc.risks = [str(x) for x in brief["risks"]][:6]
        touched.append("risks")
    if brief.get("limits"):
        doc.limits = [str(x) for x in brief["limits"]][:5]
        touched.append("limits")
    if brief.get("gates"):
        doc.gates = [
            {"label": str(x.get("label", "")), "value": str(x.get("value", "")),
             "note": str(x.get("note", "")), "tone": str(x.get("tone") or "neutral")}
            for x in brief["gates"]
        ]
        touched.append("gates")
    footer = brief.get("footer") or {}
    if footer.get("lines"):
        doc.footer_lines = [str(x) for x in footer["lines"]]
        touched.append("footer.lines")
    if footer.get("tags"):
        doc.footer_tags = [str(x) for x in footer["tags"]]
        touched.append("footer.tags")
    return touched


# --------------------------------------------------------------------------- #
# HTML 渲染
# --------------------------------------------------------------------------- #

def render_css(theme: str) -> str:
    """B 站长图刻度：1080 宽下正文 30px，手机上约合 11–12pt。"""
    t = THEMES.get(theme) or THEMES["paper"]
    return f"""
:root{{
  --paper:{t['paper']}; --card:{t['card']}; --ink:{t['ink']}; --ink2:{t['ink2']};
  --muted:{t['muted']}; --hair:{t['hair']}; --clay:{t['clay']}; --clay-bg:{t['clay_bg']};
  --green:{t['green']}; --amber:{t['amber']}; --shadow:{t['shadow']};
}}
*{{box-sizing:border-box}}
html,body{{margin:0;padding:0;background:var(--paper);overflow-x:hidden}}
body{{
  font-family:{SANS_STACK};
  color:var(--ink2); font-size:30px; line-height:1.85;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
  letter-spacing:.012em; overflow-wrap:break-word;
}}
.serif{{font-family:{SERIF_STACK};letter-spacing:-.005em}}
.num{{font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}}
.wrap{{width:100%;padding:0 72px 96px}}

/* ── hero ───────────────────────────────────────────────── */
.hero{{padding:92px 0 48px;border-bottom:2px solid var(--ink)}}
.eyebrow{{font-size:22px;letter-spacing:.2em;color:var(--clay);font-weight:600}}
.hero h1{{font-family:{SERIF_STACK};font-size:76px;line-height:1.16;margin:26px 0 14px;
  color:var(--ink);font-weight:600}}
.hero .sub{{font-size:34px;color:var(--ink2);letter-spacing:.02em}}
.hero .meta{{margin-top:26px;font-size:24px;color:var(--muted);line-height:1.7}}

/* ── section ────────────────────────────────────────────── */
.sec{{margin-top:88px}}
.sec-head{{display:flex;align-items:baseline;gap:18px;padding-bottom:18px;
  border-bottom:1px solid var(--hair);margin-bottom:44px}}
.sec-idx{{font-size:22px;letter-spacing:.16em;color:var(--clay);font-weight:700;
  font-variant-numeric:tabular-nums}}
.sec-head h2{{font-family:{SERIF_STACK};font-size:46px;margin:0;color:var(--ink);
  font-weight:600;line-height:1.3}}
.sec-note{{margin-left:auto;font-size:22px;color:var(--muted);text-align:right;
  max-width:44%;line-height:1.6}}

/* ── verdict ────────────────────────────────────────────── */
.word{{font-family:{SERIF_STACK};font-size:118px;line-height:1.04;font-weight:600;
  margin:6px 0 20px;letter-spacing:-.01em}}
.t-negative{{color:var(--clay)}} .t-positive{{color:var(--green)}}
.t-neutral{{color:var(--ink)}} .t-amber{{color:var(--amber)}}
.lede{{font-size:31px;line-height:1.9;color:var(--ink2);max-width:92%;
  text-align:justify;text-justify:inter-ideograph}}
.kv{{display:grid;grid-template-columns:repeat(3,1fr);margin-top:48px;border-top:1px solid var(--hair)}}
.kv > div{{padding:30px 26px 28px 0}}
.kv > div + div{{border-left:1px solid var(--hair);padding-left:30px}}
.kv .k{{font-size:22px;color:var(--muted);letter-spacing:.06em}}
.kv .v{{font-size:44px;color:var(--ink);font-weight:600;margin-top:12px;line-height:1.3}}
.kv .d{{font-size:22px;color:var(--muted);margin-top:10px;line-height:1.6}}

/* ── cards ──────────────────────────────────────────────── */
.cards{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}
.card{{background:var(--card);border:1px solid var(--hair);border-radius:18px;
  padding:34px 34px 30px;box-shadow:var(--shadow)}}
.card .k{{font-size:23px;color:var(--muted)}}
.card .v{{font-size:52px;font-weight:600;margin:14px 0 10px;line-height:1.25;
  color:var(--ink)}}
.card .d{{font-size:24px;color:var(--ink2);line-height:1.68}}

/* ── notes ──────────────────────────────────────────────── */
.note{{background:var(--card);border:1px solid var(--hair);border-left:5px solid var(--clay);
  border-radius:0 16px 16px 0;padding:30px 34px;margin-bottom:22px;box-shadow:var(--shadow)}}
.note.g{{border-left-color:var(--green)}}
.note .k{{font-size:22px;letter-spacing:.1em;color:var(--muted);margin-bottom:12px}}
.note .t{{font-size:28px;line-height:1.84;color:var(--ink2);
  text-align:justify;text-justify:inter-ideograph}}

/* ── dims ───────────────────────────────────────────────── */
.dim{{display:flex;gap:22px;padding:28px 0;border-bottom:1px solid var(--hair)}}
.dim:last-child{{border-bottom:0}}
.dim .tag{{flex:0 0 auto;min-width:74px;font-size:22px;font-weight:700;color:var(--clay);
  letter-spacing:.06em;padding-top:6px;font-variant-numeric:tabular-nums}}
.dim .body{{flex:1}}
.dim .h{{font-size:26px;color:var(--ink);font-weight:600;margin-bottom:10px}}
.dim .p{{font-size:27px;line-height:1.78;color:var(--ink2);
  text-align:justify;text-justify:inter-ideograph}}
.chip{{display:inline-block;font-size:20px;padding:4px 14px;border-radius:999px;
  border:1px solid var(--hair);color:var(--muted);margin-left:10px;vertical-align:2px}}
.chip.warn{{color:var(--amber);border-color:var(--amber)}}
.chip.bad{{color:var(--clay);border-color:var(--clay)}}

/* ── table ──────────────────────────────────────────────── */
table{{width:100%;border-collapse:collapse}}
th{{font-size:22px;letter-spacing:.12em;color:var(--muted);font-weight:500;text-align:left;
  padding:0 20px 16px 0;border-bottom:1px solid var(--hair)}}
td{{font-size:27px;color:var(--ink2);padding:22px 20px 22px 0;
  border-bottom:1px solid var(--hair);vertical-align:top;line-height:1.62}}
td:first-child{{color:var(--ink);font-weight:600}}
td.num{{color:var(--ink);font-variant-numeric:tabular-nums}}
th:last-child,td:last-child{{padding-right:0}}

/* ── items / bullets ────────────────────────────────────── */
.item{{display:flex;gap:20px;padding:24px 0;border-bottom:1px solid var(--hair)}}
.item:last-child{{border-bottom:0}}
.item .idx{{flex:0 0 46px;font-size:22px;font-weight:700;color:var(--clay);padding-top:6px;
  font-variant-numeric:tabular-nums;letter-spacing:.04em}}
.item .txt{{flex:1;font-size:27px;line-height:1.8;color:var(--ink2);
  text-align:justify;text-justify:inter-ideograph}}
.bullet{{display:flex;gap:18px;padding:16px 0}}
.bullet .dot{{flex:0 0 10px;height:10px;border-radius:50%;background:var(--clay);margin-top:20px}}
.bullet .txt{{flex:1;font-size:27px;line-height:1.8;color:var(--ink2);
  text-align:justify;text-justify:inter-ideograph}}
.core{{font-size:30px;line-height:1.88;color:var(--ink);background:var(--clay-bg);
  border-radius:16px;padding:32px 34px;margin-bottom:26px;
  text-align:justify;text-justify:inter-ideograph}}

/* ── footer ─────────────────────────────────────────────── */
.foot{{margin-top:96px;padding-top:32px;border-top:1px solid var(--hair);
  font-size:23px;color:var(--muted);line-height:2.0}}
.foot .tag{{display:inline-block;font-size:21px;color:var(--muted);border:1px solid var(--hair);
  border-radius:999px;padding:7px 16px;margin:0 10px 10px 0;background:var(--card)}}
.foot .disc{{margin-top:22px;font-size:22px;line-height:1.95}}
b,strong{{color:var(--ink);font-weight:600}}
code{{font-family:"SF Mono",Menlo,Consolas,monospace;font-size:.9em;
  background:var(--clay-bg);padding:2px 8px;border-radius:6px}}

/* ── panel chrome ───────────────────────────────────────── */
#clip{{overflow:hidden;position:relative}}
#panelbar{{position:fixed;left:0;right:0;bottom:0;height:72px;background:var(--paper);
  border-top:1px solid var(--hair);display:flex;align-items:center;
  padding:0 72px;font-size:22px;color:var(--muted);letter-spacing:.04em}}
#panelbar .right{{margin-left:auto;color:var(--clay);font-weight:600}}
"""


def block_hero(doc: Doc) -> str:
    code = f" {esc(doc.code)}" if doc.code else ""
    return f"""<div class="hero">
  <div class="eyebrow">{esc(doc.framework)} · {esc(doc.kind_label)}</div>
  <h1 class="serif">{esc(doc.company or '未命名标的')}{code}</h1>
  <div class="sub">精华版长图 · 可直接分享</div>
  {f'<div class="meta">{inline(doc.meta_line)}</div>' if doc.meta_line else ''}
</div>"""


def block_verdict(doc: Doc, idx) -> str | None:
    if not doc.verdict_word and not doc.verdict_line and not doc.verdict_kv:
        return None
    kv = ""
    if doc.verdict_kv:
        cells = "".join(
            f'<div><div class="k">{esc(x["label"])}</div>'
            f'<div class="v num">{inline(x["value"])}</div>'
            f'<div class="d">{inline(x["note"])}</div></div>'
            for x in doc.verdict_kv
        )
        kv = f'<div class="kv" style="grid-template-columns:repeat({len(doc.verdict_kv)},1fr)">{cells}</div>'
    lede = f'<div class="lede">{inline(doc.verdict_line)}</div>' if doc.verdict_line else ""
    # 没有可显示的结论词时（定性档常见）就只给核心判断正文，不留一个空的「—」大字
    word_html = (f'<div class="word serif t-{esc(doc.verdict_tone)}">'
                 f'{esc(doc.verdict_word)}</div>') if doc.verdict_word else ""
    heading = "结论" if doc.verdict_word else "核心判断"
    return section(idx(), heading, verdict_right(doc), f"{word_html}{lede}{kv}")


def verdict_right(doc: Doc) -> str:
    if doc.verdict_extra:
        return doc.verdict_extra
    if doc.kind == "analysis":
        return "按 §17.14 悲观档口径判定的唯一买点"
    return ""


def block_kpis(doc: Doc) -> str | None:
    if not doc.kpis:
        return None
    cards = "".join(
        f'<div class="card"><div class="k">{esc(k["label"])}</div>'
        f'<div class="v num t-{esc(k.get("tone") or "neutral")}">{inline(k["value"])}</div>'
        f'<div class="d">{inline(k["note"])}</div></div>'
        for k in doc.kpis
    )
    return f'<div class="sec"><div class="cards">{cards}</div></div>'


def block_notes(doc: Doc, idx) -> str | None:
    if not doc.notes:
        return None
    body = "".join(
        f'<div class="note{" g" if n.get("tone") == "green" else ""}">'
        f'<div class="k">{esc(n["label"])}</div>'
        f'<div class="t">{inline(n["text"])}</div></div>'
        for n in doc.notes
    )
    return section(idx(), "一句话逻辑", "最大优势与最大风险", body)


def block_dims(doc: Doc, idx) -> str | None:
    if not doc.dims:
        return None
    rows = []
    for d in doc.dims:
        chips = ""
        if d.get("confidence"):
            tone = "" if d["confidence"] in ("高", "中") else " warn"
            chips += f'<span class="chip{tone}">置信度 {esc(d["confidence"])}</span>'
        if d.get("status"):
            tone = "" if d["status"] == "充分" else (" bad" if d["status"] == "不足" else " warn")
            chips += f'<span class="chip{tone}">材料 {esc(d["status"])}</span>'
        rows.append(
            f'<div class="dim"><div class="tag">{esc(d["id"])}</div><div class="body">'
            f'<div class="h">{esc(d["name"])}{chips}</div>'
            f'<div class="p">{inline(d["headline"])}</div></div></div>'
        )
    return section(idx(), "生意面速览", "六维定性结论（逐维一句话，取自 dims/*.json 原文）",
                   "".join(rows))


def block_table(doc: Doc, idx) -> str | None:
    if not doc.table or not doc.table.get("rows"):
        return None
    cols = doc.table["columns"]
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = ""
    for row in doc.table["rows"]:
        cells = "".join(
            f'<td class="{"num" if i else ""}">{inline(c)}</td>'
            for i, c in enumerate(row)
        )
        body += f"<tr>{cells}</tr>"
    return section(idx(), "估值推导", "买点是怎么算出来的 · 全部由程序计算",
                   f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>')


def block_thesis(doc: Doc, idx) -> str | None:
    if not doc.thesis:
        return None
    core = f'<div class="core">{inline(doc.thesis["core"])}</div>' if doc.thesis.get("core") else ""
    bullets = "".join(
        f'<div class="bullet"><div class="dot"></div><div class="txt">{inline(b)}</div></div>'
        for b in doc.thesis.get("bullets") or []
    )
    return section(idx(), "投资论点", "核心论点与多头逻辑", core + bullets)


def block_risks(doc: Doc, idx) -> str | None:
    if not doc.risks:
        return None
    items = "".join(
        f'<div class="item"><div class="idx">{i:02d}</div><div class="txt">{inline(t)}</div></div>'
        for i, t in enumerate(doc.risks, 1)
    )
    return section(idx(), "风险与待验证", "报告已逐条登记，不作淡化", items)


def block_gates(doc: Doc, idx) -> str | None:
    if not doc.gates:
        return None
    body = ""
    for g in doc.gates:
        tone = {"green": "t-positive", "amber": "t-amber", "negative": "t-negative"}.get(
            g.get("tone") or "", "")
        body += (f'<tr><td>{esc(g["label"])}</td>'
                 f'<td class="num {tone}">{inline(g["value"])}</td>'
                 f'<td>{inline(g["note"])}</td></tr>')
    return section(idx(), "可信度", "不是「看着像」，而是有门禁",
                   f'<table><thead><tr><th style="width:26%">门禁</th>'
                   f'<th style="width:26%">结果</th><th>含义</th></tr></thead>'
                   f'<tbody>{body}</tbody></table>')


def block_limits(doc: Doc, idx) -> str | None:
    if not doc.limits:
        return None
    items = "".join(
        f'<div class="note"><div class="t">{inline(t)}</div></div>' for t in doc.limits
    )
    return section(idx(), "必须说清的局限", "口径缺口随文披露", items)


def block_footer(doc: Doc) -> str:
    tags = "".join(f'<span class="tag">{esc(t)}</span>' for t in doc.footer_tags)
    lines = "<br>".join(inline(l) for l in doc.footer_lines)
    disc = ("本图为程序自动渲染，内容取自本目录已审计的成果文件，未新增任何计算或判断。<br>"
            "本文为分析记录，非投资建议，不构成任何买卖要约。投资有风险，决策请自行判断。")
    return f"""<div class="foot">{tags}
  <div class="disc">{lines}{'<br>' if lines else ''}{disc}</div>
</div>"""


def section(idx: int, title: str, note: str, body: str) -> str:
    note_html = f'<span class="sec-note">{esc(note)}</span>' if note else ""
    return (f'<div class="sec"><div class="sec-head">'
            f'<span class="sec-idx">{idx:02d}</span>'
            f'<h2>{esc(title)}</h2>{note_html}</div>{body}</div>')


def build_blocks(doc: Doc) -> list[Block]:
    counter = [0]

    def idx() -> int:
        counter[0] += 1
        return counter[0]

    candidates: list[tuple[str, str | None]] = [
        ("hero", block_hero(doc)),
        ("verdict", block_verdict(doc, idx)),
        ("kpis", block_kpis(doc)),
        ("notes", block_notes(doc, idx)),
        ("dims", block_dims(doc, idx)),
        ("table", block_table(doc, idx)),
        ("thesis", block_thesis(doc, idx)),
        ("risks", block_risks(doc, idx)),
        ("gates", block_gates(doc, idx)),
        ("limits", block_limits(doc, idx)),
        ("footer", block_footer(doc)),
    ]
    return [Block(k, h) for k, h in candidates if h]


MEASURE_JS = """
<script>
(function(){
  function measure(){
    var stage = document.getElementById('stage');
    if(!stage) return;
    var base = stage.getBoundingClientRect().top + window.scrollY;
    var kids = stage.children, out = [], i, r;
    for(i=0;i<kids.length;i++){
      r = kids[i].getBoundingClientRect();
      out.push({i:i, t: Math.round(r.top + window.scrollY - base), h: Math.round(r.height)});
    }
    var total = Math.ceil(Math.max(document.documentElement.scrollHeight, base + stage.offsetHeight));
    document.documentElement.setAttribute('data-h', String(total));
    document.documentElement.setAttribute('data-blocks', JSON.stringify(out));
  }
  function arm(){ setTimeout(measure, 60); }
  if (document.fonts && document.fonts.ready) { document.fonts.ready.then(arm); }
  window.addEventListener('load', arm);
  setTimeout(measure, 400);
})();
</script>
"""


def build_html(doc: Doc, blocks: Sequence[Block], theme: str, width: int,
               clip: tuple[int, int] | None = None,
               panel_label: str = "") -> str:
    """clip=(offset, height) 时输出分P用的裁剪版。"""
    stage = "".join(b.html for b in blocks)
    css = render_css(theme)
    extra = ""
    bar = ""
    if clip is not None:
        offset, height = clip
        extra = f"#stage{{margin-top:-{offset}px}}#clip{{height:{height}px}}"
        if panel_label:
            bar = f'<div id="panelbar"><span>{esc(panel_label)}</span></div>'
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width={width}, initial-scale=1">
<title>{esc(doc.company)} · 精华长图</title>
<style>{css}
html{{width:{width}px}}body{{width:{width}px}}
.wrap{{width:{width}px}}
{extra}
</style></head><body>
<div id="clip"><div class="wrap"><div id="stage">{stage}</div></div></div>
{bar}
{MEASURE_JS}
</body></html>"""


# --------------------------------------------------------------------------- #
# Chrome 驱动（零图像库：量高度 → 整图 → 逐片重渲染）
# --------------------------------------------------------------------------- #

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
]


def find_chrome(explicit: str | None = None) -> str | None:
    for cand in ([explicit] if explicit else []) + [
        os.environ.get("DSH_SHARE_CHROME"), os.environ.get("CHROME_PATH")
    ] + CHROME_CANDIDATES:
        if not cand:
            continue
        if os.path.isabs(cand):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


def chrome_run(chrome: str, args: Sequence[str], profile: str, timeout: int = 180) -> bytes:
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--disable-lcd-text",
        "--font-render-hinting=none",
        "--force-device-scale-factor=1",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        *args,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return proc.stdout or b""


def measure_page(chrome: str, html_path: Path, work: Path, width: int) -> tuple[int, list[dict[str, int]]]:
    out = chrome_run(chrome, [
        "--virtual-time-budget=8000",
        f"--window-size={width},1200",
        "--dump-dom",
        html_path.resolve().as_uri(),
    ], str(work / "profile-measure"))
    text = out.decode("utf-8", "replace")
    m = re.search(r'data-h="(\d+)"', text)
    n = re.search(r"""data-blocks=(?:"([^"]*)"|'([^']*)')""", text)
    height = int(m.group(1)) if m else 0
    blocks: list[dict[str, int]] = []
    if n:
        raw = n.group(1) if n.group(1) is not None else n.group(2)
        try:
            parsed = json.loads(_html.unescape(raw))
            if isinstance(parsed, list):
                blocks = [b for b in parsed if isinstance(b, dict) and "t" in b and "h" in b]
        except ValueError:
            blocks = []
    return height, blocks


def png_size(path: Path) -> tuple[int, int]:
    try:
        with path.open("rb") as fh:
            head = fh.read(33)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return (0, 0)
        return struct.unpack(">II", head[16:24])
    except OSError:
        return (0, 0)


def screenshot(chrome: str, html_path: Path, out: Path, width: int, height: int,
               work: Path, tag: str) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    chrome_run(chrome, [
        "--virtual-time-budget=8000",
        f"--window-size={width},{height}",
        f"--screenshot={out}",
        html_path.resolve().as_uri(),
    ], str(work / f"profile-{tag}"))
    w, h = png_size(out)
    return out.is_file() and h > 0


def pack_panels(blocks: Sequence[dict[str, int]], total: int, panel_h: int) -> list[tuple[int, int]]:
    """按内容块边界贪心装箱 → [(offset, height), …]，绝不切断一行字。"""
    if not blocks:
        return [(0, total)] if total else []
    panels: list[tuple[int, int]] = []
    start = 0
    end = 0
    for b in blocks:
        top, h = int(b["t"]), int(b["h"])
        if top - start + h > panel_h and end > start:
            panels.append((start, end - start))
            start = top
        end = top + h
    if end > start:
        panels.append((start, end - start))
    return panels


# --------------------------------------------------------------------------- #
# 目录 / 文件发现
# --------------------------------------------------------------------------- #

REPORT_PATTERNS = [
    re.compile(r"_分析报告\.md$"),
    re.compile(r"_估值报告\.md$"),
    re.compile(r"^qualitative_report\.md$"),
    re.compile(r"investment_report\.md$"),
    re.compile(r"_报告\.md$"),
]


def discover_report(out_dir: Path) -> Path | None:
    mds = [p for p in out_dir.glob("*.md") if p.is_file()]
    for rx in REPORT_PATTERNS:
        hits = sorted((p for p in mds if rx.search(p.name)), key=lambda p: p.stat().st_size, reverse=True)
        if hits:
            return hits[0]
    # 最后兜底：最大的 md（内部工件通常更小）
    mds.sort(key=lambda p: p.stat().st_size, reverse=True)
    for p in mds:
        if not p.name.startswith("_"):
            return p
    return None


def stem_for(doc: Doc, report: Path) -> str:
    base = doc.short_name or doc.company
    if base and doc.code:
        return f"{base}_{doc.code}"
    if base:
        return base
    return report.stem


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def render(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.dir).expanduser().resolve() if args.dir else Path.cwd()
    if args.report:
        report = Path(args.report).expanduser()
        if not report.is_absolute():
            report = (Path.cwd() / report).resolve()
    else:
        report = discover_report(out_dir)
    if report is None or not report.is_file():
        raise SystemExit(f"未找到成果文件（报告 markdown）：{out_dir}")

    md = report.read_text(encoding="utf-8", errors="replace")
    doc = Doc()
    doc.kind, doc.kind_label = detect_kind(report, md)
    parse_hero(md, doc)
    doc.short_name = derive_short_name(out_dir, doc)
    sections = split_sections(md, 3)
    if doc.kind == "analysis":
        build_analysis(md, doc, out_dir, sections)
    else:
        build_generic(md, doc, out_dir, sections)

    touched: list[str] = []
    if args.brief:
        bp = Path(args.brief).expanduser()
        if not bp.is_absolute():
            bp = (Path.cwd() / bp).resolve()
        if not bp.is_file():
            raise SystemExit(f"brief 文件不存在：{bp}")
        doc.brief_used = True
        touched = apply_brief(doc, json.loads(bp.read_text(encoding="utf-8")))

    doc.sources = [report.name]
    for side in ("dims", "invariants.json", "claims.json", "section17_status.json",
                 "consistency_report.md"):
        p = out_dir / side
        if p.exists():
            doc.sources.append(side + ("/" if p.is_dir() else ""))

    blocks = build_blocks(doc)
    width = int(args.width)
    stem = args.name or stem_for(doc, report)
    png_out = Path(args.out).expanduser() if args.out else out_dir / f"{stem}_精华长图.png"
    html_path = out_dir / "_share_longimage.html"

    chrome = find_chrome(args.chrome)
    result: dict[str, Any] = {
        "ok": False,
        "report": str(report),
        "kind": doc.kind,
        "company": doc.company,
        "code": doc.code,
        "verdict": doc.verdict_word,
        "blocks": [b.kind for b in blocks],
        "width": width,
        "html": str(html_path),
        "long_image": None,
        "panels": [],
        "brief_used": doc.brief_used,
        "brief_fields": touched,
        "sources": doc.sources,
        "warnings": [],
    }

    html_path.write_text(build_html(doc, blocks, args.theme, width), encoding="utf-8")

    if not chrome:
        result["warnings"].append(
            "未找到 Chrome/Chromium：已生成 HTML，但没有渲染出图片。"
            "请设置 DSH_SHARE_CHROME 或安装 Google Chrome。")
        result["html"] = str(html_path)
        return result

    with tempfile.TemporaryDirectory(prefix="dsh-share-") as tmp:
        work = Path(tmp)
        total, geom = measure_page(chrome, html_path, work, width)
        if total <= 0:
            result["warnings"].append("Chrome 未能量出页面高度（--dump-dom 失败）。")
            return result
        result["height"] = total

        make_long = not args.panels_only
        if make_long and total > args.max_long_height:
            result["warnings"].append(
                f"整图高度 {total}px 超过上限 {args.max_long_height}px，已跳过长图，只输出分P。")
            make_long = False

        if make_long:
            ok = screenshot(chrome, html_path, png_out, width, total, work, "long")
            if ok:
                result["long_image"] = str(png_out)
                result["long_image_bytes"] = png_out.stat().st_size
            else:
                result["warnings"].append("整图截图失败。")

        if not args.long_only:
            panels = pack_panels(geom, total, int(args.panel_height))
            panel_dir = out_dir / f"{stem}_精华长图_分P"
            if panels:
                panel_dir.mkdir(parents=True, exist_ok=True)
            # 清理上一轮遗留的旧分P，避免张数变少后残留
            for old in panel_dir.glob("*.png"):
                if int(re.search(r"(\d+)", old.stem).group(1)) > len(panels):
                    old.unlink()

            def make_panel(job: tuple[int, tuple[int, int]]) -> str | None:
                i, (offset, height) = job
                label = (f"{doc.company} {doc.code}".strip() or stem) + f" · {i + 1}/{len(panels)}"
                p_html = out_dir / f"_share_longimage_p{i + 1:02d}.html"
                p_html.write_text(
                    build_html(doc, blocks, args.theme, width, clip=(offset, height),
                               panel_label=label),
                    encoding="utf-8")
                target = panel_dir / f"{stem}_精华长图_{i + 1:02d}.png"
                okk = screenshot(chrome, p_html, target, width,
                                 height + PANEL_BAR_H, work, f"p{i}")
                p_html.unlink(missing_ok=True)
                return str(target) if okk else None

            with ThreadPoolExecutor(max_workers=min(4, max(1, len(panels)))) as pool:
                made = list(pool.map(make_panel, list(enumerate(panels))))
            result["panels"] = [p for p in made if p]
            result["panel_dir"] = str(panel_dir)
            result["panel_count"] = len(result["panels"])
            if len(result["panels"]) != len(panels):
                result["warnings"].append("部分分P渲染失败。")

    result["ok"] = bool(result["long_image"] or result["panels"])
    if not args.keep_html:
        pass  # HTML 保留在产物目录，便于复核版式（与既有 _share_longimage.html 约定一致）
    return result


PANEL_BAR_H = 72


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="龟龟投资框架 · 精华长图渲染器")
    ap.add_argument("--dir", help="个股产物目录，如 output/06862_海底捞（默认当前目录）")
    ap.add_argument("--report", help="显式指定报告 markdown；默认自动发现")
    ap.add_argument("--brief", help="可选 brief.json，人工精选文案覆盖自动抽取")
    ap.add_argument("--out", help="整图输出路径")
    ap.add_argument("--name", help="输出文件名主干（默认 {公司}_{代码}）")
    ap.add_argument("--width", type=int, default=1080, help="图片宽度，默认 1080")
    ap.add_argument("--panel-height", type=int, default=1440, help="分P目标高度，默认 1440")
    ap.add_argument("--max-long-height", type=int, default=16000,
                    help="整图最大高度，超出则只出分P（默认 16000）")
    ap.add_argument("--theme", default="paper", choices=sorted(THEMES), help="配色主题")
    ap.add_argument("--chrome", help="Chrome/Chromium 可执行文件路径")
    ap.add_argument("--long-only", action="store_true", help="只出整图")
    ap.add_argument("--panels-only", action="store_true", help="只出分P")
    ap.add_argument("--keep-html", action="store_true", help="保留中间 HTML（默认即保留）")
    ap.add_argument("--json", action="store_true", help="只输出结果 JSON")
    args = ap.parse_args(argv)

    try:
        result = render(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，如实报错
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if args.json:
        print(RESULT_MARKER + json.dumps(result, ensure_ascii=False))
    else:
        if result.get("error"):
            print("渲染失败：" + result["error"], file=sys.stderr)
        else:
            if result.get("long_image"):
                print(f"整图：{result['long_image']}  {result.get('height')}px")
            for p in result.get("panels") or []:
                print(f"分P：{p}")
            for w in result.get("warnings") or []:
                print("注意：" + w, file=sys.stderr)
        print(RESULT_MARKER + json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
