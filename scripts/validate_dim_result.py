#!/usr/bin/env python3
"""validate_dim_result.py — 校验 L1 维度结果契约（dims/{dim}.json）。

契约本体：shared/qualitative/references/dim_result.schema.json（JSON Schema 2020-12）
设计说明：shared/qualitative/references/dim_result_contract.md

为什么要有这个 CLI
------------------
包 2 决策 5：schema 校验失败必须**硬失败**（而非只告警），最多重试 2 次；仍失败则该维度标
critical 交 L2 决定。若只告警不阻断，就会退化成 v1 的"散文交接"，协作问题（信息不对称 /
口径漂移 / 漏检）会立刻回归。因此需要一个可被 pipeline 直接调用、给退出码的校验器。

Usage::

    python3 scripts/validate_dim_result.py --input output/002984_森麒麟/dims
    python3 scripts/validate_dim_result.py --input output/002984_森麒麟/dims/D2.json --dim D2
    python3 scripts/validate_dim_result.py --input .../dims/all.json      # 数组形式（单上下文臂）

Exit codes: 0 全部通过 · 1 存在校验失败 · 2 文件/契约不可用
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, List, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SCHEMA = os.path.join(
    REPO_ROOT, "shared", "qualitative", "references", "dim_result.schema.json"
)


def load_schema(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _validator(schema: Dict[str, Any]):
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "需要 jsonschema（已在 requirements.txt）；请先 bash init.sh"
        ) from e
    Draft202012Validator.check_schema(schema)  # 契约本身是否合法
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_instances(instances: List[Any], validator) -> List[str]:
    """校验一组实例，返回可读的错误列表（空 = 全部通过）。"""
    errors: List[str] = []
    for idx, inst in enumerate(instances):
        where = "" if len(instances) == 1 else f"[{idx}] "
        for err in sorted(validator.iter_errors(inst), key=lambda e: list(e.path)):
            loc = "/".join(str(p) for p in err.path) or "(root)"
            errors.append(f"{where}{loc}: {err.message}")
    return errors


def load_instances(path: str) -> Tuple[List[Any], List[str]]:
    """载入一个文件或目录下的 dims/*.json。数组文件按多实例处理。

    返回 ``(instances, files)``。
    """
    files: List[str] = []
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.json")))
    else:
        files = [path]

    instances: List[Any] = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            instances.extend(data)
        else:
            instances.append(data)
    return instances, files



def check_slice_coverage(instances: List[Any]) -> List[str]:
    """切片契约硬门禁：把"证据基础够不够"从提示词要求变成机械要求。

    为什么需要：切片由启发式选页（TOC 锚点 + 关键词兜底），而 `dropped_pages` /
    `slice_truncated` 过去**不被任何门禁消费**（实测 0 引用），`material_status` 又由
    **读了那份不完整切片的同一个代理**自评。06862 实测：D6 切片不含资产负债表页与
    借款附注页（provided 仅 161/166/228/271-273/283），于是漏计 20.29 亿元债券。

    三条规则（都可机械判定，不替代理做"够不够"的判断）：
    - **R1** `slice.dropped_pages > 0` ⇒ 必须存在 `slice_truncated` flag；
    - **R2** `used_chars >= budget_chars`（预算用满，大概率被截断）⇒ 同上；
    - **R3** 引用的年报页码必须**落在它拿到的切片页内**（允许 ±1 的页签/印刷页偏移）
      ⇒ 越界即 FAIL。这条直接堵住"引用了没给你的页"。
    """
    errs: List[str] = []
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        dim = inst.get("dimension") or "?"
        sl = inst.get("slice") or {}
        flags = inst.get("flags") or []
        codes = {f.get("code") for f in flags if isinstance(f, dict)}
        dropped = sl.get("dropped_pages")
        used, budget = sl.get("used_chars"), sl.get("budget_chars")
        pages = sl.get("provided_pages") or []
        total = sl.get("total_pages")

        # R1 / R2：截断必须显式
        if isinstance(dropped, int) and dropped > 0 and "slice_truncated" not in codes:
            errs.append(f"{dim}: slice.dropped_pages={dropped} > 0，但缺 `slice_truncated` flag"
                        "（未覆盖页不得静默）")
        if (isinstance(used, int) and isinstance(budget, int) and budget > 0 and used >= budget
                and "slice_truncated" not in codes):
            errs.append(f"{dim}: used_chars={used} 已达预算 {budget}（切片被预算截断），"
                        "但缺 `slice_truncated` flag")

        # R4：引用页码必须在切片范围内（±1 容差）
        if pages:
            allowed = set()
            for pg in pages:
                if isinstance(pg, int):
                    allowed.update({pg - 1, pg, pg + 1})
            for i, e in enumerate(inst.get("evidence") or [], 1):
                if not isinstance(e, dict):
                    continue
                src = e.get("source") or {}
                if src.get("kind") != "annual_report":
                    continue
                pg = src.get("page")
                if isinstance(pg, int) and pg not in allowed:
                    errs.append(f"{dim}: evidence[{i}] 引用年报第 {pg} 页，"
                                f"但该维度切片只提供 {sorted(p for p in pages if isinstance(p, int))} "
                                "（越界引用不得出现）")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(
        description="校验 dims/{dim}.json 是否符合 L1 契约",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --input output/002984_森麒麟/dims
  %(prog)s --input output/002984_森麒麟/dims/D2.json
  %(prog)s --input some/all.json        # 6 个契约对象的数组
        """,
    )
    ap.add_argument("--input", required=True, help="单个 json / dims 目录 / 数组 json")
    ap.add_argument("--schema", default=DEFAULT_SCHEMA, help="契约文件路径")
    ap.add_argument("--quiet", action="store_true", help="只输出结论")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: 输入不存在: {args.input}", file=sys.stderr)
        return 2
    if not os.path.exists(args.schema):
        print(f"Error: 契约文件不存在: {args.schema}", file=sys.stderr)
        return 2

    try:
        schema = load_schema(args.schema)
        validator = _validator(schema)
    except Exception as e:
        print(f"Error: 载入/检查契约失败: {e}", file=sys.stderr)
        return 2

    try:
        instances, files = load_instances(args.input)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error: 无法解析输入 JSON: {e}", file=sys.stderr)
        return 2

    if not instances:
        print(f"Error: {args.input} 下没有可校验的 JSON", file=sys.stderr)
        return 2

    # 契约本身的问题（含 $ref 无法解析——check_schema 不解析 $ref，会在校验时才暴露）
    # 必须与"实例不合契约"区分开：前者 exit 2，后者 exit 1。
    try:
        errors = validate_instances(instances, validator)
    except Exception as e:  # noqa: BLE001 — 任何校验器崩溃都属契约/工具问题
        print(f"Error: 契约校验过程失败（多为 $ref 不可解析或 schema 结构错误）: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if errors:
        print(f"❌ 契约校验失败：{len(errors)} 处问题（{len(instances)} 个实例，{len(files)} 个文件）")
        for e in errors[:40]:
            print(f"   - {e}")
        if len(errors) > 40:
            print(f"   … 其余 {len(errors) - 40} 处省略")
        print("\n按决策 5：硬失败 → 修正后重试（最多 2 次）；仍失败则该维度标 critical 交 L2。")
        return 1

    cov_errors = check_slice_coverage(instances)
    if cov_errors:
        print(f"❌ 切片覆盖度门禁失败：{len(cov_errors)} 处问题")
        for e in cov_errors:
            print(f"   - {e}")
        print("\n覆盖度是可核验属性：要么补页，要么如实置 slice_truncated / 非充分状态。")
        return 1

    if not args.quiet:
        dims = [i.get("dimension") for i in instances if isinstance(i, dict)]
        print(f"✅ 契约校验通过：{len(instances)} 个实例（{', '.join(str(d) for d in dims) or '-'}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
