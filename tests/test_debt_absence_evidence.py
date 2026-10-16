"""Tests for the debt-absence evidence recorder (``FinancialsMixin``).

回归锚点：C2（债务构成可核验）原本把「声明了却全年度为空」一律判 FAIL，
于是**真的没有银行借款**的发行人在结构上无法与 06862 那种**映射标签没命中上游**
区分 —— 实测 100 个产物中 40 个因此 FAIL（五粮液/中国移动/江苏金租）。
判据必须取自**上游科目清单**，而不是"值为空"这个结果。

这两条测试锁住判据的两个方向：
  · 上游只有租赁负债（00506 形态）⇒ 空的短贷/长贷/债券行是**正确表示**；
  · 上游存在带值却无人认领的债类科目（06862 形态）⇒ 证据必须被拒，C2 保持 FAIL。
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.modules.setdefault("tushare_collector", type(sys)("tushare_collector"))

from tushare_modules.financials import FinancialsMixin  # noqa: E402


class _Fake(FinancialsMixin):
    def __init__(self):
        self._store = {}


def _pivot(cols=(), **kwargs):
    data = {"end_date": ["20251231"], "ts_code": ["00506.HK"]}
    for c in cols:
        data[c] = [kwargs.get(c, float("nan"))]
    return pd.DataFrame(data)


class TestDebtNameCoverage:
    """纯集合判据：上游债类名字是否被映射全部认领。"""

    def test_lease_only_upstream_has_no_gap(self):
        covered, uncovered = _Fake._debt_name_coverage(
            {"融资租赁负债(流动)", "融资租赁负债(非流动)", "现金及等价物", "存货"})
        assert uncovered == set()
        assert "融资租赁负债(流动)" in covered

    def test_unmapped_debt_name_is_flagged(self):
        """上游有带值的债类科目、却不在任何 field 的认领名单里 ⇒ 真正的映射缺口。

        `可转换票据及债券` 是 06862 上游真实存在的科目；`贷款及垫款` 是资产侧科目
        （所以它只作噪声，不代表负债）。判据只看集合关系，不看值。
        """
        _, uncovered = _Fake._debt_name_coverage({"可转换票据及债券"})
        assert "可转换票据及债券" in uncovered
        _, uncovered2 = _Fake._debt_name_coverage({"应付债券", "应付票据(非流动)",
                                                   "短期贷款", "长期贷款",
                                                   "融资租赁负债(流动)", "融资租赁负债(非流动)"})
        assert uncovered2 == set(), "本仓库映射认领的债类名字必须全部被认出"

    def test_non_debt_names_are_ignored(self):
        _, uncovered = _Fake._debt_name_coverage({"存货", "现金及等价物", "总资产"})
        assert uncovered == set()


class TestUpstreamNamesWithValues:
    """只认**展示期内带值**的上游科目：空壳行与窗口外历史都不算。"""

    def _df(self):
        return pd.DataFrame({
            "end_date": ["20251231", "20241231", "20011231"],
            "ind_name": ["应付债券", "应付债券", "短期贷款"],
            "ind_value": [None, None, 319421200.0],
        })

    def test_window_out_history_is_excluded(self):
        vals = _Fake._upstream_names_with_values(self._df(), ["20251231", "20241231"])
        assert "短期贷款" not in vals        # 2001 年的历史余额不算今天的科目
        assert "应付债券" not in vals        # 行在、但窗口内没有数值

    def test_valued_name_inside_window_is_kept(self):
        df = pd.DataFrame({"end_date": ["20251231"], "ind_name": ["应付债券"],
                           "ind_value": [2028881000.0]})
        assert _Fake._upstream_names_with_values(df, ["20251231"]) == {"应付债券"}


class TestRecordDebtAbsence:
    """端到端：写出 field_absence 日志（C2 的放行证据）。"""

    def test_lease_only_issuer_records_verified_absent(self):
        f = _Fake()
        piv = _pivot(["st_borr", "lt_borr", "bond_payable", "lease_liab"],
                     lease_liab=39.59)
        f._record_debt_absence(
            "4", piv, "00506.HK",
            upstream_names={"融资租赁负债(流动)", "融资租赁负债(非流动)"},
            upstream_has_value={"融资租赁负债(流动)", "融资租赁负债(非流动)"},
            display_periods=["20251231"])
        j = f._store["field_absence"]
        assert j["4.st_borr"]["status"] == "verified_absent"
        assert j["4.lt_borr"]["status"] == "verified_absent"
        assert j["4.bond_payable"]["status"] == "verified_absent"
        assert j["4.notes_payable_noncur"]["status"] == "verified_absent"
        assert j["4.st_borr"]["upstream_debt_names_unmapped"] == []
        # 有值的租赁负债不该出现在"缺失"日志里
        assert "4.lease_liab" not in j

    def test_mapping_regression_refuses_the_evidence(self):
        """上游有带值的「应付债券」却无人认领 ⇒ 不得写出 verified_absent。"""
        f = _Fake()
        piv = _pivot(["st_borr", "lt_borr", "bond_payable"])
        f._record_debt_absence(
            "4", piv, "06862.HK",
            upstream_names={"应付债券", "可转换票据及债券"},
            upstream_has_value={"应付债券", "可转换票据及债券"},
            display_periods=["20251231"])
        for key, entry in f._store["field_absence"].items():
            assert entry["status"] != "verified_absent", key
        assert f._store["field_absence"]["4.bond_payable"]["status"] == "mapping_incomplete"

    def test_historical_value_outside_window_does_not_block(self):
        """00506 的真实坑：上游 2001–2003 年确有短期贷款余额，展示期却没有。"""
        f = _Fake()
        piv = _pivot(["st_borr", "lt_borr", "bond_payable", "lease_liab"],
                     lease_liab=39.59)
        f._record_debt_absence(
            "4", piv, "00506.HK",
            # upstream_names 含二十年前的科目名（长表全历史），但窗口内不带值
            upstream_names={"短期贷款", "长期贷款", "融资租赁负债(流动)", "融资租赁负债(非流动)"},
            upstream_has_value={"融资租赁负债(流动)", "融资租赁负债(非流动)"},
            display_periods=["20251231"])
        assert f._store["field_absence"]["4.st_borr"]["status"] == "verified_absent"

    def test_a_share_wide_table_is_not_treated_as_line_item(self):
        """A 股没有行项目清单 ⇒ 记录 unreported，C2 不放行（fail-closed）。"""
        f = _Fake()
        piv = _pivot(["st_borr"])
        f._record_debt_absence(
            "4", piv, "000858.SZ",
            upstream_names={"短期借款"}, upstream_has_value=set(), display_periods=["20251231"])
        assert f._store["field_absence"] == {}

    def test_us_line_item_api_is_recorded(self):
        """美股同样走行项目长表 ⇒ 证据机制必须对美股生效。"""
        f = _Fake()
        piv = _pivot(["st_borr", "lt_borr"])
        f._record_debt_absence(
            "4", piv, "AAPL.US",
            upstream_names={"短期贷款"}, upstream_has_value=set(), display_periods=["20251231"])
        assert f._store["field_absence"]["4.st_borr"]["status"] == "verified_absent"
