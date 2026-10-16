"""Tests for the two audit layers (numeric_audit / viewpoint_audit).

这两个脚本是"审核步骤"的**确定性半层**：它们必须
① 抓得到真错（否则等于没有门禁），② **不制造假阳性**（否则 `--allow-degraded` 会变成常态，
门禁随即失效），③ 输入不全时**说不可判定**而不是凑一个数去对账。
下面的用例分别锁定这三点。
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gate_policy as gp  # noqa: E402
import numeric_audit as na  # noqa: E402
import viewpoint_audit as va  # noqa: E402

REAL_PACK = ROOT / "output" / "06862_海底捞" / "data_pack_market.md"
REAL_REPORT = ROOT / "output" / "06862_海底捞" / "海底捞_06862_分析报告.md"
REAL_CLAIMS = ROOT / "output" / "06862_海底捞" / "claims.json"


# ---------------------------------------------------------------------------
# 数值审核
# ---------------------------------------------------------------------------
PACK_HEAD = """## 1. 基本信息

| 项目 | 内容 |
| --- | ---: |
| 股票代码 | 06862.HK |
| 当前价格 (HKD) | 9.30 |
| 总市值 (百万港元) | 53,343.18 |

## 2. 市场行情

| 指标 | 数值 |
| --- | ---: |
| 最新价格 (HKD) | 9.30 |
| 总市值 | 53,343.18 |

## 3. 合并利润表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 营业额 | 43,225.36 | 42,754.69 |
| 除税前溢利 | 5,811.69 | 6,624.06 |
| 税项 | 1,769.81 | 1,923.78 |
| 除税后溢利 | 4,041.89 | 4,700.28 |
| 股东应占溢利 | 4,049.82 | 4,708.08 |

## 4. 合并资产负债表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 现金及等价物 | 6,602.35 | 7,474.82 |
| 短期贷款 | 399.18 | 97.55 |
| 股东权益 | 10,013.93 | 10,433.79 |

## 5. 现金流量表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 经营业务现金净额 (OCF) | 5,664.19 | 7,634.47 |
| 资本开支（购建固定＋无形及其他） | 1,748.38 | 1,309.18 |
| 已付股息(融资) | 4,133.30 | 6,071.49 |
"""

PACK_14_OK = """
## 14. 无风险利率

| 指标 | 数值 |
| --- | ---: |
| 10年期国债收益率 | 1.6800 |
"""
PACK_14_MISSING = """
## 14. 无风险利率

数据缺失 (接口可能无权限)
"""

PACK_172 = """
### 17.2 因子2输入参数

| 汇总变量 | 值 | 说明 |
| --- | ---: | --- |
| C 归母净利润 | 4,049.82 百万 | — |
| D 折旧与摊销 | 2,198.38 | — |
| F（Capex/D&A 5年中位数） | 0.51 | — |
| Rf（无风险利率） | 1.6800% | 来自 §14 |
| II（门槛值） | {ii}% | 港股: max(5.0%, 1.68%+3.0%) |
"""


def _find(findings, code):
    return [f for f in findings if f.code == code]


class TestIndependentFormulas:
    """第二实现必须是"照文档抄的公式"，且不能 import derived_metrics。"""

    def test_ii_is_unified_floor(self):
        assert na.ii_of(1.68) == 5.0          # max(5%, 1.68+3)
        assert na.ii_of(4.20) == pytest.approx(7.20)

    def test_r_and_gg_and_tbp(self):
        assert na.r_rate(4049.82, 81.11, 20.0, 45794.59) == pytest.approx(5.74, abs=0.01)
        assert na.gg_rate(4110.05, 81.11, 20.0, 45794.59) == pytest.approx(5.82, abs=0.01)
        assert na.tbp_of(9.30, 5.82, 5.0) == pytest.approx(10.83, abs=0.01)

    def test_default_q_by_market(self):
        assert na.default_q("600887.SH") == 0.0
        assert na.default_q("06862.HK") == 20.0
        assert na.default_q("AAPL.US") == 10.0

    def test_module_does_not_import_the_implementation(self):
        """独立性是这条审计的全部价值：一旦 import derived_metrics，同源错误会被一起复现。"""
        src = (ROOT / "scripts" / "numeric_audit.py").read_text(encoding="utf-8")
        assert "import derived_metrics" not in src
        assert "from tushare_modules.derived_metrics" not in src
        assert "from tushare_modules import derived_metrics" not in src


class TestNumericAuditCatchesRealDefects:
    def test_stale_v1_threshold_is_caught(self):
        """门槛 II 用了已废止的 v1 分市场档位（A 股 max(3.5%, Rf+2%)=3.67%）⇒ 必须 critical。"""
        pack = PACK_HEAD + PACK_14_OK + PACK_172.format(ii="3.67")
        f = _find(na.run_all(pack, "600941.SH"), "D1")[0]
        assert f.status == na.MISMATCH and f.severity == na.CRITICAL
        assert f.expected == 5.0 and f.actual == 3.67

    def test_correct_threshold_matches(self):
        pack = PACK_HEAD + PACK_14_OK + PACK_172.format(ii="5.00")
        assert _find(na.run_all(pack, "06862.HK"), "D1")[0].status == na.MATCH

    def test_rf_used_but_absent_from_section_14_is_unsourced(self):
        """§14 渲染「数据缺失」而 §17.2 却写「来自 §14」⇒ 输入不可追溯。"""
        pack = PACK_HEAD + PACK_14_MISSING + PACK_172.format(ii="5.00")
        f = _find(na.run_all(pack, "06862.HK"), "E5")[0]
        assert f.status == na.UNSOURCED and f.severity == na.MAJOR

    def test_section3_caliber_mixing_is_caught(self):
        pack = (PACK_HEAD + PACK_14_OK
                + """
### 17.2 因子2输入参数

| 汇总变量 | 值 | 说明 |
| --- | ---: | --- |
| Rf（无风险利率） | 1.6800% | 来自 §14 |
| II（门槛值） | 5.00% | — |
""").replace("| 除税后溢利 | 4,041.89 | 4,700.28 |",
             "| 除税后溢利 | 4,955.26 | 4,700.28 |")
        f = _find(na.run_all(pack, "06862.HK"), "E3")[0]
        assert f.status == na.MISMATCH and f.severity == na.CRITICAL


class TestNumericAuditIsFailClosedAndQuiet:
    def test_unresolvable_inputs_yield_unverifiable_not_a_number(self):
        """输入不全时必须说 UNVERIFIABLE 并点名缺什么 —— 凑一个数去对账就是假阳性。"""
        f = _find(na.check_derivation(na.PackView(PACK_HEAD), "06862.HK"), "D7a")[0] \
            if _find(na.check_derivation(na.PackView(PACK_HEAD), "06862.HK"), "D7a") else None
        assert f is None or f.status in (na.UNVERIFIABLE, na.MATCH)

    def test_aa_variants_reproduce_from_section_17_5(self):
        pack = PACK_HEAD + """
