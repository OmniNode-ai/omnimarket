# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Census group 4, third part: the session front door entry and receipt no longer name the ledger copy.

The entry used to refuse a start with the copy's path variable unset, and the host mode used to open the
member's ledger copy (its name, that it is a file, its archive directory). Both checks belong to the
preflight's own ledger checks, which the team receipt assertion already requires, so the two handlers
name no ledger file or path variable. Each case reads the live tree, so it fails on the tree before this
change (both files hold counted hits and a row of the shipped ratchet) and passes after it. The positive
control proves the same classifier still sees the hit of a file that keeps its row.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.classify import (
    executable_hits,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusOverlay,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
PACKAGE = ROOT / "src/omnimarket/nodes/node_work_ledger_file_census_effect"
RATCHET = PACKAGE / "ratchet.yaml"

ENTRY = "src/omnimarket/nodes/node_session_front_door_compute/handlers/entry.py"
RECEIPT = "src/omnimarket/nodes/node_session_front_door_compute/handlers/receipt.py"
CLEARED = (ENTRY, RECEIPT)
# Still listed: the session resume handler keeps a citation grammar that names the live ledger file.
STILL_LISTED = "src/omnimarket/nodes/node_session_resume_compute/handlers/dangling.py"


def _overlay() -> ModelCensusOverlay:
    data = yaml.safe_load((PACKAGE / "overlay.example.yaml").read_text())
    return ModelCensusOverlay.model_validate(data)


@pytest.mark.parametrize("path", CLEARED)
def test_a_cleared_file_holds_no_executable_ledger_file_read_or_write(
    path: str,
) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    hits = executable_hits(path, text, _overlay())
    assert hits == (), [f"{h.kind.value} {h.path}:{h.line}" for h in hits]


@pytest.mark.parametrize("path", CLEARED)
def test_a_cleared_file_has_no_ratchet_row(path: str) -> None:
    rows: dict[str, int] = yaml.safe_load(RATCHET.read_text())["files"]["omnimarket"]
    assert path not in rows


def test_the_repository_row_is_the_count_of_listed_files() -> None:
    table = yaml.safe_load(RATCHET.read_text())
    assert table["ratchet"]["omnimarket"] == len(table["files"]["omnimarket"])


def test_positive_control_the_classifier_still_sees_a_kept_file() -> None:
    text = (ROOT / STILL_LISTED).read_text(encoding="utf-8")
    assert executable_hits(STILL_LISTED, text, _overlay())
    rows: dict[str, int] = yaml.safe_load(RATCHET.read_text())["files"]["omnimarket"]
    assert STILL_LISTED in rows
