"""Contract tests for the L1 dimension result schema (`dims/{dim}.json`).

Purpose: a schema that is only "documented" gets silently loosened. These tests make the
contract executable:

1. the schema itself is valid (JSON Schema Draft 2020-12);
2. positive fixtures validate (D2 充分 / D6 不适用 / D1 不足 — all three material states);
3. negative cases **must be rejected** (each one encodes a v1-era failure mode we never
   want back: value-domain drift, missing provenance, unbacked inference, over-long
   silence on thin material, stray unregistered fields);
4. the contract stays in sync with `output_schema.md` (every parameter defined there must
   exist in the contract) and with the downstream `factor_interface.md` consumers.

If someone adds a parameter to `output_schema.md` without updating the contract, test 4
fails — that is the point.
"""

import copy
import json
import pathlib
import re

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "shared" / "qualitative" / "references" / "dim_result.schema.json"
OUTPUT_SCHEMA = ROOT / "shared" / "qualitative" / "references" / "output_schema.md"
FACTOR_INTERFACE = ROOT / "strategies" / "turtle" / "references" / "factor_interface.md"
FIXTURES = ROOT / "tests" / "fixtures" / "dim_result"

SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

# Parameters downstream (Agent C / valuation) consumes from the qualitative report.
FACTOR_INTERFACE_CONSUMERS = {
    "capital_intensity",
    "collection_mode",
    "moat_type",
    "moat_flywheel",
    "moat_rating",
    "cyclicality",
    "cycle_position",
    "management_rating",
    "mda_credibility",
    "mda_impact",
    "holding_structure",
    "sotp_discount_pct",
    "competitors",
    "industry_keywords",
}


def _load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def d2_golden():
    return _load_fixture("D2_golden.json")


def _contract_params() -> set[str]:
    """All parameter names declared in the per-dimension params subschemas."""
    names = set()
    for key, sub in SCHEMA["$defs"].items():
        if key.startswith("paramsD"):
            names |= set(sub.get("properties", {}))
    return names


def _documented_params() -> set[str]:
    """Parameter names from output_schema.md tables (first column, snake_case)."""
    text = OUTPUT_SCHEMA.read_text(encoding="utf-8")
    return set(re.findall(r"^\|\s*([a-z_][a-z0-9_]*)\s*\|", text, re.M))


# ===== 1. schema validity =====


def test_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(SCHEMA)


def test_schema_declares_version_and_dimensions():
    assert SCHEMA["properties"]["schema_version"]["const"] == "1.0"
    assert set(SCHEMA["properties"]["dimension"]["enum"]) == {"D1", "D2", "D3", "D4", "D5", "D6"}


# ===== 2. positive fixtures (all three material states) =====


