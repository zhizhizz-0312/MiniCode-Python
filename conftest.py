"""Pytest collection controls for repository-local legacy smoke scripts."""

from __future__ import annotations

import os
import tempfile

import pytest

# Keep skill-system state out of the developer's real home directory.  Without
# this, exercising /skills or the load_skill tool in a test would append to
# ~/.mini-code/skill-stats.json.  Tests that care about the location still set
# the variable themselves (setdefault leaves an explicit value alone).
_TEST_STATE_DIR = os.path.join(tempfile.gettempdir(), "minicode-pytest-skill-state")
os.environ.setdefault(
    "MINI_CODE_SKILL_STATS_PATH", os.path.join(_TEST_STATE_DIR, "skill-stats.json")
)
os.environ.setdefault(
    "MINI_CODE_SKILL_STATE_PATH", os.path.join(_TEST_STATE_DIR, "skill-state.json")
)
# Trajectories are the corpus the distillation stage learns from, so a test that
# exercises the collector must not append synthetic turns to the real store.
os.environ.setdefault(
    "MINI_CODE_TRAJECTORY_PATH", os.path.join(_TEST_STATE_DIR, "trajectories")
)
# Same for orchestration audit trails.  This one was missed at first and it showed:
# the suite wrote hundreds of entries into ~/.mini-code/orchestrations/run1/, so the
# developer's own `replay` command showed test noise.
os.environ.setdefault(
    "MINI_CODE_ORCHESTRATION_HOME", os.path.join(_TEST_STATE_DIR, "orchestrations")
)

# The security chain reviews every tool call.  Left on, a test that calls a tool
# would need credentials and would write the audit trail into the developer's home
# directory, so it is off for the suite by default; the chain's own tests build a
# gate with an explicit mode instead.
os.environ["MINI_CODE_SECURITY_MODE"] = "off"
os.environ.setdefault(
    "MINI_CODE_SECURITY_AUDIT_PATH",
    os.path.join(_TEST_STATE_DIR, "security-audit.jsonl"),
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "benchmark: mark test as a benchmark test.")


# These root-level scripts are manual smoke/integration utilities from earlier
# development rounds. Normal pytest coverage lives under tests/.
collect_ignore = [
    "smoke_test.py",
    "test_chinese_input.py",
    "test_integration.py",
    "test_optim.py",
    "test_run.py",
    "test_state_integration.py",
    "visual_test.py",
]

collect_ignore_glob = [
    "benchmarks/*.py",
]


@pytest.fixture
def memory_manager(tmp_path):
    """Create a MemoryManager with temporary paths."""
    from minicode.memory import MemoryManager
    return MemoryManager(project_root=tmp_path)


@pytest.fixture
def memory_with_entries(memory_manager):
    """Create a MemoryManager pre-populated with test entries."""
    from minicode.memory import MemoryScope
    entries = [
        ("project", "architecture", "Uses FastAPI for REST API backend", ["api", "fastapi"]),
        ("project", "code-pattern", "All functions use snake_case naming", ["convention", "naming"]),
        ("project", "testing", "Tests use pytest with fixtures", ["test", "pytest"]),
        ("user", "preference", "Always respond in Chinese", ["language", "chinese"]),
        ("local", "decision", "Use SQLite for development database", ["database", "sqlite"]),
    ]
    for scope, category, content, tags in entries:
        memory_manager.add_entry(
            MemoryScope(scope), category, content, tags
        )
    return memory_manager


@pytest.fixture
def mock_memory_search():
    """Mock search function for testing prompt injection."""
    def mock_search(query, scope=None, limit=20, min_relevance=0.1):
        from minicode.memory import MemoryEntry, MemoryScope
        return [
            MemoryEntry(id="test-1", scope=MemoryScope.PROJECT, category="test", content=f"Mock result for: {query}"),
        ]
    return mock_search


@pytest.fixture
def temp_workspace(tmp_path):
    """Create a temporary workspace with basic structure."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "tests").mkdir()
    (workspace / "src" / "main.py").write_text("# Main file\n")
    return str(workspace)
