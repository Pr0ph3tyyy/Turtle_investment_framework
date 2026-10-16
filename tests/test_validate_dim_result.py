"""Tests for scripts/validate_dim_result.py — the L1 contract gate.

包 2 决策 5 requires the contract check to be a **hard failure** (not a warning), because a
warning-only gate degenerates into the v1 "prose handoff". These tests pin the CLI contract:
exit 0 = valid, 1 = validation failure, 2 = unusable input/schema.
"""

import json
import pathlib
import sys

import pytest

import validate_dim_result as vdr

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "dim_result"
SCHEMA = ROOT / "shared" / "qualitative" / "references" / "dim_result.schema.json"


def _validator():
    return vdr._validator(vdr.load_schema(str(SCHEMA)))


def test_schema_loads_and_validator_builds():
    assert str(SCHEMA) == vdr.DEFAULT_SCHEMA or SCHEMA.exists()
    _validator()


@pytest.mark.parametrize("name", ["D2_golden.json", "D6_not_applicable.json", "D1_insufficient.json"])
def test_valid_fixtures_pass(name):
    inst = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert vdr.validate_instances([inst], _validator()) == []


def test_invalid_instance_reports_located_error():
    inst = json.loads((FIXTURES / "D2_golden.json").read_text(encoding="utf-8"))
    del inst["params"]["moat_rating"]
    errors = vdr.validate_instances([inst], _validator())
    assert errors, "缺必填字段必须报错"
    assert any("moat_rating" in e for e in errors)
    assert any("params" in e for e in errors), "错误应带字段路径"


def test_array_file_is_validated_element_wise(tmp_path):
    """A single all.json holding six contract objects must validate element-wise."""
    d2 = json.loads((FIXTURES / "D2_golden.json").read_text(encoding="utf-8"))
    d6 = json.loads((FIXTURES / "D6_not_applicable.json").read_text(encoding="utf-8"))
    p = tmp_path / "all.json"
    p.write_text(json.dumps([d2, d6], ensure_ascii=False), encoding="utf-8")
    instances, files = vdr.load_instances(str(p))
    assert len(instances) == 2 and len(files) == 1
    assert vdr.validate_instances(instances, _validator()) == []

    d2_bad = json.loads((FIXTURES / "D2_golden.json").read_text(encoding="utf-8"))
    d2_bad["params"]["moat_rating"] = "中等"  # 值域漂移
    p.write_text(json.dumps([d2, d2_bad], ensure_ascii=False), encoding="utf-8")
    instances, _ = vdr.load_instances(str(p))
    errors = vdr.validate_instances(instances, _validator())
    assert errors and any("[1]" in e for e in errors), "数组中的第 2 个元素出错必须带下标"


def test_directory_input_collects_all_json(tmp_path):
    for name in ("D2_golden.json", "D6_not_applicable.json"):
        (tmp_path / name).write_text((FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    instances, files = vdr.load_instances(str(tmp_path))
    assert len(files) == 2 and len(instances) == 2


# ===== CLI exit codes =====


def _run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["validate_dim_result.py", *argv])
    code = vdr.main()
    capsys.readouterr()
    return code


def test_cli_exit_0_on_valid(monkeypatch, capsys):
    assert _run(monkeypatch, capsys, "--input", str(FIXTURES / "D2_golden.json")) == 0


def test_cli_exit_1_on_contract_failure(monkeypatch, capsys, tmp_path):
    inst = json.loads((FIXTURES / "D2_golden.json").read_text(encoding="utf-8"))
    inst["verdict"]["material_status"] = "够用"  # 非枚举值
    p = tmp_path / "D2.json"
    p.write_text(json.dumps(inst, ensure_ascii=False), encoding="utf-8")
    assert _run(monkeypatch, capsys, "--input", str(p)) == 1


def test_cli_exit_2_on_missing_input(monkeypatch, capsys, tmp_path):
    assert _run(monkeypatch, capsys, "--input", str(tmp_path / "nope.json")) == 2


def test_cli_exit_2_on_broken_schema(monkeypatch, capsys, tmp_path):
    """An unresolvable $ref must be reported as a *contract* problem (exit 2), not as an
    instance violation (exit 1). ``check_schema`` does not resolve $ref, so the failure only
    surfaces during validation — hence the explicit try/except in main()."""
    bad = tmp_path / "schema.json"
    bad.write_text(json.dumps({
        "type": "object",
        "properties": {"dimension": {"$ref": "#/$defs/missing"}},  # must be resolved
        "required": ["dimension"],
    }))
    code = _run(monkeypatch, capsys, "--input", str(FIXTURES / "D2_golden.json"), "--schema", str(bad))
    assert code == 2


def test_cli_exit_2_on_empty_directory(monkeypatch, capsys, tmp_path):
    assert _run(monkeypatch, capsys, "--input", str(tmp_path)) == 2
