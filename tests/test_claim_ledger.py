"""Tests for the §17 claim ledger and PCN-style numeric verification.

Locks down known_issues.md P2-20 改动 1/2/3:
- claims carry a **unit from an enum** and a `null` value requires a reason
- the decision cells are rendered as KV with claim ids (better numeric retrieval
  than wide tables), and
- verification is **fail-closed**: only a mechanical match counts, so a misquoted,
  mis-united or fabricated number cannot pass.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from claim_ledger import (  # noqa: E402
    ClaimError,
    ClaimLedger,
    UNIT_ENUM,
    extract_tokens,
    load_claims,
    parse_quantity,
    verify_report,
    write_claims,
)


@pytest.fixture()
def ledger():
    led = ClaimLedger("06862.HK")
    led.add("cm17.11.aa2y.m3.gg", "精算穿透回报率 GG（AA_2y）", 2.89, "%",
            time="2024", basis="AA_2y=8710, M=58.08%, Q=0%, II=5.31%",
            policy="round:2", section="17.11")
    led.add("cm17.11.aa2y.m3.tbp", "目标买入价（AA_2y）", 14.96, "元/股",
            time="2024", policy="round:2", section="17.11")
    led.add("cm17.2.mktcap", "总市值", 46129.05, "百万",
            policy="alias", section="17.2")
    led.add("cm17.2.mktcap_tol", "总市值（容忍降精度）", 46129.05, "百万",
            policy="alias:0.1", section="17.2")
    return led


class TestParseQuantity:
    @pytest.mark.parametrize("text,expected", [
        ("4.63%", (4.63, "%")),
        ("2.89 %", (2.89, "%")),
        ("65,160.06 百万港元", (65160.06, "百万港元")),
        ("1.23亿", (1.23, "亿")),
        ("456 万元", (456.0, "万元")),
        ("-2.72", (-2.72, None)),
    ])
    def test_parses(self, text, expected):
        assert parse_quantity(text) == expected

    def test_longer_unit_wins(self):
        """'港元/股' must not be truncated to '港元' by the alternation order."""
        assert parse_quantity("99.31 港元/股") == (99.31, "港元/股")
        assert parse_quantity("27.50 元/股") == (27.5, "元/股")

    def test_no_number(self):
        assert parse_quantity("无数字") == (None, None)


class TestClaimContract:
    def test_unit_must_be_in_enum(self, ledger):
        with pytest.raises(ClaimError, match="不在 UNIT_ENUM"):
            ledger.add("bad.unit", "x", 1.0, "百万亿")

    def test_null_value_requires_note(self, ledger):
        """null ≠ 0 — an unavailable value must say why."""
        with pytest.raises(ClaimError, match="必须给出 note"):
            ledger.add("bad.null", "x", None, "%")
        claim = ledger.add("ok.null", "x", None, "%", note="yc_cb 无权限")
        assert claim.display == "—"

    def test_policy_must_be_known(self, ledger):
        with pytest.raises(ClaimError, match="不在"):
            ledger.add("bad.policy", "x", 1.0, "%", policy="vibes")

    def test_duplicate_id_same_indicator_replaces(self, ledger):
        """Recomputing a section replaces its claim rather than colliding."""
        ledger.add("cm17.11.aa2y.m3.gg", "精算穿透回报率 GG（AA_2y）", 3.10, "%")
        assert ledger.get("cm17.11.aa2y.m3.gg").value == 3.10
        assert len(ledger.claims()) == 4          # fixture has 4; replace adds none

    def test_duplicate_id_different_indicator_is_an_error(self, ledger):
        """Two different quantities must not share one claim id."""
        with pytest.raises(ClaimError, match="不能改指"):
            ledger.add("cm17.11.aa2y.m3.gg", "完全不同的指标", 1.0, "%")


class TestVerification:
    def test_round_policy_accepts_exact_display(self, ledger):
        assert ledger.verify("cm17.11.aa2y.m3.gg", "2.89%")[0] is True

    def test_round_policy_rejects_tampering(self, ledger):
        """The headline property: a wrong number cannot pass."""
        ok, detail = ledger.verify("cm17.11.aa2y.m3.gg", "7.20%")
        assert ok is False
        assert "!=" in detail

    def test_round_policy_rejects_lost_precision(self, ledger):
        assert ledger.verify("cm17.11.aa2y.m3.gg", "2.9%")[0] is False

    def test_unit_mismatch_fails(self, ledger):
        ok, detail = ledger.verify("cm17.11.aa2y.m3.gg", "2.89 港元")
        assert ok is False
        assert "单位不符" in detail

    def test_unknown_claim_fails(self, ledger):
        ok, detail = ledger.verify("cm17.does.not.exist", "1.0%")
        assert ok is False
        assert "未知声明" in detail

    def test_null_claim_cannot_be_quoted_as_a_number(self, ledger):
        ledger.add("null.claim", "无风险利率", None, "%", note="无权限")
        ok, detail = ledger.verify("null.claim", "1.688%")
        assert ok is False
        assert "null" in detail

    def test_alias_converts_sanctioned_units(self, ledger):
        """百万 ↔ 亿: the `_HK_MKT_CAP_RAW_FLOOR` class of unit bug."""
        assert ledger.verify("cm17.2.mktcap", "46,129.05 百万")[0] is True
        assert ledger.verify("cm17.2.mktcap", "46,129.05 百万港元")[0] is True

    def test_alias_is_strict_about_precision_loss(self, ledger):
        """46,129.05 百万 written as 461.29 亿 is a *precision loss* → must fail."""
        ok, detail = ledger.verify("cm17.2.mktcap", "461.29 亿")
        assert ok is False
        assert "降精度" in detail

    def test_alias_with_explicit_tolerance_accepts(self, ledger):
        assert ledger.verify("cm17.2.mktcap_tol", "461.29 亿")[0] is True

    def test_tolerance_policy_requires_a_qualifier(self, ledger):
        ledger.add("tol.claim", "约值", 100.0, "百万", policy="tolerance:2,0")
        assert ledger.verify("tol.claim", "约 101 百万")[0] is True
        ok, detail = ledger.verify("tol.claim", "101 百万")
        assert ok is False
        assert "限定词" in detail


class TestUnitCompatibility:
    """`%` / `pct` / `pct点` differ in meaning but not in dimension.

    Live regression: a claim declared `pct点` renders as `0.78%` (the pack shows
    all of them with a % sign), so a report copying the *displayed* value was
    rejected — a false failure that would train the agent to mistrust the gate.
    """

    def test_percent_family_is_compatible(self):
        from claim_ledger import units_compatible
        assert units_compatible("%", "pct")
        assert units_compatible("%", "pct点")
        assert units_compatible(None, "pct点")     # no suffix → no conflict

    def test_different_scales_are_not_compatible(self):
        """百万 ↔ 亿 needs `alias`: that is a scale change, not a label variant."""
        from claim_ledger import units_compatible
        assert not units_compatible("百万", "亿")
        assert not units_compatible("港元", "%")

    def test_pct_point_claim_accepts_percent_quote(self):
        led = ClaimLedger("x")
        led.add("v", "GG−II", 0.78, "pct点", policy="round:2")
        assert led.verify("v", "0.78%")[0] is True
        assert led.verify("v", "0.78 pct")[0] is True

    def test_still_rejects_a_genuinely_wrong_unit(self):
        led = ClaimLedger("x")
        led.add("v", "GG−II", 0.78, "pct点", policy="round:2")
        ok, detail = led.verify("v", "0.78 港元")
        assert ok is False
        assert "单位不符" in detail


class TestKvRendering:
    def test_renders_claim_bound_kv(self, ledger):
        out = ledger.render_kv(["cm17.11.aa2y.m3.gg"], title="决策单元格")
        assert "claim: cm17.11.aa2y.m3.gg" in out
        assert "unit: %" in out
        assert "policy: round:2" in out
        assert "value: 2.89%" in out
        assert "basis:" in out

    def test_null_value_shows_the_note(self, ledger):
        ledger.add("n.claim", "不可得", None, "%", note="yc_cb 无权限")
        out = ledger.render_kv(["n.claim"])
        assert "value: null" in out
        assert "yc_cb 无权限" in out


class TestReportVerification:
    REPORT = (
        "精算穿透回报率为 <claim id=\"cm17.11.aa2y.m3.gg\">2.89%</claim>，"
        "目标价 <claim id=\"cm17.11.aa2y.m3.tbp\">14.96</claim>，"
        "但这里写错成 <claim id=\"cm17.11.aa2y.m3.gg\">7.20%</claim>，"
        "还有一个裸数字 3.14。"
    )

    def test_extracts_tokens(self, ledger):
        tokens = extract_tokens(self.REPORT)
        assert len(tokens) == 3
        assert tokens[0]["id"] == "cm17.11.aa2y.m3.gg"

    def test_partitions_verified_and_failed(self, ledger):
        result = verify_report(self.REPORT, ledger)
        assert result["tokens"] == 3
        assert len(result["verified"]) == 2
        assert len(result["failed"]) == 1
        assert result["failed"][0]["quoted"] == "7.20%"

    def test_no_tokens_means_nothing_is_trusted(self, ledger):
        result = verify_report("这里全是裸数字 1.0 2.0 3.0", ledger)
        assert result["tokens"] == 0
        assert result["verified"] == []


class TestSerialisation:
    def test_roundtrip(self, ledger, tmp_path):
        out = write_claims(tmp_path / "claims.json", ledger,
                           units={"reporting_currency": "CNY"})
        loaded = load_claims(out)
        assert len(loaded) == len(ledger)
        assert loaded.get("cm17.11.aa2y.m3.gg").value == 2.89
        assert loaded.verify("cm17.11.aa2y.m3.gg", "2.89%")[0] is True

    def test_snapshot_carries_schema_version_and_units(self, ledger):
        snap = ledger.snapshot({"reporting_currency": "CNY"})
        assert snap["schema_version"] == "1.0"
        assert snap["stock_code"] == "06862.HK"
        assert snap["units"]["reporting_currency"] == "CNY"
        assert len(snap["claims"]) == len(ledger)
