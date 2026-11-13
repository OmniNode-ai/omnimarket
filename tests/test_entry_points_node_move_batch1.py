"""Batch-1 node entry points are registered and importable."""

import importlib
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BATCH1_NODES = (
    "node_c32_auth_boundary_probe_effect",
    "node_host_reconcile_effect",
    "node_pr_landing_liveness_read_effect",
)


def unregistered(entries: dict[str, str], names: tuple[str, ...]) -> list[str]:
    return [name for name in names if entries.get(name) != f"omnimarket.nodes.{name}"]


def _load_entries() -> dict[str, str]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]


@pytest.mark.parametrize("name", BATCH1_NODES)
def test_batch1_node_is_registered_and_importable(name: str) -> None:
    entries = _load_entries()
    assert entries[name] == f"omnimarket.nodes.{name}"
    assert (ROOT / "src/omnimarket/nodes" / name / "contract.yaml").is_file()
    importlib.import_module(f"omnimarket.nodes.{name}")


def test_unregistered_detects_missing_entry() -> None:
    entries = _load_entries()
    missing = BATCH1_NODES[0]
    broken = dict(entries)
    del broken[missing]
    assert unregistered(broken, BATCH1_NODES) == [missing]
    assert unregistered(entries, BATCH1_NODES) == []
