"""O（注销型回购年均）接线：头部回显、审计复算的**单位**与**公式**一致性。

实测事故两连（2026-10-16，06078 海吉亚医疗）：

1. **§17.11 头部硬编码 `O = 0（默认）`** —— 产物带了 `o.json`（O≠0）时，表 A/表 B
   与 §17.14 都已按 O 算，头部却自称 O=0 ⇒ 同一小节自相矛盾，且下游照头部取 O=0
   会把**正确的** GG 判成 MISMATCH。
2. **`numeric_audit` 的独立复算不传 o** —— canonical 是
   `turtle_core.decision.penetration_return = [基准值×M×(1−Q) + O] / 市值`，且文档明写
   「R 与 GG 共用本函数，区别只在 base（R 传 C、GG 传 AA）」。审计脚本自己漏掉 O 项，
   于是 O≠0 的产物一律被误报 MISMATCH（这就是「脚本误判必须回灌脚本」的实例）。
   另：该脚本 `cap_pack` 单位是**百万**，故 `o` 也必须传百万值，**不得**再 ×1e6。
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


class TestAuditOUnitAndFormula:
    """`numeric_audit` 的 R/GG 复算必须与 canonical 同公式、同单位。"""

    def _pv(self, o_line: str):
        import numeric_audit as na
        pack = (
            "### 17.11 穿透回报率网格（税后）\n\n"
            f"> 基准值 C = 164.53 百万元，市值 = 5,064.78 百万元，"
            f"当前股价 = 9.56 港元，门槛 II = 5.00%，{o_line}\n\n"
            "#### 表A：粗算穿透回报率 R（基准值 = C 归母净利润）\n\n"
            "| 支付率 M | Q=20%（默认） |\n| --- | ---: |\n| 法3 4.53% ★ | 0.57% |\n\n"
            "#### 表B：精算穿透回报率 GG（基准值 = AA）\n\n"
            "| AA×M | GG Q=20%（默认） | 目标买入价（港元） |\n| --- | ---: | ---: |\n"
            "| AA_2y×法3 ★ | 0.58% | 1.10 |\n"
        )
        return na.PackView(pack)

    def test_reads_o_from_header_when_present(self):
        import numeric_audit as na
        assert na._o_from_header(self._pv("O = 22.83 百万元（来自 o.json）。")) == 22.83

    def test_reads_zero_when_default(self):
        import numeric_audit as na
        assert na._o_from_header(self._pv("O = 0（默认）。")) == 0.0

    def test_r_and_gg_share_one_formula_with_o(self):
        """同 base 差异下，R 与 GG 都必须含 O 项（canonical 明写二者共用）。"""
        import numeric_audit as na
        c, aa, m, q, cap, o_mm = 164.53, 177.35, 4.53, 20.0, 5064.78, 22.83
        r_no_o = na.r_rate(c, m, q, cap)
        gg_no_o = na.gg_rate(aa, m, q, cap)
        r_o = na.r_rate(c, m, q, cap, o=o_mm)
        gg_o = na.gg_rate(aa, m, q, cap, o=o_mm)
        # O 的贡献是**同一个绝对增量**（O/市值），与 base 无关
        assert abs((r_o - r_no_o) - (gg_o - gg_no_o)) < 1e-9
        assert abs((r_o - r_no_o) - o_mm / cap * 100) < 1e-9
        # 单位护栏：若误传 o=o_mm*1e6，增量会放大 1e6 倍
        assert (na.r_rate(c, m, q, cap, o=o_mm * 1e6) - r_no_o) > 1e5

    def test_no_e6_scaling_in_source(self):
        """回归锚点：调用点不得把百万值再 ×1e6（我第一次就是这么错的）。"""
        src = (ROOT / "scripts" / "numeric_audit.py").read_text(encoding="utf-8")
        assert "_o_val = _o_mm * 1e6" not in src, "O 与 cap_pack 同为百万，不能再 ×1e6"
        assert src.count("o=_o_val") >= 3, "R 与 GG（含 §17.14 各 k 档）都必须传 o"


class TestSection1711HeaderEchoesO:
    """§17.11 头部必须回显 o.json 的实际 O，不得硬编码 0。"""

    def test_header_is_not_hardcoded_zero(self):
        src = (ROOT / "scripts" / "tushare_modules" / "derived_metrics.py").read_text(
            encoding="utf-8")
        assert "门槛 II = {ii:.2f}%，O = 0（默认）。" not in src, (
            "头部仍硬编码 O=0 ⇒ 带 o.json 的产国会自相矛盾")
        assert "_o_in.is_default" in src and "O = {_o_in.o_annual_mm" in src


class TestCvClaimUnit:
    """`cm17.5.cv` 必须与 §17.5 正文同单位（%）。

    实测事故（2026-10-16，06078）：`cv` 是**比值** 0.2119，正文渲染 `21.19%`（×100），
    而 claim 登记时直接传了比值却声明 `unit="%"` ⇒ **账本值比正文小 100 倍**。
    下游若以账本为真值，会把 21% 的波动率当成 0.21% 的极低波动，从而高估收入稳定性
    与外推可信度。这与同节 `λ` 的单位处理同源（该节正文一律 ×100）。
    """

    def test_cv_registration_scales_ratio_to_percent(self):
        src = (ROOT / "scripts" / "tushare_modules" / "derived_metrics.py").read_text(
            encoding="utf-8")
        assert '"cm17.5.cv", "收入波动率 CV", cv, "%"' not in src, (
            "CV 以比值登记却声明 % ⇒ 账本比正文小 100 倍")
        assert "cv * 100" in src, "CV 必须 ×100 后再以 % 登记"
