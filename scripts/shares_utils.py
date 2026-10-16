#!/usr/bin/env python3
"""shares_utils.py —— 股本口径的**单一解析点**（known_issues.md #25）。

问题
----
旧实现用 `总市值 ÷ 当前价` 反推股数（`_get_mktcap_shares`），口径混用：
总额是**期末**市值、价格是**当前**价。实测 06862：期末价反推 5,574.00 百万股（与年报
附注 35 一致）vs 当前价反推 5,735.83 百万股 —— 差 2.79%。它同时污染
§17.8 ①②、§17.11/§17.14 的目标买入价、以及 I9 的股数交叉核验。

决策（2026-10-14，用户拍板）
--------------------------
**以年报的已发行股数为准**。因此解析优先级里，"年报派生值"必须排在"任何反推值"之前；
反推值只在没有任何申报值时使用，且**必须带着降级标记**流到下游与报告里 ——
一个不知道自己是降级值的降级值，正是 #25 反复复发的原因。

契约
----
`{output_dir}/shares.json`::

    {"shares_mm": 5574.0, "date": "2025-12-31",
     "source": "年报附注35「已发行股本」", "kind": "reported"}

- `shares_mm` = **百万股**（与报表金额单位一致，便于每股量直接相除）；
- `kind` ∈ {reported, upstream, implied_period_end, implied_current}，
  缺省按 `reported` 处理（人工/代理填入即视为已核对）。

解析优先级：`--shares` → `TUSHARE_SHARES_MM` → `shares.json` → A 股上游 `total_share`
→ 期末市值÷期末价 → 当前市值÷当前价。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

# kind 的语义与可信度（降级顺序即列表顺序）
KINDS = ("reported", "upstream", "implied_period_end", "implied_current")
KIND_LABEL = {
    "reported": "年报/人工申报值（**权威**）",
    "upstream": "上游接口的当前股本（非期末口径）",
    "implied_period_end": "期末市值 ÷ 期末价（**反推**，口径一致但依赖市值正确）",
    "implied_current": "当前市值 ÷ 当前价（**反推 + 口径混用**，#25 的原始形态）",
}
# 权威性排序：数字越小越可信
KIND_RANK = {k: i for i, k in enumerate(KINDS)}
TRUSTED = ("reported",)


@dataclass
class ShareCount:
    shares_mm: Optional[float]
    kind: str
    source: str
    date: Optional[str] = None

    @property
    def trusted(self) -> bool:
        return self.kind in TRUSTED

    @property
    def degraded(self) -> bool:
        return self.kind not in TRUSTED

    def as_row_cells(self) -> list:
        """``["总股本（百万股）", "5,574.00（年报/人工申报值…；来源: …）"]``

        **返回列表而不是字符串**：早先返回 Markdown 行字符串，调用方 `val_rows.append(...)`
        被 `format_table` 当成可迭代对象 ⇒ 一个字符一列（实测 67 列），行渲染成空白。
        表格行的形状必须与表头列数一致（§1 是两列：项目／内容）。
        """
        if self.shares_mm is None:
            return ["总股本（百万股）", f"—（未取得；{self.source}）"]
        warn = "" if self.trusted else " ⚠️ **降级值**"
        return ["总股本（百万股）",
                f"{self.shares_mm:,.2f}（{KIND_LABEL.get(self.kind, self.kind)}{warn}；"
                f"来源: {self.source}" + (f"；日期: {self.date}" if self.date else "") + "）"]

    def as_note_line(self) -> str:
        """§17.8 的口径说明行。

        **必须保留「总股本（百万股）」这个标签**：它是全仓解析申报股本的锚点
        （`declared_from_pack`）。改成别的措辞会让 E9/D7e/I9 全部认不出来 ——
        渲染与解析的契约必须写在同一个文件里。
        """
        cells = self.as_row_cells()
        return f"> **股本口径**：{cells[0]} = {cells[1]}"

    def as_row(self) -> str:
        """Markdown 行（供人读的日志/表格；**不要**直接 append 进 `format_table` 的行列表）。"""
        cells = self.as_row_cells()
        return "| " + " | ".join(cells) + " |"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind_label"] = KIND_LABEL.get(self.kind, self.kind)
        d["trusted"] = self.trusted
        return d


def shares_path(output_path: str | Path) -> Path:
    """sidecar 路径固定在输出目录（与 fx.json 同级）。

    接受**数据包文件路径或目录**两种写法：调用方有时给 `output/x/data_pack_market.md`、
    有时直接给 `output/x/`。只按"文件的父目录"解释会把目录参数解析到上一层，
    从而静默找不到 sidecar（首版实测：测试传目录 → 退化成反推值）。
    """
    p = Path(output_path)
    base = p if p.is_dir() else p.parent
    return base / "shares.json"


def parse_shares_mm(raw) -> Optional[float]:
    """接受 百万股 / 亿股 / 万股 / 股 的写法，统一成百万股。

    为什么必须宽容：年报附注常写「5,574,000,000 股」或「55.74 亿股」，
    而契约要的是百万股。让调用方在脑子里换算正是数字出错的经典入口。
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).replace(",", "").strip()
    unit_factor = 1.0
    for suffix, factor in (("亿股", 100.0), ("万股", 0.01), ("千股", 1e-3),
                           ("股", 1e-6), ("百万股", 1.0), ("百万", 1.0), ("亿", 100.0)):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            unit_factor = factor
            break
    try:
        return float(s) * unit_factor
    except ValueError:
        return None


