# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Census group 3, second slice: three files stop naming the ledger file or its path variable.

Each case reads the live tree, so it fails on the tree before this change (each file still holds a counted
reader or writer and a row of the shipped ratchet) and passes once they are changed and their rows are dropped.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_work_ledger_file_census_effect.adapters import (
    GitRepositorySource,
    YamlRatchetStore,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers import (
    HandlerFileCensus,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.classify import (
    executable_hits,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusOverlay,
    ModelCensusRequest,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / "src/omnimarket/nodes/node_work_ledger_file_census_effect"
RATCHET = PACKAGE / "ratchet.yaml"

NODES = "src/omnimarket/nodes"
CLEARED = (
    f"{NODES}/node_pr_duplicate_guard_compute/contract.yaml",
    f"{NODES}/node_projection_work_events/ledger_view.py",
    f"{NODES}/node_projection_work_ledger/parity.py",
)


def _overlay() -> ModelCensusOverlay:
    data = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    return ModelCensusOverlay.model_validate(data)


def _rows() -> dict[str, int]:
    rows: dict[str, int] = yaml.safe_load(RATCHET.read_text())["files"]["omnimarket"]
    return rows


@pytest.mark.parametrize("path", CLEARED)
def test_a_cleared_file_holds_no_executable_ledger_file_read_or_write(
    path: str,
) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    hits = executable_hits(path, text, _overlay())
    assert hits == (), [f"{h.kind.value} {h.path}:{h.line}" for h in hits]


@pytest.mark.parametrize("path", CLEARED)
def test_a_cleared_file_has_no_ratchet_row(path: str) -> None:
    assert path not in _rows()


def test_the_live_census_passes_without_the_cleared_files() -> None:
    overlay = _overlay()
    result = HandlerFileCensus(
        overlay,
        GitRepositorySource(ROOT, overlay.max_file_bytes, {"omnimarket": "."}),
        YamlRatchetStore(ROOT / overlay.ratchet_file),
        InMemoryEmissionSink(),
    ).handle(
        ModelCensusRequest(
            correlation_id="group3-part2-omn26331",
            repositories=("omnimarket",),
        )
    )
    assert result.passed, (result.failure_code, result.failure_detail)
    (count,) = result.counts
    assert not set(CLEARED).intersection(hit.path for hit in count.hits)