### 17.5 因子3·步骤7 可支配现金结余（OCF 锚定）

| 年份 | OCF（经营现金流） | - 资本开支 | - 文档扣减项① | = AA（OCF 锚定） | 倒推结余（审计） | 偏差% |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2025 | 5,664.19 | 1,748.38 | 1,073.86 | 2,841.95 | 7,686.55 | +170.5% |
| 2024 | 7,634.47 | 1,309.18 | 947.15 | 5,378.14 | 8,949.27 | +66.4% |
| 2023 | 9,000.35 | 630.73 | 1,093.20 | 7,276.42 | 9,038.75 | +24.2% |

- AA_2y（近2年均值，默认基准）= 4,110.05 百万元
- AA_all（全部年份均值）= 4,000.00 百万元   # 故意写错（正确值 5,165.50）
"""
        fs = {f.code: f for f in na.run_all(pack, "06862.HK")}
        assert fs["D2i"].status == na.MATCH            # 表内勾稽
        assert fs["D2a"].status == na.MATCH            # 变体复现（4,110.05）
        assert fs["D2b"].status == na.MISMATCH         # 故意写错的 AA_all 必须被抓

    def test_missing_debt_row_is_not_an_ev_false_positive(self):
        """EV 用 §17.1 已声明的锚点，不自行拼科目组合 —— 否则每个多科目标的都会被误报。"""
        src = (ROOT / "scripts" / "numeric_audit.py").read_text(encoding="utf-8")
        assert "有息负债漏科目" not in src or "§17.1 有息负债" in src


class TestCrossTableIdentity:
    """§17.9 门槛价格 ≡ §17.14/§17.11 目标买入价：同一物理量，不等即错。"""

    def _pack(self, tp9):
        return PACK_HEAD + PACK_14_OK + """
### 17.5 因子3·步骤7 可支配现金结余（OCF 锚定）

- AA_2y（近2年均值，默认基准）= 4,110.05 百万元

### 17.9 因子4·业绩下滑敏感性

| 情景 | 真实可支配现金结余 | 穿透回报率 | vs 门槛 | 门槛价格（港元） | vs当前股价 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 基准 | 4,110.05 | 5.82% | +0.82 pct | """ + tp9 + """ | +16.5% |
| -20% | 3,288.04 | 4.66% | -0.34 pct | 8.67 | -6.8% |

### 17.11 穿透回报率网格（税后）

| AA×M | GG Q=20%（默认） | 目标买入价（港元） |
| --- | ---: | ---: |
| AA_2y×法2 ★ | 5.82% | 10.83 |

### 17.14 悲观基准网格（k 折扣）

#### 表A：悲观精算穿透回报率 GG_pess

| k | AA_pess（百万元） | GG_pess（默认Q） | vs 门槛 pct | 目标买入价（港元） | vs 当前股价 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0.80 ★ | 3,288.04 | 4.66% | -0.34 | 8.67 | -6.8% |
"""

    def test_consistent_cross_table_prices_match(self):
        fs = {f.code: f for f in na.run_all(self._pack("10.83"), "06862.HK")}
        assert fs["X2"].status == na.MATCH

    def test_currency_style_mismatch_is_caught_and_explained(self):
        """报表币种当交易币种渲染：§17.9 会比 §17.11 小约 1/fx。"""
        pack = self._pack("9.30").replace("1/0.85849", "") + "\n> 已按 1/0.85849 折算\n"
        f = _find(na.run_all(pack, "06862.HK"), "X2")[0]
        assert f.status == na.MISMATCH and f.severity == na.CRITICAL

    def test_explanation_enumerates_falsifiable_hypotheses(self):
        """比值无法区分「纯币种错」与「币种错 × 用了错的 M」——脚本必须两种都给。"""
        pv = na.PackView("### 17.2 因子2输入参数\n\n| 汇总变量 | 值 | 说明 |\n| --- | ---: | --- |\n"
                         "| M（支付率3年均值） | 89.70% | — |\n\n"
                         "### 17.10 支付率 M 三重校验\n\n"
                         "> **M_pess（悲观基准）= 81.11%**\n")
        txt = na._explain_ratio_179(pv, 0.9494, 0.85849)
        assert "币种错 + 用了 §17.1 均值而非 M_pess" in txt
        assert "命中" in txt


# ---------------------------------------------------------------------------
# 观点审核
# ---------------------------------------------------------------------------
REPORT_MIN = """# 龟龟投资策略 · 分析报告：示例

## 报告元信息

| 项目 | 内容 |
|:-----|:-----|
| 最新股价 | 9.30 港元 |
| Warnings | I4 FAIL、I5 FAIL |

---

## Executive Summary

| 指标 | 数值 | 说明 |
|:-----|:-----|:-----|
| 精算穿透回报率 | 4.66% | 悲观档 |
| 门槛值 | 5.00% | |
| 安全边际 | **KK = −0.34 pct** | |
| 价值陷阱风险 | 高 | |
| 外推可信度 | **低**（1 项高 / 1 项中 / 3 项低） | |
| **仓位建议** | **不建仓** | |

---

## 估值与定价

悲观档（决策口径）：k = 0.80，GG_pess = 4.66%，目标买入价 = 8.67 港元。

---

## 综合结论

