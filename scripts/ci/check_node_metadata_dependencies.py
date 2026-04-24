#!/usr/bin/env python3
"""Validate ONCP node metadata dependency declarations."""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from packaging.requirements import InvalidRequirement, Requirement

LOCAL_IMPORT_ROOTS = {"omnimarket"}

MODULE_TO_DISTRIBUTION = {
    "aiohttp": "aiohttp",
    "aiokafka": "aiokafka",
    "asyncpg": "asyncpg",
    "confluent_kafka": "confluent-kafka",
    "httpx": "httpx",
    "omnibase_compat": "omnibase-compat",
    "omnibase_core": "omnibase_core",
    "omnibase_infra": "omnibase-infra",
    "omnibase_spi": "omnibase-spi",
    "omnimemory": "omninode-memory",
    "onex_change_control": "onex-change-control",
    "psycopg2": "psycopg2-binary",
    "pydantic": "pydantic",
    "qdrant_client": "qdrant-client",
    "structlog": "structlog",
    "typing_extensions": "typing-extensions",
    "yaml": "pyyaml",
}

SHARED_RUNTIME_DEPENDENCIES = {
    "omnibase_core",
    "packaging",
    "pydantic",
    "python-dateutil",
    "pyyaml",
}


@dataclass(frozen=True)
class NodeDependencyFinding:
    """A dependency-scope violation for one node."""

    node_name: str
    message: str


def normalize_distribution_name(name: str) -> str:
    """Normalize package names for dependency comparisons."""

    return name.lower().replace("_", "-")


def dependency_name(requirement: str) -> str:
    """Return the normalized distribution name from a requirement string."""

    try:
        return normalize_distribution_name(Requirement(requirement).name)
    except InvalidRequirement as exc:
        raise ValueError(f"Invalid dependency requirement {requirement!r}") from exc


def iter_node_dirs(nodes_dir: Path) -> list[Path]:
    """Return direct node package directories."""

    return sorted(
        path
        for path in nodes_dir.iterdir()
        if path.is_dir() and path.name.startswith("node_")
    )


def import_roots_from_file(path: Path) -> set[str]:
    """Parse top-level absolute import roots from a Python file."""

    tree = ast.parse(path.read_text(), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", maxsplit=1)[0])
    return roots


def imported_distributions(node_dir: Path) -> set[str]:
    """Return normalized external distributions imported by a node package."""

    distributions: set[str] = set()
    for path in node_dir.rglob("*.py"):
        if any(part in {"node_tests", "tests", "test"} for part in path.parts):
            continue
        if path.name.startswith("test_"):
            continue
        for root in import_roots_from_file(path):
            if root in LOCAL_IMPORT_ROOTS or root in sys.stdlib_module_names:
                continue
            distribution = MODULE_TO_DISTRIBUTION.get(root)
            if distribution is None:
                distributions.add(normalize_distribution_name(root))
            else:
                distributions.add(normalize_distribution_name(distribution))
    return distributions


def load_node_dependencies(metadata_path: Path) -> set[str]:
    """Load normalized dependency names from metadata.yaml."""

    raw = yaml.safe_load(metadata_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{metadata_path} must contain a mapping")
    dependencies = raw.get("dependencies", [])
    if dependencies is None:
        dependencies = []
    if not isinstance(dependencies, list) or not all(
        isinstance(dep, str) for dep in dependencies
    ):
        raise ValueError(f"{metadata_path} dependencies must be a list[str]")
    return {dependency_name(dep) for dep in dependencies}


def check_node_dependency_scope(
    *,
    nodes_dir: Path,
    shared_dependencies: Iterable[str] = SHARED_RUNTIME_DEPENDENCIES,
) -> list[NodeDependencyFinding]:
    """Validate that node-owned imports are represented in metadata.yaml."""

    shared_dependency_names = {
        normalize_distribution_name(dep) for dep in shared_dependencies
    }
    findings: list[NodeDependencyFinding] = []

    for node_dir in iter_node_dirs(nodes_dir):
        metadata_path = node_dir / "metadata.yaml"
        if not metadata_path.exists():
            findings.append(
                NodeDependencyFinding(
                    node_name=node_dir.name,
                    message="missing metadata.yaml",
                )
            )
            continue

        metadata_dependencies = load_node_dependencies(metadata_path)
        node_imports = imported_distributions(node_dir)
        node_owned_imports = node_imports - shared_dependency_names
        missing = sorted(node_owned_imports - metadata_dependencies)
        if missing:
            findings.append(
                NodeDependencyFinding(
                    node_name=node_dir.name,
                    message=(
                        "metadata.yaml missing dependencies for imports: "
                        + ", ".join(missing)
                    ),
                )
            )

    return findings


def format_findings(findings: Sequence[NodeDependencyFinding]) -> str:
    """Format dependency-scope findings for CI output."""

    lines = ["ONCP node metadata dependency scope violations:"]
    for finding in findings:
        lines.append(f"- {finding.node_name}: {finding.message}")
    return "\n".join(lines)


def main() -> int:
    """Run the dependency-scope check."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--nodes-dir",
        type=Path,
        default=Path("src/omnimarket/nodes"),
        help="Directory containing node_* packages.",
    )
    args = parser.parse_args()

    findings = check_node_dependency_scope(
        nodes_dir=args.nodes_dir,
    )
    if findings:
        print(format_findings(findings), file=sys.stderr)
        return 1
    print("ONCP node metadata dependency scope: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
