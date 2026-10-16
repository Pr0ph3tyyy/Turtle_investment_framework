"""§6 分红历史（港股）的 fail-closed 展示。

背景（06862 实测）：上游 `hk_fina_indicator.dps_hkd` 对港股呈「按公告快照」填充特征——
2023–2026 各年**恒为 0.7610**，而同一张表的「派息率 (%)」列是 `DPS ÷ EPS`，会随 EPS 变化而
**看起来逐年不同**（实测 96.40 / 87.83 / 84.87%），于是伪装成真实的派息政策，被下游当成
「分配意愿下降」的证据（龟龟 Agent B 曾据此给出扣分项）。

修法：检测到 stuck 特征且备用源不可用时，**不给派息率列**（fail-closed）并写明原因；
DPS 原值保留在表中（以便不变式 I5 仍能机械识别该列失真）。
**刻意不改 `_store["dividends"]`**：§17.10 的 M_rec 在 DPS 恒定时本就采用法2（现金流口径），
清空 store 会让 M 失效——这是"只修展示层、保留 §17 输入"的边界。
"""

import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, "scripts")


def _client():
    from tushare_collector import TushareClient
    with patch("tushare_collector.ts") as mock_ts:
        mock_ts.pro_api.return_value = MagicMock()
        c = TushareClient("test_token")
    c._yf_available = False
    return c


def _stuck_df(n=5, dps=0.7610):
    """上游快照特征：多个报告期、同一个 DPS 值。"""
    return pd.DataFrame([
        {"end_date": f"{2026 - i}1231", "dps_hkd": dps, "divi_ratio": None}
        for i in range(n)
    ])


class TestSection6FailClosed:
    def test_stuck_dps_suppresses_payout_column(self):
        c = _client()
        with patch.object(c, "_cached_call", return_value=_stuck_df()), \
             patch.object(c, "_get_annual_df", return_value=pd.DataFrame()):
            out = c._get_dividends_hk("06862.HK")
        assert "上游快照失真" in out, "表头必须显式标注该列失真"
        assert "DPS 序列不可用" in out and "fail-closed" in out
        assert "不得" in out and "分配意愿" in out
        # 派息率列不给值（DPS 原值保留，供不变式 I5 识别）
        assert "96.40" not in out and "87.83" not in out
        assert "0.7610" in out

    def test_varying_dps_keeps_payout_column(self):
        """正常序列（各年不同）不受影响，派息率照常给出。"""
        c = _client()
        df = pd.DataFrame([
            {"end_date": "20251231", "dps_hkd": 0.384, "divi_ratio": None},
            {"end_date": "20241231", "dps_hkd": 0.898, "divi_ratio": None},
            {"end_date": "20231231", "dps_hkd": 0.150, "divi_ratio": None},
        ])
        income = pd.DataFrame([{"end_date": "20251231", "basic_eps": 1.0}])
        with patch.object(c, "_cached_call", return_value=df), \
             patch.object(c, "_get_annual_df", return_value=income):
            out = c._get_dividends_hk("06862.HK")
        assert "上游快照失真" not in out
        assert "DPS 序列不可用" not in out

    def test_store_is_not_cleared(self):
        """只修展示层：`_store["dividends"]` 必须保留，否则 §17.10 的 M 会失效。"""
        c = _client()
        with patch.object(c, "_cached_call", return_value=_stuck_df()), \
             patch.object(c, "_get_annual_df", return_value=pd.DataFrame()):
            c._get_dividends_hk("06862.HK")
        assert "dividends" in c._store
        assert not c._store["dividends"].empty, "M_rec 的法2 依赖该 store 不被清空"
