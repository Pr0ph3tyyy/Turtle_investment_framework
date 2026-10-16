"""Tests for scripts/invariants.py — the pack-level invariant engine.

Each test states the *invariant*, feeds a pack that either satisfies or violates it, and
checks the verdict. The last test is the regression that matters: the real 06862 data pack
must fail exactly the five invariants that the manual review found broken.
"""

import pathlib

import pytest

import invariants as inv

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_PACK = ROOT / "output" / "06862_海底捞" / "data_pack_market.md"
REAL_FX = ROOT / "output" / "06862_海底捞" / "fx.json"


# ─────────────────────────── fixtures ───────────────────────────

CLEAN_PACK = """
### 17.1 财务趋势速览

| 指标 | 2025 | 2024 | 2023 | 5年CAGR |
| --- | ---: | ---: | ---: | ---: |
| 营业收入（百万元） | 43,225.36 | 42,754.69 | 41,453.35 | 2.54% |
| 归母净利润（百万元） | 4,049.82 | 4,708.08 | 4,499.08 | N/A（基年负） |

> 营业收入：基年 2021，**4 期**（2021–2025）
> ⚠️ 归母净利润：基年 2021 为负（-4,163.18），**CAGR 在该窗口无定义** ⇒ 本列记 N/A；若改用首个盈利年 2022 为基年，3 期 CAGR = 43.36%（**口径不同，不得与其它行并列或相加**）

### 17.8 因子4·绝对估值与"买入就是胜利"基准价

| 方法 | 基准价（港元） | 计算逻辑 |
| --- | ---: | --- |
| ① 净流动资产/股 | 1.30 | (现金+交易性金融资产−有息负债)/总股本；已按 1/0.85849 由报表币种折为港元 |
| ② 每股净资产 | 2.09 | 归母权益/总股本；已按 1/0.85849 由报表币种折为港元 |
| ③ 10年最低价 | 9.08 | 周线最低收盘价 |
| ④ 股息隐含价 | 25.37 | 3年均DPS÷max(Rf,3%) |

> **币种说明**：本表各行一律为**交易币种（港元）**。① ② ⑤ 由报表币种（元/股）按 1/0.85849 折算后参与算术平均；未经折算的报表币种每股量**不得**与本表混用或求均值。

**综合基准价（算术平均）= 9.46 港元**

## 4. 合并资产负债表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 现金及现金等价物 | 6,602.35 | 7,474.82 |

## 5. 现金流量表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 经营业务现金净额 (OCF) | 5,664.19 | 7,634.47 |
| 投资业务现金净额 | -3,029.97 | -91.49 |
| 融资业务现金净额 | -3,506.69 | -7,630.06 |

## 6. 分红历史

| 年度 | 每股股息 (HKD) | 派息率 (%) |
| --- | ---: | ---: |
| 2025 | 0.3380 | 44.10 |
| 2024 | 0.5070 | 60.20 |
| 2023 | 0.1500 | 20.00 |

### 17.13 收入敏感性（λ 外推）

| 收入情景 | 结余（百万元） | 精算回报率 GG | vs 门槛 pct |
| --- | ---: | ---: | ---: |
| 1.0× | 4,110.05 | 5.99% | +0.99 |
| 0.9× | 3,699.04 | 5.39% | +0.39 |
| 0.8× | 3,288.04 | 4.79% | -0.21 |
"""

# §5 与 §4 对不上（2025 差额 1,439.24），其余同上
BAD_CASH_PACK = CLEAN_PACK.replace("-3,506.69", "-4,945.93")


def _by_code(findings):
    return {f.code: f for f in findings}


def _fx():
    return {"factor_to_reporting": 0.85849, "reporting_currency": "CNY",
            "trading_currency": "HKD"}


# ─────────────────────────── individual invariants ───────────────────────────

class TestCagrInvariant:
    def test_clean_pack_labels_every_cagr(self):
        assert inv.invariant_cagr_periods(CLEAN_PACK).status == inv.PASS

    def test_bare_number_without_period_fails(self):
        bad = CLEAN_PACK.replace("2.54% |", "2.54% |").replace(
            "> 营业收入：基年 2021，**4 期**（2021–2025）", "")
        f = inv.invariant_cagr_periods(bad)
        assert f.status == inv.FAIL
        assert any("未标注基年/期数" in e or "无法判定" in e for e in f.evidence)

    def test_na_without_reason_fails(self):
        bad = CLEAN_PACK.replace(
            "> ⚠️ 归母净利润：基年 2021 为负（-4,163.18），**CAGR 在该窗口无定义** ⇒ 本列记 N/A；"
            "若改用首个盈利年 2022 为基年，3 期 CAGR = 43.36%"
            "（**口径不同，不得与其它行并列或相加**）", "")
        f = inv.invariant_cagr_periods(bad)
        assert f.status == inv.FAIL

    def test_missing_section_is_unevaluated_not_pass(self):
        assert inv.invariant_cagr_periods("no sections here").status == inv.UNEVALUATED