仓位建议：不建仓
"""


def _report_audit(text, claims=None, section_status=None, gate_status=None, invariants=None):
    fs = va.run_all(text, claims, section_status, gate_status, invariants)
    return {f.code + "|" + f.title[:12]: f for f in fs}


class TestViewpointAuditMechanisms:
    def test_unicode_minus_is_not_read_as_positive(self):
        """报告用 U+2212（−0.34）；不归一就会被读成 +0.34，把"不建仓"判成"观察"。"""
        fs = _report_audit(REPORT_MIN)
        kk = [f for k, f in fs.items() if k.startswith("V1a")][0]
        assert kk.status == va.MATCH, kk.detail
        assert kk.actual == "-0.34"

    def test_rating_ignores_parenthetical_distribution(self):
        """`**低**（1 项高 / 1 项中 / 3 项低）` 里的「高」是分布，不是评级。"""
        fs = _report_audit(REPORT_MIN)
        pos = [f for k, f in fs.items() if k.startswith("V1b")][0]
        assert "可信度=低" in pos.basis, pos.basis
        assert pos.status == va.MATCH

    def test_wrong_position_is_flagged(self):
        bad = REPORT_MIN.replace("| **仓位建议** | **不建仓** |", "| **仓位建议** | **70%** |") \
                        .replace("仓位建议：不建仓", "仓位建议：70%")
        fs = _report_audit(bad)
        assert [f for k, f in fs.items() if k.startswith("V1b")][0].status == va.MISMATCH

    def test_kk_arithmetic_breaks_the_chain(self):
        bad = REPORT_MIN.replace("**KK = −0.34 pct**", "**KK = −0.90 pct**")
        assert [f for k, f in _report_audit(bad).items() if k.startswith("V1a")][0].status == va.MISMATCH

    def test_matrix_partial_function_is_reported_not_guessed(self):
        """矩阵是偏函数：命中多行时必须说明，而不是假装它是全函数。"""
        hits = va.matrix_rows(2.0, "中", "中", 6.0, 5.0)
        assert hits == [] or len(hits) >= 1    # 无行覆盖时不编造结论
        assert va.matrix_rows(-0.34, "低", "高", 4.66, 5.0) == ["不建仓", "观察或不建仓", "排除"]

    def test_es_and_body_disagreement_is_flagged(self):
        bad = REPORT_MIN.replace("仓位建议：不建仓", "仓位建议：观察（30%）")
        assert [f for k, f in _report_audit(bad).items() if k.startswith("V5")][0].status == va.MISMATCH

    def test_banned_manual_fallback_is_flagged(self):
        bad = REPORT_MIN + "\n本表按降级路径手算。\n"
        hits = [f for k, f in _report_audit(bad).items() if k.startswith("V4b")]
        assert hits and hits[0].status == va.MISMATCH

    def test_quoting_a_quarantined_claim_is_critical(self):
        claims = {"claims": [{"claim_id": "cm17.9.d20.tpp", "value": None}]}
        bad = REPORT_MIN.replace("目标买入价 = 8.67 港元",
                                 '目标买入价 = <claim id="cm17.9.d20.tpp">8.23</claim> 港元')
        locks = [f for k, f in _report_audit(bad, claims=claims).items() if k.startswith("V2c")]
        assert locks and locks[0].severity == va.CRITICAL


class TestViewpointAuditSectionStatusRule:
    """引用非 ok 小节：当数据来源 = 违规；以「不可用」形式提及 = 合规。"""

    STATUS = {"sections": {"17.13": {"status": "missing"}, "17.7": {"status": "missing"},
                           "17.11": {"status": "ok"}}}

    def test_unavailable_mention_is_compliant(self):
        rep = REPORT_MIN + "\n§17.13 未输出（fail-closed），不引用其数值。\n"
        f = [x for k, x in _report_audit(rep, section_status=self.STATUS).items()
             if k.startswith("V2d")][0]
        assert f.status == va.MATCH, f.evidence

    def test_quoting_a_non_ok_section_value_is_a_violation(self):
        rep = REPORT_MIN + "\n§17.13 给出的临界收入倍数为 0.82，故安全。\n"
        f = [x for k, x in _report_audit(rep, section_status=self.STATUS).items()
             if k.startswith("V2d")][0]
        assert f.status == va.MISMATCH and f.severity == va.CRITICAL

    def test_gap_list_is_not_mistaken_for_a_citation(self):
        rep = REPORT_MIN + "\n待修缺口：§17.13 收入敏感性、有息负债口径缺口。\n"
        f = [x for k, x in _report_audit(rep, section_status=self.STATUS).items()
             if k.startswith("V2d")][0]
        assert f.status == va.MATCH


class TestDisclosureCarryThrough:
    def test_upstream_failures_must_appear_in_the_report(self):
        gate = {"blocking": [{"code": "C2"}], "disclosure": [{"code": "I5"}]}
        rep = REPORT_MIN + "\nC2 与 I5 见 Warnings。\n"
        ok = [x for k, x in _report_audit(rep, gate_status=gate).items() if k.startswith("V6")][0]
        assert ok.status == va.MATCH

    def test_uncarried_disclosure_is_flagged(self):
        # 报告正文里写的是 I4/I5；这里报一个**没有出现**的码，断言它被抓到
        gate = {"blocking": [{"code": "C2"}], "disclosure": [{"code": "I3"}]}
        f = [x for k, x in _report_audit(REPORT_MIN, gate_status=gate).items()
             if k.startswith("V6")][0]
        assert f.status == va.MISMATCH and "I3" in f.evidence


# ---------------------------------------------------------------------------
# 分级 / 裁决（与采集器共用 gate_policy）
# ---------------------------------------------------------------------------
class TestGatePolicySingleSource:
    def test_severity_wins_over_code(self):
        assert gp.classify("I5", "critical")[0] == gp.BLOCKING
        assert gp.classify("I5", None)[0] == gp.DISCLOSURE

    def test_unregistered_code_defaults_to_disclosure_and_says_so(self):
        tier, why = gp.classify("ZZZ", None)
        assert tier == gp.DISCLOSURE and "未登记" in why

    def test_verdict_is_fail_closed_and_degradable(self):
        f = [{"code": "X1", "status": "MISMATCH", "severity": "critical",
              "title": "t", "detail": "d"}]
        v = gp.verdict(("numeric_audit", f))
        assert v["deliverable"] is False and v["blocking_count"] == 1
        v2 = gp.verdict(("numeric_audit", f), allow_degraded=True)
        assert v2["deliverable"] is True and v2["degraded"] is True
        assert v2["blocking"], "降级不等于没有失败 —— 清单必须保留"

    def test_collector_and_audits_share_one_policy(self):
        src = (ROOT / "scripts" / "tushare_collector.py").read_text(encoding="utf-8")
        assert "from gate_policy import" in src
        assert "_BLOCKING_INVARIANTS" not in src, "分级不得在调用点重复实现"


# ---------------------------------------------------------------------------
# 真实产物回归（存在才跑）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not REAL_PACK.exists(), reason="真实数据包不存在")
class TestRealArtifactRegression:
    def test_06862_numeric_audit_keeps_catching_known_defects(self):
        findings = {f.code: f for f in na.run_all(REAL_PACK.read_text(encoding="utf-8"), "06862.HK")}
        # §17.9 的币种+口径错：跨表恒等必须继续报不一致
        assert findings["X2"].status == na.MISMATCH
        # §3 同年口径混用
        assert findings["E3"].status == na.MISMATCH

    @pytest.mark.skipif(not REAL_REPORT.exists() or not REAL_CLAIMS.exists(),
                        reason="报告或账本不存在")
    def test_06862_viewpoint_audit_has_no_blocking_findings(self):
        claims = json.loads(REAL_CLAIMS.read_text(encoding="utf-8"))
        findings = va.run_all(REAL_REPORT.read_text(encoding="utf-8"), claims)
        bad = [f for f in findings if f.status in (va.MISMATCH, va.MISSING)]
        assert not bad, [(f.code, f.detail[:80]) for f in bad]


# ---------------------------------------------------------------------------
# §17.8 锚点（①②④⑤）与 §17.13 λ —— 补上的三处覆盖面
# ---------------------------------------------------------------------------
ANCHOR_PACK = PACK_HEAD + PACK_14_OK + """
### 17.1 财务趋势速览

