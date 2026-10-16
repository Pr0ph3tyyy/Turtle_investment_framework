"""turtle_screener.prices —— Yahoo chart 价格源测试（全部离线，注入假 fetcher）。"""

from __future__ import annotations

import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from turtle_screener import prices as PR  # noqa: E402


def _chart(closes, currency="HKD", rmp=None, ts=(1_760_000_000,)):
    return {"chart": {"result": [{
        "meta": {"currency": currency, "regularMarketPrice": rmp},
        "timestamp": list(ts),
        "indicators": {"quote": [{"close": list(closes)}]},
    }], "error": None}}


class TestSymbolMapping:
    @pytest.mark.parametrize("ts_code,expected", [
        ("600941.SH", "600941.SS"),      # Yahoo 用 .SS，.SH 会 404
        ("000921.SZ", "000921.SZ"),
        ("06862.HK", "6862.HK"),         # 5 位补零 → 4 位
        ("00700.HK", "0700.HK"),
        ("AAPL.US", "AAPL"),             # 美股裸 ticker
        ("AAPL", "AAPL"),
    ])
    def test_mapping(self, ts_code, expected):
        assert PR.to_yahoo_symbol(ts_code) == expected


class TestParseChart:
    def test_takes_last_non_null_close(self):
        """港股当日未完成 bar 的 close 是 None —— 直接取 [-1] 会得到 None。"""
        close, rmp, ccy, bars, _ = PR.parse_chart(_chart([1.0, 2.0, None], rmp=None))
        assert close == pytest.approx(2.0)
        assert bars == 3
        assert ccy == "HKD"

    def test_all_null_closes_yields_none(self):
        close, rmp, _c, _b, _a = PR.parse_chart(_chart([None, None]))
        assert close is None and rmp is None

    def test_regular_market_price_is_preserved(self):
        close, rmp, *_ = PR.parse_chart(_chart([1.0, None], rmp=1.25))
        assert close == pytest.approx(1.0)
        assert rmp == pytest.approx(1.25)

    def test_empty_result_raises(self):
        with pytest.raises(ValueError):
            PR.parse_chart({"chart": {"result": [], "error": {"code": "Not Found"}}})

    def test_as_of_from_last_timestamp(self):
        *_rest, as_of = PR.parse_chart(_chart([1.0], ts=(1_760_000_000,)))
        assert as_of and len(as_of) == 10


class TestPriceQuote:
    def test_prefers_regular_market_price(self):
        q = PR.PriceQuote(ts_code="X.HK", close=424.80, regular_market_price=425.2)
        assert q.price == pytest.approx(425.2)

    def test_falls_back_to_close(self):
        q = PR.PriceQuote(ts_code="X.HK", close=424.80)
        assert q.price == pytest.approx(424.80)

    def test_error_quote_is_not_ok(self):
        q = PR.PriceQuote(ts_code="X.HK", error="HTTP 404")
        assert q.ok is False and q.price is None


