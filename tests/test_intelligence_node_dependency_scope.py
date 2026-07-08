"""Regression tests for intelligence node dependency scoping."""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).parent.parent
_NODES_DIR = _REPO_ROOT / "src" / "omnimarket" / "nodes"
_HEAVY_INTELLIGENCE_PACKAGES = (
    "adaptive-classifier",
    "omninode-intelligence",
    "sentence-transformers",
    "torch",
    "transformers",
)


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


def test_intelligence_ml_stack_is_optional_not_root_dependency() -> None:
    """Market installs the ML stack only when intelligence runtime is requested."""
    with (_REPO_ROOT / "pyproject.toml").open("rb") as f:
        pyproject = tomllib.load(f)

    root_dependencies = pyproject["project"]["dependencies"]
    optional_dependencies = pyproject["project"]["optional-dependencies"]
    intelligence_dependencies = optional_dependencies["intelligence"]

    for package_name in _HEAVY_INTELLIGENCE_PACKAGES:
        assert not any(dep.startswith(package_name) for dep in root_dependencies)

    assert any(
        dep.startswith("adaptive-classifier") for dep in intelligence_dependencies
    )
    assert not any(
        dep.startswith("omninode-intelligence") for dep in intelligence_dependencies
    )


def test_lock_keeps_intelligence_ml_stack_behind_extra() -> None:
    """The lock may resolve ML packages, but base omnimarket must not require them."""
    with (_REPO_ROOT / "uv.lock").open("rb") as f:
        lock = tomllib.load(f)

    omnimarket = next(
        package for package in lock["package"] if package["name"] == "omnimarket"
    )
    root_dependencies = {dep["name"] for dep in omnimarket["dependencies"]}
    optional_dependencies = omnimarket["optional-dependencies"]["intelligence"]

    assert "adaptive-classifier" not in root_dependencies
    assert "omninode-intelligence" not in root_dependencies
    assert optional_dependencies == [{"name": "adaptive-classifier"}]


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
