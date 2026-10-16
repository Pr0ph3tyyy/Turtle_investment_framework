"""Tests for scripts/field_coverage.py —— 「自洽 ≠ 完整」这一洞的门禁。

回归锚点：06862 的真实 P0 缺口——上游有 `应付债券 2,028,881 千元`，但映射标签被写成
『应付票据(非流动)』⇒ §4 的「应付债券」行**声明了却全年度为空**，有息负债漏计 20.29 亿元，
而 I1–I7 全部绿灯（它们只查包内自洽）。本门禁必须机械抓住这一类。
"""

import pathlib

import pytest

import field_coverage as fc

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEGACY = ROOT / "tests" / "fixtures" / "legacy_packs" / "06862_pre_fix_data_pack.md"

# 一份"干净"的最小包：必备行齐备、债类各自成行且非空
CLEAN = """
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
| 长期贷款 | — | 12.00 |
| 应付债券 | 2,028.88 | 2,073.18 |
| 融资租赁负债(流动) | 856.80 | 888.82 |
| 融资租赁负债(非流动) | 2,649.70 | 3,069.10 |
| 总资产 | 22,090.20 | 21,141.00 |
| 总负债 | 12,085.07 | 10,600.00 |
| 股东权益 | 10,013.93 | 10,433.79 |

## 5. 现金流量表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 经营业务现金净额 (OCF) | 5,664.19 | 7,634.47 |
| 投资业务现金净额 | -3,029.97 | -91.49 |
| 融资业务现金净额 | -4,945.93 | -7,630.06 |
| 资本开支（购建固定＋无形及其他） | 1,748.38 | 1,309.18 |

### 17.1 财务趋势速览

| 指标 | 2025 | 2024 |
| --- | ---: | ---: |
| 有息负债（百万元） | 2,428.06 | 2,170.72 |
"""


def _codes(findings):
    return {f.code: f for f in findings}


class TestRequiredRows:
    def test_clean_pack_passes(self):
        assert fc.check_required_rows(CLEAN).status == fc.PASS

    def test_missing_required_row_fails(self):
        bad = CLEAN.replace("| 税项 | 1,769.81 | 1,923.78 |", "")
        f = fc.check_required_rows(bad)
        assert f.status == fc.FAIL and any("税项" in e for e in f.evidence)

    def test_missing_section_is_reported(self):
        f = fc.check_required_rows("## 3. 合并利润表\n\n无表\n")
        assert f.status == fc.FAIL and any("§4" in e or "§5" in e for e in f.evidence)


