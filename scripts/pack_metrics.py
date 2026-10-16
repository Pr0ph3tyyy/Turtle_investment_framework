#!/usr/bin/env python3
"""pack_metrics.py —— 数据包的**结构化数值件**与**单点读取器**。

为什么需要（known_issues.md §三十三）
------------------------------------
计算层把结果**渲染成 markdown**，审计层再**用正则把它读回来**。于是"同一份数据"
有多少个读取器就有多少种解释 —— 普查结果：

| 项 | 数量 |
|---|---|
| 依赖正则解析文本的脚本 | 18 / 39 |
| `_num()` 的独立实现 | 11 处 |
| `_mean_first3()` 的独立实现 | 10 处 |
| `_sec()`（切小节）的独立实现 | 6 处 |
| `_row()` / `is_table_sep()` | 5 / 3 处 |

实测代价（同一会话内我写了 4 版 `_sec`，每版错法不同）：

1. `^#{2,3}\\s*3\\b` 命中 `### 17.3`（`.` 是词边界）⇒ §3 利润表被切成 §17 片段；
2. 在 `### 17.10` 处截断 §17.5 ⇒ AA 整段丢失；
3. 把 `⚠️ AA_2y 与 AA_all 差异 2352.2%` 这行的 2352.2 当成 **AA_2y 的值**；
4. 正则字符类 `[\\-\\d,\\.]` 在 Python `re` 里形成非法范围 ⇒ **负数永远匹配不到**。

本模块把"取值"收敛成**一个**实现：计算层产出 `{output_dir}/metrics.json`，
所有审计/下游只经本模块读取。markdown 保留给人看，不再是机器接口。

契约
----
```json
{"schema_version": "1.0",
 "tables": {"4": {"header": ["项目 (百万元)", "2026H1", "2025", ...],
                  "rows": {"商誉": ["—", "1,846.80", ...]}}},
 "units": {...}, "rules_version": "..."}
```

设计取舍：`rows` 的键是**行标签原文**（含全角括号等），值是该行的原始单元格字符串。
**不做数值转换**（那是调用方的事，且转换规则因单位而异）；本模块只保证
"同一个标签 ⇒ 同一个值"，消除"读取器差异"这一类故障。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Sequence

SCHEMA_VERSION = "1.0"
METRICS_FILENAME = "metrics.json"

# 只解析到二级标题（`## N.` / `## 17.`）：§17 的子节是三级，按小节号单独取。
_HEAD_RE = re.compile(r"^(#{2,4})\s+(.+?)\s*$", re.M)
_SEP_RE = re.compile(r"^\|?[\s:|-]+\|[\s:|-]*$")


# ---------------------------------------------------------------------------
# 解析（单点）
# ---------------------------------------------------------------------------
def _headings(text: str):
    out = []
    for m in _HEAD_RE.finditer(text):
        title = m.group(2).strip()
        toks = title.split()
        num = toks[0].rstrip(".") if toks else ""
        # `head_start` 是标题行起点（**截断必须用它**）；`start` 是正文起点。
        # 混用会留下下一节的标题行 —— 实测：`section_text("17.5")` 里残留
        # `### 17.6 因子4·股价分位`，于是下游按标签取值可能命中下一节内容。
        out.append({"level": len(m.group(1)), "title": title, "num": num,
                    "head_start": m.start(), "start": m.end(),
                    "line": text[:m.start()].count("\n") + 1})
    return out


def section_numbers(text: str) -> List[str]:
    """本包出现的全部小节号（入 `## 4.`、`### 17.5`）。"""
    return [h["num"] for h in _headings(text) if h["num"] and h["num"][0].isdigit()]


def section_text(text: str, number: str) -> str:
    """取编号为 `number` 的小节段落。

    终止规则刻意简单：**到下一个同级或更高级标题为止**。
    先前"比较大纲深度"的版本有两处实测错切（`_sec("3")` 吞掉 `### 17.3`、
    `_sec("17.5")` 吞掉 `### 17.6`）；"同级即止"在 markdown 里是对"本节内容"的
    保守理解 —— 宁可多含一点，也不要把本节的值切掉（切掉会把"读不到"误判成"没有"）。
    """
    hs = _headings(text)
    want = str(number)
    top = want.split(".")[0]
    for i, h in enumerate(hs):
        if h["num"] != want:
            continue
        end = len(text)
        for h2 in hs[i + 1:]:
            if h2["level"] <= h["level"]:
                end = h2["head_start"]      # 切在下一节**标题行起点**
                break
            # 更深层标题：**编号不同源**则属于别的一级小节 ⇒ 停。
            # 为什么补这条（实测）：`section_text("14")` 原先会越过 `### 17.2`
            # 继续吞掉 §17 的子块 ⇒ §14 区间里出现 §17.2 的 `Rf（无风险利率）` 行，
            # 让"Rf 无来源"的检查误判为 MATCH（tests/test_audits.py 当场抓到）。
            # 非编号的深层标题（`### 年度行情汇总`）仍算本节子块 ⇒ 继续，
            # 否则 `## 11.` 的年度表读不到、I17 永远 UNEVALUATED。
            tk2 = (h2["title"].split() or [""])[0].rstrip(".")
            if tk2 and tk2[0].isdigit() and tk2.split(".")[0] != top:
                end = h2["head_start"]
                break
        return text[h["start"]:end]
    return ""


def parse_tables(text: str) -> Dict[str, dict]:
    """把每个小节的 markdown 表格解析成 `{header, rows}`。

    同一小节有多张表时，后表并入（行标签冲突则加 `#2`、`#3` 后缀）——
    这样"按标签取值"永远拿到唯一结果，不会因跨表污染而取错。
    """
    out: Dict[str, dict] = {}
    for h in _headings(text):
        num = h["num"]
        if not num or not num[0].isdigit():
            continue
        body = section_text(text, num)
        block: List[List[str]] = []
        tables: List[tuple] = []
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("|") and s.endswith("|"):
                if _SEP_RE.match(s):
                    continue
                block.append([c.strip() for c in s.strip("|").split("|")])
            elif block:
                if len(block) >= 2:
                    tables.append((block[0], block[1:]))
                block = []
        if len(block) >= 2:
            tables.append((block[0], block[1:]))
        if not tables:
            continue
        header = tables[0][0]
        rows: Dict[str, list] = {}
        for _hdr, body_rows in tables:
            for r in body_rows:
                if not r:
                    continue
                key = r[0]
                if key in rows:
                    n = 2
                    while f"{key}#{n}" in rows:
                        n += 1
                    key = f"{key}#{n}"
                rows[key] = r
        out[num] = {"header": header, "rows": rows}
    return out


def parse_bullets(text: str) -> Dict[str, Dict[str, float]]:
    """把**列表项**里的赋值解析成 `{section: {label: value}}`。

    为什么必须落成数据：§17.5 的 `AA_2y` / `AA_all` / 经营杠杆 λ 是**列表项**而不是
    表格行，而"找到标签就取后面第一个数"会把
    `⚠️ AA_2y 与 AA_all 差异 2352.2%` 那行的 2352.2 当成 AA_2y 的值（实测踩过）。
    这里要求 `=` 后**紧跟数字**，且只认赋值句。

    ⚠️ **单位**：值沿用 pack 的**显示口径**（金额为"百万元"，与 `units.amount_unit` 一致），
    不是元。调用方若需要元请显式乘 1e6 —— 本模块不做静默换算。
    """
    out: Dict[str, Dict[str, float]] = {}
    for h in _headings(text):
        # ⚠️ 循环变量不要叫 `num` —— 会遮蔽同名函数 `num()`（实测 TypeError:
        # 'str' object is not callable）。`parse_tables` 里同样避开了这个坑。
        secnum = h["num"]
        if not secnum or not secnum[0].isdigit():
            continue
        body = section_text(text, secnum)
        found: Dict[str, float] = {}
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("|") or "=" not in s:
                continue
            # 形如 `- AA_2y（近2年均值，默认基准）= 2,063.87 百万元`
            # 允许百分号：`- **M_rec = 102.48%（法2）** — …` 是 §17.10 的关键读数，
            # 不带 `%` 分支会让它整条被丢掉（迁移 instrument_audit 时实测）。
            m = re.match(r"^[-*+]?\s*([^=（(]{2,40})[（(]?[^=]*=\s*"
                         r"(-?[0-9][0-9,]*(?:\.[0-9]+)?)\s*%?", s)
            if m:
                label = m.group(1).strip()
                # 排除警告/说明行被误当标签（实测抓到过 `⚠️ λ`）：标签必须是
                # 以字母、数字或 CJK 开头的短标识，且不含警告符号。
                if re.search(r"[⚠⛔❌✅]", label) or not re.match(r"[A-Za-z0-9\u4e00-\u9fff]", label):
                    continue
                v = num(m.group(2))
                if label and v is not None:
                    found[label] = v
        if found:
            out[secnum] = found
    return out


def build(pack_text: str, *, rules_version: str = "", units: Optional[dict] = None) -> dict:
    """从 pack 文本产出 metrics.json 的 payload。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "rules_version": rules_version,
        "units": units or {},
        "tables": parse_tables(pack_text),
        "bullets": parse_bullets(pack_text),
    }


