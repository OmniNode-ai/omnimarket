# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed suite document ships inside the package and the node's census matches an independent recount of its raw rows."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest

from omnimarket.models.eval_suite_census.committed_suite import load_committed_suite
from omnimarket.models.eval_suite_census.model_suite_census_request import (
    ModelCodeEvalSuiteCensusRequest,
)
from omnimarket.nodes.node_code_eval_suite_census_compute.handlers.handler_code_eval_suite_census import (
    HandlerCodeEvalSuiteCensus,
)
from tests.nodes.node_code_eval_suite_census_compute.support import overlay

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


def _packaged_text() -> str:
    return (
        files("omnimarket")
        .joinpath("configs", "code_eval_suite.v1.json")
        .read_text(encoding="utf-8")
    )


def _raw() -> dict[str, object]:
    return json.loads(_packaged_text())


def _copies_src(dockerfile_text: str) -> bool:
    """True when a COPY line (flags allowed) copies src/ to /opt/omnimarket/src/."""
    for line in dockerfile_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY"):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        args = parts[1:]
        operands = [a for a in args if not a.startswith("-")]
        if (
            len(operands) == 2
            and operands[0] == "src/"
            and operands[1] == "/opt/omnimarket/src/"
        ):
            return True
    return False


def test_the_suite_ships_inside_the_package() -> None:
    assert files("omnimarket").joinpath("configs", "code_eval_suite.v1.json").is_file()


def test_there_is_no_second_copy_outside_the_package() -> None:
    assert not (REPO_ROOT / "benchmarks" / "code_eval_suite" / "suite.v1.json").exists()


def test_the_loader_returns_the_rows_of_the_packaged_file() -> None:
    loaded = load_committed_suite()
    expected = ModelCodeEvalSuiteCensusRequest.model_validate(_raw())
    assert loaded == expected
    assert len(loaded.tasks) > 0


def test_the_census_over_the_packaged_suite_counts_every_row() -> None:
    raw = _raw()
    answer = HandlerCodeEvalSuiteCensus(overlay=overlay()).handle(
        load_committed_suite()
    )
    assert answer.failure is None
    assert answer.candidates_total == len(raw["tasks"]) + sum(
        e["count"] for e in raw.get("pre_control_exclusions", [])
    )


def test_the_runtime_image_copies_the_whole_src_tree() -> None:
    dockerfile_text = (REPO_ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")
    assert _copies_src(dockerfile_text)


def test_the_image_copy_check_fails_on_a_dockerfile_without_the_src_copy() -> None:
    dockerfile_text = (REPO_ROOT / "Dockerfile.runtime").read_text(encoding="utf-8")
    lines = dockerfile_text.splitlines()
    kept = [
        line
        for line in lines
        if not (line.strip().startswith("COPY") and "src/" in line)
    ]
    assert not _copies_src("\n".join(kept))
    assert not _copies_src("")