class TestPerShareCurrencyInvariant:
    def test_declared_conversion_passes(self):
        assert inv.invariant_per_share_currency(CLEAN_PACK, _fx()).status == inv.PASS

    def test_undeclared_conversion_fails(self):
        bad = CLEAN_PACK.replace("> **币种说明**", "> **另一段说明**")
        assert inv.invariant_per_share_currency(bad, _fx()).status == inv.FAIL

    def test_same_currency_needs_no_declaration(self):
        assert inv.invariant_per_share_currency(CLEAN_PACK, {"factor_to_reporting": 1.0}).status == inv.PASS

    def test_no_fx_file_is_treated_as_single_currency(self):
        assert inv.invariant_per_share_currency(CLEAN_PACK, None).status == inv.PASS


class TestCashTieOutInvariant:
    def test_reconciling_pack_passes(self):
        assert inv.invariant_cash_tie_out(CLEAN_PACK).status == inv.PASS

    def test_broken_reconciliation_fails_with_gap(self):
        f = inv.invariant_cash_tie_out(BAD_CASH_PACK)
        assert f.status == inv.FAIL
        assert any("差额" in e for e in f.evidence)

    def test_unevaluated_when_sections_missing(self):
        assert inv.invariant_cash_tie_out("### 17.1 x").status == inv.UNEVALUATED


class TestDividendIntegrityInvariant:
    def test_varying_dps_passes(self):
        assert inv.invariant_dividend_integrity(CLEAN_PACK).status == inv.PASS

    def test_constant_dps_fails(self):
        bad = CLEAN_PACK.replace("| 2025 | 0.3380 | 44.10 |", "| 2025 | 0.7610 | 44.10 |") \
                       .replace("| 2024 | 0.5070 | 60.20 |", "| 2024 | 0.7610 | 60.20 |") \
                       .replace("| 2023 | 0.1500 | 20.00 |", "| 2023 | 0.7610 | 20.00 |")
        f = inv.invariant_dividend_integrity(bad)
        assert f.status == inv.FAIL and "完全相同" in f.detail

    def test_constant_pe_pb_columns_fail(self):
        """§12 的 PE/PB 跨年度恒定（06862 实测 14.05 / 5.74）与 §6 DPS 同属填充重复缺陷。"""
        pack = CLEAN_PACK + """
## 12. 关键财务指标

| 指标 | 2025 | 2024 | 2023 | 2022 | 2021 |
| --- | ---: | ---: | ---: | ---: | ---: |
| PE (TTM) | 11.62 | 11.39 | 14.05 | 14.05 | 14.05 |
| PB | 4.74 | 4.65 | 5.74 | 5.74 | 5.74 |
"""
        f = inv.invariant_dividend_integrity(pack)
        assert f.status == inv.FAIL
        assert "§12" in f.detail and "PE (TTM)" in f.detail

    def test_duplicate_year_rows_fail(self):
        bad = CLEAN_PACK.replace("| 2025 | 0.3380 | 44.10 |",
                                 "| 2025 | 0.3380 | 44.10 |\n| 2025 | 0.3840 | 50.00 |")
        f = inv.invariant_dividend_integrity(bad)
        assert f.status == inv.FAIL and "重复行" in f.detail


class TestSensitivityDirectionInvariant:
    def test_descending_balance_passes(self):
        assert inv.invariant_sensitivity_direction(CLEAN_PACK).status == inv.PASS

    def test_balance_rising_as_revenue_falls_fails(self):
        bad = CLEAN_PACK.replace("| 0.9× | 3,699.04 |", "| 0.9× | 10,415.39 |") \
                        .replace("| 0.8× | 3,288.04 |", "| 0.8× | 16,720.74 |")
        f = inv.invariant_sensitivity_direction(bad)
        assert f.status == inv.FAIL and "方向性错误" in f.detail

    def test_absent_section_passes(self):
        """§17.13 不输出是 fail-closed 的正确结果，不算违反。"""
        f = inv.invariant_sensitivity_direction(CLEAN_PACK.split("### 17.13")[0])
        assert f.status == inv.PASS


# ─────────────────────────── aggregate + CLI ───────────────────────────