def resolve_shares(cli_value=None, output_path: Optional[str | Path] = None,
                   upstream_share_wan: Optional[float] = None,
                   bs_share_raw: Optional[float] = None,
                   period_end_cap_mm: Optional[float] = None,
                   period_end_close: Optional[float] = None,
                   period_end_date: Optional[str] = None,
                   current_cap_mm: Optional[float] = None,
                   current_close: Optional[float] = None) -> ShareCount:
    """按契约优先级解析股本，返回带来源与降级标记的 :class:`ShareCount`。

    参数里所有 cap 都是**交易币种、百万**，close 是交易币种价格 —— 两者相除即百万股。
    """
    # 1) 显式 CLI
    v = parse_shares_mm(cli_value)
    if v and v > 0:
        return ShareCount(v, "reported", "--shares 显式提供（视为已核对）")
    # 2) 环境变量
    v = parse_shares_mm(os.environ.get("TUSHARE_SHARES_MM"))
    if v and v > 0:
        return ShareCount(v, "reported", "TUSHARE_SHARES_MM")
    # 3) sidecar（reported 的常规入口）
    if output_path is not None:
        p = shares_path(output_path)
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                v = parse_shares_mm(data.get("shares_mm"))
                if v and v > 0:
                    kind = str(data.get("kind") or "reported")
                    if kind not in KINDS:
                        kind = "reported"
                    return ShareCount(v, kind,
                                      str(data.get("source") or "shares.json"),
                                      data.get("date"))
            except (OSError, ValueError):
                pass
    # 3.5) **资产负债表申报股数**（`balancesheet.total_share`，原始**股**）。
    # 为什么排在 daily_basic 之前：本项目的每股指标（BPS / EPS）都是**期末**口径
    # （取自 `fina_indicator`，见 §17.8 ②「归母权益/总股本」），而 `daily_basic.total_share`
    # 是**当前**口径 —— 两者在发生增发/回购后不同期，会让同一份产物里"股数"有两个值
    # （findings D-15：兴业银行 申报 21,163.00 vs 当前 21,162.86 百万股；
    #  I9 的路径 B 曾因此类不同源而误阻断 31 只银行/券商/电力股）。
    # 该字段是**财报申报值**，故 kind = reported（不是降级值）。
    if bs_share_raw and bs_share_raw > 0:
        return ShareCount(bs_share_raw / 1e6, "reported",
                          "Tushare balancesheet.total_share（期末申报值）")

    # 4) A 股上游 total_share（万股）—— 上游当前口径，非期末
    if upstream_share_wan and upstream_share_wan > 0:
        return ShareCount(upstream_share_wan / 100.0, "upstream",
                          "Tushare daily_basic.total_share（当前股本，非期末）")
    # 5) 期末市值 ÷ 期末价（口径一致的反推）
    if period_end_cap_mm and period_end_close:
        return ShareCount(period_end_cap_mm / period_end_close, "implied_period_end",
                          "期末市值 ÷ 期末价（反推，两者同期）", period_end_date)
    # 6) 当前市值 ÷ 当前价（#25 的原始形态）
    if current_cap_mm and current_close:
        return ShareCount(current_cap_mm / current_close, "implied_current",
                          "当前市值 ÷ 当前价（反推 + 口径混用）")
    return ShareCount(None, "implied_current", "无任何可用来源")


