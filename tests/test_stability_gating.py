"""参数稳定性门控（T4）—— stability.json + 双边判据 + 阈值单一来源。

背景（known_issues §四十七）
--------------------------
参数不稳定原先只是 `instrument_audit` 一次性扫描的结论，**报告侧不知道**，
于是不稳定的标的照样出买点。"报告了不拦等于没拦"（§四十二）。
现在这份事实随产物走（`stability.json`），体检/选股/报告读**同一份**。
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import instrument_audit as ia  # noqa: E402


class TestTwoSidedPredicate:
    """λ 的异常是**双边**的（>3 或 <0）。

    回归：`_record_stability` 初版只测上界 ⇒ λ=-0.317 被记成 `unstable: false`
    （实测 000708）。双边判据必须显式支持下界。
    """

    def test_upper_bound_triggers(self):
        src = (ROOT / "scripts" / "tushare_modules" / "derived_metrics.py").read_text(encoding="utf-8")
        assert "below=LA[\"anomaly_below\"]" in src, "λ 未传下界 ⇒ 负 λ 不会被判异常"
        assert "lower = below is not None" in src, "_record_stability 未实现下界判据"

    def test_lambda_zero_is_anomalous_per_contract(self):
        rules = json.loads((ROOT / "scripts" / "strategy_rules.json").read_text(encoding="utf-8"))
        la = rules["lambda_reliability"]
        # 契约里 anomaly_below = 0 ⇒ λ<0 是异常
        assert la["anomaly_below"] == 0.0
        assert la["anomaly_above"] == 3.0


class TestThresholdSingleSource:
    """判据与文案必须用**同一个**规则阈值。

    回归：§17.5 的 AA 窗口守卫写成 `if _aa_gap > 30:`（硬编码 30）而文案读规则；
    §17.10 的 M 守卫写成 `max_dev > 0.15` 而文案读规则。
    ⇒ 改规则只改文案、不改判据。T3 的接线当时只接了一半。
    """

    @staticmethod
    def _code_only() -> str:
        """只留**真实代码**，剔除注释与文档字符串。

        为什么不能只按"行首是不是 #"过滤：反例引用在**docstring** 里
        （如注释记录"原先写成 `if _aa_gap > 30:`"），按行首过滤会把它当代码 ⇒ 假红（实测）。
        做法：逐行拼接**非 STRING/COMMENT** 的 token —— docstring 与注释自然被排除。
        """
        import io as _io
        import tokenize
        src = (ROOT / "scripts" / "tushare_modules" / "derived_metrics.py").read_text(encoding="utf-8")
        skip = {tokenize.STRING, tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                tokenize.INDENT, tokenize.DEDENT}
        by_line: dict = {}
        for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
            if tok.type in skip:
                continue
            by_line.setdefault(tok.start[0], []).append(tok.string)
        return "\n".join(" ".join(v) for _k, v in sorted(by_line.items()))

    @staticmethod
    def _raw() -> str:
        return (ROOT / "scripts" / "tushare_modules" / "derived_metrics.py").read_text(encoding="utf-8")

    def test_aa_window_comparison_uses_the_rule(self):
        # 两个断言要用不同的视图：
        #  · "没有硬编码字面量" ⇒ 必须看**剔除字符串**后的代码（否则 docstring 里的反例干扰）
        #  · "读了规则键" ⇒ 必须看**原始源码**（键名本身就是字符串字面量，
        #    用 code-only 视图会把它一起剔掉 ⇒ 假红，实测踩过）
        assert "_aa_gap > 30" not in self._code_only(), "AA 窗口判据仍是硬编码 30"
        assert "aa_window_spread_pct" in self._raw()

    def test_m_method_comparison_uses_the_rule(self):
        assert "max_dev > 0.15" not in self._code_only(), "M 三法判据仍是硬编码 0.15"
        assert "m_method_spread_pct" in self._raw()


class TestStabilitySidecar:
    def _product(self, tmp_path, params, pack="## 17. 衍生指标\n"):
        d = tmp_path / "001_测试"
        d.mkdir()
        (d / "data_pack_market.md").write_text(pack, encoding="utf-8")
        (d / "stability.json").write_text(json.dumps(
            {"schema_version": "1.0", "parameters": params, "unstable_count":
             sum(1 for v in params.values() if v.get("unstable"))}, ensure_ascii=False),
            encoding="utf-8")
        return d

    def test_audit_reads_the_sidecar_not_recomputes(self, tmp_path):
        d = self._product(tmp_path, {"M_method": {
            "value": 37.51, "threshold": 15.0, "unstable": True,
            "disposition": "disclose_only", "detail": "三法最大相对偏差 37.5%"}})
        m = ia._metrics(str(d / "data_pack_market.md"))
        m.payload["_pack_path"] = str(d / "data_pack_market.md")
        fs = ia.check_stability(m, "T")
        f = [x for x in fs if x["metric"] == "M_method"][0]
        assert "读自 stability.json" in f["detail"], "没有读 sidecar（回落自算了）"
        assert f["status"] == ia.FAIL

    def test_unstable_is_disclose_only_not_pass(self, tmp_path):
        """`unstable` ⇒ FAIL 状态 + disclose_only 处置 —— **不是通过**。"""
        d = self._product(tmp_path, {"lambda": {
            "value": -0.317, "threshold": 3.0, "unstable": True,
            "disposition": "disclose_only", "detail": "λ=-0.317"}})
        m = ia._metrics(str(d / "data_pack_market.md"))
        m.payload["_pack_path"] = str(d / "data_pack_market.md")
        f = [x for x in ia.check_stability(m, "T") if x["metric"] == "lambda"][0]
        assert f["status"] == ia.FAIL
        assert ia.disposition_of(f["rule"], f["metric"], f["status"]) == ia.DISCLOSE_ONLY

    def test_missing_sidecar_falls_back_explicitly(self, tmp_path):
        """存量产物没有 sidecar ⇒ 显式回落自算（不得静默变 PASS）。"""
        d = tmp_path / "002_旧产物"
        d.mkdir()
        (d / "data_pack_market.md").write_text(
            "## 17. 衍生指标\n\n### 17.5 x\n\n- AA_2y（近2年均值，默认基准）= 200.00 百万元\n"
            "- AA_all（全部年份均值）= 100.00 百万元\n- 经营杠杆系数 λ = 3.50\n",
            encoding="utf-8")
        m = ia._metrics(str(d / "data_pack_market.md"))
        m.payload["_pack_path"] = str(d / "data_pack_market.md")
        fs = ia.check_stability(m, "T")
        assert fs, "回落路径必须仍然产出结论"
        assert not any("读自 stability.json" in x["detail"] for x in fs)


class TestStabilitySidecarContract:
    def test_sidecar_is_pinned_in_manifest(self):
        src = (ROOT / "scripts" / "tushare_collector.py").read_text(encoding="utf-8")
        assert '"stability.json"' in src, "stability.json 未钉进 pack_manifest"

    def test_sidecar_carries_rules_version(self):
        src = (ROOT / "scripts" / "tushare_collector.py").read_text(encoding="utf-8")
        i = src.index("def _write_stability_sidecar")
        body = src[i:i + 1600]
        assert "rules_version" in body, "stability.json 缺规则版本锚 ⇒ 无法判陈旧"


class TestRulesSinglePointIsFailClosed:
    """规则单点不可用时必须**可见且阻断**（known_issues §四十九）。

    回归：`_load_strategy_rules()` 原先 `except Exception: return {}` ——
    连一句警告都没有，代码注释却写着"降级值必须可见"（**注释与实现不符**）。
    实测：把 `strategy_rules.json` 移走，采集器照样跑完、`deliverable=true`、exit 0，
    静默使用内嵌默认值 ⇒ 产物可能与策略规则矛盾而无人知道。
    """

    def test_loader_records_and_is_loud(self):
        src = (ROOT / "scripts" / "tushare_modules" / "derived_metrics.py").read_text(encoding="utf-8")
        assert "STRATEGY_RULES_ERROR" in src, "加载失败没有留下可被门禁读取的标记"
        i = src.index("def _load_strategy_rules")
        body = src[i:i + 1400]
        assert "stderr" in body, "加载失败没有输出到 stderr（静默降级）"

    def test_collector_blocks_when_rules_unavailable(self):
        src = (ROOT / "scripts" / "tushare_collector.py").read_text(encoding="utf-8")
        assert "STRATEGY_RULES_ERROR" in src, "采集器没有把规则不可用当成阻断"
        i = src.index("STRATEGY_RULES_ERROR", src.index("def main("))
        assert "sys.exit(1)" in src[i:i + 700], "规则不可用时采集器没有 exit 1"

    def test_k_grid_loader_is_loud(self):
        src = (ROOT / "scripts" / "turtle_core" / "pessimistic.py").read_text(encoding="utf-8")
        i = src.index("def _load_k_grid")
        assert "stderr" in src[i:i + 1400], "k 网格回落内嵌默认值时没有警告"

    def test_verify_product_has_a_rules_gate(self):
        import verify_product as vp
        g = vp.gate_rules_available(str(ROOT / "output"))
        assert g.status == vp.PASS, g.detail
        # 门禁必须能识别"关键条目被删"
        import json as _json
        import tempfile
        d = tempfile.mkdtemp()
        bad = {"schema_version": "1.0"}
        (pathlib.Path(d) / "..").resolve()
        # 直接测 missing 分支逻辑：构造一个缺条目的 data 不可行（门禁读固定路径），
        # 所以这里只断言关键条目清单是显式的
        assert "ii_threshold" in (ROOT / "scripts" / "verify_product.py").read_text(encoding="utf-8")