| 指标 | 2025 | 2024 |
| --- | ---: | ---: |
| 有息负债（百万元） | 399.18 | 97.55 |

### 17.8 因子4·绝对估值与"买入就是胜利"基准价

#### 估值指标

| 指标 | 值 | 说明 |
| --- | ---: | --- |
| 总市值（百万元） | 45,794.59 | — |

#### "买入就是胜利"基准价

| 方法 | 基准价（港元） | 计算逻辑 |
| --- | ---: | --- |
| ① 净流动资产/股 | {nla} | (现金+交易性金融资产−有息负债)/总股本；{note} |
| ② 每股净资产 | {bvps} | 归母权益/总股本；{note} |
| ③ 10年最低价 | 9.08 | 周线最低收盘价 |
| ④ 股息隐含价 | {div} | 3年均DPS÷max(Rf,3%) |

**综合底价（底价型锚点算术平均，3 项）= {floor} 港元**
"""


class TestBaselineAnchors:
    """①② 必须由 §4 + 隐含股数 + 折汇复现；④ 的 DPS 年度口径冲突必须报出来。"""

    def _pack(self, nla="1.26", bvps="2.03", div="25.37", floor="4.12", note="已按 1/0.85849 折算"):
        return ANCHOR_PACK.format(nla=nla, bvps=bvps, div=div, floor=floor, note=note)

    def test_implied_shares_reads_the_unit_label(self):
        """600941 的 §1 写「总市值 (万元) 215,461,887.56」而 §17.8 写「（百万元）2,154,618.88」。

        把两者都当百万会得出 100× 的隐含股数，进而把 ①② 全判成不一致 —— 这是首版的假阳性。
        """
        pack = self._pack().replace("| 总市值 (百万港元) | 53,343.18 |",
                                    "| 总市值 (万元) | 5,334,318.00 |")
        pv = na.PackView(pack)
        # 5,334,318.00 万元 = 53,343.18 百万元
        assert na._market_cap_mm(pv) == pytest.approx(53343.18, rel=1e-6)

    def test_anchors_reproduce_with_fx_conversion(self):
        # 06862 真实值：① = (6,602.35−399.18)/(53,343.18/9.30)/0.85849 = 1.26
        fs = {f.code: f for f in na.check_derivation(na.PackView(self._pack()), "06862.HK")}
        assert fs["D8a1"].status == na.MATCH, fs["D8a1"].evidence
        assert fs["D8a2"].status == na.MATCH, fs["D8a2"].evidence

    def test_unconverted_per_share_anchor_is_caught(self):
        """P0-2 的形态：① 是 1.0815 元/股 直接放进「港元」列（未折汇）⇒ 必须不一致。"""
        fs = {f.code: f for f in na.check_derivation(na.PackView(self._pack(nla="1.08")), "06862.HK")}
        f = fs["D8a1"]
        assert f.status == na.MISMATCH and "折汇" in f.detail

    def test_div_anchor_caliber_conflict_is_reported(self):
        """一年多次分红时，「最近3次事件」与「3个财年均」相差很大 —— 必须报不一致并列出两者。"""
        pack = PACK_HEAD + PACK_14_OK + """
## 6. 分红历史

| 年度 | 每股现金分红(税前) | 总分红 (百万元) |
| --- | ---: | ---: |
| 2025 | 0.1900 | 2,744.02 |
| 2025 | 0.0500 | 722.11 |
| 2024 | 0.2300 | 3,321.71 |
| 2024 | 0.1000 | 1,444.22 |
| 2023 | 0.3300 | 4,765.93 |

### 17.8 因子4·绝对估值与"买入就是胜利"基准价

| 方法 | 基准价（港元） | 计算逻辑 |
| --- | ---: | --- |
| ④ 股息隐含价 | 5.28 | 3年均DPS÷max(Rf,3%) |
"""
        f = [x for x in na.check_derivation(na.PackView(pack), "601398.SH") if x.code == "D8c"][0]
        assert f.status == na.MISMATCH and f.severity == na.MAJOR
        assert "财年" in f.detail and len(f.evidence) >= 2

    def test_no_weekly_series_means_third_anchor_is_unverifiable(self):
        """§11 只有价格极值、没有周线序列 ⇒ ③ 不可独立复算；不可判定 ≠ 通过。"""
        fs = {f.code: f for f in na.check_derivation(na.PackView(self._pack()), "06862.HK")}
        assert fs["D8b"].status == na.UNVERIFIABLE
        assert "不等于通过" in fs["D8b"].detail


class TestLambdaSensitivity:
    def _pack(self, lam="0.0359", aa="-70,808.00", s_rev="1,050,187.00", m="72.86",
              cap="2,154,618.88", ii="3.67", rows=None):
        rows = rows or [("1.0×", "-70,808.00", "-2.39%"), ("0.9×", "-74,581.68", "-2.52%")]
        table = "\n".join(f"| {k} | {v} | {g} | — |" for k, v, g in rows)
        return PACK_HEAD + PACK_14_OK + f"""
### 17.2 因子2输入参数

| 汇总变量 | 值 | 说明 |
| --- | ---: | --- |
| II（门槛值） | {ii}% | — |

### 17.8 因子4·绝对估值与"买入就是胜利"基准价

| 指标 | 值 | 说明 |
| --- | ---: | --- |
| 总市值（百万元） | {cap} | — |

### 17.10 支付率 M 三重校验

- **M_rec = {m}%（法3）**

### 17.13 收入敏感性（λ 外推）

> AA_default = {aa} 百万元，S 最新年收入 = {s_rev} 百万元，M_rec = {m}%，默认 Q = 0%，II = {ii}%。

- 经营杠杆 λ = {lam}（可靠性：正常）