class TestPriceConsistencyInvariant:
    """I7：§2 最新价与 §17 网格所用价格必须一致（--refresh-market 不重建 §17，会脱钩）。"""

    PACK = """
## 2. 市场行情

| 指标 | 数值 |
| --- | ---: |
| 最新价格 (HKD) | 9.30 |

### 17.8 因子4·绝对估值

当前股价 9.30 港元，较**综合底价**（4.12）溢价 **+125.7%**
"""

    def test_matching_prices_pass(self):
        assert inv.invariant_price_consistency(self.PACK).status == inv.PASS

    def test_divergent_prices_fail(self):
        bad = self.PACK.replace("当前股价 9.30 港元", "当前股价 8.10 港元")
        f = inv.invariant_price_consistency(bad)
        assert f.status == inv.FAIL and "脱钩" in f.detail

    def test_missing_price_is_unevaluated(self):
        assert inv.invariant_price_consistency("## 2. 市场行情\n\n无价格\n").status == inv.UNEVALUATED


class TestRunAll:
    def test_clean_pack_has_no_failures(self):
        findings = inv.run_all(CLEAN_PACK, _fx())
        assert [f.code for f in findings if f.status == inv.FAIL] == []

    def test_engine_failure_is_reported_not_swallowed(self, monkeypatch):
        def boom(_pack):
            raise RuntimeError("kaboom")
        monkeypatch.setattr(inv, "INVARIANTS", (boom,))
        findings = inv.run_all(CLEAN_PACK)
        assert findings[0].status == inv.FAIL and "kaboom" in findings[0].detail

    def test_cli_exit_codes(self, tmp_path, capsys):
        good = tmp_path / "good.md"
        good.write_text(CLEAN_PACK, encoding="utf-8")
        fx = tmp_path / "fx.json"
        fx.write_text('{"factor_to_reporting": 0.85849}', encoding="utf-8")
        assert inv.main(["--pack", str(good), "--fx", str(fx), "--quiet"]) == 0

        bad = tmp_path / "bad.md"
        bad.write_text(BAD_CASH_PACK, encoding="utf-8")
        assert inv.main(["--pack", str(bad), "--fx", str(fx), "--quiet"]) == 1

    def test_cli_missing_pack_is_exit_2(self, tmp_path):
        assert inv.main(["--pack", str(tmp_path / "nope.md"), "--quiet"]) == 2

    def test_strict_treats_unevaluated_as_failure(self, tmp_path):
        thin = tmp_path / "thin.md"
        thin.write_text("just some text", encoding="utf-8")
        assert inv.main(["--pack", str(thin), "--quiet"]) == 0
        assert inv.main(["--pack", str(thin), "--quiet", "--strict"]) == 1

    def test_json_output(self, tmp_path):
        out = tmp_path / "inv.json"
        # ⚠️ 原先 fallback 成 `tmp_path`（一个**目录**）⇒ 归档旧产物后立刻 exit 2
        # （`Is a directory`）。这里改成写一个最小 pack 文件，使本用例 hermetic。
        pack = REAL_PACK
        if not pack.exists():
            pack = tmp_path / "pack.md"
            pack.write_text("## 1. 基本信息\n\n| 项目 | 内容 |\n| --- | --- |\n"
                            "| 总市值 (百万元) | 1,000.00 |\n", encoding="utf-8")
        assert inv.main(["--pack", str(pack),
                         "--json", str(out), "--quiet"]) in (0, 1)


LEGACY_PACK = ROOT / "tests" / "fixtures" / "legacy_packs" / "06862_pre_fix_data_pack.md"


@pytest.mark.skipif(not LEGACY_PACK.exists(), reason="冻结的旧数据包夹具不存在")
class TestLegacyPackRegression:
    """冻结夹具：修复前的 06862 数据包必须被机械判定出全部 5 项缺陷。

    用**夹具**而不是实时产物 —— 一旦重跑数据包（修复生效），I1/I2/I6 会转 PASS，
    断言"恰好 5 项 FAIL"就会随产物漂移而失效（这正是本测试最初踩的坑）。
    """

    def test_all_five_defects_are_caught(self):
        findings = _by_code(inv.run_all(LEGACY_PACK.read_text(encoding="utf-8"), _fx_dict()))
        failed = {code for code, f in findings.items() if f.status == inv.FAIL}
        # I3 不在此列：修复前那份包的现金行是宽口径（银行结余及现金），
        # 当时报 FAIL 属**口径误报**，2026-10-13 已纠正为 UNEVALUATED。
        # 用**包含**断言而非全等：后续新增不变式（如 I9 股数交叉核验）也会在旧包上 FAIL，
        # 而历史缺陷必须**全部仍被抓到**（全等断言会随新增检查而失效——产物耦合债）
        must_catch = {"I1", "I2", "I4", "I5", "I6"}
        assert must_catch <= failed, f"历史缺陷未被全部抓到，实际 FAIL：{failed}"
        assert findings["I3"].status == inv.UNEVALUATED