class TestDebtComposition:
    def test_clean_pack_passes(self):
        f = fc.check_debt_composition(CLEAN)
        assert f.status == fc.PASS, f.evidence

    def test_declared_but_empty_debt_row_fails(self):
        """核心回归：行在、值全空 ⇒ 标签未命中上游（06862 的债券就是这一形态）。"""
        bad = CLEAN.replace("| 应付债券 | 2,028.88 | 2,073.18 |", "| 应付债券 | — | — |")
        f = fc.check_debt_composition(bad)
        assert f.status == fc.FAIL
        assert "全年度为空" in f.detail or "标签" in f.detail
        assert any("债券" in e for e in f.evidence)

    def test_absent_debt_class_with_positive_debt_fails(self):
        bad = CLEAN.replace("| 应付债券 | 2,028.88 | 2,073.18 |", "")
        bad = bad.replace("| 长期贷款 | — | 12.00 |", "")   # 同时移除长期借款行
        f = fc.check_debt_composition(bad)
        assert f.status == fc.FAIL
        assert any("漏渲染" in f.detail or "缺失债类" in e for e in [f.detail] + f.evidence)

    def test_zero_value_row_warns_by_default_and_fails_in_strict(self):
        """显式 0.00 在包内**无法与"映射未命中后渲染成 0"区分** ⇒ 默认 WARN 披露、
        交付路径（--strict）升级为 FAIL。旧实现把它判 PASS，于是把 `—` 改成 `0.00`
        就是一条把阻断级 FAIL 洗成通过的路径。"""
        zeroed = CLEAN.replace("| 应付债券 | 2,028.88 | 2,073.18 |", "| 应付债券 | 0.00 | 0.00 |")
        f = fc.check_debt_composition(zeroed)
        assert f.status == fc.WARN, f.evidence
        assert f.strict_status == fc.FAIL
        assert any("0.00" in e for e in f.evidence)
        assert f.effective_status(strict=False) == fc.WARN
        assert f.effective_status(strict=True) == fc.FAIL

    def test_anchor_missing_fails_instead_of_passing(self):
        """锚点取不到 ⇒ 不可核验 ≠ 通过。旧实现 `debt_total is None` 直接落到 PASS，
        于是"把 §17.1 的有息负债行删掉"是一条洗白路径。"""
        no_anchor = CLEAN.replace("| 有息负债（百万元） | 2,428.06 | 2,170.72 |",
                                  "| 其他指标 | 1.00 | 1.00 |")
        f = fc.check_debt_composition(no_anchor)
        assert f.status == fc.FAIL, f.evidence
        assert "金额行" in f.detail or "锚点" in f.detail

    def test_ratio_row_cannot_serve_as_anchor(self):
        """`有息负债/总资产（%）` 被剥掉 % 后同样能过数字正则（实测 13.72），
        必须在标签层排除 —— 否则删掉金额行后比率行会被当成锚点。"""
        only_ratio = CLEAN.replace(
            "| 有息负债（百万元） | 2,428.06 | 2,170.72 |",
            "| 有息负债/总资产（%） | 13.72 | 12.11 |")
        f = fc.check_debt_composition(only_ratio)
        assert f.status == fc.FAIL
        assert "未取到" in " ".join(f.evidence) or "金额行" in f.detail

    def test_class_sum_below_anchor_fails(self):
        """各类之和低于锚点 ⇒ 至少一类被漏渲染/清零（单边判据）。"""
        under = CLEAN.replace("| 应付债券 | 2,028.88 | 2,073.18 |", "| 应付债券 | 10.00 | 10.00 |")
        under = under.replace("| 融资租赁负债(流动) | 856.80 | 888.82 |",
                              "| 融资租赁负债(流动) | 10.00 | 10.00 |")
        under = under.replace("| 融资租赁负债(非流动) | 2,649.70 | 3,069.10 |",
                              "| 融资租赁负债(非流动) | 10.00 | 10.00 |")
        f = fc.check_debt_composition(under)
        assert f.status == fc.FAIL, f.evidence
        assert any("求和" in e for e in f.evidence)

    def test_split_lease_rows_any_populated_is_present(self):
        """回归（00696 实测假 FAIL）：债类被拆成流动/非流动两行时，
        流动行全空 **不** 等于该类全空 —— 只取第一行的实现会误报 FAIL。"""
        # 00696 的真实形态：非流动行有值、流动行全空
        split = CLEAN.replace("| 融资租赁负债(流动) | 856.80 | 888.82 |",
                              "| 融资租赁负债(流动) | — | — |")
        f = fc.check_debt_composition(split)
        assert f.status == fc.PASS, f.evidence
        assert any("租赁负债" in e for e in f.evidence), f.evidence

    def test_split_lease_rows_all_empty_still_fails(self):
        """若两行都空，仍必须 FAIL —— 修的是"只看第一行"，不是放宽判据。"""
        split = CLEAN.replace("| 融资租赁负债(流动) | 856.80 | 888.82 |", "| 融资租赁负债(流动) | — | — |")
        split = split.replace("| 融资租赁负债(非流动) | 2,649.70 | 3,069.10 |",
                              "| 融资租赁负债(非流动) | — | — |")
        f = fc.check_debt_composition(split)
        assert f.status == fc.FAIL
        assert any("租赁负债" in e for e in f.evidence), f.evidence

    def test_missing_sections_is_unevaluated(self):
        assert fc.check_debt_composition("没有小节").status == fc.UNEVALUATED


class TestCli:
    def test_exit_codes(self, tmp_path):
        good = tmp_path / "g.md"
        good.write_text(CLEAN, encoding="utf-8")
        assert fc.main(["--pack", str(good), "--quiet"]) == 0
        bad = tmp_path / "b.md"
        bad.write_text(CLEAN.replace("| 应付债券 | 2,028.88 | 2,073.18 |", "| 应付债券 | — | — |"),
                       encoding="utf-8")
        assert fc.main(["--pack", str(bad), "--quiet"]) == 1
        assert fc.main(["--pack", str(tmp_path / "nope.md"), "--quiet"]) == 2

    def test_json_output(self, tmp_path):
        good = tmp_path / "g.md"
        good.write_text(CLEAN, encoding="utf-8")
        out = tmp_path / "fc.json"
        fc.main(["--pack", str(good), "--json", str(out), "--quiet"])
        import json
        assert json.loads(out.read_text(encoding="utf-8"))["findings"]


