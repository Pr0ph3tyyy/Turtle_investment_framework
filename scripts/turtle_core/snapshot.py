"""慢层财务快照 —— 季度/年度刷新，每日只读。

为什么需要慢层（设计 §1 C2）
--------------------------
龟龟公式链里**只有分母（市值/价格）是每日变的**：``AA``、``M``、``Q``、``O``、
财务质量门槛字段、底价锚点的报表币种每股量，全部来自财报期数据。

全 A 股逐股拉财报 ≈ 4–5 小时（0.5s 限速 × 5000+ 只 × 6–8 端点），日报做不完。
把财务侧固化成按**财报期**版本化的快照后，每日期只需 **1 次全市场行情调用** + 纯运算。

所以本模块的价值不是"缓存"，而是**把日报的成本从 O(股票数 × API) 降到 O(1)**。

纪律
----
- **原子写**：先写 ``.tmp`` 再 ``os.replace``，杜绝半写文件被下游读到
  （仓库已有"两次半写读取"的真实事故记录，见 AGENTS.md 阶段清单条目）。
- **版本键 = 财报期**（``fy_end``）：新财报期一出，旧快照必须被判 ``stale_snapshot``。
  用旧盈利排名**比不排名更危险**。
- **fail-closed**：任何取不到的字段留 ``None`` 并在 ``reasons`` 里登记受控原因码；
  绝不写 0 或默认值（``reasons.require`` 对未知码抛异常）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .reasons import require

__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "FinancialSnapshot",
    "snapshot_dir",
    "snapshot_path",
]

SNAPSHOT_SCHEMA_VERSION = "1.0"

#: 默认快照根目录（与选股器缓存同级）
DEFAULT_SNAPSHOT_DIR = "output/.screener_cache/snapshots"


def snapshot_dir(cache_dir: str | os.PathLike[str] | None = None) -> Path:
    base = Path(cache_dir) if cache_dir else Path(DEFAULT_SNAPSHOT_DIR)
    return base if base.name == "snapshots" else base / "snapshots"


def snapshot_path(ts_code: str, cache_dir: str | os.PathLike[str] | None = None) -> Path:
    safe = str(ts_code).replace("/", "_")
    return snapshot_dir(cache_dir) / f"{safe}.json"


@dataclass
class FinancialSnapshot:
    """单只股票的慢层财务侧不变量。

    金额一律为**报表币种原始元**（与 canonical `_get_mktcap_shares` 的 `mkt_cap` 同单位），
    每股锚点一律为**交易币种/股**（`turtle_core.floors` 已折汇）。
    """

    ts_code: str
    fy_end: str = ""                     # "20251231"；空串表示财报期未知
    built_at: str = ""
    schema_version: str = SNAPSHOT_SCHEMA_VERSION

    reporting_currency: Optional[str] = None
    trading_currency: Optional[str] = None
    factor_to_reporting: Optional[float] = None

    c_yuan: Optional[float] = None       # 最新年归母净利润
    aa: dict[str, Any] = field(default_factory=dict)
    payout: dict[str, Any] = field(default_factory=dict)
    q_default: Optional[float] = None
    q_scenarios: list[float] = field(default_factory=list)
    #: ⚠️ **已弃用，勿用**。恒为 0.0。O 请读下面的 ``o``（元，由 `_MM_KEYS` 归一）。
    #: 保留仅为兼容旧调用方；同一概念存在两个字段曾导致静默不同源（findings D-3b）。
    o_yuan: float = 0.0

    gate_fields: dict[str, Any] = field(default_factory=dict)
    floor: dict[str, Any] = field(default_factory=dict)
    total_shares: Optional[float] = None
    shares_kind: Optional[str] = None
    #: 产物 §17.8 的「总市值」（报表币种原始元）—— **仅作参考**。
    #: 引擎的每日市值一律自算（股本 × 现价 × fx），见 findings **D-10**：
    #: 二者不等意味着产物股本口径降级，会导致 screener 与报告 §17 的 GG 不相等。
    #: 用 :func:`turtle_screener.engine.check_market_cap_consistency` 校验。
    artifact_market_cap_yuan: Optional[float] = None

    #: scope/key → 受控原因码。例如 {"aa": "no_cashflow", "floor": "fx_missing"}
    #: 上游产物的门禁裁决（来自 `{product_dir}/gate_status.json`）。
    #: ``False`` = 采集器判定该包不得作为分析输入 ⇒ 引擎默认排除（findings/设计：fail-closed）。
    #: O（注销型回购年均），单位**元**（与 `mkt_cap` 同单位，供 penetration_return 用）
    o: Optional[float] = None

    gate_deliverable: Optional[bool] = None
    gate_blocking: list[str] = field(default_factory=list)

    reasons: dict[str, str] = field(default_factory=dict)
    #: 溯源：字段 → 来源说明（便于日报回答"这个数哪来的"）
    source: dict[str, str] = field(default_factory=dict)

    # ---- 构造/序列化 ----

    @classmethod
    def new(cls, ts_code: str, fy_end: str = "", **kwargs: Any) -> "FinancialSnapshot":
        return cls(ts_code=ts_code, fy_end=fy_end,
                   built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   **kwargs)

    def validate(self) -> list[str]:
        """返回问题清单（空 = 通过）。未知原因码是**硬错误**。"""
        errors: list[str] = []
        if not self.ts_code:
            errors.append("ts_code 为空")
        for scope, code in self.reasons.items():
            try:
                require(code)
            except KeyError as exc:
                errors.append(f"reasons[{scope!r}] {exc}")
        return errors

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reasons"] = dict(sorted(self.reasons.items()))
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FinancialSnapshot":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    # ---- 持久化（原子） ----

    def save(self, cache_dir: str | os.PathLike[str] | None = None) -> Path:
        errors = self.validate()
        if errors:
            raise ValueError("快照校验失败：" + "；".join(errors))
        path = snapshot_path(self.ts_code, cache_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=1, sort_keys=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)          # 原子：读者要么见旧、要么见新，绝不见半写
        return path

    @classmethod
    def load(cls, ts_code: str,
             cache_dir: str | os.PathLike[str] | None = None) -> Optional["FinancialSnapshot"]:
        path = snapshot_path(ts_code, cache_dir)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None                # 损坏快照视为不存在，由调用方重建

    # ---- 过期判定 ----

    def is_stale(self, current_fy_end: Optional[str]) -> bool:
        """当前最新财报期比快照新 ⇒ 过期。

        ``current_fy_end`` 为 ``None``（拿不到最新财报期）时返回 ``False`` ——
        因为"无法判定"不等于"过期"，此时应由调用方另行登记 ``no_annual_3y``。
        """
        if not current_fy_end or not self.fy_end:
            return False
        return str(current_fy_end) > str(self.fy_end)

    @property
    def required_scopes_ok(self) -> bool:
        """算 GG 所必需的 scope 是否齐备（``aa`` + ``payout`` + 市值/汇率）。"""
        blocking = {"no_cashflow", "no_payout", "no_market_cap", "fx_missing", "no_rf"}
        return not any(code in blocking for code in self.reasons.values())