@pytest.mark.skipif(not REAL_PACK.exists(), reason="06862 数据包不存在")
class TestLivePackStatus:
    """实时数据包：修复生效后 I1/I2 必须不再 FAIL；两个未修根因仍应被报出。"""

    def test_fixed_invariants_no_longer_fail(self):
        findings = _by_code(inv.run_all(REAL_PACK.read_text(encoding="utf-8"), _fx_dict()))
        for code in ("I1", "I2"):
            assert findings[code].status != inv.FAIL, f"{code} 仍 FAIL：{findings[code].evidence}"

    def test_unfixed_root_causes_are_still_reported(self):
        findings = _by_code(inv.run_all(REAL_PACK.read_text(encoding="utf-8"), _fx_dict()))
        assert findings["I3"].status == inv.UNEVALUATED, \
            "现金行是宽口径 ⇒ 数据包层无法勾稽，应记 UNEVALUATED 而不是误报 FAIL"
        assert findings["I5"].status == inv.FAIL, "§6 分红重复行根因未修，必须仍被报出"
        assert findings["I4"].status == inv.FAIL, "§3 同年口径混用（clean-room 发现）必须被报出"


def _fx_dict():
    if REAL_FX.exists():
        import json
        return json.loads(REAL_FX.read_text(encoding="utf-8"))
    return {"factor_to_reporting": 0.85849, "reporting_currency": "CNY",
            "trading_currency": "HKD"}


# ─────────────── 上游字段体检（I8/I9/I10）───────────────────────

BAL = """
## 2. 市场行情

| 指标 | 数值 |
| --- | ---: |
| 最新价格 (HKD) | 9.30 |
| 总市值 | 53,343.18 |

## 4. 合并资产负债表

| 项目 (百万元) | 2026H1 | 2025 | 2024 | 2023 |
| --- | ---: | ---: | ---: | ---: |
| 股东权益 | 9,765.18 | 10,013.93 | 10,433.79 | 11,526.63 |
| 少数股东权益 | 8.08 | -8.80 | -16.30 | -9.69 |
| 总负债 | 11,373.45 | 12,085.07 | 12,363.76 | 13,160.06 |
| 总资产 | 21,146.71 | 22,090.20 | 22,781.26 | 24,677.00 |
| 商誉 | 84.84 | 84.84 | 84.84 | 84.84 |

## 12. 关键财务指标

| 指标 | 2026H1 | 2025 | 2024 | 2023 |
| --- | ---: | ---: | ---: | ---: |
| ROE (%) | 17.87 | 39.61 | 42.88 | 47.43 |
| 每股净资产 (元) | 1.75 | 1.80 | 1.87 | 2.07 |
"""


class TestBalanceIdentity:
    def test_exact_identity_passes(self):
        f = inv.invariant_balance_identity(BAL)
        assert f.status == inv.PASS, f.evidence

    def test_missing_minority_is_tolerated_when_identity_holds(self):
        ok = BAL.replace("| 少数股东权益 | 8.08 | -8.80 | -16.30 | -9.69 |\n", "")
        # 去掉少数股东权益后 2025 会差 8.80（< 容差 22.09）⇒ 仍 PASS
        assert inv.invariant_balance_identity(ok).status == inv.PASS

    def test_broken_identity_fails(self):
        bad = BAL.replace("| 总资产 | 21,146.71 | 22,090.20 | 22,781.26 | 24,677.00 |",
                          "| 总资产 | 21,146.71 | 21,000.00 | 22,781.26 | 24,677.00 |")
        f = inv.invariant_balance_identity(bad)
        assert f.status == inv.FAIL and "2025" in " ".join(f.evidence)

    def test_missing_section_is_unevaluated(self):
        assert inv.invariant_balance_identity("无").status == inv.UNEVALUATED

    def test_parent_equity_label_is_not_confused_with_minority(self):
        """600941/002027 的真实标签是「归母所有者权益」，而「少数股东权益」含子串「股东权益」。

        旧实现 `row_of("股东权益","归母权益")` 先命中少数股东权益 ⇒ 母权益被丢弃、
        少数被加两遍 ⇒ **假 FAIL**（实测报差 1,388,799.00，而恒等式其实精确成立）。
        """
        a_share = (
            "## 4. 合并资产负债表\n\n"
            "| 项目 (百万元) | 2025 |\n| --- | ---: |\n"
            "| 总资产 | 2,092,882.00 |\n"
            "| 总负债 | 695,331.00 |\n"
            "| 归母所有者权益 | 1,393,175.00 |\n"
            "| 少数股东权益 | 4,376.00 |\n"
        )
        f = inv.invariant_balance_identity(a_share)
        assert f.status == inv.PASS, f.evidence
        assert any("归母所有者权益" in e for e in f.evidence)

    def test_total_equity_row_is_not_double_counted(self):
        """只有「所有者权益合计」时它已含少数股东权益，不得再叠加 mi。"""
        pack = (
            "## 4. 合并资产负债表\n\n"
            "| 项目 (百万元) | 2025 |\n| --- | ---: |\n"
            "| 总资产 | 1,100.00 |\n"
            "| 总负债 | 400.00 |\n"
            "| 所有者权益合计 | 700.00 |\n"
            "| 少数股东权益 | 50.00 |\n"
        )
        f = inv.invariant_balance_identity(pack)
        assert f.status == inv.PASS, f.evidence
        assert any("合计口径" in e for e in f.evidence)