@pytest.mark.skipif(not LEGACY.exists(), reason="冻结的旧数据包夹具不存在")
class TestLegacyPackRegression:
    """冻结夹具（修映射之前的 06862 包）：债务构成检查必须 FAIL。"""

    def test_legacy_pack_is_caught(self):
        findings = _codes(fc.run_all(LEGACY.read_text(encoding="utf-8")))
        assert findings["C2"].status == fc.FAIL, "修映射前的包必须被债务构成检查抓住"
        assert any("债券" in e for e in findings["C2"].evidence)


class TestDebtAbsenceEvidence:
    """「声明了却全年度为空」的**第二种**成因：这家发行人真的没有该科目。

    为什么必须分开：C2 只能看到"空行"这个结果，而发行人的资产负债表**结构上**就
    分不出「没有银行借款」与「映射标签没命中上游」。实测 100 个产物中 40 个因此
    FAIL（五粮液/中国移动/江苏金租全部中招）⇒ 假阳性率 40% 会让
    `--allow-degraded` 变成常态，门禁随即失效。

    判据取**上游科目清单**（采集器写入 field_absence.json），且必须双向 fail-closed：
      · 有 `verified_absent` 证据 ⇒ 放行（披露级，--strict 也不阻断）；
      · 没证据 / 半份证据 / A 股 unreported ⇒ 保持 FAIL（删 sidecar 不是洗白路径）。
    """

    # 00506 形态：上游只有租赁负债，短贷/长贷/债券三个科目**在展示期内没有数值**
    NO_DEBT = CLEAN.replace("| 短期贷款 | 399.18 | 97.55 |", "| 短期贷款 | — | — |") \
                   .replace("| 应付债券 | 2,028.88 | 2,073.18 |", "| 应付债券 | — | — |")

    EVIDENCE = {
        "4.st_borr": {"section": "4", "field": "st_borr", "status": "verified_absent",
                      "row_labels": ["短期贷款", "短期借款"]},
        "4.bond_payable": {"section": "4", "field": "bond_payable", "status": "verified_absent",
                           "row_labels": ["应付债券"]},
    }

    def test_verified_absent_releases_the_gate(self):
        f = fc.check_debt_composition(self.NO_DEBT, self.EVIDENCE)
        assert f.status == fc.PASS, f.evidence
        # 披露仍然要留下痕迹：读者必须看得到"这些空行是被证实过的"
        assert any("上游科目清单证实不存在" in e for e in f.evidence)
        assert f.effective_status(strict=True) == fc.PASS, "可核验的证据不该在交付路径被升级"

    def test_no_sidecar_is_fail_closed(self):
        f = fc.check_debt_composition(self.NO_DEBT, {})
        assert f.status == fc.FAIL

    def test_partial_evidence_still_fails(self):
        """只有债券那一份证据、短贷没有 ⇒ 不能放行（证据必须逐类齐全）。"""
        f = fc.check_debt_composition(
            self.NO_DEBT, {"4.bond_payable": self.EVIDENCE["4.bond_payable"]})
        assert f.status == fc.FAIL

    def test_mapping_incomplete_status_never_releases(self):
        """上游存在无人认领的债类科目 ⇒ 疑似映射错位，证据必须被拒。"""
        bad = dict(self.EVIDENCE)
        bad["4.bond_payable"] = {**self.EVIDENCE["4.bond_payable"],
                                 "status": "mapping_incomplete",
                                 "upstream_debt_names_unmapped": ["应付债券"]}
        f = fc.check_debt_composition(self.NO_DEBT, bad)
        assert f.status == fc.FAIL

    def test_reported_zero_is_not_absence_evidence(self):
        bad = dict(self.EVIDENCE)
        bad["4.bond_payable"] = {**self.EVIDENCE["4.bond_payable"], "status": "reported_zero"}
        f = fc.check_debt_composition(self.NO_DEBT, bad)
        assert f.status == fc.FAIL

    def test_legacy_06862_shape_still_caught(self):
        """06862 的真实形态：上游**有**应付债券（带值），映射写错 ⇒ 永远拿不到证据。"""
        assert LEGACY.is_file()
        pack = LEGACY.read_text(encoding="utf-8")
        assert fc.check_debt_composition(pack, {}).status == fc.FAIL

    def test_run_all_reads_sidecar_from_pack_dir(self, tmp_path):
        """sidecar 由采集器写在包同级目录；run_all 必须自己找到它。"""
        import json
        pack = tmp_path / "data_pack_market.md"
        pack.write_text(self.NO_DEBT, encoding="utf-8")
        (tmp_path / "field_absence.json").write_text(
            json.dumps({"schema_version": "1.0", "fields": self.EVIDENCE}),
            encoding="utf-8")
        findings = {f.code: f for f in fc.run_all(pack.read_text(encoding="utf-8"),
                                                  pack_path=str(pack))}
        assert findings["C2"].status == fc.PASS, findings["C2"].evidence

    def test_run_all_without_sidecar_fails(self, tmp_path):
        pack = tmp_path / "data_pack_market.md"
        pack.write_text(self.NO_DEBT, encoding="utf-8")
        findings = {f.code: f for f in fc.run_all(pack.read_text(encoding="utf-8"),
                                                  pack_path=str(pack))}
        assert findings["C2"].status == fc.FAIL


