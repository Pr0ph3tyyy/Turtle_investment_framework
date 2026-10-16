"""导入卫生守卫 —— 用 **subprocess** 覆盖全量测试看不见的导入顺序问题。

为什么必须是 subprocess
----------------------
2026-10-14 实测：给 `derived_metrics` 加上 `from turtle_core import pessimistic` 后出现了
**循环导入**（`turtle_core.decision` → `tushare_modules.constants` →
`tushare_modules/__init__` → `derived_metrics` → `turtle_core.pessimistic` → `.decision`）。
**全量 pytest 全绿**（1960 passed）—— 因为测试进程里的导入顺序恰好避开了环；
真正炸出来的是独立运行的 `scripts/turtle_core_selfcheck.py`。

⇒ 同进程的 import 断言无法覆盖这类缺陷；每种**入口顺序**必须在**独立解释器**里跑一遍。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

#: 每种元组是「先导入谁」，用来暴露依赖方向上的环
ENTRY_ORDERS: tuple[tuple[str, ...], ...] = (
    ("turtle_core.decision",),
    ("turtle_core.pessimistic",),
    ("turtle_core.floors",),
    ("turtle_core",),
    ("tushare_modules.derived_metrics",),
    ("tushare_modules",),
    # 最危险的一种：先动纯层，再动采集层（正式入口 selfcheck 就是这个顺序）
    ("turtle_core.decision", "turtle_core.pessimistic", "tushare_modules.derived_metrics"),
    # 反向顺序
    ("tushare_modules.derived_metrics", "turtle_core.pessimistic"),
)


def _run_imports(modules: tuple[str, ...]) -> subprocess.CompletedProcess:
    body = "\n".join(f"import {m}" for m in modules)
    body += "\nfrom turtle_core.decision import ii_threshold\n"
    body += "assert isinstance(ii_threshold('600887.SH', 1.67), float)\n"
    return subprocess.run(
        [sys.executable, "-c", body],
        cwd=str(SCRIPTS), capture_output=True, text=True, timeout=120,
    )


class TestImportHygiene:
    @pytest.mark.parametrize("modules", ENTRY_ORDERS,
                             ids=["+".join(m.split(".")[-1] for m in e) for e in ENTRY_ORDERS])
    def test_no_circular_import_for_any_entry_order(self, modules):
        proc = _run_imports(modules)
        assert proc.returncode == 0, (
            f"导入顺序 {modules} 触发失败：\n{proc.stderr[-1500:]}"
        )

    def test_selfcheck_script_runs_standalone(self):
        """`turtle_core_selfcheck.py` 是正式入口之一，必须能独立跑（不依赖 pytest 的导入顺序）。"""
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "turtle_core_selfcheck.py"), "--help"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-1200:]

    def test_screener_cli_help_runs_standalone(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "screener_daily.py"), "--help"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-1200:]

    def test_turtle_core_has_no_import_time_tushare_dependency(self):
        """纯计算层在 import 期**不得**拉起 tushare_modules 的依赖树（那是成环的根源）。"""
        body = ("import sys\n"
                "import turtle_core.decision, turtle_core.pessimistic, turtle_core.floors\n"
                "assert 'tushare_modules' not in sys.modules, "
                "sorted(m for m in sys.modules if m.startswith('tushare_modules'))\n")
        proc = subprocess.run([sys.executable, "-c", body], cwd=str(SCRIPTS),
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr[-1200:]
