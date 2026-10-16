"""Regression guard: the active prompt/orchestration scope must use DSH tool
names, not Claude Code tool names.

Added with the DSH migration (work package A). It mirrors the acceptance grep:

    grep -rnE "WebSearch|AskUserQuestion" .claude/commands .claude/skills \\
        shared/qualitative/coordinator.md shared/qualitative/qualitative_assessment.md \\
        shared/qualitative/data_collection.md shared/qualitative/agents \\
        shared/qualitative/references strategies/turtle strategies/valuation \\
        --include=*.md | grep -v legacy

plus the agent-block / pseudo-code forms (`Agent(`, `Task(`, `Bash(`, and the
Claude-only `subagent_type = "general-purpose"` parameter).

Out of scope by design: `*/legacy/**`, `prompts/**` (v1 archive),
`financial-report-minesweeper/**`, `ciguttprepare/**`.

Note: `scripts/tushare_modules/*.py` emit a tool-name-free placeholder
(`待Agent补充`) precisely so this decoupling never has to be repeated when tool
names change again.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

_SCOPE_FILES = [
    ROOT / ".claude" / "commands" / name
    for name in (
        "business-analysis.md",
        "turtle-analysis.md",
        "valuation.md",
        "download-annual-report.md",
    )
] + [
    ROOT / ".claude" / "skills" / "turtle-analysis" / "SKILL.md",
    ROOT / "AGENTS.md",
    ROOT / "shared" / "qualitative" / "coordinator.md",
    ROOT / "shared" / "qualitative" / "qualitative_assessment.md",
    ROOT / "shared" / "qualitative" / "data_collection.md",
]

_SCOPE_DIRS = [
    ROOT / "shared" / "qualitative" / "agents",
    ROOT / "shared" / "qualitative" / "references",
    ROOT / "strategies" / "turtle",
    ROOT / "strategies" / "valuation",
    # Canonical skill location: DSH discovers skills from .agents/skills/<name>/SKILL.md
    ROOT / ".agents" / "skills",
]

CLAUDE_TOOL_PATTERN = re.compile(
    r"WebSearch|AskUserQuestion|subagent_type|general-purpose|\bTask\(|\bBash\(|\bAgent\("
)


def _scoped_markdown():
    paths = [p for p in _SCOPE_FILES]
    for directory in _SCOPE_DIRS:
        paths += [p for p in directory.rglob("*.md") if "legacy" not in p.parts]
    return sorted(set(paths))


def test_scoped_files_present():
    """The scope must actually resolve to files (guards against silent scope drift)."""
    paths = _scoped_markdown()
    assert len(paths) >= 25, f"expected ≥25 scoped markdown files, found {len(paths)}"
    assert all(p.exists() for p in paths)


@pytest.mark.parametrize(
    "path", _scoped_markdown(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_no_claude_tool_names(path):
    """No Claude Code tool name or pseudo-code block survives in the active scope."""
    hits = CLAUDE_TOOL_PATTERN.findall(path.read_text(encoding="utf-8"))
    assert not hits, (
        f"{path.relative_to(ROOT)} still references Claude Code tool semantics: "
        f"{sorted(set(hits))}"
    )


def test_agent_blocks_renamed_to_subagent():
    """Agent-dispatch pseudo-code blocks became `subagent(...)` with DSH parameters."""
    text = (ROOT / "shared" / "qualitative" / "coordinator.md").read_text(encoding="utf-8")
    assert text.count("subagent(") >= 5
    assert "run_in_background = true" in text
    text = (ROOT / "strategies" / "turtle" / "coordinator.md").read_text(encoding="utf-8")
    assert text.count("subagent(") >= 2


def test_websearch_replaced_by_web_search_tool_name():
    """`WebSearch` became the DSH `web_search` tool name."""
    text = (ROOT / ".claude" / "commands" / "download-annual-report.md").read_text(
        encoding="utf-8"
    )
    assert "`web_search`" in text