class TestSection4KeyRows:
    """C3 §4 决策关键行不得"声明但从未有值"。

    起因（known_issues §四十三）：Tushare 对**不存在的字段名静默忽略** ——
    请求 `intang_assets`（正确名 `intan_assets`）导致 §4「无形资产」行
    **100/103 个产物全空**，而 §17.15 的 A 严口径就把它按 0 扣除 ⇒ 系统性高估下行净值
    （000708 实测高估 71.8 亿）。当时 §17.15 无任何门禁，所以无人发现。
    """

    @staticmethod
    def _pack(it_vals=("7,039.48", "7,107.00"), gw="18.33", cash="11,273.08"):
        return (
            "## 4. 合并资产负债表\n\n"
            "| 项目 (百万元) | 2025 | 2024 |\n| --- | ---: | ---: |\n"
            f"| 无形资产 | {' | '.join(it_vals)} |\n"
            f"| 商誉 | {gw} | {gw} |\n"
            f"| 现金及等价物 | {cash} | {cash} |\n"
            "| 总资产 | 110,322.60 | 108,000.00 |\n"
            "| 总负债 | 60,000.00 | 58,000.00 |\n"
        )

    def test_all_key_rows_populated_passes(self):
        f = fc.check_section4_key_rows(self._pack())
        assert f.status == fc.PASS, f.detail + str(f.evidence)

    def test_all_empty_row_warns_and_is_strict_fail(self):
        """行存在但展示窗口内全为空 ⇒ 默认 WARN、交付路径（--strict）FAIL。"""
        f = fc.check_section4_key_rows(self._pack(it_vals=("—", "—")))
        assert f.status == fc.WARN
        assert f.effective_status(strict=True) == fc.FAIL
        assert any("无形资产" in e for e in f.evidence)

    def test_missing_row_warns(self):
        pack = ("## 4. 合并资产负债表\n\n| 项目 (百万元) | 2025 |\n| --- | ---: |\n"
                "| 商誉 | 18.33 |\n| 现金及等价物 | 1.00 |\n"
                "| 总资产 | 2.00 |\n| 总负债 | 1.00 |\n")
        f = fc.check_section4_key_rows(pack)
        assert f.status == fc.WARN
        assert any("没有「无形资产」行" in e for e in f.evidence)

    def test_cash_label_alias_is_accepted(self):
        """A 股用「货币资金」、港股用「现金及等价物」—— 任一存在即算覆盖。"""
        pack = ("## 4. 合并资产负债表\n\n| 项目 (百万元) | 2025 |\n| --- | ---: |\n"
                "| 无形资产 | 1.00 |\n| 商誉 | 1.00 |\n| 货币资金 | 1.00 |\n"
                "| 总资产 | 2.00 |\n| 总负债 | 1.00 |\n")
        f = fc.check_section4_key_rows(pack)
        assert f.status == fc.PASS, f.detail + str(f.evidence)

    def test_missing_section_is_unevaluated(self):
        assert fc.check_section4_key_rows("## 1. x\n").status == fc.UNEVALUATED
