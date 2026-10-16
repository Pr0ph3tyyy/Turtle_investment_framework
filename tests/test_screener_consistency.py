"""scripts/screener_consistency.py —— 验收标准① 的检查器测试。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import screener_consistency as SC  # noqa: E402
from turtle_core.decision import penetration_return, target_buy_price  # noqa: E402

# 00696 实测参数（D-10 的真实形态）
FX = 0.85753
CLOSE = 8.98
II = 5.0
M_PESS = 23.29
Q = 20.0
AA_MM = 2126.61
SHARES_DEG = 3228.18e6      # 产物里的降级股本
ART_CAP_MM = 22533.63       # 产物 §17.8 市值（百万元）
SELF_CAP = SHARES_DEG * CLOSE * FX / 1e6      # ≈ 24858.99 百万


def _product(tmp_path, *, shares_line, art_cap_mm=ART_CAP_MM, k=0.80,
             with_claims=True, claims_extra=None, gg_override=None):
    d = tmp_path / "00696_中国民航信息网络"
    d.mkdir(parents=True, exist_ok=True)
    cap_yuan = art_cap_mm * 1e6
    gg = penetration_return(AA_MM * 1e6 * k, M_PESS, Q, 0.0, cap_yuan)
    tbp = target_buy_price(CLOSE, gg, II)
    gg_value = round(gg, 2) if gg_override is None else gg_override
    claims = [
        {"claim_id": "cm17.2.ii", "value": II, "unit": "%", "time": "2025"},
        {"claim_id": "cm17.2.c", "value": 2341.56, "unit": "百万", "time": "2025"},
        {"claim_id": "cm17.5.aa2y", "value": AA_MM, "unit": "百万", "time": "2025"},
        {"claim_id": "cm17.10.mpess", "value": M_PESS, "unit": "%", "time": "2025"},
        {"claim_id": "cm17.10.mrec", "value": M_PESS, "unit": "%", "time": "2025"},
        {"claim_id": "cm17.8.mktcap", "value": art_cap_mm, "unit": "百万"},
        {"claim_id": "cm17.14.k080.ggpess", "value": gg_value, "unit": "%"},
        {"claim_id": "cm17.14.k080.tbp", "value": round(tbp, 2), "unit": "港元/股"},
    ]
    claims.extend(claims_extra or [])
    payload = {"schema_version": "1.0", "stock_code": "00696.HK", "claims": claims}
    if with_claims:
        (d / "claims.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (d / "fx.json").write_text(json.dumps({
        "factor_to_reporting": FX, "reporting_currency": "CNY",
        "trading_currency": "HKD"}), encoding="utf-8")
    (d / "data_pack_market.md").write_text(
        f"| 当前价格 (HKD) | {CLOSE} |\n{shares_line}\n", encoding="utf-8")
    return d


DEGRADED = ("> **股本口径**：总股本（百万股） = 3,228.18"
            "（期末市值 ÷ 期末价（**反推**） ⚠️ **降级值**；来源: 期末市值 ÷ 期末价）")
# 与产物市值自洽的股本：24858.99 百万 / (8.98 × 0.85753) = 3228.18 —— 要让二者自洽，
# 需让 shares×close×fx == art_cap，故把产物市值改成自洽值
ART_CAP_CONSISTENT = SHARES_DEG * CLOSE * FX / 1e6


class TestParseClose:
    def test_hk_style(self):
        assert SC.parse_close("| 当前价格 (HKD) | 8.98 |") == pytest.approx(8.98)

    def test_plain_style(self):
        assert SC.parse_close("| 当前价格 | 12.34 |") == pytest.approx(12.34)

    def test_missing(self):
        assert SC.parse_close("no price here") is None


class TestConsistency:
    def test_d10_is_attributed_not_called_a_formula_bug(self, tmp_path, capsys):
        """00696 的真实形态：(b) 全对 ⇒ 公式同源；(a) 不等 ⇒ 纯 D-10。"""
        d = _product(tmp_path, shares_line=DEGRADED)
        code = SC.compare(str(d))
        out = capsys.readouterr().out
        assert code == 1
        assert "(b) 产物市值" in out
        assert "D-10 归因" in out
        assert "公式与报告同源" in out
        assert "不是" not in out.split("D-10 归因")[0][-20:]  # 不应把它当公式错

    def test_pass_when_market_cap_identity_holds(self, tmp_path, capsys):
        """市值恒等式成立时（自洽市值 == 产物市值）应判 PASS。"""
        d = _product(tmp_path, shares_line=DEGRADED, art_cap_mm=ART_CAP_CONSISTENT)
        code = SC.compare(str(d))
        out = capsys.readouterr().out
        assert code == 0
        assert "PASS" in out

    def test_formula_mismatch_is_reported_as_such(self, tmp_path, capsys):
        """篡改账本 GG_pess ⇒ (b) 也不过 ⇒ 判为公式/输入不一致，不能甩给 D-10。"""
        d = _product(tmp_path, shares_line=DEGRADED, gg_override=9.99)
        code = SC.compare(str(d))
        out = capsys.readouterr().out
        assert code == 1
        assert "公式或输入不一致" in out

    def test_missing_inputs_is_unverifiable_not_pass(self, tmp_path, capsys):
        """账本缺 AA ⇒ UNVERIFIABLE（exit 2），**不凑数**。"""
        d = tmp_path / "00696_中国民航信息网络"
        d.mkdir(parents=True)
        (d / "claims.json").write_text(json.dumps({
            "stock_code": "00696.HK",
            "claims": [{"claim_id": "cm17.2.ii", "value": 5.0, "unit": "%"}]}),
            encoding="utf-8")
        (d / "data_pack_market.md").write_text("| 当前价格 (HKD) | 8.98 |", encoding="utf-8")
        code = SC.compare(str(d))
        out = capsys.readouterr().out
        assert code == 2
        assert "UNVERIFIABLE" in out

    def test_missing_claims_file_is_usage_error(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        assert SC.compare(str(d)) == 2

    def test_close_override_is_used(self, tmp_path, capsys):
        d = _product(tmp_path, shares_line=DEGRADED)
        SC.compare(str(d), close_override=8.98)
        assert "现价: 8.98" in capsys.readouterr().out

    def test_adopted_k_selects_the_right_claim(self, tmp_path, capsys):
        """--adopted-k 0.7 时读 cm17.14.k070.*；账本没有 ⇒ 该侧为 None ⇒ FAIL。"""
        d = _product(tmp_path, shares_line=DEGRADED)
        code = SC.compare(str(d), adopted_k=0.7)
        assert code == 1
