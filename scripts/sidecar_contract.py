#!/usr/bin/env python3
"""sidecar_contract.py —— **决策输入 sidecar 的三道关**（schema / 量级 / 来源）。

为什么需要（known_issues.md §三十一）
------------------------------------
`{output_dir}/*.json` 里有 4 个文件是**决策变量的直接来源**，但由人/LLM 撰写：

| 文件 | 决定什么 |
|---|---|
| `shares.json` | 总股本 ⇒ **全部每股锚点与买点** |
| `fx.json` | HKD→CNY ⇒ 穿透回报率分母（错则系统性 ±15.65%） |
| `dividends_declared.json` | 已宣告每股股息 ⇒ M_pess ⇒ §17.14 买点 |
| `balance_sheet_overrides.json` | 上游停更科目的补录 ⇒ §17.15 防御性净值 |

`pack_manifest.json` 会钉住它们的 md5 —— 但**钉住只证明"没被改过"，不证明"当初写对了"**。
实测两起事故都出在这一层：

* overrides 的 `unit` 写成「百万元」而契约是「元」⇒ 渲染成 `0.00`（整整一轮才发现）；
* `shares.json` 缺失时按「期末市值÷期末价」反推得 3,279.35 百万股（真值 2,797.22），
  并且**循环**喂进当日市值口径。

三道关
------
1. **schema**：必需键、类型、单位/币种词表；
2. **量级**（当前完全没有）：与产出物内的交叉事实比对 —— 股数 vs「期末市值÷期末价」、
   fx 的合理区间、DPS ≤ 当年 EPS、补录科目不超过总资产的一半……；
3. **来源**：`source` + `date` + 覆盖期间三者必填，并随报告 Warnings 披露。

所有关卡 **fail-closed**：不合格即返回 `ok=False`，调用方不得静默使用该文件。

用法::

    from sidecar_contract import validate_dir
    report = validate_dir("output/00506_中国食品")
    if not report.ok:
        ...   # 拒绝使用这些输入

CLI::

    python3 scripts/sidecar_contract.py --dir output/00506_中国食品
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

FAIL, WARN, OK = "FAIL", "WARN", "OK"

# ---------------------------------------------------------------------------
# 每类 sidecar 的规格
# ---------------------------------------------------------------------------
SHARES_KINDS = ("reported", "upstream", "implied_period_end", "implied_current")
#: `none` = **该财年已宣告不派付**（显式零），与"缺这条记录"语义不同 —— 见下方
#: `check_dividends_declared` 的零值分支。缺了它，零派息年只能被省略，
#: 而按"最近 N 个财年"取窗口的均值会把它当缺失剔除。
DPS_KINDS = ("final", "interim", "proposed_final", "proposed_interim", "special", "none")


@dataclass
class Finding:
    file: str
    gate: str          # schema | magnitude | provenance
    status: str
    detail: str

    def to_dict(self) -> dict:
        return {"file": self.file, "gate": self.gate,
                "status": self.status, "detail": self.detail}


@dataclass
class SidecarReport:
    findings: List[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(f.status == FAIL for f in self.findings)

    @property
    def fails(self) -> List[Finding]:
        return [f for f in self.findings if f.status == FAIL]

    @property
    def warns(self) -> List[Finding]:
        return [f for f in self.findings if f.status == WARN]

    def to_dict(self) -> dict:
        return {"schema_version": "1.0", "ok": self.ok,
                "fail_count": len(self.fails), "warn_count": len(self.warns),
                "findings": [f.to_dict() for f in self.findings]}


class _Ctx:
    """校验上下文：连同 `data_pack_market.md` 才能做的交叉核对都放这里。"""

    def __init__(self, product_dir: str):
        self.dir = product_dir
        self.pack = ""
        p = os.path.join(product_dir, "data_pack_market.md")
        if os.path.isfile(p):
            self.pack = open(p, encoding="utf-8").read()

    def row_first(self, label: str) -> Optional[float]:
        for line in self.pack.splitlines():
            s = line.strip()
            if s.startswith("|") and label in s:
                for c in s.strip("|").split("|")[1:]:
                    m = re.fullmatch(r"\**(-?[\d,]+(?:\.\d+)?)\**", c.strip())
                    if m:
                        return float(m.group(1).replace(",", ""))
        return None

    def period_end_cap_and_price(self):
        """§1/§2 的期末市值与期末价（用于反推股数做交叉核对）。"""
        return self.row_first("总市值"), self.row_first("最新价格")


def _tables(text: str):
    """把 markdown 表格切成 [(header, [rows...])]。"""
    out, block = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            block.append(cells)
        elif block:
            if len(block) >= 2:
                out.append((block[0], block[1:]))
            block = []
    if len(block) >= 2:
        out.append((block[0], block[1:]))
    return out


def _eps_by_year(pack: str) -> Dict[str, float]:
    """从**同一张表**里取「每股基本盈利」的逐年值。

    ⚠️ 必须同表取：初版用"全篇第一个含『项目』的表头"去配 EPS 行的单元格，
    结果配到了 §1 的 `| 项目 | 内容 |`（2 列），年份列全错 ⇒ DPS/EPS 交叉核对
    永远不触发（实测该假阴性）。
    """
    out: Dict[str, float] = {}
    for header, rows in _tables(pack):
        years = [h for h in header[1:] if re.fullmatch(r"\d{4}", h)]
        if not years:
            continue
        for row in rows:
            if row and "每股基本盈利" in row[0]:
                for y, cell in zip(years, row[1:]):
                    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)", cell)
                    if m:
                        out[y] = float(m.group(1))
                return out
    return out


def _load(path: str, report: SidecarReport) -> Optional[dict]:
    name = os.path.basename(path)
    if not os.path.isfile(path):
        return None
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError) as e:
        report.findings.append(Finding(name, "schema", FAIL, f"JSON 不可解析：{e}"))
        return None
    if not isinstance(data, dict):
        report.findings.append(Finding(name, "schema", FAIL, "顶层必须是对象"))
        return None
    return data


def _require(data: dict, name: str, keys, report: SidecarReport, gate="schema") -> bool:
    miss = [k for k in keys if data.get(k) in (None, "", [], {})]
    if miss:
        report.findings.append(Finding(name, gate, FAIL, f"缺必需键：{miss}"))
        return False
    return True


def _nested_provenance(data: dict) -> tuple[bool, bool]:
    """顶层没有 source/date 时，看**覆盖期间/年度条目内**有没有。

    `balance_sheet_overrides.json` 把 source 写在每个 field 下、
    `dividends_declared.json` 写在每个年度下 —— 两者都是合法的记录方式，
    顶层缺省不应报警（否则等于强制一个无意义的重复字段）。
    """
    has_src = has_date = False
    for holder in ("overrides", "declared_dps"):
        block = data.get(holder)
        if not isinstance(block, dict):
            continue
        vals = block.values()
        for entry in vals:
            if isinstance(entry, dict):
                has_src = has_src or bool(str(entry.get("source") or "").strip())
                has_date = has_date or bool(str(entry.get("date") or "").strip())
    return has_src, has_date


def _provenance(data: dict, name: str, report: SidecarReport) -> None:
    """第 3 关：来源可核。缺 date/source 记 WARN（披露级，不阻断）。

    ⚠️ 但**顶层与嵌套都没有** source 时必须报警 —— 这正是本校验器在
    `dividends_declared.json` / `balance_sheet_overrides.json` 上抓到的真实缺口。
    """
    nested_src, nested_date = _nested_provenance(data)
    if not str(data.get("source") or "").strip() and not nested_src:
        report.findings.append(Finding(
            name, "provenance", WARN, "source 缺失（顶层与条目内都没有）⇒ 无法追溯该值的来源"))
    if not str(data.get("date") or "").strip() and not nested_date:
        report.findings.append(Finding(
            name, "provenance", WARN, "date 缺失（顶层与条目内都没有）⇒ 无法判断与财报期是否同期"))


# ---------------------------------------------------------------------------
# 各 sidecar 的校验
# ---------------------------------------------------------------------------
def check_shares(path: str, ctx: _Ctx, report: SidecarReport) -> None:
    name = os.path.basename(path)
    data = _load(path, report)
    if data is None:
        return
    if not _require(data, name, ["shares_mm"], report):
        return
    v = data.get("shares_mm")
    if not isinstance(v, (int, float)) or v <= 0:
        report.findings.append(Finding(name, "schema", FAIL, f"shares_mm 必须为正数，得到 {v!r}"))
        return
    kind = str(data.get("kind") or "reported")
    if kind not in SHARES_KINDS:
        report.findings.append(
            Finding(name, "schema", FAIL, f"kind={kind!r} 不在词表 {SHARES_KINDS}"))
    # 第 2 关：与包内「期末市值÷期末价」交叉
    cap, price = ctx.period_end_cap_and_price()
    if cap and price:
        implied = cap / price
        if implied > 0:
            dev = abs(v - implied) / implied * 100
            if dev > 5:
                report.findings.append(Finding(
                    name, "magnitude", FAIL,
                    f"shares_mm={v:,.2f} 与包内「期末市值÷期末价」={implied:,.2f} 偏离 {dev:.2f}% "
                    f"(>5%) ⇒ 要么股数错、要么市值用了混用口径（known_issues #25）"))
            else:
                report.findings.append(Finding(
                    name, "magnitude", OK, f"与期末反推值偏离 {dev:.2f}%（≤5%）"))
    _provenance(data, name, report)


def check_fx(path: str, ctx: _Ctx, report: SidecarReport) -> None:
    name = os.path.basename(path)
    data = _load(path, report)
    if data is None:
        return
    if not _require(data, name, ["factor_to_reporting"], report):
        return
    f = data.get("factor_to_reporting")
    if not isinstance(f, (int, float)):
        report.findings.append(Finding(name, "schema", FAIL, f"factor_to_reporting 非数值：{f!r}"))
        return
    # 第 2 关：HKD/CNY 合理区间（历史区间约 0.78–1.10；放宽到 0.5–1.5 只为拦荒谬值）
    if not (0.5 <= f <= 1.5):
        report.findings.append(Finding(
            name, "magnitude", FAIL,
            f"factor_to_reporting={f} 落在 [0.5, 1.5] 之外 ⇒ 疑单位/方向写反"
            f"（正确语义：交易币种 × factor = 报表币种）"))
    rc, tc = data.get("reporting_currency"), data.get("trading_currency")
    if not rc or not tc:
        report.findings.append(Finding(
            name, "schema", WARN,
            "缺 reporting_currency / trading_currency ⇒ 币种转换方向无法机械校验"))
    _provenance(data, name, report)


def check_dividends_declared(path: str, ctx: _Ctx, report: SidecarReport) -> None:
    name = os.path.basename(path)
    data = _load(path, report)
    if data is None:
        return
    dps = data.get("declared_dps")
    if not isinstance(dps, dict) or not dps:
        report.findings.append(Finding(
            name, "schema", FAIL, "declared_dps 必须是非空对象 {年份: {value, currency, …}}"))
        return
    eps = _eps_by_year(ctx.pack)
    for year, entry in dps.items():
        if not re.fullmatch(r"\d{4}", str(year)):
            report.findings.append(Finding(
                name, "schema", FAIL, f"年度键 {year!r} 不是 4 位年份"))
            continue
        if not isinstance(entry, dict):
            report.findings.append(Finding(
                name, "schema", FAIL, f"{year} 的值必须是对象（含 value/currency/source）"))
            continue
        val = entry.get("value")
        if not isinstance(val, (int, float)) or val < 0:
            report.findings.append(Finding(
                name, "schema", FAIL, f"{year}.value 必须为非负数，得到 {val!r}"))
            continue
        # ⚠️ **`value = 0` 是合法且必要的一类事实**：它表示「该财年**已宣告派付零股息**」，
        # 与「没有这条记录（= 未知/缺失）」是**两回事**。旧实现要求 `val > 0`，
        # 于是零派息年只能被**省略**，而 `_mean_first3` 是按「最近 3 个财年」取窗口的
        # ⇒ 省略会被静默当作缺失剔除，**把三年均值系统性抬高**
        # （06078 实测：2024/2025 两个真零年被剔除，法2 三年均值由 4.61% 被抬到 7.64%，
        #  法3 更因窗口内只剩一个点而根本没做除法）。
        # 这正是 clean-room 报的「0 渲染成 —，无法区分零派息与缺失」。
        if val == 0:
            report.findings.append(Finding(
                name, "schema", OK if entry.get("kind") == "none" else WARN,
                f"{year}: 显式零派息（已宣告不派付）"
                + ("" if entry.get("kind") == "none"
                   else f"；建议 kind='none'，现为 {entry.get('kind')!r}")))
        ccy = str(entry.get("currency") or "").upper()
        if ccy not in ("CNY", "HKD", "USD"):
            report.findings.append(Finding(
                name, "schema", FAIL, f"{year}.currency={ccy!r} 不在 (CNY, HKD, USD)"))
        if entry.get("kind") and str(entry["kind"]) not in DPS_KINDS:
            report.findings.append(Finding(
                name, "schema", WARN, f"{year}.kind={entry['kind']!r} 不在词表 {DPS_KINDS}"))
        # 第 2 关：DPS ≤ 同年 EPS（派息率 >100% 虽可能但不该静默通过）
        e = eps.get(str(year))
        if e is not None and val > e * 1.5:
            report.findings.append(Finding(
                name, "magnitude", FAIL,
                f"{year}: DPS {val} > EPS {e} × 1.5 ⇒ 疑单位错（元/分）或年份错配"))
        elif e is not None:
            report.findings.append(Finding(
                name, "magnitude", OK, f"{year}: DPS/EPS = {val / e * 100:.1f}%"))
    _provenance(data, name, report)


def check_balance_overrides(path: str, ctx: _Ctx, report: SidecarReport) -> None:
    name = os.path.basename(path)
    data = _load(path, report)
    if data is None:
        return
    unit = str(data.get("unit") or "")
    if unit not in ("元", "yuan", "CNY"):
        report.findings.append(Finding(
            name, "schema", FAIL,
            f"unit={unit!r} 不是「元」⇒ 与 `_pivot_hk_line_items` 的 ind_value 口径不一致，"
            f"会被 §4 渲染再除 1e6（实测：写成百万元 ⇒ 渲染成 0.00）"))
    ovr = data.get("overrides")
    if not isinstance(ovr, dict) or not ovr:
        report.findings.append(Finding(name, "schema", FAIL, "overrides 必须是非空对象"))
        return
    ta = ctx.row_first("总资产")
    for field_name, per_year in ovr.items():
        if not isinstance(per_year, dict):
            report.findings.append(Finding(name, "schema", FAIL, f"{field_name} 必须是 {{年份: 数值}}"))
            continue
        for year, v in per_year.items():
            # 这些是**元数据键**而不是年度键（采集器侧 `_apply_balance_overrides`
            # 只认数值、跳过它们）；校验器必须用同一套 skip 集，否则会把说明文字
            # 误报成"非数值"（本校验器初版即如此 → 2 条假阳性）。
            if year in ("source", "kind", "reason", "note", "date", "periods"):
                continue
            if not isinstance(v, (int, float)):
                report.findings.append(Finding(
                    name, "schema", FAIL, f"{field_name}.{year}={v!r} 非数值"))
                continue
            if ta and abs(v) > ta * 0.5e6:
                report.findings.append(Finding(
                    name, "magnitude", FAIL,
                    f"{field_name}.{year}={v:,.0f} 超过总资产的一半 ⇒ 疑单位错（百万 vs 元）"))
    _provenance(data, name, report)


CHECKS: Dict[str, Callable[[str, _Ctx, SidecarReport], None]] = {
    "shares.json": check_shares,
    "fx.json": check_fx,
    "dividends_declared.json": check_dividends_declared,
    "balance_sheet_overrides.json": check_balance_overrides,
}


def validate_dir(product_dir: str) -> SidecarReport:
    """对产物目录里的全部决策 sidecar 跑三道关。**只读**。"""
    report = SidecarReport()
    ctx = _Ctx(product_dir)
    for fname, fn in CHECKS.items():
        path = os.path.join(product_dir, fname)
        if not os.path.isfile(path):
            report.findings.append(Finding(
                fname, "schema", WARN,
                "文件不存在 —— 若该标的确实需要它（如港股需 fx/shares），"
                "采集器会走降级路径；降级本身有标记，但决策精度下降"))
            continue
        fn(path, ctx, report)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="决策 sidecar 三道关（schema/量级/来源），只读")
    ap.add_argument("--dir", required=True, help="产物目录，如 output/00506_中国食品")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    if not os.path.isdir(a.dir):
        print(f"Error: 目录不存在 {a.dir}", file=sys.stderr)
        return 2
    rep = validate_dir(a.dir)
    if not a.quiet:
        icon = {OK: "✅", FAIL: "❌", WARN: "⚠️"}
        print(f"sidecar 三道关 · {a.dir}")
        for f in rep.findings:
            print(f"  {icon.get(f.status,'?')} [{f.gate:10s}] {f.file:32s} {f.detail}")
        print(f"\n结论：{'可交付' if rep.ok else '存在阻断项'} "
              f"(FAIL {len(rep.fails)} · WARN {len(rep.warns)})")
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as fh:
            json.dump(rep.to_dict(), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
