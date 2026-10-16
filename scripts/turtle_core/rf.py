"""无风险利率 Rf 的解析与 `rf.json` 契约 —— **本项目唯一需要外部输入的字段**。

为什么必须外部供给
----------------
`II = max(5%, Rf + 3%)` 是全市场排名的门槛。Rf 错 ⇒ **全市场口径全错**。
而实测（2026-10-14）Tushare `yc_cb` 对本 token **无权限**：

    抱歉，您没有接口(yc_cb)访问权限 …

所以 Rf 必须由外部提供。本模块照 `fx.json` 的 option-A 契约定义 `rf.json`：

.. code-block:: json

    {"rf_pct": 1.0242,
     "date": "2026-10-14",
     "source": "10年期国债到期收益率（中国债券信息网）"}

解析顺序（与 `fx_utils.resolve_fx_hkd_cny` 同构，便于记忆）
-------------------------------------------------------
``--rf`` → ``TUSHARE_RF`` → ``{output_dir}/rf.json`` → 数据包 §14 已记录的利率。

**缺失即 fail-closed**：返回 ``None`` 并给出 ``rf_missing``，
调用方必须 ABORT（不得用 0 或任何默认值兜底 —— `II` 会退化成 5% 下限，
看起来"正常"，实则让排名失去意义，这正是最危险的失败形态）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

#: 合理区间（百分数）。与 `tushare_collector._resolve_rf` 的范围检查一致：
#: 超界几乎必然是单位错（如把 1.67% 写成 0.0167）或填错字段。
RF_MIN = 0.0
RF_MAX = 15.0

RF_JSON_NAME = "rf.json"


def is_plausible(rf_pct: Optional[float]) -> bool:
    """``0 < rf < 15``（百分数）。``None`` 或超界均视为不可用。"""
    if rf_pct is None:
        return False
    try:
        v = float(rf_pct)
    except (TypeError, ValueError):
        return False
    return RF_MIN < v < RF_MAX


def _coerce(value) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return v if is_plausible(v) else None


def load_rf_json(output_dir: str | os.PathLike[str]) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """读 ``{output_dir}/rf.json``。返回 ``(rf_pct, date, source)``；缺失/非法 ⇒ ``(None, None, None)``。

    接受 ``rf_pct`` 的别名 ``rf``（与 `fx.json` 接受 ``hkd_cny`` 别名同理）。
    """
    path = Path(output_dir)
    if path.name == RF_JSON_NAME:
        path = path.parent
    candidate = path / RF_JSON_NAME
    if not candidate.exists():
        return None, None, None
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None, None, None
    if not isinstance(data, dict):
        return None, None, None
    rf = _coerce(data.get("rf_pct", data.get("rf")))
    if rf is None:
        return None, None, None
    return rf, data.get("date"), data.get("source")


def parse_rf_from_data_pack(text: str) -> Optional[float]:
    """从数据包 §14 已渲染的文本里取无风险利率（最后一次兜底）。

    匹配 ``无风险利率`` 同行的一个百分数，例如 ``| 无风险利率（10年期国债） | 1.67% |``。
    """
    import re
    if not text:
        return None
    for m in re.finditer(r"无风险利率[^\n]*?([\d]+(?:\.\d+)?)\s*%", text):
        v = _coerce(m.group(1))
        if v is not None:
            return v
    return None


def resolve_rf(cli_value: Optional[float] = None,
               output_dir: str | os.PathLike[str] | None = None,
               data_pack_path: str | os.PathLike[str] | None = None,
               ) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """按既定顺序解析 Rf。返回 ``(rf_pct, source_label, reason_code)``。

    - 成功：``(值, 来源标签, None)``
    - 失败：``(None, None, "rf_missing")`` —— **唯一**的失败形态，
      调用方据此 ABORT（`turtle_core.reasons.require("rf_missing")` 已验证该码在受控词表内）。
    """
    v = _coerce(cli_value)
    if v is not None:
        return v, "--rf", None

    env = os.environ.get("TUSHARE_RF")
    v = _coerce(env)
    if v is not None:
        return v, "TUSHARE_RF", None

    if output_dir is not None:
        rf, date, source = load_rf_json(output_dir)
        if rf is not None:
            label = RF_JSON_NAME
            if source:
                label += f"（{source}" + (f" {date}" if date else "") + "）"
            return rf, label, None

    if data_pack_path is not None:
        try:
            text = Path(data_pack_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        v = parse_rf_from_data_pack(text)
        if v is not None:
            return v, "数据包 §14", None

    return None, None, "rf_missing"


def make_rf_json_entry(rf_pct: float, date: Optional[str] = None,
                       source: Optional[str] = None) -> dict:
    """构造 `rf.json` 的内容（供协调器/人工每周更新时写入）。"""
    if not is_plausible(rf_pct):
        raise ValueError(f"Rf={rf_pct} 不在合理区间 (0, {RF_MAX}) 内 —— 是否把百分数写成了小数？")
    return {"rf_pct": float(rf_pct), "date": date, "source": source}


def write_rf_json(output_dir: str | os.PathLike[str], rf_pct: float,
                  date: Optional[str] = None, source: Optional[str] = None) -> Path:
    """原子写 `rf.json`（先 `.tmp` 再 `os.replace`，杜绝半写被读到）。"""
    path = Path(output_dir)
    if path.name == RF_JSON_NAME:
        path = path.parent
    path.mkdir(parents=True, exist_ok=True)
    target = path / RF_JSON_NAME
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(make_rf_json_entry(rf_pct, date, source),
                              ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target