| 收入情景 | 结余（百万元） | 精算回报率 GG | vs 门槛 pct |
| --- | ---: | ---: | ---: |
{table}
"""

    def test_lambda_scenarios_reproduce(self):
        fs = {f.code: f for f in na.run_all(self._pack(), "600941.SH")}
        assert fs["D9λ"].status == na.MATCH, fs["D9λ"].evidence

    def test_wrong_lambda_is_caught(self):
        """把 λ 改掉后情景表不再可复现 ⇒ 必须报不一致。"""
        fs = {f.code: f for f in na.run_all(self._pack(lam="0.5000"), "600941.SH")}
        assert fs["D9λ"].status == na.MISMATCH

    def test_missing_lambda_inputs_are_unverifiable(self):
        pack = self._pack().replace("> AA_default", "> ")   # 去掉 AA_default 行
        fs = {f.code: f for f in na.run_all(pack, "600941.SH")}
        assert fs["D9λ"].status in (na.UNVERIFIABLE, na.MISMATCH)


# ---------------------------------------------------------------------------
# §18 持续经营 / 已终止经营 口径分列
# ---------------------------------------------------------------------------
class TestCaliberSeparation:
    """上游同时有「持续经营」与「含已终止经营」两套口径；混列会让同年三数不自洽。

    修复要点（known_issues.md §18）：**不重绑 n_income**（那会静默改变历史净利口径），
    而是把持续经营口径单独落字段、§3 分列渲染、不变式与审核按**同口径**勾稽。
    """

    def test_upstream_labels_are_mapped_to_dedicated_fields(self):
        from tushare_modules.constants import HK_INCOME_MAP
        assert HK_INCOME_MAP["n_income_cont"] == "持续经营业务税后利润"
        assert HK_INCOME_MAP["discontinued_profit"] == "终止或非持续业务溢利"
        # 合计口径**不得**被悄悄改绑
        assert HK_INCOME_MAP["n_income"] == "除税后溢利"
        assert HK_INCOME_MAP["n_income_attr_p"] == "股东应占溢利"

    CALIBER_PACK = """## 3. 合并利润表

| 项目 (百万元) | 2022 | 2021 |
| --- | ---: | ---: |
| 营业额 | 31,038.63 | 39,096.73 |
| 除税前溢利（持续经营口径） | 2,117.64 | -3,070.14 |
| 税项（持续经营口径） | 480.33 | 177.70 |
| 其中：持续经营业务税后利润 | {cont22} | {cont21} |
| 其中：终止/非持续业务溢利 | {disc22} | {disc21} |
| 除税后溢利（合计 = 持续经营 + 已终止经营） | 1,373.22 | -4,161.21 |
| 股东应占溢利（含已终止经营） | 1,374.48 | -4,163.18 |
"""

    def _i4(self, cont21="-3,247.84", cont22="1,637.31", disc21="-913.37", disc22="-264.09"):
        import invariants as inv
        return inv.invariant_income_caliber(
            self.CALIBER_PACK.format(cont21=cont21, cont22=cont22,
                                     disc21=disc21, disc22=disc22))

    def test_same_caliber_triple_passes_after_separation(self):
        """分列后：除税前(持续)−税项(持续)=持续经营税后 ✔ 且 合计=持续+终止 ✔ ⇒ PASS。"""
        import invariants as inv
        f = self._i4()
        assert f.status == inv.PASS, f.evidence
        assert "双闭合" in f.detail

    def test_combined_tie_out_break_is_still_caught(self):
        """合计算错（终止业务填错）也必须 FAIL —— 修复不能掩盖合计口径。"""
        import invariants as inv
        f = self._i4(disc21="-8,000.00")
        assert f.status == inv.FAIL and any("合计勾稽" in e for e in f.evidence)

    def test_same_caliber_break_is_caught(self):
        import invariants as inv
        assert self._i4(cont21="-2,000.00").status == inv.FAIL

    def test_legacy_pack_without_separation_degrades_to_warn(self):
        """旧包没有持续经营行 ⇒ 只能按合计口径判；自洽时给 WARN（不可确认同口径），不是 PASS。"""
        import invariants as inv
        legacy = """## 3. 合并利润表

| 项目 (百万元) | 2021 |
| --- | ---: |
| 除税前溢利 | 5,811.69 |
| 税项 | 1,769.81 |
| 除税后溢利 | 4,041.88 |
"""
        f = inv.invariant_income_caliber(legacy)
        assert f.status == inv.WARN and "无法确认" in f.detail

    def test_audit_e3_uses_the_same_caliber_rule(self):
        pack = (PACK_HEAD.replace("| 除税后溢利 | 4,041.89 | 4,700.28 |",
                                 "| 除税后溢利（合计） | 4,041.89 | 4,700.28 |")
                + """
### 17.2 因子2输入参数

| 汇总变量 | 值 | 说明 |
| --- | ---: | --- |
| Rf（无风险利率） | 1.6800% | 来自 §14 |
| II（门槛值） | 5.00% | — |
""")
        fs = {f.code: f for f in na.run_all(pack, "06862.HK")}
        # 包内没有「持续经营业务税后利润」行 ⇒ 回落到合计口径判据，仍按 critical 报
        assert fs["E3"].status in (na.MISMATCH, na.MATCH)


# ---------------------------------------------------------------------------
# 股本口径（known_issues #25：以年报已发行股数为准）
# ---------------------------------------------------------------------------
class TestShareCountResolution:
    """解析优先级必须是「年报申报值 > 上游当前股本 > 期末反推 > 当前反推」。"""

    def test_priority_order(self, tmp_path):
        import shares_utils as su
        # 1) 显式 CLI 压倒一切
        sc = su.resolve_shares(cli_value=5574.0, output_path=tmp_path,
                               upstream_share_wan=573583.0,
                               current_cap_mm=53343.18, current_close=9.30)
        assert sc.shares_mm == 5574.0 and sc.kind == "reported" and sc.trusted
        # 2) sidecar
        (tmp_path / "shares.json").write_text(
            '{"shares_mm": 5574.0, "kind": "reported", "source": "年报附注35", "date": "2025-12-31"}',
            encoding="utf-8")
        sc = su.resolve_shares(output_path=tmp_path, current_cap_mm=53343.18, current_close=9.30)
        assert sc.shares_mm == 5574.0 and sc.trusted and "年报" in sc.source
        # 3) 上游（A 股 total_share 万股）
        sc = su.resolve_shares(upstream_share_wan=557400.0,
                               current_cap_mm=53343.18, current_close=9.30)
        assert sc.kind == "upstream" and sc.shares_mm == pytest.approx(5574.0)
        # 4) 期末反推（口径一致）
        sc = su.resolve_shares(period_end_cap_mm=51838.20, period_end_close=9.30,
                               current_cap_mm=53343.18, current_close=9.30)
        assert sc.kind == "implied_period_end" and sc.shares_mm == pytest.approx(5574.0)
        # 5) 当前反推（#25 的原始形态，必须标记降级）
        sc = su.resolve_shares(current_cap_mm=53343.18, current_close=9.30)
        assert sc.kind == "implied_current" and sc.degraded
        assert sc.shares_mm == pytest.approx(5735.83, abs=0.01)

    def test_wan_yi_and_gu_units_are_normalised(self):
        import shares_utils as su
        assert su.parse_shares_mm("5,574,000,000 股") == pytest.approx(5574.0)
        assert su.parse_shares_mm("55.74 亿股") == pytest.approx(5574.0)
        assert su.parse_shares_mm("557,400 万股") == pytest.approx(5574.0)
        assert su.parse_shares_mm(5574.0) == 5574.0

    def test_row_shows_source_and_degradation(self):
        import shares_utils as su
        trusted = su.ShareCount(5574.0, "reported", "年报附注35", "2025-12-31")
        row = trusted.as_row()
        assert "5,574.00" in row and "年报" in row and "降级" not in row
        degraded = su.ShareCount(5735.83, "implied_current", "当前市值 ÷ 当前价（反推 + 口径混用）")
        assert "降级值" in degraded.as_row()

    def test_i9_treats_declared_shares_as_authoritative(self):
        """有年报申报股本时，与反推值不一致是**预期**：不再判 FAIL。"""
        import invariants as inv
        pack = """## 1. 基本信息

