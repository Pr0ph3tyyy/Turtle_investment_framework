"""数值单元格解析的**语义契约**（跨脚本一致性）。

背景（known_issues.md §三十三 / §三十四）
----------------------------------------
同一件事（"把 markdown 单元格变成数字"）在仓库里有 5+ 份实现：

| 位置 | 特点 |
|---|---|
| `pack_metrics.num` | **单点**（P1a 引入） |
| `invariants._num` | `fullmatch` |
| `field_coverage._numeric_cells` | 对整行做，跳过非数值 |
| `quality_control._num` | `search` —— 会**剥掉脚注**（`1.23*` → `1.23`） |
| `screener_pool._num` | 解析的是 **Tushare 浮点**，不是 pack 文本 |

它们**故意**不完全相同（例如 `quality_control` 必须容忍脚注 `*` / `†`），
所以不该强行合并成一个。但**核心语义必须一致**，否则同一个数字在不同检查里
含义不同 —— 这正是"多读取器"故障的温床。

本测试把契约写死：
1. 所有实现必须在**规范输入**上同解；
2. 有意不同之处必须逐条登记，并说明理由；
3. 任何新差异都会让测试变红（而不是悄悄存在）。
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pack_metrics as pm  # noqa: E402

#: 规范输入 → 期望（None = 缺失，**不是 0**）
CANONICAL = {
    "9155.64": 9155.64,
    "9,155.64": 9155.64,          # 千分位必须支持（pack 全文都是这个格式）
    "-204060.00": -204060.0,      # 负数是真实存在的（AA 可为负）
    "-204,060.00": -204060.0,     # 千分位 + 负数
    "3.63%": 3.63,
    "**3.63%**": 3.63,
    "—": None,                     # 全角破折号 = 缺失
    "-": None,
    "": None,
    "N/A": None,
}

#: 有意与规范不同的输入（登记理由，避免"悄悄漂移"）
INTENTIONAL_DIFFERENCES = {
    # quality_control 要剥脚注，因此用 search 取第一个数字
    "quality_control.py": {"1.23*": 1.23, "约1.5": 1.5},
}


def _implementations():
    """(名字, 可调用(cell)->float|None, 文件) —— 只收"把单元格文本变数字"的实现。"""
    import importlib
    out = [("pack_metrics.num", pm.num, "pack_metrics.py")]
    for mod, fn, fname in (("invariants", "_num", "invariants.py"),
                           ("quality_control", "_num", "quality_control.py")):
        try:
            m = importlib.import_module(mod)
            f = getattr(m, fn, None)
            if f is not None:
                out.append((f"{mod}.{fn}", f, fname))
        except Exception:  # noqa: BLE001
            pass
    return out


class TestCanonicalAgreement:
    @pytest.mark.parametrize("cell,expected", sorted(CANONICAL.items()))
    def test_single_point_matches_contract(self, cell, expected):
        got = pm.num(cell)
        if expected is None:
            assert got is None, f"pack_metrics.num({cell!r}) 应读成缺失，得到 {got}"
        else:
            assert got == pytest.approx(expected)

    def test_all_implementations_agree_on_canonical_inputs(self):
        diffs = []
        for name, fn, fname in _implementations():
            for cell, expected in CANONICAL.items():
                if cell in INTENTIONAL_DIFFERENCES.get(fname, {}):
                    continue
                try:
                    got = fn(cell)
                except Exception as e:  # noqa: BLE001
                    got = f"ERR:{type(e).__name__}"
                if expected is None:
                    ok = got is None
                else:
                    ok = isinstance(got, (int, float)) and abs(got - expected) < 1e-9
                if not ok:
                    diffs.append(f"{name}({cell!r}) = {got!r}，契约要求 {expected!r}")
        assert not diffs, "数值解析语义漂移：\n  " + "\n  ".join(diffs)


class TestExplicitDifferences:
    """有意差异必须**登记**：既不能悄悄存在，也不能无意间消失。"""

    def test_registered_differences_still_hold(self):
        import importlib
        for fname, cases in INTENTIONAL_DIFFERENCES.items():
            mod = importlib.import_module(fname.replace(".py", ""))
            for cell, expected in cases.items():
                got = getattr(mod, "_num")(cell)
                assert got == pytest.approx(expected), (
                    f"{fname} 对 {cell!r} 的行为变了（{got!r}），"
                    f"若这是有意改动，请同步更新 INTENTIONAL_DIFFERENCES"
                )


class TestMissingIsNotZero:
    """`—` 必须读成 None。把缺失当 0 会让"没有该项"变成"该项为零"。"""

    def test_dash_is_none_everywhere(self):
        for name, fn, _f in _implementations():
            assert fn("—") is None, f"{name} 把 '—' 读成了数值"

    def test_screener_pool_parses_api_floats_not_text(self):
        """`screener_pool._num` 的输入是 Tushare 浮点，不是 pack 文本。

        它**刻意不**做千分位清洗；但它的 `or 0` 回退意味着解析失败会静默变成 0
        （市值 0 ⇒ 该股被排除）。这里只固化"它现在做什么"，并把风险写进台账。
        """
        import screener_pool as SP
        assert SP._num(9155.64) == pytest.approx(9155.64)
        assert SP._num("9155.64") == pytest.approx(9155.64)
        # 已知：带千分位的字符串会读成 None —— 若上游改成返回字符串，这里会静默变 0
        assert SP._num("9,155.64") is None