class TestClient:
    def _client(self, fn, **kw):
        return PR.YahooChartClient(fetcher=fn, retries=kw.pop("retries", 0),
                                   backoff=0.0, **kw)

    def test_happy_path(self):
        c = self._client(lambda s, r, i: _chart([1.0, 2.0], rmp=2.1))
        q = c.quote("06862.HK")
        assert q.ok and q.price == pytest.approx(2.1)
        assert q.currency == "HKD"

    def test_http_error_is_recorded_not_raised(self):
        def boom(s, r, i):
            raise urllib.error.HTTPError(s, 404, "Not Found", {}, None)
        q = self._client(boom).quote("600941.SH")
        assert q.ok is False and "HTTP 404" in q.error

    def test_url_error_is_recorded(self):
        def boom(s, r, i):
            raise urllib.error.URLError("dns fail")
        q = self._client(boom).quote("AAPL.US")
        assert q.ok is False and "URLError" in q.error

    def test_parse_error_is_recorded(self):
        def boom(s, r, i):
            raise json.JSONDecodeError("bad", "", 0)
        q = self._client(boom).quote("AAPL.US")
        assert q.ok is False and "parse" in q.error

    def test_empty_price_is_flagged(self):
        q = self._client(lambda s, r, i: _chart([None, None])).quote("AAPL.US")
        assert q.ok is False and q.error == "empty_price"

    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(s, r, i):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.URLError("transient")
            return _chart([1.0])
        c = PR.YahooChartClient(fetcher=flaky, retries=3, backoff=0.0)
        q = c.quote("AAPL.US")
        assert q.ok and calls["n"] == 3

    def test_retries_exhausted_records_last_error(self):
        calls = {"n": 0}

        def always(s, r, i):
            calls["n"] += 1
            raise urllib.error.URLError("down")
        c = PR.YahooChartClient(fetcher=always, retries=2, backoff=0.0)
        q = c.quote("AAPL.US")
        assert q.ok is False and calls["n"] == 3

    def test_batch_keeps_failures(self):
        """失败股必须留在结果里带 error —— 不抛、不静默丢（调用方按 reason code 处置）。"""
        def fn(s, r, i):
            if s.startswith("6862"):
                raise urllib.error.URLError("down")
            return _chart([3.0])
        c = self._client(fn, workers=4)
        out = c.quotes(["06862.HK", "0700.HK", "AAPL.US"])
        assert set(out) == {"06862.HK", "0700.HK", "AAPL.US"}
        assert out["06862.HK"].ok is False
        assert out["0700.HK"].ok is True
        assert c.last_stats == {"requested": 3, "ok": 2, "failed": 1}

    def test_batch_dedups_and_handles_empty(self):
        seen = []
        c = self._client(lambda s, r, i: (seen.append(s), _chart([1.0]))[1])
        c.quotes(["AAPL.US", "AAPL.US"])
        assert seen == ["AAPL"]
        assert c.quotes([]) == {}
        assert c.last_stats["requested"] == 0

    def test_batch_preserves_input_order_mapping(self):
        c = self._client(lambda s, r, i: _chart([2.0]), workers=2)
        out = c.quotes(["600941.SH", "000921.SZ"])
        assert out["600941.SH"].price == pytest.approx(2.0)


class TestSnapshotRoundTrip:
    def test_round_trip_gives_idempotent_reruns(self):
        """价格必须落当日快照，否则重跑会因实时报价漂移而不幂等。"""
        c = PR.YahooChartClient(fetcher=lambda s, r, i: _chart([1.0], rmp=1.1),
                                retries=0, backoff=0.0)
        payload = c.snapshot_payload(["06862.HK", "AAPL.US"])
        assert payload["stats"]["ok"] == 2
        restored = PR.YahooChartClient.from_snapshot(payload)
        assert set(restored) == {"06862.HK", "AAPL.US"}
        assert restored["06862.HK"].price == pytest.approx(1.1)
        assert restored["AAPL.US"].ok

    def test_snapshot_json_serializable(self):
        c = PR.YahooChartClient(fetcher=lambda s, r, i: _chart([1.0]),
                                retries=0, backoff=0.0)
        json.dumps(c.snapshot_payload(["AAPL.US"]))

    def test_snapshot_keeps_errors(self):
        def fn(s, r, i):
            raise urllib.error.URLError("down")
        c = PR.YahooChartClient(fetcher=fn, retries=0, backoff=0.0)
        payload = c.snapshot_payload(["AAPL.US"])
        assert payload["quotes"]["AAPL.US"]["error"]
        assert PR.YahooChartClient.from_snapshot(payload)["AAPL.US"].ok is False


class TestNoNetworkByDefault:
    def test_default_fetch_hits_the_chart_endpoint(self, monkeypatch):
        """默认抓取器必须打 query1 chart 端点（而不是 yfinance）。"""
        captured = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(_chart([1.0])).encode()

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _Resp()

        monkeypatch.setattr(PR.urllib.request, "urlopen", fake_urlopen)
        q = PR.YahooChartClient(retries=0, backoff=0.0).quote("06862.HK")
        assert q.ok
        assert "query1.finance.yahoo.com/v8/finance/chart/6862.HK" in captured["url"]
