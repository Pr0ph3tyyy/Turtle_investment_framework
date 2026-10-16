#!/usr/bin/env python3
"""turtle_core_selfcheck.py —— 用 `turtle_core` 从 claim 账本**反算** canonical 已发布的决策数字。

为什么需要它
------------
设计文档的验收标准第 1 条是「screener 的 GG_pess / II / 目标买入价 / 安全边际
必须与 Workflow 2 报告的 §17 claim 逐字段相等」。这条要求有一个前提：
**turtle_core 本身能复现 canonical 的数字**。本脚本就是那个前提的机械证明。

它只读 `claims.json`（不读 markdown、不联网、不重跑采集），因此毫秒级、可进 CI。

检查项
------
- `cm17.11.ra.{m1|m2|m3}`       ← ``穿透回报率(C, m_N, Q默认, O, 市值)``
- `cm17.11.{aa2y|aaall|aaexcl}.{m1|m2|m3}.gg` ← ``穿透回报率(AA_x, m_N, Q默认, O, 市值)``
- `cm17.11.*.vsii`              ← ``GG − II``
- `cm17.11.*.hh`                ← ``R(同法) − GG``
- `cm17.11.*.tbp`               ← 反解隐含现价，要求各档一致（现价是同一个）
- `cm17.14.k*.ggpess`           ← ``穿透回报率(AA_2y × k, M_pess, Q默认, O, 市值)``

用法::

    .venv/bin/python scripts/turtle_core_selfcheck.py \
        --claims output/00696_中国民航信息网络/claims.json
    # 退出码 0 = 全部复现；1 = 有不可复现项（逐条打印）

⚠️ 本脚本**不判断数值对不对**，只判断"turtle_core 与 canonical 是否同源"。
真正的对错由 `numeric_audit.py` 负责 —— 两者职责不同，不要混。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from turtle_core import decision as D  # noqa: E402
from turtle_core import pessimistic as P  # noqa: E402

TOL = 0.015          # 账本数值保留 2 位小数，容许双重舍入


def _get(claims: dict, cid: str):
    c = claims.get(cid)
    return None if c is None else c.get("value")


def _num(claims: dict, cid: str) -> float | None:
    v = _get(claims, cid)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _close(a: float | None, b: float | None, tol: float = TOL) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def run(claims_path: Path) -> int:
    payload = json.loads(claims_path.read_text(encoding="utf-8"))
    claims = {c["claim_id"]: c for c in payload.get("claims", [])}
    ts_code = payload.get("stock_code") or ""
    kind = D.market_kind(ts_code)
    q_default = D.Q_DEFAULT[kind]

    mkt_cap = _num(claims, "cm17.8.mktcap")
    c_yuan_mm = _num(claims, "cm17.2.c")
    ii = _num(claims, "cm17.2.ii")
    aa = {k: _num(claims, f"cm17.5.{k}") for k in ("aa2y", "aaall", "aaexcl")}
    m = {k: _num(claims, f"cm17.10.{k}") for k in ("m1", "m2", "m3")}
    m_pess = _num(claims, "cm17.10.mpess")

    o_yuan_mm = 0.0          # canonical 硬编码 0（findings D-3）

    print(f"# turtle_core 自检 — {claims_path}")
    print(f"标的 {ts_code}（{kind}，Q默认={q_default}%）  "
          f"市值={mkt_cap} 百万  II={ii}%  C={c_yuan_mm} 百万")
    print()

    results: list[tuple[str, str, object, object, bool]] = []

    # --- 粗算 R ---
    for mk, mv in m.items():
        cid = f"cm17.11.ra.{mk}"
        stated = _num(claims, cid)
        if stated is None:
            continue
        expect = D.penetration_return(c_yuan_mm, mv, q_default, o_yuan_mm, mkt_cap)
        results.append((cid, "R = C×M×(1−Q)/市值", stated, expect, _close(stated, expect)))

    # --- 精算 GG ---
    for ak, av in aa.items():
        for mk, mv in m.items():
            cid = f"cm17.11.{ak}.{mk}.gg"
            stated = _num(claims, cid)
            if stated is None:
                continue
            expect = D.penetration_return(av, mv, q_default, o_yuan_mm, mkt_cap)
            results.append((cid, f"GG = {ak}×{mk}×(1−Q)/市值", stated, expect,
                            _close(stated, expect)))

            vsii = _num(claims, f"cm17.11.{ak}.{mk}.vsii")
            if vsii is not None:
                results.append((f"cm17.11.{ak}.{mk}.vsii", "vsii = GG − II",
                                vsii, D.margin(expect, ii), _close(vsii, D.margin(expect, ii))))

            r_cid = None
            for rk, rv in m.items():
                if abs((rv or 0) - (mv or 0)) < 1e-9:
                    r_cid = f"cm17.11.ra.{rk}"
                    break
            r_stated = _num(claims, r_cid) if r_cid else None
            hh = _num(claims, f"cm17.11.{ak}.{mk}.hh")
            if hh is not None and r_stated is not None:
                exp_hh = D.hh(r_stated, expect)
                results.append((f"cm17.11.{ak}.{mk}.hh", "hh = R − GG", hh, exp_hh,
                                _close(hh, exp_hh)))

    # --- 悲观档 ---
    for k in P.PESS_K_VALUES:
        kk = P.k_slug(k)
        cid = f"cm17.14.{kk}.ggpess"
        stated = _num(claims, cid)
        if stated is None:
            continue
        av = aa["aa2y"]
        expect = D.penetration_return(None if av is None else av * k,
                                     m_pess, q_default, o_yuan_mm, mkt_cap)
        results.append((cid, f"GG_pess = AA_2y×{k:g}×M_pess×(1−Q)/市值",
                        stated, expect, _close(stated, expect)))

    # --- 目标买入价：反解隐含现价，各档必须一致 ---
    implied: dict[str, float] = {}
    for cid, c in claims.items():
        if not cid.endswith(".tbp") or c.get("value") is None:
            continue
        gg_cid = cid[:-4] + ".gg" if cid.startswith("cm17.11") else None
        gg = _num(claims, gg_cid) if gg_cid else _num(claims, cid[:-4] + ".ggpess")
        if gg and ii and gg > 0:
            implied[cid] = float(c["value"]) * ii / gg
    if implied:
        spreads = (max(implied.values()) - min(implied.values())) / max(implied.values())
        ok = spreads <= 0.02
        _cid0 = next(iter(implied))
        results.append(("所有 *.tbp 的隐含现价", "同一现价（离散度 ≤2%）",
                        f"n={len(implied)}",
                        f"{min(implied.values()):.3f}–{max(implied.values()):.3f} "
                        f"(散度 {spreads*100:.2f}%)", ok))

    # --- 输出 ---
    bad = [r for r in results if not r[4]]
    print(f"{'claim_id':<40} {'口径':<34} {'账本':>10} {'turtle_core':>12}  判定")
    print("-" * 108)
    for cid, what, stated, expect, ok in results:
        s = f"{stated:.4f}" if isinstance(stated, float) else str(stated)
        e = f"{expect:.4f}" if isinstance(expect, float) else str(expect)
        print(f"{cid:<40} {what:<34} {s:>10} {e:>12}  {'✅' if ok else '❌'}")
    print()
    print(f"检查 {len(results)} 项，复现 {len(results)-len(bad)} 项，"
          f"不可复现 {len(bad)} 项（容差 ±{TOL}）")
    return 1 if bad else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="turtle_core ↔ claims.json 同源自检")
    ap.add_argument("--claims", required=True, help="claims.json 路径")
    args = ap.parse_args()
    path = Path(args.claims)
    if not path.exists():
        print(f"claims.json 不存在：{path}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(run(path))


if __name__ == "__main__":
    main()