class TestShareCountConsistency:
    def test_inconsistent_share_count_fails(self):
        """06862 真实形态：市值隐含 5,735.83 百万股 vs 权益/每股净资产 5,580.10（差 2.8%）。"""
        f = inv.invariant_share_count_consistency(BAL)
        assert f.status == inv.FAIL
        assert any("路径A" in e for e in f.evidence)

    def test_consistent_share_count_passes(self):
        ok = BAL.replace("| 总市值 | 53,343.18 |", "| 总市值 | 51,894.93 |")  # 5,574M × 9.30
        f = inv.invariant_share_count_consistency(ok)
        assert f.status == inv.PASS, f.evidence

    def test_missing_inputs_is_unevaluated(self):
        assert inv.invariant_share_count_consistency("无").status == inv.UNEVALUATED


class TestConstantHistoryColumns:
    def test_constant_goodwill_warns_but_does_not_fail(self):
        f = inv.invariant_constant_history_columns(BAL)
        assert f.status == inv.WARN, "恒定列应 WARN（有时真实恒定，如商誉不变）"
        assert any("商誉" in e for e in f.evidence)

    def test_no_multiyear_table_passes(self):
        assert inv.invariant_constant_history_columns("## 1. 基本面\n\n| a | b |\n| - | - |\n| 1 | 2 |").status == inv.PASS

    def test_i5_covered_rows_are_not_double_reported(self):
        f = inv.invariant_constant_history_columns(
            BAL.replace("| 商誉 | 84.84 | 84.84 | 84.84 | 84.84 |",
                        "| PE (TTM) | 14.05 | 14.05 | 14.05 | 14.05 |"))
        assert f.status == inv.PASS, "PE/PB/DPS 已由 I5 以 FAIL 覆盖，I10 不重复报"


class TestI11ShareCountAcrossSections:
    """I11 §16 总股本 ↔ §17.8 股本口径一致性。

    起因（known_issues §四十一）：§16「股权质押」**此前不受任何门禁覆盖**，于是
    Tushare `pledge_stat` 的**万股**被 `divider=1e4` 化成**亿股**、标签却写「万股」
    ⇒ **10,000 倍**误标长期存活（实测工商银行写成「总股本 (万股) = 2696.12」，
    实为 2,696.12 亿股）。内部「质押比例」是两个同单位值相除，**不受影响**，
    所以错误不会自己暴露 —— 只有**跨小节**比对才能看见。
    """

    @staticmethod
    def _pack(s16_unit: str, s16_value: str = "50.47", s178: str = "5,047.16") -> str:
        return (
            "## 16. 股权质押\n\n"
            "| 项目 | 数值 |\n| --- | ---: |\n"
            "| 质押笔数 | 2 |\n"
            f"| 无限售质押 ({s16_unit}) | 0.09 |\n"
            f"| 总股本 ({s16_unit}) | {s16_value} |\n"
            "| 质押比例 (%) | 0.18 |\n\n"
            "## 17. 衍生指标\n\n### 17.8 因子4·绝对估值\n\n"
            f"> **股本口径**：总股本（百万股） = {s178}（权威）\n"
        )

    def test_old_mislabel_is_caught(self):
        """回归：标签写「万股」而值其实是亿股 ⇒ 必须 FAIL（这正是被放过的那个错）。"""
        f = inv.invariant_share_count_across_sections(self._pack("万股"))
        assert f.status == inv.FAIL
        assert "10,000" in f.detail or "对不上" in f.detail

    def test_corrected_label_passes(self):
        f = inv.invariant_share_count_across_sections(self._pack("亿股"))
        assert f.status == inv.PASS, f.detail + str(f.evidence)

    def test_million_share_label_passes(self):
        f = inv.invariant_share_count_across_sections(self._pack("百万股", "5,047.16"))
        assert f.status == inv.PASS

    def test_wan_share_with_matching_value_passes(self):
        """单位真是万股且值与之一致时也应 PASS（不能把正确的写法判错）。"""
        f = inv.invariant_share_count_across_sections(self._pack("万股", "504,716.00"))
        assert f.status == inv.PASS

    def test_missing_section_16_is_unevaluated(self):
        f = inv.invariant_share_count_across_sections("## 1. x\n\n| a | b |\n")
        assert f.status == inv.UNEVALUATED

    def test_missing_17_8_declaration_is_unevaluated(self):
        """§17.8 声明缺失时必须 UNEVALUATED（**不是** PASS —— 没有证据不等于通过）。"""
        pack = ("## 16. 股权质押\n\n| 项目 | 数值 |\n| --- | ---: |\n"
                "| 总股本 (亿股) | 50.47 |\n")
        f = inv.invariant_share_count_across_sections(pack)
        assert f.status == inv.UNEVALUATED

    def test_unknown_unit_is_unevaluated_not_pass(self):
        f = inv.invariant_share_count_across_sections(self._pack("股"))
        assert f.status == inv.UNEVALUATED


