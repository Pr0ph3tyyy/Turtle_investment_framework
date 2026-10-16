#!/usr/bin/env python3
"""verify_product.py —— **产物验收单点**：一条命令跑完全部门禁，给一个结论。

为什么需要（known_issues §四十八，T5）
------------------------------------
到 2026-10-15 为止，"新流程产出的文件是否合理可靠"这件事，是靠**人工一条条敲命令**
验证的：sidecar 三道关 → 数值件双跑 → 版本锚 → 阶段清单 → 不变式 → 字段覆盖 →
门禁裁决 → 报告侧四项交付门禁。**这个过程没有自动化** ⇒
下一个标的交付时，没有人会全部跑一遍，而漏掉任何一条都可能是静默的。

本脚本把那条链固化成**一个入口、一个结论**。它**只读**，不改任何产物。

两层门禁
--------
* **包级**（采集完即可跑，不需要报告）：sidecar / 数值件 / 版本锚 / 阶段清单 /
  不变式 / 字段覆盖 / 采集器裁决；
* **交付级**（需要报告）：claim 绑定、报告保真、观点审核（子进程调既有 CLI，
  因为它们有既定的退出码契约）。

另接两条此前**未接入任何交付前置**的信号：
* `rules_version` —— 陈旧产物必须被点名（否则会把旧规则的化石当当前结论）；
* `instrument_audit` 的 `disposition` —— `blocked` 阻断，`disclose_only` 仅披露。

用法::

    .venv/bin/python scripts/verify_product.py --dir output/600887_伊利股份
    .venv/bin/python scripts/verify_product.py --dir … --report … --dims … --claims … --strict

退出码：0 通过 · 1 有阻断 · 2 用法/输入错误。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"

ICON = {PASS: "✅", FAIL: "❌", WARN: "⚠️", SKIP: "➖"}


@dataclass
class Gate:
    name: str
    status: str
    detail: str
    evidence: List[str] = field(default_factory=list)


def _read_json(path: str):
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 包级门禁
# ---------------------------------------------------------------------------
def gate_rules_available(d: str) -> Gate:
    """规则单点（`strategy_rules.json`）必须可用。

    为什么单独一条门禁（known_issues §四十九）：该文件缺失时，
    `derived_metrics` 会回落到**内嵌默认值**继续跑 —— 产物可能与策略规则矛盾，
    而且"回落到旧常量"是最难察觉的失败方向。原先连一句警告都没有。
    """
    p = os.path.join(HERE, "strategy_rules.json")
    if not os.path.isfile(p):
        return Gate("策略规则单点", FAIL, f"缺 {os.path.basename(p)} ⇒ 数值可能来自内嵌默认值")
    try:
        data = json.loads(open(p, encoding="utf-8").read())
    except (OSError, ValueError) as e:
        return Gate("策略规则单点", FAIL, f"不可解析：{type(e).__name__}: {e}")
    # 逐条核对关键阈值确实能被读到（防"文件在、但键被删"）
    missing = [k for k in ("ii_threshold", "dividend_tax_q_pct", "lambda_reliability",
                           "stability_gates", "pessimistic_k_grid", "entity_basis")
               if k not in data]
    if missing:
        return Gate("策略规则单点", FAIL, f"缺关键条目：{missing}")
    return Gate("策略规则单点", PASS, f"{len(data)} 个条目，关键条目齐备")


def gate_sidecars(d: str) -> Gate:
    import sidecar_contract as sc
    rep = sc.validate_dir(d)
    if not rep.ok:
        return Gate("决策 sidecar 三道关", FAIL,
                    f"{len(rep.fails)} 处不合格", [f"{f.file}: {f.detail}" for f in rep.fails])
    return Gate("决策 sidecar 三道关", PASS,
                f"FAIL 0 · WARN {len(rep.warns)}",
                [f"{f.file}: {f.detail}" for f in rep.warns])


def gate_metrics(d: str) -> Gate:
    import pack_metrics as pm
    if not os.path.isfile(os.path.join(d, "metrics.json")):
        return Gate("结构化数值件（双跑）", FAIL,
                    "缺 metrics.json —— 审计层没有唯一取数入口（旧产物）")
    r = pm.compare(d)
    if not r["ok"]:
        return Gate("结构化数值件（双跑）", FAIL,
                    f"{len(r['mismatches'])} 处不一致", r["mismatches"][:6])
    return Gate("结构化数值件（双跑）", PASS, f"{r['checked']} 行逐格一致")


def gate_rules_version(d: str) -> Gate:
    import rules_version as rv
    cur = rv.compute()
    got = rv.read_anchor(d)
    if got == cur:
        return Gate("规则版本锚", PASS, f"{cur}（同代）")
    if got:
        return Gate("规则版本锚", FAIL,
                    f"**陈旧**：产物 {got} ≠ 当前 {cur} ⇒ 不得与当前规则结论混用")
    return Gate("规则版本锚", WARN, f"无版本锚（产生于该机制之前）；当前 {cur}")


def gate_manifest(d: str) -> Gate:
    """核对清单里每个输入**在本产物目录内**是否仍与记录一致。

    ⚠️ 刻意**不**用 `stage_manifest.check()`：它按清单**记录的路径**去找文件，
    而记录的是「相对建立时 cwd 的路径」。于是
      · 产物被复制/移动后，清单仍指向**原位置** ⇒ 拿着副本去校验会**假通过**
        （实测：把 /tmp/t4 复制到 tmp 后校验，改动副本的 pack 竟然通过）；
      · 真正的语义是"**这个产物**的输入有没有被改过" ⇒ 必须按
        `d/<basename>` 解析，而不是按记录路径。
    `stage_manifest.py --check` 的 CLI 语义（按记录路径）对"原位校验"是合理的，
    两条路径各司其职。
    """
    import hashlib
    p = os.path.join(d, "pack_manifest.json")
    if not os.path.isfile(p):
        return Gate("阶段清单（上游冻结）", FAIL, "缺 pack_manifest.json")
    m = _read_json(p) or {}
    diffs, checked = [], 0
    for e in (m.get("inputs") or []):
        name = os.path.basename(str(e.get("path") or ""))
        target = os.path.join(d, name)
        if not name or not os.path.isfile(target):
            diffs.append(f"缺失：{name or e.get('path')}")
            continue
        checked += 1
        digest = hashlib.md5(open(target, "rb").read()).hexdigest()
        if e.get("md5") and digest != e["md5"]:
            diffs.append(f"已变更：{name}（清单 {str(e['md5'])[:12]}… → 现在 {digest[:12]}…）")
    if diffs:
        return Gate("阶段清单（上游冻结）", FAIL, f"{len(diffs)} 处不一致", diffs[:6])
    return Gate("阶段清单（上游冻结）", PASS, f"{checked} 项输入未被改动")


def gate_invariants(d: str) -> Gate:
    import invariants as inv
    pack = open(os.path.join(d, "data_pack_market.md"), encoding="utf-8").read()
    fx = _read_json(os.path.join(d, "fx.json"))
    findings = inv.run_all(pack, fx)
    fails = [f for f in findings if f.status == "FAIL"]
    unk = [f for f in findings if f.status == "UNEVALUATED"]
    ev = [f"[{f.code}] {f.title} — {f.detail}" for f in fails] or \
         [f"[{f.code}] {f.title} — {f.detail}" for f in unk]
    if fails:
        return Gate("数据包不变式", FAIL, f"{len(fails)} 项 FAIL", ev[:6])
    return Gate("数据包不变式", PASS,
                f"PASS {sum(1 for f in findings if f.status == 'PASS')} · "
                f"UNEVALUATED {len(unk)}", ev[:4])


def gate_field_coverage(d: str) -> Gate:
    import field_coverage as fc
    pack_path = os.path.join(d, "data_pack_market.md")
    pack = open(pack_path, encoding="utf-8").read()
    findings = fc.run_all(pack, sidecar_dir=d, pack_path=pack_path)
    fails = [f for f in findings if f.effective_status(strict=True) == "FAIL"]
    ev = [f"[{f.code}] {f.title} — {f.detail}" for f in fails]
    if fails:
        return Gate("字段覆盖", FAIL, f"{len(fails)} 项不达标", ev[:6])
    return Gate("字段覆盖", PASS, f"{len(findings)} 项检查通过")


def gate_gate_status(d: str) -> Gate:
    g = _read_json(os.path.join(d, "gate_status.json"))
    if g is None:
        return Gate("采集器裁决", FAIL, "缺 gate_status.json")
    if not g.get("deliverable", False):
        blocks = [f"{b.get('code')} {b.get('title')}" for b in (g.get("blocking") or [])]
        return Gate("采集器裁决", FAIL, "deliverable=false", blocks[:6])
    dis = [f"{x.get('code')} {x.get('title')}" for x in (g.get("disclosure") or [])]
    return Gate("采集器裁决", PASS, "deliverable=true", dis[:4])


def gate_instrument(d: str) -> Gate:
    """仪器体检的 disposition（此前未接入任何交付前置）。"""
    import instrument_audit as ia
    pack_path = os.path.join(d, "data_pack_market.md")
    if not os.path.isfile(pack_path):
        return Gate("仪器体检（处置）", SKIP, "无数据包")
    m = ia._metrics(pack_path)
    m.payload["_pack_path"] = pack_path
    code = os.path.basename(d)
    fs = (ia.check_entity_basis(m, code) + ia.check_stability(m, code)
          + ia.check_measure_naming(m, code))
    for f in fs:
        f["disposition"] = ia.disposition_of(f.get("rule", ""), f.get("metric", ""),
                                             f.get("status", ""))
    blocked = [f for f in fs if f.get("status") in ("FAIL", "UNKNOWN")
               and f.get("disposition") == ia.BLOCKED]
    disc = [f for f in fs if f.get("status") in ("FAIL", "UNKNOWN")
            and f.get("disposition") == ia.DISCLOSE_ONLY]
    ev = [f"{f['rule']}/{f['metric']}: {f['detail'][:80]}" for f in (blocked + disc)][:6]
    if blocked:
        return Gate("仪器体检（处置）", FAIL,
                    f"blocked {len(blocked)} · disclose_only {len(disc)}", ev)
    return Gate("仪器体检（处置）", PASS,
                f"blocked 0 · disclose_only {len(disc)}（披露项，不阻断）", ev)


# ---------------------------------------------------------------------------
# 交付级门禁（有报告才跑）
# ---------------------------------------------------------------------------
def _run(cmd: List[str]) -> tuple:
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(HERE))
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def gate_report_gates(d: str, report: str, claims: str, dims: str, strict: bool) -> List[Gate]:
    """跑报告侧四项门禁。

    ⚠️ **必须把产物重定向到临时目录**：`viewpoint_audit --output-dir` 与
    `report_consistency --output` 都会**写文件**。本脚本承诺"只读"，
    初版直接传 `--output-dir <产物目录>` ⇒ 实测把 `viewpoint_audit.md/.json`
    写进了被验收的产物里（自己污染自己的验收对象）。
    """
    import tempfile
    out: List[Gate] = []
    py = sys.executable
    if not (report and os.path.isfile(report)):
        out.append(Gate("报告侧交付门禁", SKIP, "未提供报告（包级验收）"))
        return out

    tmp = tempfile.mkdtemp(prefix="verify_product_")
    steps = [
        ("claim 绑定", [py, os.path.join(HERE, "report_consistency.py"),
                        "--report", report, "--claims", claims, "--gates",
                        "--output", os.path.join(tmp, "consistency.md")]
                       + (["--strict"] if strict else [])),
        # check_report_fidelity 无 --output：只读，直接跑
        ("报告保真 I8/I9", [py, os.path.join(HERE, "check_report_fidelity.py"),
                            "--report", report, "--dims", dims,
                            "--claims", claims] + (["--strict"] if strict else [])),
        ("观点审核", [py, os.path.join(HERE, "viewpoint_audit.py"),
                      "--report", report, "--output-dir", tmp]
                     + (["--strict"] if strict else [])),
    ]
    for name, cmd in steps:
        rc, log = _run(cmd)
        tail = [ln for ln in log.strip().splitlines() if ln.strip()][-3:]
        out.append(Gate(f"报告·{name}", PASS if rc == 0 else FAIL,
                        f"exit {rc}", tail))
    return out


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="产物验收单点：跑全部门禁，给一个结论（只读）")
    ap.add_argument("--dir", required=True, help="产物目录，如 output/600887_伊利股份")
    ap.add_argument("--report", default=None, help="分析报告路径（给了才跑交付级门禁）")
    ap.add_argument("--claims", default=None)
    ap.add_argument("--dims", default=None)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--skip-instrument", action="store_true")
    ap.add_argument("--json", dest="json_out", default=None)
    a = ap.parse_args(argv)

    d = a.dir.rstrip("/")
    if not os.path.isdir(d):
        print(f"Error: 目录不存在 {d}", file=sys.stderr)
        return 2
    if not os.path.isfile(os.path.join(d, "data_pack_market.md")):
        print(f"Error: {d} 不是产物目录（无 data_pack_market.md）", file=sys.stderr)
        return 2

    claims = a.claims or os.path.join(d, "claims.json")
    dims = a.dims or os.path.join(d, "dims")

    gates: List[Gate] = []
    for fn in (gate_rules_available, gate_sidecars, gate_metrics, gate_rules_version,
               gate_manifest,
               gate_invariants, gate_field_coverage, gate_gate_status):
        try:
            gates.append(fn(d))
        except Exception as e:  # noqa: BLE001 —— 门禁自身故障必须可见，且不得静默通过
            gates.append(Gate(getattr(fn, "__name__", "?"), FAIL,
                              f"门禁执行异常：{type(e).__name__}: {e}"))
    if not a.skip_instrument:
        try:
            gates.append(gate_instrument(d))
        except Exception as e:  # noqa: BLE001
            gates.append(Gate("仪器体检（处置）", FAIL,
                              f"门禁执行异常：{type(e).__name__}: {e}"))
    gates.extend(gate_report_gates(d, a.report, claims, dims, a.strict))

    print("=" * 78)
    print(f"产物验收 · {d}")
    print("=" * 78)
    for g in gates:
        print(f"  {ICON.get(g.status, '?')} {g.name:22s} {g.detail}")
        for e in g.evidence[:3]:
            print(f"       · {str(e)[:104]}")

    n_fail = sum(1 for g in gates if g.status == FAIL)
    n_warn = sum(1 for g in gates if g.status == WARN)
    n_skip = sum(1 for g in gates if g.status == SKIP)
    print()
    if n_fail:
        print(f"结论：**不可交付** —— {n_fail} 条门禁未通过"
              f"（WARN {n_warn} · SKIP {n_skip}）。修数据后重跑，不得手算绕过。")
    elif n_skip:
        print(f"结论：**包级通过**（{n_skip} 条交付级门禁未跑 —— 未提供报告）。"
              f"报告写完后必须用 --report 再跑一次。")
    else:
        print(f"结论：**通过**（WARN {n_warn} 项为披露级）")

    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as fh:
            json.dump({"dir": d, "fail": n_fail, "warn": n_warn, "skip": n_skip,
                       "gates": [{"name": g.name, "status": g.status,
                                  "detail": g.detail, "evidence": g.evidence}
                                 for g in gates]}, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
