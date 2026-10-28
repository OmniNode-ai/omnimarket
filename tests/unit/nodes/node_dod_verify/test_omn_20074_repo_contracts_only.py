# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20074: node_dod_verify reads a ticket's contract from product repositories only.

With no contract path the collector used to fall back to onex_change_control
when no product repository governed the ticket: it ran ``git fetch`` against the
OCC clone, added a worktree of ``origin/dev`` and loaded ``contracts/<ticket>.yaml``
from it. The repository's own ``contracts/`` is now the only source. A ticket
no product repository carries fails closed with a stated reason, and the OCC
clone is neither fetched nor read, even when it holds a contract for the ticket.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from tests.unit.nodes.node_dod_verify.omn_20886_repo_world import World, contract

pytestmark = pytest.mark.unit

_AUDIENCE = EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_contract_only_on_occ_is_missing_and_occ_is_never_touched(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = "OMN-90301"
    world.merge("omnimarket", 91, f"feat({ticket}): no contract", {"src/x.py": "x\n"})
    occ = world.occ({ticket: contract(ticket, "dod-occ", ["AC1"])})
    world.install(monkeypatch)
    monkeypatch.setenv("CONTRACT_REPO_DIR", str(occ))

    real_run: Callable[..., Any] = subprocess.run
    touched_occ: list[list[str]] = []

    def spy(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(cmd, list) and str(occ) in [str(c) for c in cmd]:
            touched_occ.append([str(c) for c in cmd])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)

    def refuse_occ_root(_self: EvidenceCollector) -> Path | None:
        raise AssertionError("missing product contract must not resolve the OCC root")

    monkeypatch.setattr(EvidenceCollector, "_resolve_occ_root", refuse_occ_root)
    collector = EvidenceCollector()
    results = collector.collect(ticket, execution_audience=_AUDIENCE)

    assert len(results) == 1
    assert results[0].evidence_id == "contract"
    assert results[0].status is EnumEvidenceCheckStatus.FAILED
    message = results[0].message or ""
    assert message.startswith(
        "CONTRACT_MISSING: no product repository carries "
        f"contracts/{ticket}.yaml in a merged pull request for {ticket}; "
        "onex_change_control is not consulted."
    )
    assert touched_occ == []
    assert collector.occ_refresh_outcome is None
    assert collector.occ_resolved_sha is None
