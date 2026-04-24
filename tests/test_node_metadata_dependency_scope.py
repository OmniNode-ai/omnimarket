"""Tests for ONCP node metadata dependency scope enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_node_metadata_dependencies import (
    NodeDependencyFinding,
    check_node_dependency_scope,
    format_findings,
)

_REPO_ROOT = Path(__file__).parent.parent
_NODES_DIR = _REPO_ROOT / "src" / "omnimarket" / "nodes"


def _write_node(
    root: Path,
    node_name: str,
    *,
    imports: str,
    dependencies: list[str] | None,
) -> None:
    node_dir = root / node_name
    node_dir.mkdir(parents=True)
    (node_dir / "__init__.py").write_text(imports)
    if dependencies is not None:
        dependencies_yaml = "\n".join(f'  - "{dep}"' for dep in dependencies)
        (node_dir / "metadata.yaml").write_text(
            "\n".join(
                [
                    f"name: {node_name}",
                    'version: "1.0.0"',
                    'description: "Test node"',
                    "pack: test",
                    "display_name: Test Node",
                    "node_role: effect",
                    "dependencies:",
                    dependencies_yaml or "  []",
                    "",
                ]
            )
        )


def test_checker_rejects_missing_metadata(tmp_path: Path) -> None:
    """Every node package must have metadata.yaml."""

    _write_node(
        tmp_path,
        "node_missing_metadata",
        imports="import httpx\n",
        dependencies=None,
    )

    findings = check_node_dependency_scope(
        nodes_dir=tmp_path,
    )

    assert findings == [
        NodeDependencyFinding(
            node_name="node_missing_metadata",
            message="missing metadata.yaml",
        )
    ]


def test_checker_rejects_node_import_missing_metadata_dependency(
    tmp_path: Path,
) -> None:
    """Node-owned external imports must be declared in metadata dependencies."""

    _write_node(
        tmp_path,
        "node_missing_dependency",
        imports="import httpx\n",
        dependencies=["omnibase_core>=0.39.0"],
    )

    findings = check_node_dependency_scope(
        nodes_dir=tmp_path,
    )

    assert len(findings) == 1
    assert findings[0].node_name == "node_missing_dependency"
    assert "httpx" in findings[0].message


def test_checker_allows_shared_runtime_dependencies(tmp_path: Path) -> None:
    """Framework dependencies do not need repeated per-node declarations."""

    _write_node(
        tmp_path,
        "node_shared_dependency",
        imports="from pydantic import BaseModel\n",
        dependencies=["omnibase_core>=0.39.0"],
    )

    findings = check_node_dependency_scope(
        nodes_dir=tmp_path,
    )

    assert findings == []


def test_current_repo_node_metadata_dependency_scope_is_clean() -> None:
    """Current omnimarket node metadata must satisfy dependency-scope policy."""

    findings = check_node_dependency_scope(nodes_dir=_NODES_DIR)

    assert findings == [], format_findings(findings)


@pytest.mark.parametrize(
    ("module_import", "expected_dependency"),
    [
        ("import omnimemory\n", "omninode-memory"),
        ("import qdrant_client\n", "qdrant-client"),
        ("import yaml\n", "pyyaml"),
    ],
)
def test_checker_normalizes_module_imports_to_distribution_names(
    tmp_path: Path,
    module_import: str,
    expected_dependency: str,
) -> None:
    """Import roots are compared against package distribution names."""

    _write_node(
        tmp_path,
        "node_normalized_dependency",
        imports=module_import,
        dependencies=[expected_dependency],
    )

    findings = check_node_dependency_scope(
        nodes_dir=tmp_path,
    )

    assert findings == []