@pytest.mark.parametrize(
    "fixture",
    ["D2_golden.json", "D6_not_applicable.json", "D1_insufficient.json"],
)
def test_positive_fixtures_validate(fixture):
    instance = _load_fixture(fixture)
    errors = sorted(VALIDATOR.iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def test_material_states_are_all_covered_by_fixtures():
    states = {_load_fixture(f)["verdict"]["material_status"] for f in
              ("D2_golden.json", "D6_not_applicable.json", "D1_insufficient.json")}
    assert states == {"充分", "不足", "不适用"}


# ===== 3. negatives: each must be rejected =====


def _cases():
    """(label, mutation) — every entry must fail validation."""
    return [
        ("D2 缺必填 moat_rating", lambda d: d["params"].pop("moat_rating")),
        ("值域漂移 moat_rating='中等'(应为 '中')", lambda d: d["params"].update(moat_rating="中等")),
        ("inference 缺 basis", lambda d: d["key_metrics"][2]["source"].pop("basis")),
        ("annual_report 缺 page", lambda d: d["evidence"][0]["source"].pop("page")),
        ("value=null 缺 note", lambda d: d["key_metrics"][2].pop("note")),
        ("D2 正文不足 800 字", lambda d: d.update(narrative="太短。")),
        ("computed_metrics 的 ref 不合规(§17.1)", lambda d: d["key_metrics"][0]["source"].update(ref="§17.1")),
        ("未登记的多余参数", lambda d: d["params"].update(moat_rating_v2="强")),
        ("dropped_pages>0 却无 slice_truncated flag", lambda d: d.update(flags=[])),
        ("material_status 非枚举值", lambda d: d["verdict"].update(material_status="够用")),
        ("缺 material_status", lambda d: d["verdict"].pop("material_status")),
        ("D2 material_status=充分 但正文过短（材料不足却不说）",
         lambda d: d["verdict"].update(material_status="充分") or d.update(narrative="短")),
    ]


@pytest.mark.parametrize("label,mutate", _cases(), ids=[c[0] for c in _cases()])
def test_negative_cases_are_rejected(d2_golden, label, mutate):
    instance = copy.deepcopy(d2_golden)
    mutate(instance)
    errors = list(VALIDATOR.iter_errors(instance))
    assert errors, f"契约未拦住：{label}"


def test_not_applicable_requires_flag(d2_golden):
    """不适用 ⇒ must carry not_applicable flag."""
    instance = copy.deepcopy(d2_golden)
    instance["dimension"] = "D2"
    instance["verdict"]["material_status"] = "不适用"
    instance["flags"] = [{"code": "other", "severity": "info"}]
    assert list(VALIDATOR.iter_errors(instance))


def test_insufficient_requires_uncertainty_and_flag(d2_golden):
    """不足 ⇒ must carry uncertainty + data_insufficient flag."""
    instance = copy.deepcopy(d2_golden)
    instance["verdict"]["material_status"] = "不足"
    instance["uncertainty"] = []
    instance["flags"] = []  # neither uncertainty nor flag
    assert list(VALIDATOR.iter_errors(instance))


def test_d6_not_triggered_must_be_not_applicable():
    """holding_structure=false ⇒ material_status must be 不适用."""
    instance = _load_fixture("D6_not_applicable.json")
    instance["verdict"]["material_status"] = "充分"
    assert list(VALIDATOR.iter_errors(instance))


def test_d6_triggered_must_not_be_not_applicable():
    """holding_structure=true ⇒ material_status may not be 不适用."""
    instance = _load_fixture("D6_not_applicable.json")
    instance["params"] = {"holding_structure": True, "sotp_value_mm": 12000.0, "sotp_discount_pct": 25.0}
    instance["verdict"]["material_status"] = "不适用"
    assert list(VALIDATOR.iter_errors(instance))


def test_d6_not_applicable_cannot_carry_sotp_numbers():
    instance = _load_fixture("D6_not_applicable.json")
    instance["params"]["sotp_value_mm"] = 12000.0
    assert list(VALIDATOR.iter_errors(instance))


def test_d3_non_cyclical_forces_cycle_position_na():
    instance = copy.deepcopy(_load_fixture("D2_golden.json"))
    instance["dimension"] = "D3"
    instance["params"] = {"cyclicality": "非周期", "cycle_position": "顶部",
                          "regulatory_risk": "低", "industry_keywords": ["轮胎"]}
    instance["narrative"] = "非周期行业判断。" * 60  # ≥400 字，隔离字数因素
    assert list(VALIDATOR.iter_errors(instance))


def test_d3_narrative_floor_is_400():
    instance = copy.deepcopy(_load_fixture("D2_golden.json"))
    instance["dimension"] = "D3"
    instance["params"] = {"cyclicality": "强周期", "cycle_position": "中段",
                          "regulatory_risk": "低", "industry_keywords": ["轮胎"]}
    instance["narrative"] = "短。"  # < 400
    assert list(VALIDATOR.iter_errors(instance))


# ===== 4. contract ⇄ spec sync (drift guards) =====


def test_contract_covers_every_documented_parameter():
    """Every parameter in output_schema.md must exist in the contract."""
    documented = _documented_params()
    contract = _contract_params()
    missing = sorted(documented - contract)
    assert not missing, f"output_schema.md 已定义但契约缺失: {missing}"


def test_downstream_consumers_are_all_present():
    """Every qualitative parameter factor_interface.md consumes must exist."""
    contract = _contract_params()
    missing = sorted(FACTOR_INTERFACE_CONSUMERS - contract)
    assert not missing, f"下游消费的参数在契约中缺失: {missing}"


def test_no_unregistered_extra_params_in_contract():
    """The contract must not invent parameters the spec does not define."""
    documented = _documented_params()
    contract = _contract_params()
    extra = sorted(contract - documented)
    assert not extra, f"契约中存在 output_schema.md 未定义的参数: {extra}"


def test_factor_interface_consumers_are_still_referenced_in_contract_docs():
    """Guard against renaming a consumer param without updating the interface doc."""
    text = FACTOR_INTERFACE.read_text(encoding="utf-8")
    absent = sorted(p for p in FACTOR_INTERFACE_CONSUMERS if p not in text)
    assert not absent, f"factor_interface.md 未提及: {absent}"


def test_moat_rating_domain_is_the_turtle_mapping_source():
    """The 强/较强/中/弱 domain is what factor_interface maps 优质/中性/负面 from."""
    assert SCHEMA["$defs"]["paramsD2"]["properties"]["moat_rating"]["enum"] == ["强", "较强", "中", "弱"]
