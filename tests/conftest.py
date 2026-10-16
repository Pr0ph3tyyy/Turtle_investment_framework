"""Shared test fixtures for Turtle Investment Framework."""

import json
import os
import sys

import pytest

# Add scripts/ to path so we can import from there
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
MOCK_TUSHARE_DIR = os.path.join(FIXTURES_DIR, "mock_tushare_responses")


@pytest.fixture
def fixtures_dir():
    """Path to the test fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture
def mock_tushare_dir():
    """Path to the mock Tushare API responses directory."""
    return MOCK_TUSHARE_DIR


@pytest.fixture
def load_mock_response():
    """Factory fixture to load a mock Tushare API response by filename."""

    def _load(filename: str) -> dict:
        filepath = os.path.join(MOCK_TUSHARE_DIR, filename)
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    return _load


@pytest.fixture
def sample_stock_code():
    """Standard test stock code (Yili 伊利股份)."""
    return "600887.SH"


@pytest.fixture
def tmp_output_dir(tmp_path):
    """Temporary output directory for test file generation."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return output_dir


# Mark integration tests that require live API
integration = pytest.mark.skipif(
    not os.environ.get("TUSHARE_TOKEN"),
    reason="TUSHARE_TOKEN not set, skipping integration test",
)


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "integration: mark test as requiring live API access")


@pytest.fixture(autouse=True)
def _isolate_env_file(monkeypatch, tmp_path):
    """Prevent _load_env_file from finding the real .env during tests."""
    import config as config_mod

    monkeypatch.setattr(config_mod, "__file__", str(tmp_path / "scripts" / "config.py"))


# ---------------------------------------------------------------------------
# 归档产物（`output/_legacy/`）作为**只读冻结夹具**
#
# 背景（2026-10-15）：用户明确"不在意此前产出的报告"，107 个旧产物统一归档到
# `output/_legacy/`。归档后一批测试立刻失败 —— 它们**隐式依赖**真实产物目录
# （`output/00506_中国食品/…`），而它们测的逻辑（覆盖率、结构、完整性）与是哪个
# 标的毫无关系。那是"测试与环境耦合"，不是功能缺陷。
#
# 处置分两类：
#   · **能 hermetic 的一律 hermetic**：`tests/fixtures/claims_min.json`、
#     最小合成 pack（见 test_bare_token_coverage / test_invariants）；
#   · **确实需要"多样的真实输入"的**（行为冻结、结构扫描）读归档产物 ——
#     冻结的真实数据比合成的更适合当夹具，但必须**显式**、集中、可跳过。
# ---------------------------------------------------------------------------

LEGACY_ROOT = os.path.join(os.path.dirname(__file__), "..", "output", "_legacy")


@pytest.fixture
def legacy_root():
    """归档产物根目录；不存在时跳过（不要静默用空目录假装通过）。"""
    if not os.path.isdir(LEGACY_ROOT):
        pytest.skip(f"归档产物目录不存在：{LEGACY_ROOT}")
    return LEGACY_ROOT


def legacy_product(name: str) -> str:
    """返回归档产物的绝对路径；不存在时由调用方决定是否 skip。

    模块级 helper（不是 fixture）以便在 `@pytest.mark.parametrize` 里使用。
    """
    return os.path.abspath(os.path.join(LEGACY_ROOT, name))
