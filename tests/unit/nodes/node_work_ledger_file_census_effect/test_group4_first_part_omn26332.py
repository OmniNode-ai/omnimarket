# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Census group 4, first part: the probe handler and two overlay examples stop naming the ledger file.

Each case reads the live tree, so it fails on the tree before this change (the probe handler and the tailnet
example still hold a counted reader or writer and a row of the shipped ratchet, and the copy example still holds
three) and passes once they are moved.
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

PROBE_HANDLER = (
    "src/omnimarket/nodes/node_review_key_credit_probe_effect/handlers/"
    "handler_review_key_credit_probe.py"
)
TAILNET_EXAMPLE = (
    "src/omnimarket/nodes/node_tailnet_https_serve_effect/overlay.example.yaml"
)
COPY_EXAMPLE = "src/omnimarket/nodes/node_work_ledger_copy_effect/overlay.example.yaml"

#: Files cleared of every counted hit by this change (their rows are dropped).
CLEARED = (PROBE_HANDLER, TAILNET_EXAMPLE)
#: The copy example held three counted hits before this change; the ledger file path is gone from its append
#: command and the rest stay for a later change, so its row is lowered, not dropped.
COPY_EXAMPLE_ROW_BEFORE = 3


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


def test_the_copy_example_row_is_lowered_never_raised() -> None:
    text = (ROOT / COPY_EXAMPLE).read_text(encoding="utf-8")
    live = len(executable_hits(COPY_EXAMPLE, text, _overlay()))
    row = _rows()[COPY_EXAMPLE]
    assert live <= row < COPY_EXAMPLE_ROW_BEFORE


@pytest.mark.parametrize("path", [TAILNET_EXAMPLE, COPY_EXAMPLE])
def test_the_example_report_command_appends_with_no_ledger_file_path(
    path: str,
) -> None:
    report = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))["report"]
    assert report["append_argv"] == ["onex-ledger", "--append"]
    assert not [k for k in report.get("environment", {}) if k.endswith("_PATH")]


def test_the_live_census_passes_without_the_cleared_files() -> None:
    overlay = _overlay()
    result = HandlerFileCensus(
        overlay,
        GitRepositorySource(ROOT, overlay.max_file_bytes, {"omnimarket": "."}),
        YamlRatchetStore(ROOT / overlay.ratchet_file),
        InMemoryEmissionSink(),
    ).handle(
        ModelCensusRequest(
            correlation_id="group4-first-part-omn26332",
            repositories=("omnimarket",),
        )
    )
    assert result.passed, (result.failure_code, result.failure_detail)
    (count,) = result.counts
    assert not set(CLEARED).intersection(hit.path for hit in count.hits)