# ---------------------------------------------------------------------------
# 读取（单点）
# ---------------------------------------------------------------------------
class Metrics:
    """结构化数值件的读取器。审计/下游**只准**经它取值。"""

    def __init__(self, payload: dict, source: str = ""):
        self.payload = payload or {}
        self.source = source
        self.tables: Dict[str, dict] = self.payload.get("tables") or {}

    # --- 元信息 ---
    @property
    def rules_version(self) -> str:
        return str(self.payload.get("rules_version") or "")

    @property
    def units(self) -> dict:
        return self.payload.get("units") or {}

    def section_numbers(self) -> List[str]:
        return sorted(self.tables, key=lambda s: [int(x) for x in s.split(".") if x.isdigit()])

    # --- 取值 ---
    def header(self, section: str) -> List[str]:
        return list((self.tables.get(str(section)) or {}).get("header") or [])

    def years(self, section: str) -> List[str]:
        """表头里像"年度/期间"的列标签（2025 / 2026H1）。"""
        return [h for h in self.header(section)
                if re.fullmatch(r"\d{4}(?:H1|Q[1-4])?", h)]

    def rows(self, section: str) -> Dict[str, list]:
        return dict((self.tables.get(str(section)) or {}).get("rows") or {})

    def row(self, section: str, label: str) -> Optional[list]:
        """按标签取整行（精确匹配优先，其次"包含"匹配）。"""
        rows = self.rows(section)
        if label in rows:
            return rows[label]
        for k, v in rows.items():
            if label in k:
                return v
        return None

    def cell(self, section: str, label: str, year: Optional[str] = None) -> Optional[str]:
        """取某个标签在某年（或第一个数值列）的**原始字符串**。"""
        r = self.row(section, label)
        if not r:
            return None
        if year is None:
            return r[1] if len(r) > 1 else None
        hdr = self.header(section)
        try:
            idx = hdr.index(str(year))
        except ValueError:
            return None
        return r[idx] if idx < len(r) else None

    def value(self, section: str, label: str, year: Optional[str] = None) -> Optional[float]:
        """同 `cell`，但转成 float（"—" / "" / 缺失 → None）。"""
        return num(self.cell(section, label, year))

    def series(self, section: str, label: str) -> Dict[str, float]:
        """按表头年度列返回 `{year: value}`。"""
        r = self.row(section, label)
        if not r:
            return {}
        hdr = self.header(section)
        out: Dict[str, float] = {}
        for i, h in enumerate(hdr):
            if i == 0 or i >= len(r):
                continue
            v = num(r[i])
            if v is not None:
                out[h] = v
        return out

    def bullets(self, section: str) -> Dict[str, float]:
        return dict((self.payload.get("bullets") or {}).get(str(section)) or {})

    def bullet_value(self, section: str, label: str) -> Optional[float]:
        """取**列表项**赋值（`- AA_2y（…）= 2,063.87 百万元`），单位同 pack 显示口径。"""
        b = self.bullets(section)
        if label in b:
            return b[label]
        for k, v in b.items():
            if label in k:
                return v
        return None


