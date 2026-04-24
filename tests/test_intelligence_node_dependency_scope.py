"""Regression tests for intelligence node dependency scoping."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).parent.parent
_NODES_DIR = _REPO_ROOT / "src" / "omnimarket" / "nodes"


def _load_metadata(node_name: str) -> dict:
    with (_NODES_DIR / node_name / "metadata.yaml").open() as f:
        return yaml.safe_load(f)


def test_omninode_intelligence_is_not_a_root_dependency() -> None:
    """Migrated intelligence nodes must not pull the legacy package at root."""
    with (_REPO_ROOT / "pyproject.toml").open("rb") as f:
        pyproject = tomllib.load(f)

    root_dependencies = pyproject["project"]["dependencies"]
    root_sources = pyproject.get("tool", {}).get("uv", {}).get("sources", {})

    assert not any(dep.startswith("omninode-intelligence") for dep in root_dependencies)
    assert "omninode-intelligence" not in root_sources


def test_intelligence_nodes_do_not_require_omniintelligence_package() -> None:
    """Migrated nodes use omnimarket-owned primitives, not omniintelligence."""
    for node_name in ("node_intelligence_orchestrator", "node_intelligence_reducer"):
        metadata = _load_metadata(node_name)
        assert not any(
            dep.startswith("omninode-intelligence") for dep in metadata["dependencies"]
        )


def test_quality_scoring_compute_remains_runtime_light() -> None:
    """Quality scoring owns intelligence topics but has no omniintelligence import."""
    metadata = _load_metadata("node_quality_scoring_compute")
    assert not any(
        dep.startswith("omninode-intelligence") for dep in metadata["dependencies"]
    )
