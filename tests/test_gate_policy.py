"""Tests for scripts/gate_policy.py —— 交付裁决的**单点**分级表。

两条真实缺口（2026-10-16，06078 海吉亚医疗实测）：

1. `C3`（§4 决策关键行"声明但从未有值"）**从未登记**在本表里 ⇒ `classify` 回落到
   `DEFAULT_TIER=DISCLOSURE`。该股的 §4「商誉」行在各年全空，而 §17.15 把它按 0 扣除、
   §17.8 把「商誉/总资产」渲染成 0.00%；C3 如实报了 `WARN`，门禁仍判「可交付」。
   真实后果：防御性净值被高估 **34.41 亿元**（A 口径 −5,667.24 → 修正后 −9,108.37）。
   这正是 C3 自己 docstring 里预警过的 000708 形态。
2. `client.stock_code` **从未被赋值** ⇒ 每一份 `gate_status.json` 都写 `"stock_code": ""`，
   事后核对某份裁决属于哪只股票只能靠目录名。
"""

import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gate_policy as gp  # noqa: E402


class TestSection4KeyRowTierIsRegistered:
    """`C3` 必须被登记为阻断级，而不是靠 DEFAULT_TIER 兜底。"""

    def test_c3_is_registered(self):
        assert "C3" in gp._CODE_TIER, (
            "C3 未登记 ⇒ classify() 回落 DEFAULT_TIER 并标「未登记分级」")

    def test_c3_tier_is_blocking(self):
        tier, why = gp.classify("C3")
        assert tier == gp.BLOCKING, f"C3 应为阻断级，实为 {tier}"
        assert "未登记分级" not in why, "C3 仍在走 DEFAULT_TIER 兜底"

    def test_c3_fail_blocks_delivery(self):
        """`--strict` 下 C3 升级为 FAIL ⇒ 必须真的阻断交付。"""
        v = gp.verdict(("field_coverage", [
            {"code": "C3", "status": "FAIL", "title": "§4 决策关键行覆盖",
             "detail": "§4「商誉」行在展示窗口内全为空"}]))
        assert v["deliverable"] is False
        assert v["blocking_count"] == 1 and v["blocking"][0]["code"] == "C3"

    def test_c3_warn_stays_disclosure_and_says_so(self):
        """默认模式下 C3 只返回 WARN ⇒ 不阻断，但必须进 disclosure 并被点名。

        这条锁定的是**已知边界**而不是认可它：采集器从不以 `--strict` 调 C3，
        所以默认交付路径下 C3 挡不住任何东西 —— 上游修好之前，报告必须自己披露。
        """
        v = gp.verdict(("field_coverage", [
            {"code": "C3", "status": "WARN", "title": "§4 决策关键行覆盖",
             "detail": "§4「商誉」行在展示窗口内全为空"}]))
        assert v["deliverable"] is True and v["blocking_count"] == 0
        codes = [d["code"] for d in v["disclosure"]]
        assert codes == ["C3"], "C3 的 WARN 必须留在 disclosure 里被看见"

    def test_allow_degraded_never_hides_the_list(self):
        v = gp.verdict(("field_coverage", [
            {"code": "C3", "status": "FAIL", "title": "t", "detail": "d"}]),
            allow_degraded=True)
        assert v["deliverable"] is True and v["degraded"] is True
        assert v["blocking_count"] == 1, "降级放行不得清空失败清单"


class TestGateStatusCarriesStockCode:
    """`gate_status.json` 的 `stock_code` 必须真实，不能恒为空串。"""

    def _client(self):
        with patch("tushare_collector.ts") as mock_ts:
            mock_ts.pro_api.return_value = MagicMock()
            import tushare_collector as tc
            return tc.TushareClient("test_token")

    def test_verdict_writes_the_client_stock_code(self):
        from tushare_collector import _gate_verdict

        client = self._client()
        client.stock_code = "06078.HK"
        v = _gate_verdict(client, [], [], [])
        assert v["stock_code"] == "06078.HK"

    def test_main_assigns_stock_code_to_client(self):
        """回归锚点：`client.stock_code = ts_code` 这一行不得被删掉。

        断言源码而非行为：给 `main()` 造完整 mock 的成本远高于这条缺口的价值
        （缺口本身只是"少了一行赋值"），源码级断言足以挡住回退。
        """
        src = (ROOT / "scripts" / "tushare_collector.py").read_text(encoding="utf-8")
        assert "client.stock_code = ts_code" in src, (
            "采集器必须把校验后的 ts_code 写回 client，否则 gate_status.json 的 "
            "stock_code 恒为空串")
