"""产物验收单点（T5）—— 一条命令跑完全部门禁，给一个结论。

起因：到 2026-10-15，"新流程产出是否可靠"是靠**人工一条条敲命令**验证的。
这个过程没有自动化 ⇒ 下一个标的交付时没人会全跑一遍，漏掉任何一条都可能是静默的。
"""

import json
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY_EXE = sys.executable

#: 一个真实的、机制齐备的产物（新流程采集出来的）
CANDIDATES = ["/tmp/t4", str(ROOT / "output" / "00506_中国食品")]


def _run(d, *extra):
    return subprocess.run(
        [PY_EXE, str(SCRIPTS / "verify_product.py"), "--dir", str(d), *extra],
        capture_output=True, text=True, cwd=str(ROOT))


def _ready_product():
    for c in CANDIDATES:
        p = pathlib.Path(c)
        if (p / "data_pack_market.md").is_file() and (p / "metrics.json").is_file() \
                and (p / "stability.json").is_file():
            return p
    return None


def _make_current(product: pathlib.Path) -> None:
    """把复制过来的产物"翻新"成当前规则版本 —— 模拟采集器的**先盖章、后打清单**顺序。

    为什么必须这么做：本测试复制的是**既有产物**，而任何一次代码改动都会改变
    `rules_version`（它涵盖 derived_metrics 等公式脚本）⇒ 副本天然"陈旧"，
    那是版本锚的**正确行为**，不该让"干净产物应通过"这条用例失败。
    翻新后：锚同代 + 清单按新内容重打 ⇒ 其余门禁才是被测对象。
    （版本锚自身的拦截能力由 `test_stale_rules_version_fails` 单独覆盖。）
    """
    import sys as _sys
    _sys.path.insert(0, str(SCRIPTS))
    import rules_version as rv
    import stage_manifest as sm
    cur = rv.compute()
    for name in ("claims.json", "section17_status.json", "metrics.json",
                 "invariants.json", "field_coverage.json", "gate_status.json"):
        f = product / name
        if not f.is_file():
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        d["rules_version"] = cur
        f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    cand = ["data_pack_market.md", "claims.json", "section17_status.json",
            "invariants.json", "field_coverage.json", "field_absence.json",
            "fx.json", "shares.json", "dividends_declared.json",
            "balance_sheet_overrides.json", "metrics.json", "stability.json"]
    inputs = [product / c for c in cand if (product / c).is_file()]
    (product / "pack_manifest.json").write_text(
        json.dumps(sm.build("pack", inputs), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")



class TestUsageErrors:
    def test_nonexistent_dir_is_exit_2(self, tmp_path):
        assert _run(tmp_path / "nope").returncode == 2

    def test_non_product_dir_is_exit_2(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert _run(d).returncode == 2


class TestCatchesDefects:
    """每条门禁都必须**真的能拦住**（否则只是装饰）。"""

    @pytest.fixture
    def product(self, tmp_path):
        src = _ready_product()
        if src is None:
            pytest.skip("工作区没有机制齐备的产物可供复制")
        dst = tmp_path / "600000_测试"
        shutil.copytree(src, dst)
        _make_current(dst)
        return dst

    def test_clean_product_is_pack_level_pass(self, product):
        r = _run(product)
        assert r.returncode == 0, r.stdout[-1500:]
        assert "包级通过" in r.stdout or "**通过**" in r.stdout

    def test_missing_metrics_json_fails(self, product):
        (product / "metrics.json").unlink()
        r = _run(product)
        assert r.returncode == 1
        assert "缺 metrics.json" in r.stdout

    def test_tampered_metrics_json_fails(self, product):
        """数值件被改动 ⇒ 双跑比对必须发现（这是迁移安全网）。"""
        p = product / "metrics.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        sec = next(iter(d["tables"]))
        label = next(iter(d["tables"][sec]["rows"]))
        d["tables"][sec]["rows"][label][1] = "999,999.99"
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        r = _run(product)
        assert r.returncode == 1
        assert "不一致" in r.stdout

    def test_missing_gate_status_fails(self, product):
        (product / "gate_status.json").unlink()
        r = _run(product)
        assert r.returncode == 1
        assert "缺 gate_status.json" in r.stdout

    def test_tampered_manifest_fails(self, product):
        """上游被改动 ⇒ 阶段清单必须报出来。"""
        pack = product / "data_pack_market.md"
        pack.write_text(pack.read_text(encoding="utf-8") + "\n<!-- tampered -->\n",
                        encoding="utf-8")
        r = _run(product)
        assert r.returncode == 1
        assert "已变更" in r.stdout or "缺失" in r.stdout

    def test_stale_rules_version_fails(self, product):
        """版本锚不同代 ⇒ 必须点名陈旧（这是 T5 之前**未接入任何前置**的信号）。"""
        for name in ("claims.json", "section17_status.json", "metrics.json",
                     "pack_manifest.json", "gate_status.json", "invariants.json"):
            f = product / name
            if not f.is_file():
                continue
            d = json.loads(f.read_text(encoding="utf-8"))
            d["rules_version"] = "deadbeef0000"
            f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        r = _run(product)
        assert "陈旧" in r.stdout


class TestReportLevelGates:
    def test_without_report_the_delivery_gates_are_skipped_and_flagged(self, tmp_path):
        src = _ready_product()
        if src is None:
            pytest.skip("无可用产物")
        dst = tmp_path / "600000_测试"
        shutil.copytree(src, dst)
        _make_current(dst)
        r = _run(dst)
        assert "未提供报告" in r.stdout
        assert "包级通过" in r.stdout
        # 退出码 0（包级通过），但输出必须**明说**交付级门禁没跑
        assert r.returncode == 0

    def test_missing_report_path_is_skip_not_fail(self, tmp_path):
        src = _ready_product()
        if src is None:
            pytest.skip("无可用产物")
        dst = tmp_path / "600000_测试"
        shutil.copytree(src, dst)
        r = _run(dst, "--report", str(tmp_path / "nope.md"))
        assert "未提供报告" in r.stdout


class TestManifestPathSemantics:
    """清单的两条校验路径**语义不同**，必须各司其职（实测踩出来的）。

    * `stage_manifest.check()`（CLI）按**清单记录的路径**去找文件 ——
      对"原位校验"是对的；产物被移动后原位置若不复存在，它报"缺失"（正确：冻结对象没了）。
    * `verify_product.gate_manifest` 按 **`<dir>/<basename>`** 解析 ——
      对"校验这一件产物"是对的；否则拿着副本去校验会**假通过**
      （实测：复制 /tmp/t4 到 tmp 后改动副本的 pack，按记录路径校验竟然通过，
       因为记录指向的**原件**没动）。
    """

    def test_verify_gate_detects_change_to_the_copy(self, tmp_path):
        src = _ready_product()
        if src is None:
            pytest.skip("无可用产物")
        dst = tmp_path / "copy"
        shutil.copytree(src, dst)
        pack = dst / "data_pack_market.md"
        pack.write_text(pack.read_text(encoding="utf-8") + "\n<!-- x -->\n", encoding="utf-8")
        r = _run(dst)
        assert "已变更" in r.stdout, "按本目录解析应能发现副本被改"

    def test_recorded_path_check_reports_missing_when_original_is_gone(self, tmp_path):
        """把清单里的路径指向一个不存在的位置 ⇒ CLI 语义报"缺失"（不是静默通过）。"""
        import stage_manifest as sm
        mk = tmp_path / "600000_测试"
        mk.mkdir()
        (mk / "data_pack_market.md").write_text("## 1. x\n", encoding="utf-8")
        man = sm.build("pack", [mk / "data_pack_market.md"])
        man["inputs"][0]["path"] = "output/does_not_exist/data_pack_market.md"
        mp = mk / "pack_manifest.json"
        mp.write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
        diffs = sm.check(str(mp))
        assert diffs and any("缺失" in x for x in diffs)

    def test_verify_gate_reports_missing_entry_in_its_own_dir(self, tmp_path):
        """清单声明了一个本目录里没有的文件 ⇒ verify 的门禁必须 FAIL。"""
        import stage_manifest as sm
        mk = tmp_path / "600000_测试"
        mk.mkdir()
        (mk / "data_pack_market.md").write_text("## 1. x\n", encoding="utf-8")
        man = sm.build("pack", [mk / "data_pack_market.md"])
        man["inputs"].append({"path": "output/600000_测试/ghost.json",
                              "md5": "0" * 32, "bytes": 1, "status": "ok"})
        (mk / "pack_manifest.json").write_text(json.dumps(man, ensure_ascii=False),
                                               encoding="utf-8")
        import verify_product as vp
        g = vp.gate_manifest(str(mk))
        assert g.status == vp.FAIL
        assert any("缺失" in e for e in g.evidence)


class TestAggregatorIsReadOnly:
    """本脚本承诺**只读** —— 不得污染被验收的产物。

    回归：初版把 `viewpoint_audit --output-dir <产物目录>` 直接传下去，
    实测在产物里写出 `viewpoint_audit.md/.json`（自己污染自己的验收对象）。
    """

    def test_no_new_files_are_written_into_the_product(self, tmp_path):
        src = _ready_product()
        if src is None:
            pytest.skip("无可用产物")
        dst = tmp_path / "600000_测试"
        shutil.copytree(src, dst)
        before = {p.name for p in dst.rglob("*") if p.is_file()}
        rep = None
        for cand in dst.glob("*分析报告.md"):
            rep = cand
            break
        # 没报告也应保持只读
        _run(dst, *(["--report", str(rep)] if rep else []))
        after = {p.name for p in dst.rglob("*") if p.is_file()}
        assert not (after - before), f"只读承诺被破坏，新增了：{sorted(after - before)}"
