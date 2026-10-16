"""Guards for the v3 pipeline wiring in the prompt layer.

Removing the "single agent + 1M context" design is easy to *describe* and easy to silently
regress. These tests pin the wiring in the active orchestration files:

- the coordinator must run slice → 6 parallel dimensions → synthesis, with the contract gate;
- the Claude Code mirror and the DSH entry points must describe the same pipeline;
- **no active file may claim the old "1M context" / full-report single-agent design**.

They complement ``tests/test_dsh_tool_names.py`` (which forbids Claude tool names) and
``tests/test_dim_result_contract.py`` (which pins the contract itself).
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

COORDINATOR = ROOT / "shared" / "qualitative" / "coordinator.md"
COMMAND = ROOT / ".claude" / "commands" / "business-analysis.md"
AGENTS = ROOT / "AGENTS.md"
SKILL = ROOT / ".agents" / "skills" / "business-analysis" / "SKILL.md"
ASSESSMENT = ROOT / "shared" / "qualitative" / "qualitative_assessment.md"
README = ROOT / "README.md"

# v3 pipeline markers every entry point must mention
PIPELINE_MARKERS = ["dimension_slicer.py", "validate_dim_result.py", "dims/"]


def _read(path):
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", [COORDINATOR, COMMAND, AGENTS, SKILL])
def test_entry_points_describe_the_v3_pipeline(path):
    text = _read(path)
    missing = [m for m in PIPELINE_MARKERS if m not in text]
    assert not missing, f"{path.relative_to(ROOT)} 未提及 v3 环节: {missing}"


@pytest.mark.parametrize("path", [COORDINATOR, COMMAND])
def test_pipeline_steps_are_explicit(path):
    text = _read(path)
    for marker in ["2.0", "2.1", "2.2"]:
        assert marker in text, f"{path.relative_to(ROOT)} 缺少 Step {marker}"
    assert "material_status" in text, "必须提到材料状态（三态契约字段）"


def test_coordinator_references_the_contract():
    text = _read(COORDINATOR)
    assert "dim_result.schema.json" in text
    assert "report_consistency.py" in text, "审计门必须保留"


@pytest.mark.parametrize("path", [COORDINATOR, COMMAND, AGENTS, SKILL, ASSESSMENT, README])
def test_no_file_claims_the_removed_1m_single_agent_design(path):
    """The old design must not linger anywhere user- or agent-facing."""
    text = _read(path)
    assert "1M context" not in text, f"{path.relative_to(ROOT)} 仍在声称 1M context"
    assert "1M 上下文" not in text
    assert "单 Agent 全量分析" not in text
    assert "Mode A" not in text, f"{path.relative_to(ROOT)} 仍引用已移除的 Mode A"


def test_cleanroom_must_not_read_dimension_json():
    """Step 2X loses its anti-anchoring property if it can read the dimension results."""
    text = _read(COORDINATOR)
    step_2x = text[text.find("Step 2X") :]
    assert "dims/**" in step_2x, "clean-room 的禁读清单必须包含 dims/**"


def test_slices_are_declared_internal_not_deliverables():
    for path in (COORDINATOR, COMMAND):
        text = _read(path)
        assert "非交付物" in text
        assert "dims/" in text and "slices/" in text
        assert "DELIVER ONLY" in text or "sole deliverable" in text


def test_assessment_explains_per_dimension_usage():
    """The framework file is now read section-by-section, not cover-to-cover."""
    text = _read(ASSESSMENT)
    assert "使用方式" in text
    assert "dim_result.schema.json" in text
    assert "材料不足" in text or "material_status" in text
    assert "只负责" in text, "必须说明每个子代理只负责一个维度"


def test_shared_prefix_is_declared_byte_identical():
    """Cache friendliness and numeric single-sourcing both depend on this rule."""
    text = _read(COORDINATOR)
    assert "逐字节相同" in text


def test_audit_step_forbids_stale_baseline_copies():
    """Live-run regression: the Step 3B auditor read a baseline backup that happened to sit
    inside the audited directory, and produced a whole audit (1 major + 13 minor) quoting a
    report that was not the deliverable. The prompt must forbid stale copies explicitly."""
    text = _read(COORDINATOR)
    step_3b = text[text.find("### 3B") : text.find("### 3C")]
    assert "不得读取" in step_3b
    assert "baseline" in step_3b
    assert "md5" in step_3b, "审计必须自证被审文件身份（路径+大小+md5）"
    # 基线存放约定：不得留在被审目录内
    assert "output/_baselines/" in text