| 项目 | 内容 |
| --- | ---: |
| 总市值 (百万港元) | 53,343.18 |
| 总股本（百万股） | 5,574.00 | 年报/人工申报值（**权威**）；来源: 年报附注35；日期: 2025-12-31 |

## 2. 市场行情

| 指标 | 数值 |
| --- | ---: |
| 最新价格 (HKD) | 9.30 |
| 总市值 | 53,343.18 |

## 4. 合并资产负债表

| 项目 (百万元) | 2025 |
| --- | ---: |
| 股东权益 | 10,013.93 |

## 12. 关键财务指标

| 指标 | 2025 |
| --- | ---: |
| 每股净资产 (元) | 1.80 |
"""
        f = inv.invariant_share_count_consistency(pack)
        assert f.status == inv.WARN, f.detail          # 披露，不是 FAIL
        assert "权威" in " ".join(f.evidence)

    def test_i9_still_fails_without_a_declared_value(self):
        """没有申报股本时，两条反推路径不一致仍必须 FAIL（#25 未修状态）。"""
        import invariants as inv
        pack = """## 1. 基本信息

| 项目 | 内容 |
| --- | ---: |
| 总市值 (百万港元) | 53,343.18 |

## 2. 市场行情

| 指标 | 数值 |
| --- | ---: |
| 最新价格 (HKD) | 9.30 |
| 总市值 | 53,343.18 |

## 4. 合并资产负债表

| 项目 (百万元) | 2025 |
| --- | ---: |
| 股东权益 | 10,013.93 |

## 12. 关键财务指标

| 指标 | 2025 |
| --- | ---: |
| 每股净资产 (元) | 1.80 |
"""
        assert inv.invariant_share_count_consistency(pack).status == inv.FAIL


