# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plan owner check and its reader node find their overlays under an overlay root (OMN-24646).

A process whose ``ONEX_SKILL_OVERLAY_ROOTS`` names a root holding no
``node_work_ledger_reader_effect/overlay.yaml`` cannot read the open claims: the reader node's
loader raises ``NodeOverlayNotFoundError`` and the check answers UNKNOWN (exit 2). The same
process with the overlay under the root reads a real verdict. The join is the root layout
``<root>/<node>/overlay.yaml``, written here into a temporary root, so the test needs no other
checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.nodes.node_plan_owner_check_effect.runtime_plan_owner_check import main
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import (
    NODE_NAME as READER_NODE,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import (
    load_overlay as load_reader_overlay,
)
from tests.unit.nodes.node_plan_owner_check_effect.test_runtime_plan_owner_check import (
    OWNER_OVERLAY,
    READER_OVERLAY,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    FakeStateReader,
    claim_entity,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_open_claims_window import (
    StampedQueryNodePointRead,
)

pytestmark = pytest.mark.unit

OWNER_NODE = "node_plan_owner_check_effect"


def roots_env(root: Path) -> dict[str, str]:
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def write_overlay(root: Path, node: str, text: str) -> None:
    (root / node).mkdir(parents=True)
    (root / node / "overlay.yaml").write_text(text)


def test_plan_owner_reader_overlay_absent_from_the_root_raises_not_found(
    tmp_path: Path,
) -> None:
    with pytest.raises(NodeOverlayNotFoundError) as raised:
        load_reader_overlay(roots_env(tmp_path))
    assert READER_NODE in str(raised.value)
    assert str(tmp_path) in str(raised.value)


def test_plan_owner_reader_overlay_present_in_the_root_loads(tmp_path: Path) -> None:
    write_overlay(tmp_path, READER_NODE, READER_OVERLAY)
    overlay = load_reader_overlay(roots_env(tmp_path))
    assert overlay.moved_readers == ("cutover-owner",)


def test_plan_owner_check_reads_unknown_when_the_root_lacks_the_reader_overlay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_overlay(tmp_path, OWNER_NODE, OWNER_OVERLAY)
    code = main(
        [],
        environ=roots_env(tmp_path),
        point_read=StampedQueryNodePointRead(FakeStateReader((claim_entity(),))),
        clock=lambda: NOW,
    )
    out = capsys.readouterr()
    assert code == 2
    assert out.out.startswith("UNKNOWN plan-owner | node_work_ledger_reader_effect:")
    assert "verdict=UNKNOWN reason=overlay-unreadable" in out.err


def test_plan_owner_check_reads_a_verdict_when_the_root_holds_both_overlays(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_overlay(tmp_path, READER_NODE, READER_OVERLAY)
    write_overlay(tmp_path, OWNER_NODE, OWNER_OVERLAY)
    code = main(
        [],
        environ=roots_env(tmp_path),
        point_read=StampedQueryNodePointRead(FakeStateReader((claim_entity(),))),
        clock=lambda: NOW,
    )
    out = capsys.readouterr()
    assert (code, out.out.strip()) == (0, "OWNED cutover-plan-owner | lane-x")
    assert "verdict=UNKNOWN" not in out.err
