"""Tests for scripts/stage_manifest.py —— 阶段交接的版本钉住。

对应三次真实事故：运行期改上游状态文件导致报告自相矛盾（审计 F1）、读到半写产物、
审计读了陈旧基线。纸面规则挡不住，必须"变更即报错"。
"""

import json
import pathlib
import sys

sys.path.insert(0, "scripts")

import stage_manifest as sm  # noqa: E402


def _files(tmp_path):
    a = tmp_path / "pack.md"
    a.write_text("内容 A", encoding="utf-8")
    b = tmp_path / "claims.json"
    b.write_text("{}", encoding="utf-8")
    return a, b


class TestWriteAndCheck:
    def test_roundtrip_is_clean(self, tmp_path):
        a, b = _files(tmp_path)
        assert sm.main(["--write", "pack", "--out-dir", str(tmp_path),
                        "--input", str(a), "--input", str(b)]) == 0
        man = tmp_path / "pack_manifest.json"
        assert man.is_file()
        assert sm.main(["--check", str(man)]) == 0

    def test_changed_input_is_detected(self, tmp_path):
        a, b = _files(tmp_path)
        sm.main(["--write", "pack", "--out-dir", str(tmp_path),
                 "--input", str(a), "--input", str(b)])
        a.write_text("内容 A（被改写）", encoding="utf-8")     # 等价于"运行期改了上游"
        rc = sm.main(["--check", str(tmp_path / "pack_manifest.json")])
        assert rc == 1
        problems = sm.check(tmp_path / "pack_manifest.json")
        assert any("已变更" in p and "pack.md" in p for p in problems)

    def test_missing_input_is_detected(self, tmp_path):
        a, b = _files(tmp_path)
        sm.main(["--write", "pack", "--out-dir", str(tmp_path), "--input", str(a)])
        a.unlink()
        assert sm.main(["--check", str(tmp_path / "pack_manifest.json")]) == 1

    def test_missing_input_at_write_time_is_reported(self, tmp_path):
        rc = sm.main(["--write", "pack", "--out-dir", str(tmp_path),
                      "--input", str(tmp_path / "nope.md")])
        assert rc == 1
        man = json.loads((tmp_path / "pack_manifest.json").read_text(encoding="utf-8"))
        assert man["inputs"][0]["status"] == "missing"

    def test_manifest_records_size_and_md5(self, tmp_path):
        a, _ = _files(tmp_path)
        payload = sm.build("pack", [a])
        e = payload["inputs"][0]
        assert e["status"] == "ok" and len(e["md5"]) == 32 and e["bytes"] == len("内容 A".encode())

    def test_unreadable_manifest_is_exit_2(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        assert sm.main(["--check", str(bad)]) == 2

    def test_json_output_of_check(self, tmp_path):
        a, _ = _files(tmp_path)
        sm.main(["--write", "pack", "--out-dir", str(tmp_path), "--input", str(a)])
        a.write_text("改了", encoding="utf-8")
        out = tmp_path / "res.json"
        sm.main(["--check", str(tmp_path / "pack_manifest.json"), "--json", str(out)])
        assert json.loads(out.read_text(encoding="utf-8"))["problems"]