# 数据包里的申报股本有两种形态，**必须是同一个正则**（否则解析会随渲染方式漂移）：
#   ① §1 表格行   `| 总股本（百万股） | 5,574.00 | 年报… |`
#   ② §17.8 说明  `> **股本口径**：总股本（百万股）= 5,574.00（年报…；来源: …）`
_DECLARED_RE = re.compile(
    r"总股本（百万股）[^0-9\n]{0,12}([\d,]+(?:\.\d+)?)([^\n]{0,160})")


def declared_from_pack(pack_text: str):
    """从数据包里读申报股本 → ``(shares_mm | None, trusted, raw_cell)``。

    单点实现：`numeric_audit`（E9/D7e）与 `invariants`（I9）都必须用**同一个**解析，
    否则"同一个数被两处各解释一次"就会再次漂移 —— 这正是股本口径问题(#25)的成因。
    """
    m = _DECLARED_RE.search(pack_text or "")
    if not m:
        return None, False, ""
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None, False, m.group(0)
    cell = m.group(2) or ""
    trusted = (("年报" in cell or "人工申报" in cell or "申报值" in cell)
               and "反推" not in cell)
    return val, trusted, m.group(0)


def make_sidecar_entry(shares_mm: float, source: str, date: Optional[str] = None,
                       kind: str = "reported") -> dict:
    return {"shares_mm": float(shares_mm), "kind": kind,
            "source": source, "date": date}


# ---------------------------------------------------------------------------
# 与采集器/渲染层的贴合层
# ---------------------------------------------------------------------------
def resolve_from_store(client) -> "ShareCount":
    """按解析优先级从 client 的 `_store` 取股本（供 §1 渲染与 §17 计算共用）。

    缓存到 `_store["share_count"]`：§1 与 §17 必须看到**同一个**股本 ——
    两处各自解析正是 #25 复发的方式。
    """
    # 只缓存**可信**（申报）值。降级值不缓存 —— 否则在 §11/§12 加载完成之前的一次
    # 调用（例如 §1 渲染时）会把"当前市值÷当前价"钉死，之后 §17 复用这个缓存，
    # 于是 §1 与 §17 各显示一个股本（实测：§1 5,735.83 vs §17 5,574.00）。
    # 降级值每次都重新解析（纯计算，无 IO），代价可忽略，换来"越晚调用越准"。
    cached = client._store.get("share_count")
    if cached is not None and getattr(cached, "trusted", False):
        return cached

    basic = client._store.get("basic_info")
    bi = None
    if basic is not None and not getattr(basic, "empty", True):
        bi = basic.iloc[0]
    code = str(bi.get("ts_code")) if bi is not None else ""
    close = client._safe_float(bi.get("close")) if bi is not None else None
    upstream_wan = client._safe_float(bi.get("total_share")) if bi is not None else None

    if client._is_hk(code):
        cap = client._hk_market_cap_raw(bi)
        cap_mm = (cap / 1e6) if cap else None
    elif client._is_us(code):
        v = client._safe_float(bi.get("total_mv")) if bi is not None else None
        cap_mm = (v / 1e6) if v else None
    else:
        v = client._safe_float(bi.get("total_mv")) if bi is not None else None
        cap_mm = (v / 100.0) if v else None

    _pe_cap, _pe_close, _pe_date = client._period_end_cap_and_close()
    sc = resolve_shares(
        cli_value=client._store.get("shares_mm"),
        output_path=client._store.get("_output_path"),
        upstream_share_wan=upstream_wan,
        period_end_cap_mm=_pe_cap,
        period_end_close=_pe_close,
        period_end_date=_pe_date,
        current_cap_mm=cap_mm,
        current_close=close,
    )
    if sc.trusted or sc.shares_mm is None:
        client._store["share_count"] = sc
    return sc
