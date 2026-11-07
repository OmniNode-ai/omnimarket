# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Redpanda parity coverage of every node of the stopped-line fill, and no skipped parity test (plan task T8, criterion 6).

``parity_coverage_complete`` enumerates the nine node directories of the plan's section 4 and fails on a node that no
collected ``floor_fill_parity`` test covers. ``parity_zero_skipped`` runs the marker with ``-rs`` and fails on any
skipped test: with ``FLOOR_FILL_PARITY_BOOTSTRAP`` set it runs both legs (the Redpanda leg included) and every test
must run; without it the Redpanda leg cannot run, so only the in-memory leg is selected and every test of it must run.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
NODES_ROOT = REPO / "src" / "omnimarket" / "nodes"
MARKER = "floor_fill_parity"
BOOTSTRAP_ENV = "FLOOR_FILL_PARITY_BOOTSTRAP"

# The nine node directories section 4 of the plan adds (T1, T4, T12, T11 twice, T2, T13, T15 twice).
NODES = (
    "node_floor_fill_candidates_compute",
    "node_floor_fill_lines_compute",
    "node_floor_fill_reducer",
    "node_floor_task_ticket_payload_compute",
    "node_floor_task_ticket_effect",
    "node_lane_process_effect",
    "node_floor_fill_orchestrator",
    "node_remote_lane_start_orchestrator",
    "node_remote_lane_start_effect",
)


def parity_files() -> list[Path]:
    """Every test file that carries the marker, found by reading the files, so a node cannot hide behind a directory name."""
    roots = [REPO / "tests", *NODES_ROOT.glob("*/tests")]
    found: list[Path] = []
    for root in roots:
        for path in sorted(root.rglob("test_*.py")):
            if f"pytest.mark.{MARKER}" in path.read_text(encoding="utf-8"):
                found.append(path)
    return found


def pytest_run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--no-cov", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=False,
    )


def covered_nodes(collected_ids: list[str]) -> dict[str, list[str]]:
    """The nodes each collected parity test covers: its file lies in the node's tests, or imports the node."""
    out: dict[str, list[str]] = {node: [] for node in NODES}
    for test_id in collected_ids:
        path = REPO / test_id.split("::", 1)[0]
        text = path.read_text(encoding="utf-8")
        for node in NODES:
            if (
                f"/{node}/" in f"/{path.relative_to(REPO)}"
                or f"omnimarket.nodes.{node}" in text
            ):
                out[node].append(test_id)
    return out


def test_parity_coverage_complete_every_node_directory_exists() -> None:
    missing = [node for node in NODES if not (NODES_ROOT / node).is_dir()]
    assert missing == [], f"node directories of section 4 not on this tree: {missing}"


def test_parity_coverage_complete() -> None:
    files = parity_files()
    assert files, "no test file carries the floor_fill_parity marker"
    done = pytest_run("--collect-only", "-q", "-m", MARKER, *map(str, files))
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    ids = [line for line in done.stdout.splitlines() if "::" in line]
    covered = covered_nodes(ids)
    uncovered = [node for node, tests in covered.items() if not tests]
    assert uncovered == [], f"nodes with no collected {MARKER} test: {uncovered}"
    # every node is covered by its own in-memory leg and its Redpanda leg
    no_redpanda = [
        node
        for node, tests in covered.items()
        if not any("redpanda" in test for test in tests)
    ]
    assert no_redpanda == [], (
        f"nodes with no Redpanda parity leg collected: {no_redpanda}"
    )


def test_parity_coverage_complete_a_node_with_no_parity_test_is_named() -> None:
    """The control: with one node's tests withheld from the collected ids, the check names that node."""
    files = parity_files()
    done = pytest_run("--collect-only", "-q", "-m", MARKER, *map(str, files))
    ids = [line for line in done.stdout.splitlines() if "::" in line]
    withheld = "node_lane_process_effect"
    kept = [i for i in ids if withheld not in i]
    uncovered = [node for node, tests in covered_nodes(kept).items() if not tests]
    assert uncovered == [withheld]


def test_parity_zero_skipped() -> None:
    files = parity_files()
    bootstrap = os.environ.get(BOOTSTRAP_ENV)
    selection = MARKER if bootstrap else f"{MARKER} and not kafka"
    done = pytest_run("-rs", "-q", "-m", selection, *map(str, files))
    summary = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    skipped = re.findall(r"^SKIPPED .*$", done.stdout, flags=re.MULTILINE)
    assert skipped == [], "\n".join(skipped)
    assert not re.search(r"\d+ skipped", summary), summary
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1000:]
    assert re.search(r"\d+ passed", summary), summary
