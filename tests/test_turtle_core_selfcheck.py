"""turtle_core_selfcheck —— 用合成账本锁定自检工具本身。

真实产物 `output/00696_中国民航信息网络/claims.json` 早于当前代码，
不能当 golden（见 turtle_core_extraction_findings.md 附录）；这里用**构造**的
一致账本，保证"复现 ⇒ 退出 0、篡改 ⇒ 退出 1"这两件事被机械钉住。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import turtle_core_selfcheck as SC  # noqa: E402
from turtle_core import decision as D  # noqa: E402
from turtle_core import pessimistic as P  # noqa: E402

TS = "00696.HK"
Q = D.Q_DEFAULT["HK"]           # 20.0
II = 5.0
MKT = 22533.63                  # 百万，报表币种
C = 2341.56                      # 百万
AA_2Y, AA_ALL, AA_EXCL = 2126.61, 1473.17, 1939.08
M1, M2, M3 = 50.152269, 23.288545, 51.036667
M_PESS = 23.29


def _claim(cid, value, unit="%"):
    return {"claim_id": cid, "value": value, "unit": unit, "indicator": cid,
            "entity": TS, "time": "2025", "basis": "synthetic", "policy": "round:2"}


def _payload() -> dict:
    claims = [
        _claim("cm17.8.mktcap", MKT, "百万"),
        _claim("cm17.2.c", C, "百万"),
        _claim("cm17.2.ii", II),
        _claim("cm17.10.m1", M1), _claim("cm17.10.m2", M2), _claim("cm17.10.m3", M3),
        _claim("cm17.10.mpess", M_PESS),
        _claim("cm17.5.aa2y", AA_2Y, "百万"),
        _claim("cm17.5.aaall", AA_ALL, "百万"),
        _claim("cm17.5.aaexcl", AA_EXCL, "百万"),
    ]
    r = {}
    for tag, mv in (("m1", M1), ("m2", M2), ("m3", M3)):
        val = round(D.penetration_return(C, mv, Q, 0.0, MKT), 2)
        r[tag] = val
        claims.append(_claim(f"cm17.11.ra.{tag}", val))
    for ak, av in (("aa2y", AA_2Y), ("aaall", AA_ALL), ("aaexcl", AA_EXCL)):
        for tag, mv in (("m1", M1), ("m2", M2), ("m3", M3)):
            gg = round(D.penetration_return(av, mv, Q, 0.0, MKT), 2)
            claims.append(_claim(f"cm17.11.{ak}.{tag}.gg", gg))
            claims.append(_claim(f"cm17.11.{ak}.{tag}.vsii", round(gg - II, 2), "pct点"))
            claims.append(_claim(f"cm17.11.{ak}.{tag}.hh",
                                 round(D.hh(r[tag], gg), 2), "pct点"))
            if gg > 0:
                claims.append(_claim(f"cm17.11.{ak}.{tag}.tbp", round(8.98 * gg / II, 2),
                                     "港元/股"))
    for k in P.PESS_K_VALUES:
        gg = round(D.penetration_return(AA_2Y * k, M_PESS, Q, 0.0, MKT), 2)
        claims.append(_claim(f"cm17.14.{P.k_slug(k)}.ggpess", gg))
        if gg > 0:
            claims.append(_claim(f"cm17.14.{P.k_slug(k)}.tbp", round(8.98 * gg / II, 2),
                                 "港元/股"))
    return {"schema_version": "1.0", "stock_code": TS, "claims": claims,
            "units": {"trading_currency": "HKD", "reporting_currency": "CNY"}}


@pytest.fixture()
def claims_file(tmp_path):
    def _write(payload: dict):
        path = tmp_path / "claims.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path
    return _write


class TestSelfCheck:
    def test_consistent_ledger_reproduces(self, claims_file, capsys):
        """构造的**自洽**账本必须 100% 复现 —— 这是 turtle_core 与 canonical 同源的证明。"""
        code = SC.run(claims_file(_payload()))
        out = capsys.readouterr().out
        assert code == 0
        assert "不可复现 0 项" in out

    def test_tampered_value_is_caught(self, claims_file, capsys):
        p = _payload()
        for c in p["claims"]:
            if c["claim_id"] == "cm17.14.k080.ggpess":
                c["value"] = c["value"] + 0.5      # 篡改 0.5 pct
        code = SC.run(claims_file(p))
        out = capsys.readouterr().out
        assert code == 1
        assert "cm17.14.k080.ggpess" in out

    def test_all_seven_k_dials_are_checked(self, claims_file, capsys):
        SC.run(claims_file(_payload()))
        out = capsys.readouterr().out
        for k in P.PESS_K_VALUES:
            assert f"cm17.14.{P.k_slug(k)}.ggpess" in out

    def test_implied_close_consistency_is_checked(self, claims_file, capsys):
        """所有 *.tbp 必须隐含同一个现价；把一档的 tbp 改坏要能抓到。"""
        p = _payload()
        for c in p["claims"]:
            if c["claim_id"] == "cm17.11.aa2y.m2.tbp":
                c["value"] = round(c["value"] * 1.5, 2)
        code = SC.run(claims_file(p))
        out = capsys.readouterr().out
        assert code == 1
        assert "隐含现价" in out

    def test_missing_optional_claims_do_not_crash(self, claims_file, capsys):
        """账本缺某些 §17 小节时应跳过而非抛异常（fail-closed 但不崩）。"""
        p = {"schema_version": "1.0", "stock_code": TS, "units": {}, "claims": [
            _claim("cm17.8.mktcap", MKT, "百万"),
            _claim("cm17.2.c", C, "百万"),
            _claim("cm17.2.ii", II),
            _claim("cm17.10.m2", M2),
            _claim("cm17.5.aa2y", AA_2Y, "百万"),
        ]}
        code = SC.run(claims_file(p))
        assert code in (0, 1)          # 只要不抛
        assert "检查" in capsys.readouterr().out
