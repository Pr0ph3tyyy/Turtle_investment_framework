"""turtle_screener.universe —— 三市场宇宙枚举与洁净度过滤测试（全离线）。"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from turtle_screener import universe as U  # noqa: E402

TODAY = date(2026, 10, 14)


def _src(pages):
    """pages: api_name -> list[record] 或 callable(**kw)->list[record]"""
    def fetcher(api_name, **kw):
        v = pages.get(api_name, [])
        return v(**kw) if callable(v) else v
    return U.UniverseSource(fetcher)


class TestIsStName:
    @pytest.mark.parametrize("name,expected", [
        ("*ST海航", True), ("ST中安", True), ("PT水仙", True),
        ("退市海润", True), ("宁德时代", False), ("", False), (None, False),
    ])
    def test_markers(self, name, expected):
        assert U.is_st_name(name) is expected


class TestFetchA:
    def test_maps_fields(self):
        src = _src({"stock_basic": [
            {"ts_code": "600941.SH", "name": "中国移动", "industry": "电信",
             "list_date": "20220105"},
        ]})
        rows = src.fetch_a()
        assert len(rows) == 1
        assert rows[0].market == "A" and rows[0].industry == "电信"

    def test_skips_rows_without_code(self):
        src = _src({"stock_basic": [{"name": "无代码"}, {"ts_code": "X.SH"}]})
        assert [r.ts_code for r in src.fetch_a()] == ["X.SH"]


class TestFetchHk:
    def test_basic(self):
        src = _src({"hk_basic": [{"ts_code": "06862.HK", "name": "海底捞",
                                  "list_date": "20180926"}]})
        rows = src.fetch_hk()
        assert rows[0].market == "HK" and rows[0].ts_code == "06862.HK"


class TestFetchUsPaging:
    def test_pages_until_short_page(self):
        """单页上限 6000；实测 offset 生效。翻到不足一页即停。"""
        def us_basic(limit=6000, offset=0, **kw):
            total = 6000 + 5
            codes = [f"T{i:05d}" for i in range(offset, min(offset + limit, total))]
            return [{"ts_code": c, "name": c, "classify": "EQ"} for c in codes]

        src = U.UniverseSource(lambda a, **kw: us_basic(**kw) if a == "us_basic" else [])
        rows = src.fetch_us()
        assert len(rows) == 6005

    def test_stops_on_empty_page(self):
        calls = {"n": 0}

        def fetcher(api_name, **kw):
            calls["n"] += 1
            return [] if kw.get("offset", 0) > 0 else [
                {"ts_code": f"A{i}", "classify": "EQ"} for i in range(6000)]

        rows = U.UniverseSource(fetcher).fetch_us()
        assert len(rows) == 6000
        assert calls["n"] == 2

    def test_dedups_across_pages(self):
        def fetcher(api_name, **kw):
            if kw.get("offset", 0) == 0:
                return [{"ts_code": "DUP", "classify": "EQ"}] * 6000
            return [{"ts_code": "DUP", "classify": "EQ"}, {"ts_code": "NEW", "classify": "EQ"}]

        rows = U.UniverseSource(fetcher).fetch_us()
        assert [r.ts_code for r in rows] == ["DUP", "NEW"]

    def test_respects_max_pages(self):
        def fetcher(api_name, **kw):
            return [{"ts_code": f"T{kw.get('offset',0)}{i}", "classify": "EQ"}
                    for i in range(6000)]

        src = U.UniverseSource(fetcher, max_pages=2)
        assert len(src.fetch_us()) == 12000


class TestHygiene:
    def _rows(self):
        return [
            U.UniverseRow("600941.SH", "中国移动", "A", list_date="20220105"),
            U.UniverseRow("000001.SZ", "*ST平安", "A", list_date="19910403"),
            U.UniverseRow("000002.SZ", "万科A", "A", list_date="19910129"),
            U.UniverseRow("000003.SZ", "退市公司", "A", list_date="19900101"),
            U.UniverseRow("000004.SZ", "新股", "A", list_date="20260101"),
            U.UniverseRow("000005.SZ", "已退市", "A", list_date="19900101",
                          delist_date="20250101"),
        ]

    def test_reasons(self):
        res = U.apply_hygiene(self._rows(), today=TODAY, min_listing_years=3)
        assert res.excluded["000001.SZ"] == "st"
        assert res.excluded["000003.SZ"] == "st"
        assert res.excluded["000004.SZ"] == "too_young"
        assert res.excluded["000005.SZ"] == "delisted"
        assert {r.ts_code for r in res.kept} == {"600941.SH", "000002.SZ"}

    def test_counts_tally(self):
        res = U.apply_hygiene(self._rows(), today=TODAY)
        assert res.counts["st"] == 2
        assert res.counts["delisted"] == 1
        assert res.counts["too_young"] == 1

    def test_us_classify_filter(self):
        """实测：us_basic 混入 PF 385 / GDR 34 / 韩股 —— 只留 EQ(+ADR)。"""
        rows = [
            U.UniverseRow("AAPL", "Apple", "US", classify="EQ", list_date="19801212"),
            U.UniverseRow("BAC.PF", "Pref", "US", classify="PF", list_date="19900101"),
            U.UniverseRow("SOME.GDR", "GDR", "US", classify="GDR", list_date="19900101"),
            U.UniverseRow("BABA", "Alibaba ADR", "US", classify="ADR", list_date="20140919"),
        ]
        res = U.apply_hygiene(rows, today=TODAY, market="US")
        assert {r.ts_code for r in res.kept} == {"AAPL", "BABA"}
        assert res.counts["non_equity_class"] == 2

    def test_classify_filter_not_applied_to_a_shares(self):
        rows = [U.UniverseRow("600941.SH", "中国移动", "A", classify="PF",
                              list_date="20220105")]
        assert U.apply_hygiene(rows, today=TODAY).counts == {}

    def test_min_listing_years_zero_disables_gate(self):
        rows = [U.UniverseRow("NEW", "新股", "A", list_date="20261001")]
        assert len(U.apply_hygiene(rows, today=TODAY, min_listing_years=0).kept) == 1

    def test_all_reason_codes_are_controlled(self):
        res = U.apply_hygiene(self._rows(), today=TODAY)
        from turtle_core import reasons
        for code in res.excluded.values():
            reasons.require(code)      # 未知码会抛

    def test_custom_classify_keep_list(self):
        rows = [U.UniverseRow("X", "x", "US", classify="PF", list_date="19900101")]
        res = U.apply_hygiene(rows, today=TODAY, market="US",
                             keep_classify=("EQ", "ADR", "PF"))
        assert len(res.kept) == 1


class TestClean:
    def test_multi_market(self):
        src = _src({
            "stock_basic": [{"ts_code": "600941.SH", "name": "中国移动",
                             "list_date": "20220105"}],
            "hk_basic": [{"ts_code": "06862.HK", "name": "海底捞",
                          "list_date": "20180926"}],
            "us_basic": [],
        })
        out = src.clean(("A", "HK", "US"), today=TODAY)
        assert set(out) == {"A", "HK", "US"}
        assert len(out["A"].kept) == 1 and len(out["HK"].kept) == 1
        assert src.last_stats == {"A": 1, "HK": 1, "US": 0}

    def test_unknown_market_raises(self):
        with pytest.raises(ValueError):
            _src({}).fetch(("JP",))


class TestNaNFromUpstream:
    """上游对在上市股票返回 **NaN** 的 delist_date —— 而 NaN 是真值，会误判全宇宙退市。"""

    def test_nan_delist_date_is_not_delisted(self):
        import math
        rows = [{"ts_code": "600941.SH", "name": "中国移动", "industry": "电信",
                 "list_date": "20220105", "delist_date": math.nan}]
        src = _src({"stock_basic": rows})
        got = src.fetch_a()
        assert got[0].delist_date == ""
        res = U.apply_hygiene(got, today=date(2026, 10, 14))
        assert len(res.kept) == 1, res.excluded

    def test_nan_list_date_is_empty(self):
        import math
        src = _src({"stock_basic": [{"ts_code": "X.SH", "name": "x", "list_date": math.nan}]})
        assert src.fetch_a()[0].list_date == ""

    def test_string_nan_is_empty(self):
        src = _src({"stock_basic": [{"ts_code": "X.SH", "name": "x", "delist_date": "nan"}]})
        assert src.fetch_a()[0].delist_date == ""

    def test_real_delist_date_still_excluded(self):
        rows = [{"ts_code": "X.SH", "name": "x", "list_date": "19900101",
                 "delist_date": "20250101"}]
        res = U.apply_hygiene(_src({"stock_basic": rows}).fetch_a(), today=date(2026, 10, 14))
        assert res.excluded == {"X.SH": "delisted"}

    def test_full_universe_survives_nan(self):
        import math
        rows = [{"ts_code": f"60{i:04d}.SH", "name": f"公司{i}", "industry": "制造业",
                 "list_date": "20100101", "delist_date": math.nan} for i in range(500)]
        res = U.apply_hygiene(_src({"stock_basic": rows}).fetch_a(),
                              today=date(2026, 10, 14))
        assert len(res.kept) == 500