# ---------------------------------------------------------------------------
# §17.15 / §17.16 值级核验（I12 / I13）
#
# 起因：2026-10-15 盘点发现这两节**决策相关却无任何值级交叉核验** ——
# 一个算错的 §17.15 能通过**全部**包级门禁。00506 的商誉缺陷当时只被**报告级**的
# viewpoint_audit 抓到，正是这个缺口的实证。
# ---------------------------------------------------------------------------

def _pack_1715(*, cash="4,549.48", debt="68.28", gw="1,846.80", it="3,541.98",
               net_a="-907.57", net_b="2,634.41", s4_gw="1,846.80",
               s4_it="3,541.98", s4_cash="4,549.48", cap="9,155.64") -> str:
    return (
        "## 4. 合并资产负债表\n\n"
        "| 项目 (百万元) | 2025 | 2024 |\n| --- | ---: | ---: |\n"
        f"| 现金及等价物 | {s4_cash} | 4,014.40 |\n"
        f"| 无形资产 | {s4_it} | 3,544.59 |\n"
        "| 交易性金融资产 | — | — |\n"
        f"| 商誉 | {s4_gw} | 1,846.80 |\n\n"
        "## 17. 衍生指标\n\n### 17.15 防御性净值\n\n"
        f"> 现金 = 货币资金 + 交易性金融资产 = {cash} 百万元；有息负债 = {debt}。\n\n"
        "| 项目 | 金额（百万元） | 说明 |\n| --- | ---: | --- |\n"
        f"| 商誉 | {gw} | 全额扣除 |\n"
        f"| 无形资产 | {it} | A 口径扣除 |\n"
        f"| **A 严口径**（扣商誉+无形） | {net_a} | 最保守 |\n"
        f"| **B 宽口径**（只扣商誉） | {net_b} | 无形视为必需 |\n\n"
        "### 17.8 因子4·绝对估值\n\n"
        f"> **市值**：总市值 = {cap}\n"
    )


class TestI12DefensiveNetAsset:
    def test_consistent_pack_passes(self):
        f = inv.invariant_defensive_net_asset(_pack_1715())
        assert f.status == inv.PASS, f.detail + str(f.evidence)

    def test_arithmetic_error_is_caught(self):
        """A 口径与"现金−债−商誉−无形"不符 ⇒ FAIL。"""
        f = inv.invariant_defensive_net_asset(_pack_1715(net_a="-500.00"))
        assert f.status == inv.FAIL
        assert any("A 口径算不平" in e for e in f.evidence)

    def test_disagreement_with_section4_is_caught(self):
        """§17.15 的商誉与 §4 最新年报列不符 ⇒ FAIL（这正是漏检的那一类）。"""
        f = inv.invariant_defensive_net_asset(_pack_1715(s4_gw="0.00"))
        assert f.status == inv.FAIL
        assert any("商誉与 §4 不一致" in e for e in f.evidence)

    def test_empty_section4_row_warns_not_fails(self):
        """§4 该列为空而 §17.15 按 0 处理 ⇒ WARN。

        为什么只 WARN：**「确实没有商誉」与「上游停更」在包内无法区分**。
        00506 的商誉缺陷正是后者（上游 2018H1 后停更），当时两处都显示空/零、
        彼此"一致"，所以纯一致性检查抓不到 —— 必须显式提示人工确认。
        """
        # gw=0 时 A/B 必须相应变化，否则先命中"算术算不平"而不是本用例要测的歧义告警
        p = _pack_1715(gw="0.00", net_a="939.22", net_b="4,481.20", s4_gw="—")
        f = inv.invariant_defensive_net_asset(p)
        assert f.status == inv.WARN, f.detail + str(f.evidence)
        # 告警文案在 detail（不是 evidence —— evidence 承载的是复算证据）
        assert "无法区分" in f.detail
        assert any("商誉=None" in str(e) for e in f.evidence)

    def test_missing_section_is_unevaluated(self):
        assert inv.invariant_defensive_net_asset("## 1. x\n").status == inv.UNEVALUATED


