"""turtle_screener.slow_layer —— 从产物构建慢层快照测试。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from turtle_screener import slow_layer as SL  # noqa: E402

PACK = """> **股本口径**：总股本（百万股） = 3,228.18（末期市值 ÷ 期末价（**反推**，口径一致但依赖市值正确） ⚠️ **降级值**；来源: 期末市值 ÷ 期末价（反推，两者同期）；日期: 20260630）
"""

PACK_TRUSTED = """> **股本口径**：总股本（百万股） = 2,926.21（年报附注35「已发行股本」；来源: 年报）
"""

CLAIMS = {
    "schema_version": "1.0", "stock_code": "00696.HK",
    "claims": [
        {"claim_id": "cm17.2.c", "value": 2341.56, "unit": "百万", "time": "2025"},
        {"claim_id": "cm17.5.aa2y", "value": 2126.61, "unit": "百万", "time": "2025"},
        {"claim_id": "cm17.5.aaall", "value": 1473.17, "unit": "百万", "time": "2025"},
        {"claim_id": "cm17.5.aaexcl", "value": 1939.08, "unit": "百万", "time": "2025"},
        {"claim_id": "cm17.10.m1", "value": 50.152269, "unit": "%", "time": "2025"},
        {"claim_id": "cm17.10.m2", "value": 23.288545, "unit": "%", "time": "2025"},
        {"claim_id": "cm17.10.m3", "value": 51.036667, "unit": "%", "time": "2025"},
        {"claim_id": "cm17.10.mrec", "value": 23.29, "unit": "%", "time": "2025"},
        {"claim_id": "cm17.10.mpess", "value": 23.29, "unit": "%", "time": "2025"},
        {"claim_id": "cm17.8.mktcap", "value": 22533.63, "unit": "百万"},
        {"claim_id": "cm17.8.anchor_nla", "value": 4.17, "unit": "港元/股"},
        {"claim_id": "cm17.8.floor", "value": 6.94, "unit": "港元/股"},
    ],
}

FX = {"factor_to_reporting": 0.85753, "reporting_currency": "CNY",
      "trading_currency": "HKD", "date": "2026-10-13"}


def _product(tmp_path, *, claims=CLAIMS, fx=FX, pack=PACK, status=None, name="00696_中国民航信息网络"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    if claims is not None:
        (d / "claims.json").write_text(json.dumps(claims, ensure_ascii=False), encoding="utf-8")
    if fx is not None:
        (d / "fx.json").write_text(json.dumps(fx, ensure_ascii=False), encoding="utf-8")
    if pack is not None:
        (d / "data_pack_market.md").write_text(pack, encoding="utf-8")
    if status is not None:
        (d / "section17_status.json").write_text(json.dumps(status, ensure_ascii=False),
                                                 encoding="utf-8")
    return d


class TestLoadProduct:
    def test_happy_path(self, tmp_path):
        snap, warns = SL.load_product(_product(tmp_path))
        assert snap is not None
        assert snap.ts_code == "00696.HK"
        assert snap.c_yuan == pytest.approx(2341.56e6)
        assert snap.aa["aa_selected"] == pytest.approx(2126.61e6)
        assert snap.payout["m_pess"] == pytest.approx(23.29)
        assert snap.fy_end == "2025"
        assert snap.q_default == 20.0
        assert snap.reporting_currency == "CNY"
        assert snap.factor_to_reporting == pytest.approx(0.85753)

    def test_amounts_converted_to_yuan(self, tmp_path):
        snap, _ = SL.load_product(_product(tmp_path))
        assert snap.c_yuan == pytest.approx(2341.56e6)          # 百万 → 元
        assert snap.artifact_market_cap_yuan == pytest.approx(22533.63e6)

    def test_aa_selected_equals_aa_2y(self, tmp_path):
        """§17.9/13/14 用 aa_selected（= aa_2y，§17.5 硬门禁保证 ≥2 年）。"""
        snap, _ = SL.load_product(_product(tmp_path))
        assert snap.aa["aa_selected"] == snap.aa["aa_2y"]

    def test_floor_anchors_carried(self, tmp_path):
        snap, _ = SL.load_product(_product(tmp_path))
        assert snap.floor["anchor_nla"] == pytest.approx(4.17)
        assert snap.floor["floor"] == pytest.approx(6.94)

    def test_degraded_shares_flagged(self, tmp_path):
        """产物股本标了「反推/降级」⇒ 必须 warning，不能静默当申报值。"""
        snap, warns = SL.load_product(_product(tmp_path, pack=PACK))
        assert snap.total_shares == pytest.approx(3228.18e6)
        assert snap.shares_kind == "implied"
        assert any("D-10" in w for w in warns)

    def test_trusted_shares_not_flagged(self, tmp_path):
        snap, warns = SL.load_product(_product(tmp_path, pack=PACK_TRUSTED))
        assert snap.shares_kind == "reported"
        assert not any("D-10" in w for w in warns)

    def test_missing_shares_is_a_reason_not_zero(self, tmp_path):
        snap, _ = SL.load_product(_product(tmp_path, pack=""))
        assert snap.total_shares is None
        assert snap.reasons.get("shares") == "no_shares"

    def test_missing_aa_is_no_cashflow_not_zero(self, tmp_path):
        c = json.loads(json.dumps(CLAIMS))
        c["claims"] = [x for x in c["claims"] if not x["claim_id"].startswith("cm17.5.")]
        snap, _ = SL.load_product(_product(tmp_path, claims=c))
        assert "aa_selected" not in snap.aa
        assert snap.reasons.get("aa") == "no_cashflow"
        assert snap.required_scopes_ok is False

    def test_missing_payout_is_no_payout(self, tmp_path):
        c = json.loads(json.dumps(CLAIMS))
        c["claims"] = [x for x in c["claims"] if not x["claim_id"].startswith("cm17.10.")]
        snap, _ = SL.load_product(_product(tmp_path, claims=c))
        assert snap.reasons.get("payout") == "no_payout"

    def test_missing_c_is_no_annual(self, tmp_path):
        c = json.loads(json.dumps(CLAIMS))
        c["claims"] = [x for x in c["claims"] if x["claim_id"] != "cm17.2.c"]
        snap, _ = SL.load_product(_product(tmp_path, claims=c))
        assert snap.c_yuan is None
        assert snap.reasons.get("c") == "no_annual_3y"

    def test_no_claims_returns_none(self, tmp_path):
        snap, warns = SL.load_product(_product(tmp_path, claims=None))
        assert snap is None and warns

    def test_corrupt_claims_returns_none(self, tmp_path):
        d = _product(tmp_path)
        (d / "claims.json").write_text("{ broken", encoding="utf-8")
        snap, warns = SL.load_product(d)
        assert snap is None and "claims.json" in warns[0]

    def test_ts_code_from_dirname_when_absent(self, tmp_path):
        c = json.loads(json.dumps(CLAIMS))
        c.pop("stock_code")
        snap, _ = SL.load_product(_product(tmp_path, claims=c, name="600941_中国移动"))
        assert snap.ts_code == "600941"

    def test_explicit_ts_code_wins(self, tmp_path):
        snap, _ = SL.load_product(_product(tmp_path), ts_code="06862.HK")
        assert snap.ts_code == "06862.HK"
        assert snap.q_default == 20.0

    def test_missing_fx_warns_about_unconfirmed_currency(self, tmp_path):
        snap, warns = SL.load_product(_product(tmp_path, fx=None))
        assert snap is not None
        assert snap.reporting_currency is None
        assert any("报表币种未确认" in w for w in warns)

    def test_section17_missing_reasons_carried_when_known(self, tmp_path):
        # `no_data` 现在**已在**选股器词表内（tests/test_reason_vocab_sync.py 锁定包含关系）。
        # 这里改为用真正未知的码来验证"未知码 ⇒ 记 warning、不假装登记"这条降级路径。
        status = {"sections": {"17.7": {"status": "missing", "reason": "hk_field_gap"},
                               "17.12": {"status": "missing",
                                         "reason": "some_future_unknown_reason"}}}
        snap, warns = SL.load_product(_product(tmp_path, status=status))
        assert snap.reasons.get("s17.17.7") == "hk_field_gap"
        # 未知码不在选股器词表内 ⇒ 记 warning，不假装登记
        assert "s17.17.12" not in snap.reasons
        assert any("不在选股器受控词表内" in w for w in warns)

    def test_mapped_reasons_are_registered(self, tmp_path):
        """已映射进词表的码必须被如实登记（不再落入"未登记"警告）。"""
        status = {"sections": {"17.12": {"status": "missing", "reason": "no_data"},
                               "17.13": {"status": "missing", "reason": "negative_basis"}}}
        snap, warns = SL.load_product(_product(tmp_path, status=status))
        assert snap.reasons.get("s17.17.12") == "no_data"
        assert snap.reasons.get("s17.17.13") == "negative_basis"
        assert not any("不在选股器受控词表内" in w for w in warns)

    def test_decision_section_reason_is_propagated_not_hardcoded(self, tmp_path):
        """§17.14 未产出时，排除理由必须是它的**真实 reason**，不是硬编码的通用码。"""
        status = {"sections": {"17.14": {"status": "missing", "reason": "negative_basis"}}}
        snap, warns = SL.load_product(_product(tmp_path, status=status))
        assert snap.reasons.get("decision_section_missing") == "negative_basis"
        assert any("真实原因 = negative_basis" in w for w in warns)

    def test_ok_sections_are_ignored(self, tmp_path):
        status = {"sections": {"17.11": {"status": "ok"}}}
        snap, _ = SL.load_product(_product(tmp_path, status=status))
        assert not any(k.startswith("s17.") for k in snap.reasons)

    def test_fy_end_from_claim_time(self, tmp_path):
        snap, _ = SL.load_product(_product(tmp_path))
        assert snap.fy_end == "2025"

    def test_fy_end_missing_warns(self, tmp_path):
        c = json.loads(json.dumps(CLAIMS))
        for x in c["claims"]:
            x.pop("time", None)
        snap, warns = SL.load_product(_product(tmp_path, claims=c))
        assert snap.fy_end == ""
        assert any("财报期" in w for w in warns)

    def test_snapshot_validates(self, tmp_path):
        snap, _ = SL.load_product(_product(tmp_path))
        assert snap.validate() == []


class TestLoadPool:
    def test_batch(self, tmp_path):
        _product(tmp_path, name="00696_中国民航信息网络")
        c2 = json.loads(json.dumps(CLAIMS)); c2["stock_code"] = "06862.HK"
        _product(tmp_path, claims=c2, name="06862_海底捞")
        snaps, warns = SL.load_pool([tmp_path / "00696_中国民航信息网络",
                                     tmp_path / "06862_海底捞"])
        assert {s.ts_code for s in snaps} == {"00696.HK", "06862.HK"}

    def test_bad_dir_does_not_abort_batch(self, tmp_path):
        _product(tmp_path, name="00696_中国民航信息网络")
        snaps, warns = SL.load_pool([tmp_path / "00696_中国民航信息网络",
                                     tmp_path / "nope"])
        assert len(snaps) == 1
        assert any("nope" in w for w in warns)


class TestCurrencyInference:
    """后缀能确定的币种必须补上；**港股必须仍然 fail-closed**。

    首版把 A 股也判成 `fx_missing`（因为 A 股没有 fx.json），
    使 A 股产物全部无法进入排名 —— 而 A 股根本不存在 P0-2 那类币种歧义。
    """

    def test_a_share_infers_cny_and_is_not_cross_currency(self, tmp_path):
        c = json.loads(json.dumps(CLAIMS)); c["stock_code"] = "601166.SH"
        snap, warns = SL.load_product(_product(tmp_path, claims=c, fx=None,
                                               name="601166_兴业银行"))
        assert snap.reporting_currency == "CNY"
        assert snap.trading_currency == "CNY"
        assert any("推断为 CNY" in w for w in warns)

    def test_us_infers_usd(self, tmp_path):
        c = json.loads(json.dumps(CLAIMS)); c["stock_code"] = "AAPL.US"
        snap, _ = SL.load_product(_product(tmp_path, claims=c, fx=None, name="AAPL_苹果"))
        assert snap.reporting_currency == "USD"
        assert snap.trading_currency == "USD"

    def test_hk_without_declaration_stays_unconfirmed(self, tmp_path):
        """港股**不得**推断：报表币种可能是 CNY 而交易是 HKD（P0-2）。"""
        snap, warns = SL.load_product(_product(tmp_path, fx=None, name="06862_海底捞"))
        assert snap.reporting_currency is None
        assert snap.trading_currency == "HKD"
        assert any("港股" in w and "未确认" in w for w in warns)

    def test_hk_with_declaration_uses_it(self, tmp_path):
        snap, _ = SL.load_product(_product(tmp_path))
        assert snap.reporting_currency == "CNY"
        assert snap.trading_currency == "HKD"
        assert snap.factor_to_reporting == pytest.approx(0.85753)

    def test_a_share_with_declared_fx_keeps_it(self, tmp_path):
        """A 股若显式给了 fx.json，以声明为准（不覆盖）。"""
        fx = dict(FX); fx["reporting_currency"] = "CNY"
        c = json.loads(json.dumps(CLAIMS)); c["stock_code"] = "601166.SH"
        snap, _ = SL.load_product(_product(tmp_path, claims=c, fx=fx, name="601166_兴业银行"))
        assert snap.reporting_currency == "CNY"
        assert snap.factor_to_reporting == pytest.approx(0.85753)


class TestEndToEndMarketCap:
    """A 股：股本与市值同属 daily_basic 同日口径 ⇒ 自洽市值应当可算（不再 fx_missing）。"""

    def test_a_share_market_cap_is_computable(self, tmp_path):
        from turtle_screener import engine as E
        from turtle_screener.prices import PriceQuote
        c = json.loads(json.dumps(CLAIMS)); c["stock_code"] = "601166.SH"
        snap, _ = SL.load_product(_product(tmp_path, claims=c, fx=None,
                                           name="601166_兴业银行"))
        cap, why = E.resolve_market_cap(snap.total_shares, 18.15,
                                        snap.factor_to_reporting,
                                        trading_currency="CNY",
                                        reporting_currency=snap.reporting_currency)
        assert why is None and cap is not None and cap > 0

    def test_hk_without_fx_still_fails_closed(self, tmp_path):
        from turtle_screener import engine as E
        c = json.loads(json.dumps(CLAIMS))
        snap, _ = SL.load_product(_product(tmp_path, claims=c, fx=None, name="06862_海底捞"))
        cap, why = E.resolve_market_cap(snap.total_shares, 9.3, snap.factor_to_reporting,
                                        trading_currency="HKD",
                                        reporting_currency=snap.reporting_currency)
        assert cap is None and why == "fx_missing"
