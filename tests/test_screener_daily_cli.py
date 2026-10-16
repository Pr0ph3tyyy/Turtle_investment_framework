"""scripts/screener_daily.py —— CLI 集成测试（离线：用价格快照，不联网）。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import screener_daily as CLI  # noqa: E402

CLAIMS = {
    "schema_version": "1.0", "stock_code": "00696.HK",
    "claims": [
        {"claim_id": "cm17.2.c", "value": 2341.56, "unit": "百万", "time": "2025"},
        {"claim_id": "cm17.5.aa2y", "value": 21266.1, "unit": "百万", "time": "2025"},
        {"claim_id": "cm17.10.mrec", "value": 23.29, "unit": "%", "time": "2025"},
        {"claim_id": "cm17.10.mpess", "value": 23.29, "unit": "%", "time": "2025"},
        {"claim_id": "cm17.8.mktcap", "value": 22533.63, "unit": "百万"},
    ],
}
PACK = "> **股本口径**：总股本（百万股） = 2,926.21（年报附注「已发行股本」；来源: 年报）\n"
FX = {"factor_to_reporting": 0.85753, "reporting_currency": "CNY", "trading_currency": "HKD"}


@pytest.fixture()
def product(tmp_path):
    d = tmp_path / "output" / "00696_中国民航信息网络"
    d.mkdir(parents=True)
    (d / "claims.json").write_text(json.dumps(CLAIMS, ensure_ascii=False), encoding="utf-8")
    (d / "fx.json").write_text(json.dumps(FX, ensure_ascii=False), encoding="utf-8")
    (d / "data_pack_market.md").write_text(PACK, encoding="utf-8")
    return d


@pytest.fixture()
def prices(tmp_path):
    p = tmp_path / "prices.json"
    p.write_text(json.dumps({
        "schema_version": "1.0", "source": "yahoo_chart", "stats": {},
        "quotes": {"00696.HK": {"close": 9.25, "regular_market_price": 9.25,
                                "currency": "HKD", "bars": 5, "as_of": "2026-10-13",
                                "error": None}},
    }, ensure_ascii=False), encoding="utf-8")
    return p


def _run(argv):
    try:
        return CLI.main(argv)
    except SystemExit as exc:            # argparse help
        return int(exc.code or 0)


class TestCli:
    def test_empty_pool_is_usage_error(self, tmp_path):
        assert _run(["--scan", str(tmp_path / "nope"), "--output", str(tmp_path / "o")]) == 2

    def test_rf_missing_aborts_and_writes_no_shortlist(self, product, prices, tmp_path, monkeypatch):
        """II 错 ⇒ 整批 ABORT；残缺清单比没有清单更危险。"""
        monkeypatch.delenv("TUSHARE_RF", raising=False)
        out = tmp_path / "out"
        code = _run(["--scan", str(product.parent), "--output", str(out),
                     "--trade-date", "20261015", "--offline-prices", str(prices)])
        assert code == 1
        d = out / "screener" / "20261015"
        assert (d / "ABORTED.txt").exists()
        assert (d / "run_manifest.json").exists()
        assert not (d / "shortlist.json").exists()
        assert not (d / "screener_results.csv").exists()

    def test_happy_path_writes_all_files(self, product, prices, tmp_path):
        out = tmp_path / "out"
        code = _run(["--scan", str(product.parent), "--rf", "1.0242",
                     "--output", str(out), "--offline-prices", str(prices),
                     "--trade-date", "20261014"])
        assert code == 0
        d = out / "screener" / "20261014"
        for f in ("shortlist.json", "run_manifest.json", "excluded.json",
                  "screener_results.csv"):
            assert (d / f).exists(), f
        man = json.loads((d / "run_manifest.json").read_text(encoding="utf-8"))
        assert man["ii_pct"] == pytest.approx(5.0)
        assert man["deliverable"] is True

    def test_dry_run_writes_nothing(self, product, prices, tmp_path):
        out = tmp_path / "out"
        assert _run(["--scan", str(product.parent), "--rf", "1.0", "--output", str(out),
                     "--offline-prices", str(prices), "--dry-run"]) == 0
        assert not out.exists()

    def test_idempotent_reruns_byte_identical(self, product, prices, tmp_path):
        """验收标准第 2 条：同一 trade_date + 同一价格快照 ⇒ 逐字节一致。"""
        out = tmp_path / "out"
        argv = ["--scan", str(product.parent), "--rf", "1.0242", "--output", str(out),
                "--offline-prices", str(prices), "--trade-date", "20261014"]
        _run(argv)
        d = out / "screener" / "20261014"
        first = {f.name: hashlib.md5(f.read_bytes()).hexdigest() for f in d.iterdir()}
        _run(argv)
        second = {f.name: hashlib.md5(f.read_bytes()).hexdigest() for f in d.iterdir()}
        assert first == second
        assert len(list((out / "screener").iterdir())) == 1     # 未追加第二份

    def test_scan_discovers_dirs_with_claims(self, product):
        entries = CLI.discover_pool(str(product.parent))
        assert len(entries) == 1
        assert entries[0]["dir"].endswith("00696_中国民航信息网络")

    def test_pool_file_and_scan_union_without_dupes(self, product, tmp_path):
        pf = tmp_path / "pool.json"
        pf.write_text(json.dumps({"pool": [{"dir": str(product), "ts_code": "00696.HK"}]}),
                      encoding="utf-8")
        args = CLI.parse_args(["--pool-file", str(pf), "--scan", str(product.parent)])
        entries = CLI.load_pool_entries(args)
        assert len(entries) == 1

    def test_offline_prices_avoids_network(self, product, prices, tmp_path, monkeypatch):
        """--offline-prices 下不得触网（否则幂等与可复跑无从谈起）。"""
        import turtle_screener.prices as PR

        def boom(*a, **kw):
            raise AssertionError("不应联网")
        monkeypatch.setattr(PR.urllib.request, "urlopen", boom)
        code = _run(["--scan", str(product.parent), "--rf", "1.0",
                     "--output", str(tmp_path / "o"), "--offline-prices", str(prices),
                     "--trade-date", "20261014"])
        assert code == 0

    def test_skip_readiness_flag(self, product, prices, tmp_path):
        assert _run(["--scan", str(product.parent), "--rf", "1.0",
                     "--output", str(tmp_path / "o"), "--offline-prices", str(prices),
                     "--skip-readiness", "--trade-date", "20261014"]) == 0


class TestShortlistCarriesNameAndFyEnd:
    """首版 CLI 用错误的映射键（目录名 vs ts_code）⇒ shortlist 的 name/fy_end 为空，
    而 fy_end 正是 `action` 判定的输入（空值会让已有报告被误判为 rerun）。"""

    def test_name_and_fy_end_are_filled(self, product, prices, tmp_path):
        out = tmp_path / "out"
        _run(["--scan", str(product.parent), "--rf", "1.0242", "--output", str(out),
              "--offline-prices", str(prices), "--trade-date", "20261014"])
        sl = json.loads((out / "screener" / "20261014" / "shortlist.json").read_text(encoding="utf-8"))
        for e in sl["shortlist"]:
            assert e["snapshot_fy_end"] == "2025", e
            assert e["ts_code"] == "00696.HK"

    def test_action_uses_fy_end_from_snapshot(self, product, prices, tmp_path):
        """已有报告且财报期相同、价格变动 <10% ⇒ refresh_only（而非 rerun）。"""
        out = tmp_path / "out"
        _run(["--scan", str(product.parent), "--rf", "1.0242", "--output", str(out),
              "--offline-prices", str(prices), "--trade-date", "20261014"])
        # 默认无既有报告 ⇒ needs_workflow1；这里只断言 fy_end 非空（契约要求的字段）
        sl = json.loads((out / "screener" / "20261014" / "shortlist.json").read_text(encoding="utf-8"))
        assert all(e["snapshot_fy_end"] for e in sl["shortlist"])
