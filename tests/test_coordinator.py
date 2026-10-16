"""Tests for the ACTIVE coordinators — not the v1 archive.

These tests used to read ``prompts/coordinator.md``, the v1 archive that no pipeline
uses any more, and asserted Claude Code tool names (``WebSearch``,
``AskUserQuestion``). They therefore passed while guarding a dead file. They now guard
the live coordinators:

- ``strategies/turtle/coordinator.md``  — 龟龟 pipeline: Step A → 3.1 Agent B → 3.2 Agent C
- ``shared/qualitative/coordinator.md`` — 商业分析: data collection → PDF → 审计门

``TestSlashCommand`` still guards ``.claude/commands/turtle-analysis.md``, which remains
a Claude Code artifact that ships with the repo (see ``AGENTS.md`` for the DSH entry).
"""

import os

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
TURTLE_COORDINATOR = os.path.join(REPO_ROOT, "strategies", "turtle", "coordinator.md")
BA_COORDINATOR = os.path.join(REPO_ROOT, "shared", "qualitative", "coordinator.md")
COMMANDS_DIR = os.path.join(REPO_ROOT, ".claude", "commands")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def coordinator_content():
    """Active turtle coordinator."""
    return _read(TURTLE_COORDINATOR)


@pytest.fixture(scope="module")
def ba_coordinator_content():
    """Active business-analysis coordinator."""
    return _read(BA_COORDINATOR)


class TestTurtleCoordinator:
    """Verify the active 龟龟 pipeline is wired correctly."""

    def test_coordinator_exists(self):
        assert os.path.exists(TURTLE_COORDINATOR)

    def test_pipeline_ordering(self, coordinator_content):
        """Steps must appear in order: Step A → 3.1 (Agent B) → 3.2 (Agent C)."""
        idx = [coordinator_content.find(s) for s in ("Step A", "Step 3.1", "Step 3.2")]
        assert -1 not in idx, "a pipeline step is missing"
        assert idx == sorted(idx), "pipeline steps are out of order"

    def test_market_refresh_command(self, coordinator_content):
        """Step A refreshes the market sections through the collector."""
        assert "tushare_collector.py" in coordinator_content
        assert "--refresh-market" in coordinator_content

    def test_quantitative_agent_dispatch(self, coordinator_content):
        """Step 3.1 dispatches Agent B to the quantitative prompt."""
        assert "phase3_quantitative.md" in coordinator_content
        assert "Agent B" in coordinator_content

    def test_valuation_agent_dispatch(self, coordinator_content):
        """Step 3.2 dispatches Agent C to the valuation prompt."""
        assert "phase3_valuation.md" in coordinator_content
        assert "Agent C" in coordinator_content

    def test_prerequisites_are_the_ba_outputs(self, coordinator_content):
        """The pipeline consumes the business-analysis deliverables."""
        assert "qualitative_report.md" in coordinator_content
        assert "data_pack_market.md" in coordinator_content
        assert "data_pack_report.md" in coordinator_content  # optional; degraded mode

    def test_uses_dsh_tool_names(self, coordinator_content):
        """Interaction/search use DSH tool names, not Claude Code ones."""
        assert "ask_user_question" in coordinator_content
        assert "web_search" in coordinator_content
        assert "AskUserQuestion" not in coordinator_content
        assert "WebSearch" not in coordinator_content

    def test_ask_user_question_interaction_points(self, coordinator_content):
        """Several decision points are surfaced to the user."""
        count = coordinator_content.count("ask_user_question")
        assert count >= 3, f"Expected >=3 ask_user_question references, got {count}"

    def test_file_path_convention(self, coordinator_content):
        """Paths follow the {output_dir} convention."""
        assert "{output_dir}" in coordinator_content or "output/" in coordinator_content

    def test_graceful_degradation(self, coordinator_content):
        """Degraded paths are defined (missing PDF footnotes / failed refresh)."""
        assert "降级" in coordinator_content or "跳过" in coordinator_content


class TestBusinessAnalysisCoordinator:
    """The active 商业分析 coordinator owns the PDF + pre-computation steps."""

    def test_coordinator_exists(self):
        assert os.path.exists(BA_COORDINATOR)

    def test_pdf_preprocessing_wired(self, ba_coordinator_content):
        """PDF section extraction is part of the BA pipeline."""
        assert "pdf_preprocessor.py" in ba_coordinator_content

    def test_deterministic_precomputation_wired(self, ba_coordinator_content):
        """CM§1-§5 deterministic pre-computation is part of the BA pipeline."""
        assert "quality_control.py" in ba_coordinator_content

    def test_uses_dsh_tool_names(self, ba_coordinator_content):
        assert "web_search" in ba_coordinator_content
        assert "WebSearch" not in ba_coordinator_content
        assert "AskUserQuestion" not in ba_coordinator_content

    def test_file_path_convention(self, ba_coordinator_content):
        assert "{output_dir}" in ba_coordinator_content or "output/" in ba_coordinator_content


class TestSlashCommand:
    """Verify the turtle-analysis Claude Code slash command file.

    This file is a Claude Code artifact we still ship; AGENTS.md documents the DSH
    entry point (read the coordinator markdown instead of invoking a slash command).
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.cmd_path = os.path.join(COMMANDS_DIR, "turtle-analysis.md")

    def test_slash_command_exists(self):
        """Slash command file exists at .claude/commands/turtle-analysis.md."""
        assert os.path.exists(self.cmd_path), "Slash command file not found"

    def test_slash_command_references_coordinator(self):
        """Slash command references coordinator.md for execution."""
        with open(self.cmd_path, encoding="utf-8") as f:
            content = f.read()
        assert "coordinator" in content.lower() or "prompts/" in content

    def test_slash_command_includes_phases(self):
        """Slash command mentions the multi-phase pipeline."""
        with open(self.cmd_path, encoding="utf-8") as f:
            content = f.read()
        assert "Phase" in content or "phase" in content

    def test_slash_command_includes_output_path(self):
        """Slash command specifies output path convention."""
        with open(self.cmd_path, encoding="utf-8") as f:
            content = f.read()
        assert "output/" in content

    def test_slash_command_uses_arguments(self):
        """Slash command uses $ARGUMENTS for stock code input."""
        with open(self.cmd_path, encoding="utf-8") as f:
            content = f.read()
        assert "$ARGUMENTS" in content