def _pack_1716(*, narrow="4,549.48", c15="4,549.48", netcash="4,481.20",
               debt="68.28", cap="9,155.64", aa="2,115.96", aa5="2,115.96") -> str:
    return (
        "## 17. 衍生指标\n\n### 17.15 防御性净值\n\n"
        f"> 现金 = 货币资金 + 交易性金融资产 = {c15} 百万元；有息负债 = {debt}。\n\n"
        "### 17.1 财务趋势速览\n\n"
        "| 指标 | 2025 |\n| --- | ---: |\n"
        f"| 有息负债（百万元） | {debt} |\n\n"
        "### 17.5 因子3·基准可支配结余\n\n"
        f"- AA_latest（最新年结余）= {aa5} 百万元\n\n"
        "### 17.8 因子4·绝对估值\n\n"
        f"> **市值**：总市值 = {cap}\n\n"
        "### 17.16 防御性门槛组\n\n"
        f"> 现金口径：**狭义**（货币资金 + 交易性金融资产）= {narrow} 百万元。\n\n"
        "| 判据 | 文档出处 | 数值 | 结果 |\n| --- | --- | ---: | --- |\n"
        f"| ① 总现金 > 短期借款 | 因子2 | {narrow} vs 0.00 | ✅ PASS |\n"
        f"| ⑤ 净现金 > 市值 | 因子2 | {netcash} vs {cap} | ❌ FAIL |\n"
        f"| ⑥ AA_latest ≥ 拟派息 | 因子2 | {aa} vs 883.33 | ✅ PASS |\n"
    )


class TestI13DefensiveGates:
    def test_consistent_pack_passes(self):
        f = inv.invariant_defensive_gates(_pack_1716())
        assert f.status == inv.PASS, f.detail + str(f.evidence)

    def test_narrow_cash_disagreement_with_1715_is_caught(self):
        f = inv.invariant_defensive_gates(_pack_1716(narrow="9,999.00"))
        assert f.status == inv.FAIL
        assert any("狭义现金与 §17.15" in e for e in f.evidence)

    def test_net_cash_arithmetic_is_caught(self):
        """净现金 ≠ 狭义现金 − 有息负债 ⇒ FAIL。

        注意 ④ 的文案含「净现金」字样（AA_latest/(市值−净现金)），
        按标签盲找会命中 ④ 而非 ⑤（实测踩过）⇒ 用判据序号精确锚定。
        """
        f = inv.invariant_defensive_gates(_pack_1716(netcash="1,234.56"))
        assert f.status == inv.FAIL
        assert any("净现金算不平" in e for e in f.evidence)

    def test_aa_latest_disagreement_with_175_is_caught(self):
        f = inv.invariant_defensive_gates(_pack_1716(aa="9,999.99"))
        assert f.status == inv.FAIL
        assert any("AA_latest 与 §17.5" in e for e in f.evidence)

    def test_missing_section_is_unevaluated(self):
        assert inv.invariant_defensive_gates("## 1. x\n").status == inv.UNEVALUATED


# ---------------------------------------------------------------------------
# §17.3 / §17.4 / §17.7 值级核验（I14 / I15 / I16）
#
# 这三节是 2026-10-15 盘点里"无任何值级核验"的最后三节。补之前先**实证**公式
# （对全部 107 个产物验算，全部成立）⇒ 它们本身没算错；不变式的价值是回归保护
# 与跨小节一致。过程中我自己犯了两个错，都留在下面的注释里。
# ---------------------------------------------------------------------------

def _pack_173(rows=("2025 | 100.00 | 20.00 | -30.00 | 80.00 | 80.00%",)) -> str:
    body = "\n".join("| " + r + " |" for r in rows)
    return ("## 17. 衍生指标\n\n### 17.3 因子3·步骤1 真实现金收入（保守基准）\n\n"
            "| 年份 | S 营业收入 | T 应收变动 | U 合同负债变动 | 真实现金收入 | 收款比率 |\n"
            "| --- | ---: | ---: | ---: | ---: | ---: |\n" + body + "\n")


class TestI14RealCashRevenue:
    def test_conservative_rule_holds(self):
        # S=100, ΔAR=+20（扣）, ΔCL=-30（扣 30）⇒ 100−20−30 = 50，比率 50%
        f = inv.invariant_real_cash_revenue(_pack_173(("2025 | 100.00 | 20.00 | -30.00 | 50.00 | 50.00%",)))
        assert f.status == inv.PASS, f.detail + str(f.evidence)

    def test_crediting_ar_decrease_is_rejected(self):
        """把 AR 减少**加回**（100+20−30=90）⇒ FAIL：保守基准明确不加回。"""
        f = inv.invariant_real_cash_revenue(_pack_173(("2025 | 100.00 | -20.00 | -30.00 | 120.00 | 120.00%",)))
        assert f.status == inv.FAIL
        assert any("保守规则" in e or "算不平" in e or "超过" in e for e in f.evidence)

    def test_exceeding_revenue_is_rejected(self):
        f = inv.invariant_real_cash_revenue(_pack_173(("2025 | 100.00 | 0.00 | 0.00 | 150.00 | 150.00%",)))
        assert f.status == inv.FAIL
        assert any("超过" in e for e in f.evidence)

    def test_bad_ratio_is_rejected(self):
        f = inv.invariant_real_cash_revenue(_pack_173(("2025 | 100.00 | 0.00 | 0.00 | 100.00 | 42.00%",)))
        assert f.status == inv.FAIL
        assert any("收款比率" in e for e in f.evidence)

    def test_missing_section_is_unevaluated(self):
        assert inv.invariant_real_cash_revenue("## 1. x\n").status == inv.UNEVALUATED