def num(s) -> Optional[float]:
    """单元格 → float。`—`、`-`、``、`n/a` 一律 None（**不要**把缺失当 0）。"""
    if s is None:
        return None
    t = str(s).replace(",", "").replace("%", "").replace("**", "").strip()
    t = t.replace("−", "-").replace("–", "-")       # 全角/短横线负号
    m = re.fullmatch(r"-?\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None


def load(product_dir: str) -> Metrics:
    """读 `{product_dir}/metrics.json`。缺失时**抛异常**（fail-closed）。

    刻意不"回落到解析 markdown"：那会重新引入多读取器问题，且让缺失被静默容忍。
    需要兼容期的调用方请显式用 `load_or_parse()`。
    """
    p = os.path.join(product_dir, METRICS_FILENAME)
    if not os.path.isfile(p):
        raise FileNotFoundError(
            f"{p} 不存在 —— metrics.json 是审计/下游的唯一取数入口；"
            "请重跑采集器生成它（不提供 markdown 回落，否则等于恢复多读取器）")
    payload = json.loads(open(p, encoding="utf-8").read())
    return Metrics(payload, source=p)


def load_or_parse(product_dir: str) -> Metrics:
    """迁移期兼容：优先 metrics.json，缺失时**显式**解析 pack（并标记来源）。"""
    p = os.path.join(product_dir, METRICS_FILENAME)
    if os.path.isfile(p):
        return load(product_dir)
    pack = os.path.join(product_dir, "data_pack_market.md")
    text = open(pack, encoding="utf-8").read()
    return Metrics(build(text), source=f"{pack} (parsed)")


def write(product_dir: str, pack_text: str, *, rules_version: str = "",
          units: Optional[dict] = None) -> str:
    payload = build(pack_text, rules_version=rules_version, units=units)
    out = os.path.join(product_dir, METRICS_FILENAME)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    return out


# ---------------------------------------------------------------------------
# 双跑等价：metrics.json 与 pack 必须逐格相等
# ---------------------------------------------------------------------------
def compare(product_dir: str, pack_text: Optional[str] = None) -> dict:
    """比对 `metrics.json` 与**重新解析 pack** 的结果。

    `pack_text` 可由调用方显式给出 —— 采集器必须传它，因为 pack 的落盘名
    不一定是 `data_pack_market.md`（`--output` 可指向任意路径）。实测教训：
    硬编码文件名会让 `compare` 去找一个不存在的文件，从而**假报**不一致并 exit 1
    （tests/test_refresh_market.py 立刻抓到）。

    返回 `{ok, checked, mismatches:[...]}`。这是迁移的安全网：
    任何"生产者写错/消费者读错"都会在这里暴露，而不是在某个下游结论里。
    """
    pack = os.path.join(product_dir, "data_pack_market.md")
    if pack_text is None:
        if not os.path.isfile(pack):
            return {"ok": False, "checked": 0, "mismatches": [f"缺 {pack}"]}
        pack_text = open(pack, encoding="utf-8").read()
    from_pack = parse_tables(pack_text)
    try:
        from_json = load(product_dir).tables
    except FileNotFoundError as e:
        return {"ok": False, "checked": 0, "mismatches": [str(e)]}

    mismatches: List[str] = []
    checked = 0
    for sec, tbl in from_pack.items():
        j = from_json.get(sec)
        if j is None:
            mismatches.append(f"§{sec}: metrics.json 缺该小节")
            continue
        for label, row in tbl["rows"].items():
            checked += 1
            if label not in j["rows"]:
                mismatches.append(f"§{sec}/{label}: metrics.json 缺该行")
                continue
            if j["rows"][label] != row:
                mismatches.append(f"§{sec}/{label}: {j['rows'][label]} != {row}")
    for sec in from_json:
        if sec not in from_pack:
            mismatches.append(f"§{sec}: pack 里没有该小节（metrics.json 多余）")
    # 列表项（§17.5 的 AA/λ 等）同样比对
    from_pack_b = parse_bullets(pack_text)
    from_json_b = (json.loads(open(os.path.join(product_dir, METRICS_FILENAME),
                                   encoding="utf-8").read()).get("bullets") or {})
    for sec, items in from_pack_b.items():
        for label, v in items.items():
            checked += 1
            if from_json_b.get(sec, {}).get(label) != v:
                mismatches.append(f"§{sec}/{label}(bullet): "
                                  f"{from_json_b.get(sec, {}).get(label)} != {v}")
    return {"ok": not mismatches, "checked": checked, "mismatches": mismatches[:40]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="数据包结构化数值件：生成 / 双跑比对（只读或只写 metrics.json）")
    ap.add_argument("--dir", help="产物目录")
    ap.add_argument("--write", action="store_true", help="生成 metrics.json")
    ap.add_argument("--compare", action="store_true", help="双跑比对")
    ap.add_argument("--json", dest="json_out", default=None)
    a = ap.parse_args(argv)

    if not a.dir or not os.path.isdir(a.dir):
        print("Error: --dir 必须是存在的产物目录", file=sys.stderr)
        return 2

    if a.write:
        pack = os.path.join(a.dir, "data_pack_market.md")
        out = write(a.dir, open(pack, encoding="utf-8").read())
        print(f"→ {out}")

    if a.compare:
        r = compare(a.dir)
        print(f"双跑比对：check {'✅ 一致' if r['ok'] else '❌ 不一致'} · 比对 {r['checked']} 行")
        for m in r["mismatches"]:
            print(f"   · {m}")
        if a.json_out:
            with open(a.json_out, "w", encoding="utf-8") as fh:
                json.dump(r, fh, ensure_ascii=False, indent=2)
        return 0 if r["ok"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