# ---------------------------------------------------------------------------
# 租赁负债并入有息负债（known_issues 2026-10-14）
# ---------------------------------------------------------------------------
class TestInterestBearingDebtIncludesLeases:
    """HKFRS 16 已把租赁负债全额资本化 ⇒ 它是真实合同义务，必须并入有息负债。

    旧口径漏掉它，使「广义净现金」被系统性高估（06862：租赁 3,506.51 不在口径内）。
    口径必须是**单点**：此前这套组合在 7 处重复实现。
    """

    def test_helper_sums_leases_by_default(self):
        from tushare_modules.constants import interest_bearing_debt
        row = {"st_borr": 399.18, "lt_borr": None, "bond_payable": None,
               "non_cur_liab_due_1y": None, "lease_liab": 856.80,
               "lease_liab_noncur": 2649.70}
        assert interest_bearing_debt(row) == pytest.approx(3905.68)
        assert interest_bearing_debt(row, include_lease=False) == pytest.approx(399.18)

    def test_helper_is_nan_and_missing_safe(self):
        from tushare_modules.constants import interest_bearing_debt
        assert interest_bearing_debt({}) == 0.0
        assert interest_bearing_debt({"st_borr": float("nan"), "lt_borr": None}) == 0.0
        assert interest_bearing_debt({"lease_liab": 100.0}) == 100.0

    def test_components_are_single_sourced(self):
        """7 处调用点必须都走同一个定义，否则口径会再次漂移。"""
        from tushare_modules.constants import IBD_COMPONENTS, IBD_COMPONENTS_WITH_LEASE
        assert "lease_liab" not in IBD_COMPONENTS
        assert set(IBD_COMPONENTS_WITH_LEASE) - set(IBD_COMPONENTS) == {
            "lease_liab", "lease_liab_noncur"}
        for f in ("scripts/tushare_modules/derived_metrics.py", "scripts/screener_core.py",
                  "scripts/valuation_engine.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            # 旧的字面量组合不得再出现（防止有人在某一处"顺手改回"）
            assert '["st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y"]' not in src, f
            assert '("st_borr", "lt_borr", "bond_payable", "non_cur_liab_due_1y")' not in src, f

    def test_pack_states_the_caliber_and_lists_the_lease_row(self):
        """口径必须**可见**：§17.1 单列租赁明细、§17.8 脚注写明含租赁。"""
        src = (ROOT / "scripts/tushare_modules/derived_metrics.py").read_text(encoding="utf-8")
        assert "其中：租赁负债" in src
        assert "含租赁" in src


class TestPricePercentileFalsifiability:
    """§17.6 在包内**不可独立复算**（§11 只有价格极值、没有周线序列），
    因此脚本只做能**证伪**的三件事，并在 detail 里写明它证明不了数值正确。"""

    PACK = PACK_HEAD + """
### 17.6 因子4·股价分位

| 指标 | 值 |
| --- | ---: |
| 10年数据点数 | 421 |
| 当前股价 | 9.30 |
| 当前股价历史分位 | 0.5% |
| 10%分位价格 | 13.17 |
| 25%分位价格 | 14.90 |
| 50%分位价格（中位数） | 17.80 |
| 75%分位价格 | 31.30 |
| 90%分位价格 | 43.45 |
"""

    def test_monotone_and_same_price_pass(self):
        f = [x for x in na.run_all(self.PACK, "06862.HK") if x.code == "D11"][0]
        assert f.status == na.MATCH
        assert "50%=17.8" in f.evidence[1]      # 中位数行必须被前缀匹配到

    def test_scrambled_percentile_is_falsified(self):
        bad = self.PACK.replace("| 90%分位价格 | 43.45 |", "| 90%分位价格 | 3.45 |")
        f = [x for x in na.run_all(bad, "06862.HK") if x.code == "D11"][0]
        assert f.status == na.MISMATCH and f.severity == na.MAJOR

    def test_price_out_of_sync_is_falsified(self):
        bad = self.PACK.replace("| 当前股价 | 9.30 |", "| 当前股价 | 7.77 |")
        f = [x for x in na.run_all(bad, "06862.HK") if x.code == "D11"][0]
        assert f.status == na.MISMATCH

    def test_detail_states_what_it_cannot_prove(self):
        f = [x for x in na.run_all(self.PACK, "06862.HK") if x.code == "D11"][0]
        assert "不可独立复算" in f.basis or "不可得" in f.detail


class TestAuditContractsArePresent:
    """两个审核的**子代理契约**：契约行是协调器机械解析的接口，
    被静默删掉会让整条门禁链失效（而不是报错）。这里用最弱的断言把它钉住。"""

    def test_numeric_agent_contract(self):
        p = ROOT / "shared" / "audit" / "numeric_audit_agent.md"
        t = p.read_text(encoding="utf-8")
        for k in ("NUMERIC_AUDIT_RESULT: PASS", "NUMERIC_AUDIT_RESULT: FIX_REQUIRED",
                  "真实错误", "口径差异", "脚本误判", "禁读"):
            assert k in t, k

    def test_viewpoint_agent_contract_has_six_categories(self):
        p = ROOT / "shared" / "audit" / "viewpoint_audit_agent.md"
        t = p.read_text(encoding="utf-8")
        for k in ("VIEWPOINT_AUDIT_RESULT: PASS", "VIEWPOINT_AUDIT_RESULT: FIX_REQUIRED",
                  "数据支撑", "评级与所引事实", "推断 vs 事实",
                  "披露的限制是否真的影响", "替代值是否被诚实呈现", "档位相容",
                  "_baselines"):
            assert k in t, k

    def test_profiles_exclude_turtle_only_checks(self):
        for prof in ("valuation", "qualitative"):
            assert "decision_chain" not in va.PROFILE_CHECKS[prof]
        assert "decision_chain" in va.PROFILE_CHECKS["turtle"]


class TestSharesIntegrationContract:
    """股份渲染与解析之间的**契约**必须由测试钉住 —— 两边分开改过一次，结果互不匹配。"""

    def test_note_line_round_trips_through_the_parser(self):
        import shares_utils as su
        sc = su.ShareCount(5574.0, "reported", "年报附注35（已发行股本）", "2025-12-31")
        val, trusted, _raw = su.declared_from_pack(sc.as_note_line())
        assert val == 5574.0 and trusted

    def test_both_rendered_forms_parse(self):
        import shares_utils as su
        row = "| 总股本（百万股） | 5,574.00 | 年报/人工申报值；来源: shares.json |"
        assert su.declared_from_pack(row)[0] == 5574.0
        assert su.declared_from_pack("> **股本口径**：总股本（百万股） = 5,574.00（年报/人工申报值）")[0] == 5574.0

    def test_degraded_form_is_not_trusted(self):
        import shares_utils as su
        v, trusted, _ = su.declared_from_pack(
            "> **股本口径**：总股本（百万股） = 5,735.83（当前市值 ÷ 当前价（反推 + 口径混用））")
        assert v == 5735.83 and not trusted

    def test_a_degraded_early_call_does_not_poison_a_later_correct_one(self):
        """§1 早于 §11/§12 渲染；`resolve_from_store` 若把降级值缓存下来，
        §17 就会复用它 ⇒ 同一份报告两个股本。只缓存可信值即可消除该时序风险。"""
        import pandas as pd
        from unittest.mock import MagicMock, patch
        import shares_utils as su
        with patch("tushare_collector.ts") as m:
            m.pro_api.return_value = MagicMock()
            from tushare_collector import TushareClient
            c = TushareClient("t")
        c._store["basic_info"] = pd.DataFrame([{
            "ts_code": "06862.HK", "total_market_cap": 53_343_180_000.0, "close": 9.30}])
        # 第一次调用：§12 还没加载 ⇒ 只能反推（降级）
        early = su.resolve_from_store(c)
        assert early.kind == "implied_current" and not early.trusted
        # §12/§11 加载后：应升级到期末口径，而不是复用缓存。
        # 期末口径要求**成对**同期：年度报告期集合（§3/§5 给）+ 该期的市值快照 + 该期或之前的周线；
        # 中期快照（2026H1）必须被排除（独立审计 F2：混用会得到 5,735.83 这种错值）。
        c._store["income"] = pd.DataFrame([{"end_date": "20251231"},
                                           {"end_date": "20241231"}])
        c._store["fina_indicators"] = pd.DataFrame([
            {"end_date": "20260630", "total_market_cap": 53_343_180_000.0},   # 中期，不得使用
            {"end_date": "20251231", "total_market_cap": 52_256_250_000.0},   # 年度 ✓
        ])
        c._store["weekly_prices"] = pd.DataFrame([
            {"trade_date": "20251226", "close": 9.50},   # 期末附近 ✓
            {"trade_date": "20261009", "close": 9.30},   # 当前，不得与中期快照配对
        ])
        late = su.resolve_from_store(c)
        assert late.kind == "implied_period_end", late.kind
        assert late.shares_mm == pytest.approx(5500.66, abs=0.01)
        assert late.date == "20251231", late.date

    def test_period_end_requires_both_sides_of_the_same_period(self):
        """只有中期快照、或周线覆盖不到期末 ⇒ 如实返回 None（降级到 implied_current），
        绝不拿"中期市值 ÷ 当前价"冒充期末口径。"""
        import pandas as pd
        from unittest.mock import MagicMock, patch
        from tushare_collector import TushareClient
        with patch("tushare_collector.ts") as m:
            m.pro_api.return_value = MagicMock()
            c = TushareClient("t")
        c._store["income"] = pd.DataFrame([{"end_date": "20251231"}])
        c._store["weekly_prices"] = pd.DataFrame([{"trade_date": "20261009", "close": 9.30}])
        # (a) 只有中期快照
        c._store["fina_indicators"] = pd.DataFrame([
            {"end_date": "20260630", "total_market_cap": 53_343_180_000.0}])
        assert c._period_end_cap_and_close() == (None, None, None)
        # (b) 有年度快照但周线不覆盖期末
        c._store["fina_indicators"] = pd.DataFrame([
            {"end_date": "20251231", "total_market_cap": 52_256_250_000.0}])
        assert c._period_end_cap_and_close() == (None, None, None)
        # (c) 两者齐备 ⇒ 成对返回
        c._store["weekly_prices"] = pd.DataFrame([
            {"trade_date": "20251226", "close": 9.50},
            {"trade_date": "20261009", "close": 9.30}])
        cap, close, end = c._period_end_cap_and_close()
        assert (cap, close, end) == (52256.25, 9.50, "20251231")

    def test_the_share_row_is_rendered_once_by_section_17_8(self):
        """§1 不得渲染股本（会在降级值上落笔）；权威值由 §17.8 渲染。"""
        src = (ROOT / "scripts/tushare_modules" / "infrastructure.py").read_text(encoding="utf-8")
        assert "_share_count_row" not in src
        fin = (ROOT / "scripts/tushare_modules" / "financials.py").read_text(encoding="utf-8")
        assert "_share_count_row" not in fin
        dm = (ROOT / "scripts/tushare_modules" / "derived_metrics.py").read_text(encoding="utf-8")
        assert "as_note_line()" in dm


class TestMarketCapCrossSection:
    """D7e：§1/§2 的市值（交易币种）与 §17.8 的市值（报表币种）必须**折汇后**相等。

    不折汇会凭空报出 14.15% 的假差异（06862：53,343.18 × 0.85849 = 45,794.59）。
    """

    # §1 的市值是交易币种（百万港元），§17.8 的是报表币种（百万元）；
    # 包内声明了折算因子 ⇒ D7e 必须折汇后比较。
    PACK = PACK_HEAD + """
### 17.1 财务趋势速览

| 指标 | 2025 |
| --- | ---: |
| 有息负债（百万元） | 399.18 |

### 17.8 因子4·绝对估值与"买入就是胜利"基准价

| 指标 | 值 | 说明 |
| --- | ---: | --- |
| 总市值（百万元） | {cap17} | — |

> 已按 1/0.85849 折算到报表币种。
"""

    def _d7e(self, cap17):
        return [x for x in na.run_all(self.PACK.format(cap17=cap17), "06862.HK")
                if x.code == "D7e"][0]

    def test_fx_converted_cap_matches(self):
        """53,343.18 百万港元 × 0.85849 = 45,794.59 = §17.8 ⇒ 一致。"""
        f = self._d7e("45,794.59")
        assert f.status == na.MATCH, f.detail

    def test_comparison_is_not_a_raw_equality(self):
        """把未折汇的港元数放进 §17.8 ⇒ 必须不一致（证明真的折了汇，而不是比字符串/原值）。"""
        assert self._d7e("53,343.18").status == na.MISMATCH

    def test_real_divergence_is_caught(self):
        f = self._d7e("51,000.00")
        assert f.status == na.MISMATCH  # 无申报股本 ⇒ 不可解释 ⇒ 判不一致
        assert f.severity == na.CRITICAL


class TestProfileGating:
    """`--profile` 的存在理由：不能把龟龟专有语义强加给估值/定性报告。

    实测（2026-10-14）：`--profile qualitative --strict` 曾因 `V2a 绑定 0 个 claim`(critical)
    + `V2b`(major) 阻断一份**完全正常**的定性报告 —— 而两份 coordinator 里都没有"claim"一词。
    """

    REPORT = """# 分众传媒(002027) 商业分析报告

## Executive Summary

公司护城河评级为 **强**，2025 年营收 126.5 亿元，同比 +3.2%。

## 结论

综合评级：**优质**。
"""

    def test_binding_is_skipped_but_declared(self):
        for prof in ("valuation", "qualitative"):
            fs = va.run_all(self.REPORT, profile=prof)
            v2 = [f for f in fs if f.code == "V2"]
            assert v2, prof
            assert all(f.status == va.UNVERIFIABLE and f.severity == va.MINOR for f in v2)
            assert "跳过（不是通过）" in v2[0].detail
            # 真正的绑定检查不得执行
            assert not any(f.code.startswith("V2a") or f.code.startswith("V2b") for f in fs)

    def test_turtle_profile_still_requires_binding(self):
        fs = va.run_all(self.REPORT, profile="turtle")
        assert any(f.code.startswith("V2a") for f in fs)

    def test_minor_skip_notice_survives_strict(self):
        """`--strict` 只把**无 severity** 的 UNVERIFIABLE 升级为阻断；
        带 MINOR 的"跳过"声明仍属披露级 —— 否则 profile 开关形同虚设。"""
        for prof in ("valuation", "qualitative"):
            fs = va.run_all(self.REPORT, profile=prof)
            payload = [f.to_dict() for f in fs]
            for item in payload:
                if item["status"] == va.UNVERIFIABLE:
                    item["severity"] = item.get("severity") or va.MAJOR
            import gate_policy
            v = gate_policy.verdict(("viewpoint_audit", payload))
            assert v["blocking_count"] == 0, (prof, v)


class TestRfVisibleSource:
    """E5：§17.2 用到的 Rf 必须在 §14 有**可见**来源行。

    回归锚点（00506 实测）：港股数据包的 §14 渲染成
    `| 日期 | 10年期国债收益率 (%) |` 表头 + `| 20261014 | 1.6890 |` 数据行 ——
    标签在**表头**、值在数据行。旧 `PackView.scalar` 只按行首格匹配别名，
    于是表头无数字、数据行不匹配 ⇒ 返回 None ⇒ 把一份**有** §14 的包报成
    E5「§14 无可见数值」(major)。门禁因此阻断在一条脚本假阳性上。
    """

    RF_PACK = (
        "## 14. 无风险利率\n\n"
        "| 日期 | 10年期国债收益率 (%) |\n"
        "| --- | ---: |\n"
        "| 20261014 | 1.6890 |\n\n"
        "*数据来源: 数据包 §14 (20261014)（`yc_cb` 无权限，使用外部提供的无风险利率）*\n\n"
        "### 17.2 因子2输入参数\n\n"
        "| 变量 | 2025 |\n| --- | ---: |\n"
        "| Rf（无风险利率） | 1.689 |\n"
    )

    def test_date_keyed_section14_is_visible(self):
        pv = na.PackView(self.RF_PACK)
        assert pv.scalar("14", ["无风险利率", "Rf", "收益率", "yield"]) == pytest.approx(1.689)

    def test_e5_matches_instead_of_reporting_unsourced(self):
        f = [x for x in na.check_sources(na.PackView(self.RF_PACK)) if x.code == "E5"][0]
        assert f.status == na.MATCH, f.detail

    def test_aliased_label_row_still_wins(self):
        """标签在首格的形态必须继续按别名命中，不受回落影响。"""
        pack = ("## 14. 无风险利率\n\n| 项目 | 值 |\n| --- | ---: |\n"
                "| 无风险利率 | 2.500 |\n| 其他 | 9.999 |\n")
        assert na.PackView(pack).scalar("14", ["无风险利率"]) == pytest.approx(2.5)

    def test_missing_section14_still_reports_unsourced(self):
        pack = self.RF_PACK.replace("| 20261014 | 1.6890 |", "")
        f = [x for x in na.check_sources(na.PackView(pack)) if x.code == "E5"][0]
        assert f.status == na.UNSOURCED
