"""pack_metrics —— 结构化数值件与单点读取器。tests for scripts/pack_metrics.py。

每条回归都对应一次**实测事故**（同一会话内我写了 4 版 `_sec`，每版错法不同）。
本模块的存在意义就是让这类"读取器差异"不可能再发生。
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pack_metrics as pm  # noqa: E402

PACK = """## 3. 合并利润表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 营业额 | 1,000.00 | 900.00 |
| 股东应占溢利 | 60.00 | 55.00 |

## 4. 合并资产负债表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 商誉 | 1,846.80 | 1,846.80 |
| 短期贷款 | — | — |
| 交易性金融资产 | — | 12.34 |

## 5. 现金流量表

| 项目 (百万元) | 2025 | 2024 |
| --- | ---: | ---: |
| 经营业务现金净额 (OCF) | 3,229.31 | 2,847.15 |

## 17. 衍生指标（Python 预计算）

### 17.5 因子3·步骤7 可支配现金结余（OCF 锚定）

| 年份 | OCF | AA |
| --- | ---: | ---: |
| 2025 | 3,229.31 | 2,115.96 |

- AA_2y（近2年均值，默认基准）= 2,063.87 百万元
- AA_all（全部年份均值）= 1,224.47 百万元
- 经营杠杆系数 λ = 3.1720
- 收入波动率 CV = 3.63%
  ⚠️ AA_2y 与 AA_all 差异 51.1% > 30%，请审核

### 17.6 因子4·股价分位

| 指标 | 值 |
| --- | ---: |
| 当前股价 | 3.79 |

### 17.7 控股结构辅助

