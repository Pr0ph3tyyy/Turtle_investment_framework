"""规则版本锚：让"产物是否陈旧"成为机器可判的事实（P5）。

实测代价（2026-10-15）：`600011_华能国际` 的状态文件里挂着一个**陈旧**的 `exception`，
我花时间追一个当前并不存在的错误。没有版本锚，就无法区分
"这个产物反映当前规则"与"它是旧规则的化石"。

`pack_manifest.json` 钉的是**输入**的 md5 —— 它证明"输入没被中途改动"，
**不能**证明"输入与当前规则同代"。本机制补的就是这一半。
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import rules_version as rv  # noqa: E402


class TestCompute:
    def test_version_is_deterministic(self):
        assert rv.compute() == rv.compute()

    def test_version_is_12_hex(self):
        v = rv.compute()
        assert len(v) == 12 and all(c in "0123456789abcdef" for c in v)

    def test_version_changes_when_rules_change(self, tmp_path):
        """改一个规则文件 ⇒ 版本必须变（否则锚没有意义）。"""
        import shutil
        # 复制所需的规则文件到一个临时 scripts 目录
        for rel in rv.VERSIONED_FILES:
            src = ROOT / "scripts" / rel
            dst = tmp_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                shutil.copy2(src, dst)
            else:
                dst.write_text("", encoding="utf-8")
        base = rv.compute(str(tmp_path))
        (tmp_path / "strategy_rules.json").write_text('{"x": 1}', encoding="utf-8")
        assert rv.compute(str(tmp_path)) != base

    def test_version_ignores_non_formula_scripts(self, tmp_path):
        """渲染/审计脚本的文件名不在 VERSIONED_FILES 里 ⇒ 不进版本。"""
        assert "report_to_html.py" not in rv.VERSIONED_FILES
        assert "instrument_audit.py" not in rv.VERSIONED_FILES
        assert "strategy_rules.json" in rv.VERSIONED_FILES


class TestAuditAndStaleness:
    def _product(self, tmp_path, version):
        d = tmp_path / "001_测试"
        d.mkdir()
        (d / "data_pack_market.md").write_text("## 1. x\n", encoding="utf-8")
        (d / "claims.json").write_text(
            json.dumps({"schema_version": "1.0", "rules_version": version}),
            encoding="utf-8")
        return d

    def test_matching_version_is_current(self, tmp_path):
        self._product(tmp_path, rv.compute())
        rep = rv.audit(str(tmp_path))
        assert rep["counts"]["current"] == 1
        assert rep["counts"]["stale"] == 0

    def test_different_version_is_stale(self, tmp_path):
        self._product(tmp_path, "deadbeef0000")
        rep = rv.audit(str(tmp_path))
        assert rep["counts"]["stale"] == 1

    def test_missing_version_is_unanchored_not_stale(self, tmp_path):
        """无锚 ≠ 陈旧：前者产生于机制之前，后者确定反映旧规则。必须区分。"""
        d = tmp_path / "001_测试"
        d.mkdir()
        (d / "data_pack_market.md").write_text("## 1. x\n", encoding="utf-8")
        (d / "claims.json").write_text('{"schema_version":"1.0"}', encoding="utf-8")
        rep = rv.audit(str(tmp_path))
        assert rep["counts"]["unanchored"] == 1
        assert rep["counts"]["stale"] == 0

    def test_strict_exit_code(self, tmp_path):
        self._product(tmp_path, "deadbeef0000")
        assert rv.main(["--root", str(tmp_path), "--strict"]) == 1

    def test_non_product_dirs_are_skipped(self, tmp_path):
        (tmp_path / "not_a_product").mkdir()
        (tmp_path / "not_a_product" / "random.json").write_text("{}", encoding="utf-8")
        rep = rv.audit(str(tmp_path))
        assert rep["products"] == []


class TestAnchorDoesNotInvalidateManifest:
    """顺序陷阱的回归：**锚必须先盖、清单最后打**。

    清单钉住的正是 claims/section17_status/invariants/field_coverage 这些文件。
    若先打清单再盖章（本机制初版就是这样），清单**当场失效** ——
    实测 `stage_manifest --check` 报 4 处已变更，每个文件恰好大 35 字节
    （= 一行 `"rules_version": "…"`）。这类"门禁自己把门禁弄坏"的缺陷
    必须在测试里锁死，否则它只会在下一次交付时才暴露。
    """

    def test_manifest_covers_anchored_files(self):
        """清单的覆盖范围与盖章范围必须有交集 —— 有交集才需要讲究顺序。"""
        import stage_manifest as sm
        manifest_files = {"claims.json", "section17_status.json",
                          "invariants.json", "field_coverage.json"}
        assert manifest_files & set(rv.ANCHORED), (
            "清单不再覆盖被盖章的文件 ⇒ 本测试的前提变了，请重新审视顺序要求"
        )

    def test_stamp_then_pin_is_the_effective_order(self):
        """`_write_section_status` 里必须先 `_stamp_rules_version` 再 `_write_stage_manifest`。"""
        import pathlib
        src = (ROOT / "scripts" / "tushare_collector.py").read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), 1):
            if "_stamp_rules_version(output_path)" in line:
                continue
        # 两处调用点都必须满足"盖章在前"
        idx_stamp = [i for i, l in enumerate(src.splitlines())
                     if "_stamp_rules_version(output_path)" in l]
        idx_pin = [i for i, l in enumerate(src.splitlines())
                   if "_write_stage_manifest(output_path)" in l]
        # 打清单的定义处除外
        idx_pin = [i for i in idx_pin if "def " not in src.splitlines()[i]]
        assert idx_stamp and idx_pin
        assert max(idx_stamp) < max(idx_pin), (
            "盖章出现在打清单之后 ⇒ 清单会被自己的锚作废（实测 4 处已变更）"
        )

    def test_main_does_not_restamp_after_pinning(self):
        """`main()` 里不得在打清单之后再次盖章（那同样会作废清单）。"""
        src = (ROOT / "scripts" / "tushare_collector.py").read_text(encoding="utf-8")
        main_src = src[src.index("def main("):]
        # main 里不应再出现独立的盖章调用（盖章已移入 _write_section_status 的顺序中）
        assert "_stamp_rules_version(args.output)" not in main_src
