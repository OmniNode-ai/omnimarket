"""OMN-21422: the market runtime image is FROM the infra runtime base and carries both GitHub nodes."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CHECK_OPEN = "RUN /app/.venv/bin/python - <<'GITHUB_NODES'"
CHECK_CLOSE = "GITHUB_NODES"
GITHUB_NODES = ("node_github_repo_write_effect", "node_github_repo_gateway_effect")


@pytest.fixture
def dockerfile() -> str:
    return (ROOT / "Dockerfile.runtime").read_text()


def github_node_check(dockerfile: str) -> str:
    lines = dockerfile.splitlines()
    assert lines.count(CHECK_OPEN) == 1
    start = lines.index(CHECK_OPEN) + 1
    end = lines.index(CHECK_CLOSE, start)
    return "\n".join(lines[start:end]) + "\n"


def run_check(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-"],
        input=source,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.unit
def test_image_from_names_the_infra_runtime_base(dockerfile: str) -> None:
    lines = dockerfile.splitlines()
    from_lines = [line for line in lines if line.startswith("FROM ")]
    assert from_lines == ["FROM ${INFRA_RUNTIME_IMAGE}"]
    assert lines.index("ARG INFRA_RUNTIME_IMAGE") < lines.index(from_lines[0])
    assert not re.search(r"[0-9a-fA-F]{40}", dockerfile)
    assert "@sha256:" not in dockerfile


@pytest.mark.unit
def test_github_node_check_runs_after_install_and_entry_point_strip(
    dockerfile: str,
) -> None:
    check = dockerfile.index(CHECK_OPEN)
    assert check > dockerfile.index('"omnimarket @ file:///opt/omnimarket"')
    assert check > dockerfile.index(
        "-m omnibase_infra.runtime.strip_runtime_entry_points"
    )
    source = github_node_check(dockerfile)
    assert f"NODES = ({', '.join(f'"{n}"' for n in GITHUB_NODES)})" in source
    assert 'entry_points(group="onex.nodes")' in source
    assert "importlib.import_module(entry.value)" in source


@pytest.mark.unit
def test_both_github_nodes_are_registered_by_omnimarket() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    entry_points = pyproject["project"]["entry-points"]["onex.nodes"]
    for node in GITHUB_NODES:
        assert entry_points[node] == f"omnimarket.nodes.{node}"


@pytest.mark.unit
def test_image_check_passes_where_both_nodes_are_installed(dockerfile: str) -> None:
    result = run_check(github_node_check(dockerfile))
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "github nodes registered and importable: "
        "node_github_repo_write_effect,node_github_repo_gateway_effect\n"
    )


@pytest.mark.unit
def test_image_check_refuses_an_unregistered_node(dockerfile: str) -> None:
    source = github_node_check(dockerfile).replace(
        '"node_github_repo_gateway_effect"', '"node_github_repo_absent_omn21422"'
    )
    result = run_check(source)
    assert result.returncode != 0
    assert "node_github_repo_absent_omn21422: 0 onex.nodes entry points" in (
        result.stderr
    )