（不适用）
"""


class TestSectionSlicing:
    """回归 1+2：切小节的两类实测错切。"""

    def test_section_number_must_match_exactly(self):
        """编号必须**整体相等**，不能前缀匹配。

        回归：`^#{2,3}\\s*3\\b` 会命中 `## 17.3`（因为 `.` 是词边界）⇒ `_sec("3")`
        取到的是 §17.3 而不是 §3 利润表。
        """
        txt = ("## 3. 利润表\n\nAAA\n\n"
               "## 17. 衍生指标\n\n### 17.3 步骤\n\nBBB\n")
        body = pm.section_text(txt, "3")
        assert "AAA" in body
        assert "BBB" not in body
        # 反方向也要成立：§17.3 取的必须是它自己
        assert "BBB" in pm.section_text(txt, "17.3")

    def test_terminates_at_same_level(self):
        """§17.5 必须在 §17.6 处终止，否则会把下一节的值算进本节。"""
        body = pm.section_text(PACK, "17.5")
        assert "AA_2y" in body
        assert "因子4·股价分位" not in body

    def test_subsection_lookup_isolated(self):
        body = pm.section_text(PACK, "17.6")
        assert "当前股价" in body and "AA_2y" not in body


class TestNumericParsing:
    """回归 3+4：负数与缺失值。"""

    def test_negative_numbers_parse(self):
        """字符类写成 `[\\-\\d,\\.]` 会形成非法范围 ⇒ 负数永远匹配不到（实测）。"""
        assert pm.num("-204,060.00") == pytest.approx(-204060.0)
        assert pm.num("-837.13") == pytest.approx(-837.13)
        assert pm.num("−1,234.5") == pytest.approx(-1234.5)   # 全角负号

    def test_dash_is_none_not_zero(self):
        """`—` 必须读成 None —— 把缺失当 0 会让"没有该项"被算成"该项为零"。"""
        assert pm.num("—") is None
        assert pm.num("") is None
        assert pm.num("n/a") is None

    def test_percent_and_bold_stripped(self):
        assert pm.num("**3.63%**") == pytest.approx(3.63)


class TestTableAccess:
    def test_header_and_years(self):
        m = pm.Metrics(pm.build(PACK))
        assert m.header("3")[0] == "项目 (百万元)"
        assert m.years("3") == ["2025", "2024"]

    def test_value_lookup_with_full_width_paren_label(self):
        m = pm.Metrics(pm.build(PACK))
        assert m.value("5", "经营业务现金净额 (OCF)", "2025") == pytest.approx(3229.31)

    def test_partial_label_match_fallback(self):
        m = pm.Metrics(pm.build(PACK))
        assert m.value("4", "商誉", "2025") == pytest.approx(1846.80)

    def test_series(self):
        m = pm.Metrics(pm.build(PACK))
        assert m.series("3", "股东应占溢利") == {"2025": 60.0, "2024": 55.0}

    def test_missing_row_returns_none(self):
        m = pm.Metrics(pm.build(PACK))
        assert m.row("4", "不存在的行") is None
        assert m.value("4", "不存在的行") is None

    def test_duplicate_labels_across_tables_do_not_collide(self):
        """同一小节两张表若行标签相同，后者加 `#2` 后缀 —— 不静默覆盖。"""
        txt = ("## 9. 主营\n\n| 项目 | 2025 |\n| --- | ---: |\n| 收入 | 1.00 |\n\n"
               "| 项目 | 2025 |\n| --- | ---: |\n| 收入 | 2.00 |\n")
        m = pm.Metrics(pm.build(txt))
        assert m.value("9", "收入", "2025") == pytest.approx(1.0)
        assert m.value("9", "收入#2", "2025") == pytest.approx(2.0)


class TestBullets:
    """§17.5 的 AA/λ 是列表项，且**不得**取到警告行的百分比。"""

    def test_bullet_values_extracted(self):
        m = pm.Metrics(pm.build(PACK))
        assert m.bullet_value("17.5", "AA_2y") == pytest.approx(2063.87)
        assert m.bullet_value("17.5", "AA_all") == pytest.approx(1224.47)
        assert m.bullet_value("17.5", "经营杠杆系数 λ") == pytest.approx(3.1720)

    def test_warning_line_percentage_is_not_taken_as_a_value(self):
        """回归：`⚠️ AA_2y 与 AA_all 差异 51.1%` 含 AA_2y，朴素取数会取到 51.1。"""
        m = pm.Metrics(pm.build(PACK))
        assert m.bullet_value("17.5", "AA_2y") != pytest.approx(51.1)

    def test_warning_marker_is_not_a_label(self):
        m = pm.Metrics(pm.build(PACK))
        assert not any("⚠" in k for k in m.bullets("17.5"))

    def test_negative_bullet_values(self):
        """真实事故形态：AA 为负（资本开支 > 经营现金流）。"""
        txt = PACK.replace("- AA_2y（近2年均值，默认基准）= 2,063.87 百万元",
                           "- AA_2y（近2年均值，默认基准）= -204,060.00 百万元")
        m = pm.Metrics(pm.build(txt))
        assert m.bullet_value("17.5", "AA_2y") == pytest.approx(-204060.0)


class TestRoundTripAndCompare:
    def test_write_then_load_roundtrip(self, tmp_path):
        product = tmp_path / "001_测试"
        product.mkdir()
        (product / "data_pack_market.md").write_text(PACK, encoding="utf-8")
        out = pm.write(str(product), PACK, rules_version="abc123")
        assert pathlib.Path(out).name == "metrics.json"
        m = pm.load(str(product))
        assert m.rules_version == "abc123"
        assert m.value("4", "商誉", "2025") == pytest.approx(1846.80)

    def test_dual_run_equivalence_passes(self, tmp_path):
        product = tmp_path / "001_测试"
        product.mkdir()
        (product / "data_pack_market.md").write_text(PACK, encoding="utf-8")
        pm.write(str(product), PACK)
        r = pm.compare(str(product))
        assert r["ok"], r["mismatches"]
        assert r["checked"] > 10

    def test_dual_run_detects_tampering(self, tmp_path):
        """把 metrics.json 改坏 ⇒ 双跑必须报不一致（否则这层安全网没用）。"""
        product = tmp_path / "001_测试"
        product.mkdir()
        (product / "data_pack_market.md").write_text(PACK, encoding="utf-8")
        pm.write(str(product), PACK)
        payload = json.loads((product / "metrics.json").read_text(encoding="utf-8"))
        payload["tables"]["4"]["rows"]["商誉"][1] = "999.99"      # 篡改
        (product / "metrics.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        r = pm.compare(str(product))
        assert not r["ok"]
        assert any("商誉" in m for m in r["mismatches"])

    def test_load_is_fail_closed_without_metrics_json(self, tmp_path):
        """缺 metrics.json 必须**抛**，不得静默回落解析 markdown（否则多读取器回归）。"""
        product = tmp_path / "001_测试"
        product.mkdir()
        (product / "data_pack_market.md").write_text(PACK, encoding="utf-8")
        with pytest.raises(FileNotFoundError):
            pm.load(str(product))
        # 迁移期兼容入口是显式的
        m = pm.load_or_parse(str(product))
        assert m.value("4", "商誉", "2025") == pytest.approx(1846.80)


class TestNoNewDuplicateParsers:
    """棘轮：单点读取器已经存在，**新代码不得再自带一套 markdown 解析**。

    P1b 会逐个迁移既有消费者（7 个审计脚本 + turtle_screener/slow_layer）。
    本测试锁定"只能减少、不能增加" —— 否则同一类读取器差异会重新长出来
    （已知事故 4 起，见模块 docstring）。
    """

    #: 迁移前就存在、允许暂时保留的重复实现。
    #: 2026-10-15 清理后的现状（P1b/T6）：
    #:   · `_sec` 类切小节实现 **已全部清空**（全部委托 `pack_metrics.section_text`）；
    #:   · `field_coverage._numeric_cells` 已委托单点；
    #:   · 剩下这些 `_num` 里，**只有 `quality_control.py` 是真正的文本解析器**，
    #:     且它是**有意不同**的（必须剥脚注 `1.23*` → `1.23`，见
    #:     `tests/test_numeric_parsing_contract.py` 的 INTENTIONAL_DIFFERENCES）；
    #:   · `screen/*` 与 `screener_pool` 的 `_num` 解析的是 **API 浮点**（含 NaN 语义），
    #:     与"markdown 单元格 → 数字"不是同一件事，不算重复实现；
    #:   · `turtle_core_selfcheck._num(claims, cid)` 签名不同（按 claim id 取值）。
    KNOWN_LEGACY = {
        "quality_control.py": ["_num"],          # 有意保留：剥脚注语义
    }

    def test_no_new_scripts_define_their_own_num_parser(self):
        import re as _re
        scripts = ROOT / "scripts"
        offenders = []
        for f in sorted(scripts.glob("*.py")):
            if f.name in ("pack_metrics.py",):
                continue
            src = f.read_text(encoding="utf-8")
            m = _re.search(r"^def _num\(.*?(?=^def |\Z)", src, _re.M | _re.S)
            if not m:
                continue
            body = m.group(0)
            if f.name in ("screener_pool.py",) or str(f).find("/screen/") >= 0:
                continue      # 解析 API 浮点，不是 markdown 单元格
            if f.name in ("turtle_core_selfcheck.py",):
                continue      # 签名不同：_num(claims, cid)
            # **委托单点**的包装器不算重复实现（它的身体里会 import pack_metrics）：
            # 棘轮要抓的是"自带一套解析逻辑"，不是"存在名为 _num 的函数"。
            if "pack_metrics import" in body or "pack_metrics.num" in body:
                continue
            if f.name not in self.KNOWN_LEGACY:
                offenders.append(f.name)
        assert not offenders, (
            f"这些脚本新定义了自己的 `_num()`：{offenders}；"
            "请改用 scripts/pack_metrics.py 的 num()/_num 单点"
        )

    def test_pack_metrics_is_the_only_section_slicer_for_new_code(self):
        """新脚本不得再自带 `_sec`/`_section_text` 这类切小节实现。"""
        import re as _re
        scripts = ROOT / "scripts"
        allowed = {
            "pack_metrics.py",       # 单点本身
            "field_coverage.py", "instrument_audit.py", "invariants.py",
            "numeric_audit.py", "report_consistency.py", "viewpoint_audit.py",
        }
        offenders = []
        for f in sorted(scripts.glob("*.py")):
            if f.name in allowed:
                continue
            src = f.read_text(encoding="utf-8")
            if _re.search(r"^def (_sec|_section_text|_section)\(", src, _re.M):
                offenders.append(f.name)
        assert not offenders, (
            f"这些脚本新定义了自己的小节切片器：{offenders}；"
            "请改用 scripts/pack_metrics.py 的 section_text()"
        )


class TestConsumerContractIsPinned:
    """下游契约必须硬性钉住，否则"多读取器"会以新形式回归。"""

    def test_load_never_falls_back_to_markdown(self, tmp_path):
        """`load()` 缺 metrics.json 必须抛 —— 回落解析 markdown 等于恢复多读取器。"""
        product = tmp_path / "001_测试"
        product.mkdir()
        (product / "data_pack_market.md").write_text(PACK, encoding="utf-8")
        with pytest.raises(FileNotFoundError):
            pm.load(str(product))

    def test_compare_accepts_explicit_pack_text(self, tmp_path):
        """`compare(dir, pack_text)` 必须支持显式文本。

        回归：初版硬编码 `data_pack_market.md`，而 `--output` 可指向任意文件名
        ⇒ compare 去找不存在的文件、**假报**不一致并使采集器 exit 1
        （被 tests/test_refresh_market.py 当场抓到）。
        """
        product = tmp_path / "001_测试"
        product.mkdir()
        (product / "custom_name.md").write_text(PACK, encoding="utf-8")
        pm.write(str(product), PACK)
        assert pm.compare(str(product), PACK)["ok"] is True
        # 不传文本时应如实报"缺文件"，而不是抛异常
        r = pm.compare(str(product))
        assert r["ok"] is False and "缺" in r["mismatches"][0]
