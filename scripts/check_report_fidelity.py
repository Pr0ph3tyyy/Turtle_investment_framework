#!/usr/bin/env python3
"""check_report_fidelity.py — 报告级不变式：**保真**与**规则序**。

两类问题在 06862 上都被人工发现，而现有门控都看不见：

1. **保真（faithfulness）** —— 龟龟报告在「商业质量分析」里内嵌定性分析的六段维度叙述，
   并声明「数字、口径标签与 `[src:]` 标注**逐字原样保留、未作任何改动**」。
   实测六段**全部被改写**：D1/D2/D5 新增数字、D3 删掉中位数并把"净利率"改称
   "股东权益回报率"、D4 插入整段支付率分析、D6 删除一项且**自己承认改了措辞**。
   声明无法自证，因此改为**机械比对**：源 JSON 的 narrative 必须在报告中逐字出现，
   且数字多重集必须一致。

2. **规则序（rule precedence）** —— `phase3_valuation.md` 步骤4 规定**买点依据是悲观档
   目标买入价**；步骤5 又把「基准价溢价 ≤ 0%」标为「**买入就是胜利**」。
   两者都是"买"的语义，若

      综合基准价 > 悲观档目标买入价

   则框架会在它自己判定"不可买"的价格区间里发出"买入就是胜利"信号
   （06862 实测 9.34 > 8.92，当前价 9.30 正落在该区间）。
   这是一条**集合包含不变式**：{P ≤ 综合基准价} ⊆ {P ≤ 买点} ⟺ 综合基准价 ≤ 买点。

CLI::

    python3 scripts/check_report_fidelity.py --report output/.../海底捞_06862_分析报告.md \\
        --dims output/06862_海底捞/dims

退出码：0 通过 · 1 存在 FAIL · 2 输入不可读
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

FAIL, WARN, PASS, UNEVALUATED = "FAIL", "WARN", "PASS", "UNEVALUATED"
NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _strip_comments(text: str) -> str:
    """Remove HTML comments before any comparison.

    为什么必须剥离：`src in body` 是**子串**判定，把源叙述整段塞进 `<!-- … -->`
    再发布改写过的正文，旧实现会因为"子串仍在"而判 PASS/WARN —— 即用注释隐藏
    原文就能豁免保真比对。
    """
    return re.sub(r"<!--.*?-->", "", text or "", flags=re.S)


def _norm(text: str) -> str:
    """Normalise for verbatim comparison: markdown/space/emphasis, and drop [src:] tags."""
    text = _strip_comments(text)
    text = re.sub(r"\[src:[^\]]*\]", "", text)
    text = re.sub(r"[\s\*`>]+", "", text)
    return text


# 量纲/百分号标记：紧跟在数字后的这些字符说明它是**量**，不是序号。
_UNIT_AFTER = ("%", "pct", "倍", "万", "亿", "百万", "千元", "元", "港元", "美元",
               "股", "个", "家", "次", "x", "X", "×", "‰")
# 4 位年份：出现在比较里只会制造噪声（源与报告都必然带同一批年份）
_SKIP_YEARS = ("19", "20")


def _is_quantity(raw: str, after: str, before: str) -> bool:
    """判断一个数字 token 是不是**量**（而不是章节/列表序号）。

    旧实现用 `len(k) > 2` 过滤：它既漏掉 `毛利率 47%`／`ROE 45%` 这类 2 位量
    （实测对抗样本可以整段注入而不报警），又会因为把所有 ≥3 位数字都算进来而
    需要额外的年份豁免表。改用**上下文判定**：带百分号/单位、带小数点、或有效位数 ≥3
    的才算量；裸的一两位整数（`### 1.`、列表项 `2.`）不算。
    """
    if any(after.startswith(u) for u in _UNIT_AFTER):
        return True
    if "." in raw:
        return True
    digits = raw.replace(",", "").lstrip("+-")
    if len(digits) >= 3:
        return True
    return any(before.endswith(q) for q in ("约", "≈", "~", "近", "超过", "不足"))


def _numbers(text: str) -> Counter:
    """量级 token 计数（忽略 HTML 注释与 [src:] 标注）。

    保留 `_numbers` 这个名字：调用方（含测试）依赖它。
    """
    text = _strip_comments(re.sub(r"\[src:[^\]]*\]", "", text or ""))
    out: Counter = Counter()
    for m in NUM_RE.finditer(text):
        after = text[m.end():m.end() + 6]
        before = text[max(0, m.start() - 3):m.start()]
        raw = m.group(0)
        if not _is_quantity(raw, after.lstrip(" *"), before):
            continue
        if raw in ("2021", "2022", "2023", "2024", "2025", "2026", "2027"):
            continue
        out[raw] += 1
    return out


_CN_NUM = {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六"}


def _report_section(report: str, number: str) -> str:
    """Slice a report's per-dimension section, supporting both heading styles.

    - 龟龟报告：`### 1. 商业模式与资本特征`
    - 定性报告：`## 维度一：商业模式与资本特征`
    (Hard-coding one style produced a **false FAIL** on the other: the tool reported
    「声明与事实不符」 merely because it looked for the wrong heading. Not being able to
    locate a section is *cannot evaluate*, not a violation.)
    """
    cn = _CN_NUM.get(number, number)
    patterns = [
        rf"^###\s*{re.escape(number)}\.\s",
        rf"^#{{2,3}}\s*维度{re.escape(cn)}\b",
        rf"^#{{2,3}}\s*D{re.escape(number)}\b",
    ]
    for pat in patterns:
        m = re.search(pat, report, re.M)
        if not m:
            continue
        rest = report[m.end():]
        nxt = re.search(r"^#{2,3}\s", rest, re.M)
        return rest[:nxt.start()] if nxt else rest
    return ""


# 只有报告**声明**"六维叙述逐字保留"时，逐字比对才构成约束。
# 综合报告（qualitative_report）本就是改写式综合 —— 对它做逐字断言是误用。
# 判据必须精确：实测「本报告按 D4 的 uncertainty 原样保留该不对称风险」这类无关措辞
# 会误触发（"原样保留"≠"叙述原样保留"）。
_VERBATIM_CLAIM_RE = re.compile(
    r"逐字原样保留|未作任何改动"
    r"|逐字[^。\n]{0,12}(保留|引用|嵌入|呈现)"
    r"|(叙述|原文|维度)[^。\n]{0,12}逐字")


def check_narrative_fidelity(report: str, dims: Dict[str, str]) -> dict:
    """I8：每个维度的 narrative 必须在报告的**对应小节**内逐字出现。

    只允许两处差异：`[src:]` 标注、以及小节标题/前言。比对范围必须限制在该维度小节内——
    与整份报告比对既无意义（相似度 ~7%）也会把全报告的数字都报成"新增"。
    """
    if not dims:
        return {"code": "I8", "title": "叙述保真（报告 ↔ dims JSON）", "status": UNEVALUATED,
                "detail": "未提供 dims 目录", "evidence": []}
    claims_verbatim = bool(_VERBATIM_CLAIM_RE.search(report))
    failures: List[str] = []
    evidence: List[str] = []
    warnings: List[str] = []
    missing: List[str] = []
    for dim in sorted(dims):
        number = dim.lstrip("Dd")
        section = _report_section(report, number)
        if not section:
            missing.append(dim)
            continue
        src = _norm(dims[dim])
        body = _norm(section)
        if not src:
            continue
        if src in body:
            extra = dict(_numbers(section) - _numbers(dims[dim]))
            if extra:
                warnings.append(f"{dim}: 叙述逐字一致，但小节内另有**未标注来源的量** {extra}"
                                "（可能是综合层追加的分析；已按量纲/百分号判定，"
                                "不再用 len>2 过滤 —— 那会漏掉 `毛利率 47%` 这类 2 位量）")
            else:
                evidence.append(f"{dim}: 逐字一致（{len(src)} 字）")
            continue
        ratio = SequenceMatcher(None, src, body).ratio()
        where = ""
        for probe in (src[:120], src[len(src) // 2: len(src) // 2 + 120], src[-120:]):
            if probe and probe not in body:
                where = f"首个缺失片段：…{probe[:70]}…"
                break
        num_src, num_sec = _numbers(dims[dim]), _numbers(section)
        added = dict(num_sec - num_src)
        dropped = dict(num_src - num_sec)
        detail = f"{dim}: 相似度 {ratio:.1%}"
        if where:
            detail += f"；{where}"
        if added:
            detail += f"；该节新增数字 {added}"
        if dropped:
            detail += f"；该节丢失数字 {dropped}"
        failures.append(detail)
    if missing and not failures:
        if claims_verbatim:
            return {"code": "I8", "title": "叙述保真（报告 ↔ dims JSON）", "status": FAIL,
                    "detail": "报告声明六维叙述「逐字原样保留」，但机械比对找不到对应小节",
                    "evidence": [f"{d}: 未找到维度小节" for d in missing]}
        return {"code": "I8", "title": "叙述保真（报告 ↔ dims JSON）", "status": UNEVALUATED,
                "detail": "报告未内嵌逐字维度叙述（未声明逐字保留）⇒ I8 不适用，非违规",
                "evidence": [f"{d}: 未找到维度小节" for d in missing]}
    if failures and not claims_verbatim:
        # 报告未声明逐字保留 ⇒ 改写是设计允许的（综合层职责）。
        # **但 `--strict` 下必须能升级为 FAIL**：`_effective()` 只认本 finding 自带的
        # `strict_status`，旧实现没给 ⇒ 交付门禁 ② 在该分支上是 fail-open，
        # 而且"是否声明逐字保留"由报告作者自己的措辞决定 ⇒ 删掉那句话即可绕过
        # （实测：同一份被改写的报告，带该句 exit 1、删掉该句 exit 0）。独立审计 F1。
        return {"code": "I8", "title": "叙述保真（报告 ↔ dims JSON）", "status": WARN,
                "strict_status": FAIL,
                "detail": "报告未声明逐字保留 ⇒ 允许改写式综合；以下为各维度与源叙述的差异"
                          "（--strict 下按 FAIL 处理：改写必须由报告显式声明）",
                "evidence": failures}
    if failures:
        return {"code": "I8", "title": "叙述保真（报告 ↔ dims JSON）", "status": FAIL,
                "detail": "报告声明六维叙述「逐字原样保留、未作任何改动」，但机械比对不一致；"
                          "声明必须成立，或改为在文内显式标注改写内容",
                "evidence": failures}
    if warnings:
        return {"code": "I8", "title": "叙述保真（报告 ↔ dims JSON）", "status": WARN,
                "detail": "叙述本身逐字一致，但小节内混入了未标注来源的**量** —— "
                          "文档允许「插入你自己的评注段」，但评注里的数字必须能溯源；"
                          "交付路径（--strict）下按 FAIL 处理",
                "evidence": warnings, "strict_status": FAIL}
    return {"code": "I8", "title": "叙述保真（报告 ↔ dims JSON）", "status": PASS,
            "detail": "全部维度叙述在各自小节内逐字一致", "evidence": evidence}


def _table_rows(text: str) -> List[List[str]]:
    rows, block = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            block.append(cells)
        elif block:
            rows.extend(block[1:])
            block = []
    rows.extend(block[1:] if block else [])
    return rows


def check_rule_precedence(report: str, ledger=None) -> dict:
    """I9：综合基准价 ≤ 采纳档目标买入价（否则"买入就是胜利"会在不可买区间触发）。

    **数值来源优先级（2026-10-14 收紧）**：给了 `--claims` 时，两个操作数都取自
    **声明账本**（`cm17.8.floor` / `cm17.14.k<KKK>.tbp`），而不是从报告正文里正则抓。
    为什么必须这样：正文抓取可以被两处操作同时改写而仍然"自洽"——实测把综合底价
    4.12→9.50、k=0.80 买点 8.67→9.75 后，I9 报 `✅ 9.5 ≤ 9.75 PASS`（真值 8.67）。
    账本路径下 I9 检查的是**模型实际产出的数**，抄写错误由 claim 门禁单独负责。
    """
    # 综合基准价：既可能是 `**综合基准价（算术平均）= 9.34 港元**`，也可能是表格行
    # 优先取**综合底价**（语义分组的正确参照）；旧报告只有"综合基准价"（跨语义均值）时回退，
    # 并在 detail 里标明用的是哪一支，避免读者误以为两者等价。
    composite = None
    label_used = "综合底价"
    composite_src = "报告正文"
    if ledger is not None and "cm17.8.floor" in ledger:
        _c = ledger.get("cm17.8.floor")
        if _c is not None and _c.value is not None:
            composite = float(_c.value)
            composite_src = "claims.json（cm17.8.floor）"
    if composite is None:
        m = re.search(r"综合底价（底价型锚点算术平均[^）]*）\**\s*[=＝]?\s*\**([\d.]+)", report)
        if m:
            composite = float(m.group(1))
        else:
            m = re.search(r"综合基准价（算术平均）\**\s*[=＝]?\s*\**([\d.]+)", report)
            if m:
                composite = float(m.group(1))
                label_used = "综合基准价（旧口径：跨语义均值）"
    if composite is None:
        for row in _table_rows(report):
            if row and "综合基准价" in row[0]:
                for cell in row[1:]:
                    mm = re.fullmatch(r"\**([\d.]+)\**", cell.strip())
                    if mm:
                        composite = float(mm.group(1))
                        break
                break

    km = re.search(r"(?:选定|采纳|决策口径)[^\n]{0,40}?k\s*[=＝]\s*\**([\d.]+)", report)
    adopted_k = float(km.group(1)) if km else None

    # 目标买入价：**账本优先**，其次 claim token，最后才是表格行
    target = None
    target_src = "报告正文"
    if ledger is not None and adopted_k is not None:
        cid = "cm17.14.k%03d.tbp" % round(adopted_k * 100)
        _t = ledger.get(cid)
        if _t is not None and _t.value is not None:
            target = float(_t.value)
            target_src = f"claims.json（{cid}）"
    if target is None and adopted_k is not None:
        k_tag = f"{adopted_k:.2f}".replace(".", "")
        token = re.search(rf'cm17\.14\.k{k_tag}\.tbp"[^>]*>\s*\**([\d.]+)', report)
        if token:
            target = float(token.group(1))
    if target is None and adopted_k is not None:
        for row in _table_rows(report):
            if not row:
                continue
            lead = re.match(r"\**([\d.]+)", row[0].replace("*", "").replace("★", "").strip())
            if lead and abs(float(lead.group(1)) - adopted_k) < 1e-9:
                cands = [float(c) for c in (re.fullmatch(r"\**([\d.]+)\**", x.strip()).group(1)
                                            if re.fullmatch(r"\**([\d.]+)\**", x.strip()) else None
                                            for x in row[1:]) if c is not None]
                # 目标买入价是港元价格：排除 pct（含 +/- 或小数位>1）与 百万 量级
                prices = [c for c in cands if c < 1000]
                if prices:
                    target = prices[-1]
                break

    if composite is None or adopted_k is None or target is None:
        return {"code": "I9", "title": "规则序：综合基准价 ≤ 采纳档目标买入价", "status": UNEVALUATED,
                "detail": f"无法解析（综合基准价={composite}，采纳档 k={adopted_k}，目标买入价={target}）",
                "evidence": []}
    ok = composite <= target + 1e-9
    ev = [f"{label_used} = {composite}（来源：{composite_src}）",
          f"采纳档 k = {adopted_k:g} → 目标买入价 = {target}（来源：{target_src}）",
          "不变式：{P ≤ 综合基准价} ⊆ {P ≤ 买点} ⟺ 综合基准价 ≤ 买点"]
    if not ok:
        return {"code": "I9", "title": "规则序：综合基准价 ≤ 采纳档目标买入价", "status": FAIL,
                "detail": f"综合基准价 {composite} > 采纳档（k={adopted_k:g}）目标买入价 {target}："
                          "框架会在它自己判定『不可买』的价格区间发出「买入就是胜利」信号"
                          "（步骤4 规定买点 = 悲观档目标价，步骤5 的 ≤0% 溢价标签必须服从它）",
                "evidence": ev}
    return {"code": "I9", "title": "规则序：综合基准价 ≤ 采纳档目标买入价", "status": PASS,
            "detail": f"{composite} ≤ {target}", "evidence": ev}


def run_all(report: str, dims: Dict[str, str], ledger=None) -> List[dict]:
    findings = []
    for fn, args in ((check_narrative_fidelity, (report, dims)),
                     (check_rule_precedence, (report, ledger))):
        try:
            findings.append(fn(*args))
        except Exception as exc:  # noqa: BLE001
            findings.append({"code": fn.__name__, "title": "检查执行失败", "status": FAIL,
                             "detail": f"{type(exc).__name__}: {exc}", "evidence": []})
    return findings


def _load_claims(path: Optional[str]):
    """Optional claim ledger for the ledger-first I9 path."""
    if not path:
        return None
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from claim_ledger import load_claims
    return load_claims(path)


def _effective(f: dict, strict: bool) -> str:
    return (f.get("strict_status") or f["status"]) if strict else f["status"]


def _load_dims(path: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path:
        return out
    for f in sorted(Path(path).glob("D*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("narrative"):
                out[f.stem] = data["narrative"]
        except (OSError, json.JSONDecodeError):
            continue
    return out


def render(findings: Sequence[dict], strict: bool = False) -> str:
    icon = {PASS: "✅", FAIL: "❌", WARN: "⚠️", UNEVALUATED: "❓"}
    lines = []
    for f in findings:
        st = _effective(f, strict)
        mark = "" if st == f["status"] else f"（默认 {f['status']}）"
        lines.append(f"{icon.get(st, '?')} [{f['code']}] {f['title']} — {st}{mark}")
        lines.append(f"    {f['detail']}")
        for e in f.get("evidence", [])[:8]:
            e = str(e)
            lines.append(f"      · {e[:300]}{'…' if len(e) > 300 else ''}")
    fails = [f for f in findings if _effective(f, strict) == FAIL]
    unk = [f for f in findings if _effective(f, strict) == UNEVALUATED]
    warns = [f for f in findings if _effective(f, strict) == WARN]
    lines.append("")
    lines.append(f"汇总：FAIL {len(fails)} · WARN {len(warns)} · UNEVALUATED {len(unk)} · "
                 f"PASS {sum(1 for f in findings if _effective(f, strict) == PASS)}"
                 + ("（--strict：WARN 级信号已升级为 FAIL）" if strict else ""))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="报告级保真与规则序不变式（fail-closed）")
    ap.add_argument("--report", required=True)
    ap.add_argument("--dims", default=None, help="dims/*.json 目录（校验内嵌叙述保真）")
    ap.add_argument("--claims", default=None, metavar="CLAIMS_JSON",
                    help="§17 声明账本：I9 的两个操作数改为取账本值（cm17.8.floor / "
                         "cm17.14.k<KKK>.tbp），而不是从报告正文正则抓取")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--strict", action="store_true",
                    help="交付路径：UNEVALUATED 计失败，且 WARN 级信号（如小节内混入未溯源量）升级为 FAIL")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    try:
        report = Path(args.report).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error: 无法读取报告 {args.report}: {exc}", file=sys.stderr)
        return 2

    ledger = None
    if args.claims:
        try:
            ledger = _load_claims(args.claims)
        except Exception as exc:  # noqa: BLE001
            print(f"Error: 声明账本不可读 {args.claims}: {exc}", file=sys.stderr)
            return 2

    findings = run_all(report, _load_dims(args.dims), ledger)
    if not args.quiet:
        print(render(findings, strict=args.strict))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"report": args.report,
                        "findings": [{**f, "effective_status": _effective(f, args.strict)}
                                     for f in findings]},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    if any(_effective(f, args.strict) == FAIL for f in findings):
        return 1
    if args.strict and any(_effective(f, True) == UNEVALUATED for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
