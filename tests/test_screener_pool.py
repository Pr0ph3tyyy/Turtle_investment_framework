"""scripts/screener_pool.py —— 常备池构建测试（全离线，注入假 fetcher）。

重点覆盖两个真实缺陷：
1. 上游 NaN 的 delist_date 让全宇宙被误判退市（见 test_turtle_screener_universe）；
2. 首版排序给换手率 0.3 权重 ⇒ 高换手小盘股排到深度价值股之前，
   与龟龟（分红/分配能力为核心）的方向相反。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import screener_pool as SP  # noqa: E402

TRADE_DATE = "20261013"


def _mk(code, name, industry, mv_yi, pe, dv, turn=0.5, list_date="20100101"):
    return {"ts_code": code, "name": name, "industry": industry,
            "list_date": list_date, "delist_date": float("nan")}, {
        "ts_code": code, "close": 10.0, "pe_ttm": pe, "pb": 1.0,
        "total_mv": mv_yi * 10000.0, "circ_mv": mv_yi * 10000.0,
        "dv_ttm": dv, "turnover_rate": turn}


def _fetch(rows):
    def fetch(api_name, **kw):
        if api_name == "stock_basic":
            return [r for r, _q in rows]
        if api_name == "daily_basic":
            return [q for _r, q in rows]
        return []
    return fetch


BIG_VALUE = _mk("601166.SH", "兴业银行", "银行", 3841, 5.09, 5.87, turn=0.54)
SMALL_HYPE = _mk("002264.SZ", "新华都", "互联网", 57, 35.0, 1.87, turn=21.16)
MID_DIV = _mk("000651.SZ", "格力电器", "家用电器", 2220, 7.97, 7.56, turn=0.84)
TINY = _mk("000002.SZ", "太小公司", "制造业", 10, 10.0, 3.0)
ILLIQ = _mk("000003.SZ", "无人交易", "制造业", 500, 10.0, 3.0, turn=0.01)
LOSS = _mk("000004.SZ", "亏损公司", "制造业", 500, -3.0, 3.0)
NODIV = _mk("000005.SZ", "不分红", "制造业", 500, 10.0, 0.0)


class TestPctiles:
    def test_monotone(self):
        p = SP._pctiles([1.0, 2.0, 3.0])
        assert p == [0.0, 0.5, 1.0]

    def test_ties_get_average_rank(self):
        p = SP._pctiles([1.0, 2.0, 2.0, 4.0])
        assert p[1] == p[2]

    def test_single_and_empty(self):
        assert SP._pctiles([5.0]) == [0.5]
        assert SP._pctiles([]) == []


class TestScoringDirection:
    def _pool(self, rows, limit=10, **kw):
        return SP.build_pool(limit, trade_date=TRADE_DATE, fetch=_fetch(rows),
                             min_market_cap_yi=50.0, min_turnover_pct=0.3,
                             max_pe=40.0, **kw)

    def test_dividend_value_beats_high_turnover_small_cap(self):
        """首版缺陷回归：高换手小盘股**不得**因换手率高而排到深度价值股之前。"""
        pool = self._pool([BIG_VALUE, SMALL_HYPE, MID_DIV])["pool"]
        order = [e["ts_code"] for e in pool]
        assert order.index("601166.SH") < order.index("002264.SZ")
        assert order.index("000651.SZ") < order.index("002264.SZ")

    def test_highest_dividend_ranks_near_top(self):
        pool = self._pool([BIG_VALUE, SMALL_HYPE, MID_DIV])["pool"]
        assert pool[0]["ts_code"] in ("000651.SZ", "601166.SH")

    def test_turnover_is_not_a_bonus(self):
        """换手率只是下限：把一只的换手抬高不应改善它的名次。"""
        a = _mk("600001.SH", "低换手", "制造业", 1000, 10.0, 5.0, turn=0.4)
        b = _mk("600002.SH", "高换手", "制造业", 1000, 10.0, 5.0, turn=15.0)
        pool = self._pool([a, b])["pool"]
        assert pool[0]["ts_code"] == "600001.SH"      # 同分时按代码稳定排序，换手无关

    def test_weights_are_documented_and_sum_to_one(self):
        assert abs(sum(SP.POOL_SCORE_WEIGHTS.values()) - 1.0) < 1e-9


class TestFilters:
    def _pool(self, rows, **kw):
        return SP.build_pool(10, trade_date=TRADE_DATE, fetch=_fetch(rows),
                             min_market_cap_yi=50.0, min_turnover_pct=0.3,
                             max_pe=40.0, **kw)

    def test_tiny_cap_excluded(self):
        codes = [e["ts_code"] for e in self._pool([BIG_VALUE, TINY])["pool"]]
        assert "000002.SZ" not in codes

    def test_illiquid_excluded(self):
        codes = [e["ts_code"] for e in self._pool([BIG_VALUE, ILLIQ])["pool"]]
        assert "000003.SZ" not in codes

    def test_loss_maker_excluded(self):
        codes = [e["ts_code"] for e in self._pool([BIG_VALUE, LOSS])["pool"]]
        assert "000004.SZ" not in codes

    def test_no_dividend_excluded(self):
        """不分红 ⇒ 无法进入龟龟的分配能力/穿透回报率评估。"""
        codes = [e["ts_code"] for e in self._pool([BIG_VALUE, NODIV])["pool"]]
        assert "000005.SZ" not in codes

    def test_industry_cap(self):
        rows = [_mk(f"6001{i:02d}.SH", f"银行{i}", "银行", 1000 + i, 8.0, 6.0)
                for i in range(20)]
        payload = self._pool(rows, max_per_industry_frac=0.10)
        assert payload["counts"]["picked"] <= 10

    def test_nan_delist_date_does_not_empty_universe(self):
        """上游 NaN ⇒ 不得把整个宇宙判成退市（首版真实 bug）。"""
        payload = self._pool([BIG_VALUE, MID_DIV])
        assert payload["counts"]["universe"] >= 2


class TestPoolFile:
    def test_write_show_round_trip(self, tmp_path, capsys):
        payload = SP.build_pool(10, trade_date=TRADE_DATE,
                                fetch=_fetch([BIG_VALUE, MID_DIV]))
        p = SP.write_pool(payload, str(tmp_path / "pool.json"))
        assert p.exists() and not list(tmp_path.glob("*.tmp"))
        assert SP.show_pool(str(p)) == 0
        out = capsys.readouterr().out
        assert "601166.SH" in out and "常备池" in out

    def test_show_missing_pool_is_usage_error(self, tmp_path):
        assert SP.show_pool(str(tmp_path / "nope.json")) == 2

    def test_dir_field_points_at_product_dir(self):
        payload = SP.build_pool(10, trade_date=TRADE_DATE,
                                fetch=_fetch([BIG_VALUE]), output="out")
        e = payload["pool"][0]
        assert e["dir"].startswith("out/")
        assert e["ts_code"].split(".")[0] in e["dir"]

    def test_json_serializable(self):
        payload = SP.build_pool(10, trade_date=TRADE_DATE, fetch=_fetch([BIG_VALUE]))
        json.dumps(payload, ensure_ascii=False)