def _pack_174(rows=("2025 | 91,662.16 | 7,171.30 | 1,032.23 | 709.86 | 100,575.55",)) -> str:
    body = "\n".join("| " + r + " |" for r in rows)
    return ("## 17. 衍生指标\n\n### 17.4 因子3·步骤4 经营性现金支出\n\n"
            "| 年份 | W1 供应商 | W2 员工 | W3 现金税 | W4 利息 | W 合计 |\n"
            "| --- | ---: | ---: | ---: | ---: | ---: |\n" + body + "\n")


class TestI15OperatingCashOutflow:
    def test_sum_identity_holds(self):
        assert inv.invariant_operating_cash_outflow(_pack_174()).status == inv.PASS

    def test_sum_mismatch_is_caught(self):
        f = inv.invariant_operating_cash_outflow(
            _pack_174(("2025 | 100.00 | 20.00 | 30.00 | 40.00 | 999.00",)))
        assert f.status == inv.FAIL
        assert any("W 合计" in e for e in f.evidence)

    def test_negative_items_are_allowed(self):
        """W3 现金税可因退税为负、W4 利息可因利息收入>支出为负。

        回归：初版断言"现金支出不应为负"，在 **61/107** 个产物上误报 ——
        那是**未经证实的假设**，不是发现的缺陷。求和恒等式才是我实证过的。
        """
        f = inv.invariant_operating_cash_outflow(
            _pack_174(("2025 | 100.00 | 20.00 | -5.00 | -10.00 | 105.00",)))
        assert f.status == inv.PASS, f.detail + str(f.evidence)


def _pack_177(*, ibd_c="33,234.29", ibd_p="5,054.43", cash_c="11,273.08",
              cash_p="47.21", nc_c="-21,961.22", nc_p="-5,007.22", sub="84.8%") -> str:
    return (
        "## 4. 合并资产负债表\n\n| 项目 (百万元) | 2025 | 2024 |\n| --- | ---: | ---: |\n"
        f"| 货币资金 | {cash_c} | 8,833.50 |\n\n"
        "## 17. 衍生指标\n\n### 17.1 财务趋势速览\n\n"
        "| 指标 | 2025 |\n| --- | ---: |\n"
        f"| 有息负债（百万元） | {ibd_c} |\n\n"
        "### 17.7 控股结构辅助\n\n"
        "| 指标 | 合并口径 | 母公司口径 |\n| --- | ---: | ---: |\n"
        f"| 有息负债 | {ibd_c} | {ibd_p} |\n"
        f"| 现金 | {cash_c} | {cash_p} |\n"
        f"| 净现金 | {nc_c} | {nc_p} |\n"
        f"| 子公司层面负债占比 | {sub} | — |\n"
    )


class TestI16HoldingStructure:
    def test_consistent_pack_passes(self):
        f = inv.invariant_holding_structure(_pack_177())
        assert f.status == inv.PASS, f.detail + str(f.evidence)

    def test_net_cash_arithmetic_is_caught(self):
        f = inv.invariant_holding_structure(_pack_177(nc_c="-11,111.11"))
        assert f.status == inv.FAIL
        assert any("合并净现金算不平" in e for e in f.evidence)

    def test_sub_ratio_is_recomputed(self):
        f = inv.invariant_holding_structure(_pack_177(sub="50.0%"))
        assert f.status == inv.FAIL
        assert any("子公司层面负债占比" in e for e in f.evidence)

    def test_disagreement_with_17_1_is_caught(self):
        f = inv.invariant_holding_structure(_pack_177(ibd_c="99,999.99"))
        assert f.status == inv.FAIL
        assert any("§17.1" in e for e in f.evidence)

    def test_missing_section_is_unevaluated(self):
        assert inv.invariant_holding_structure("## 1. x\n").status == inv.UNEVALUATED

    def test_separator_row_is_not_parsed_as_data(self):
        """回归：分隔行判据曾写成"超集"（`>`）⇒ 恒 False，正常数据行被丢弃，
        I16 在 107/107 个产物上静默 UNEVALUATED。"""
        f = inv.invariant_holding_structure(_pack_177())
        assert f.status != inv.UNEVALUATED, "分隔行判据又写反了"
