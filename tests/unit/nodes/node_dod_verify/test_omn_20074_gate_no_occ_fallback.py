# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20074: the durable evidence gate reads no onex_change_control clone or ref.

When no product repository governs a ticket (the product verdict is NOT_ENGAGED)
the gate used to fall back to three checks against an OCC clone located by
``CONTRACT_REPO_DIR``: a tracked receipt under ``drift/dod_receipts/<ticket>``,
the cited merge commit, and the contract on the OCC governance ref. It now fails
closed with a product-repository refusal and reads no OCC path.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_evidence_github_effect import (
    HandlerDurableEvidenceGateEffect,
)
from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
    ModelDurableEvidenceGateRunCommand,
)
from tests.unit.nodes.node_dod_verify.omn_20886_repo_world import (
    World,
    _git,
    contract,
)

pytestmark = pytest.mark.unit

_NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_dod_verify"
)

_RETIRED_NAMES = (
    "_resolve_occ_root",
    "OCC_GOVERNANCE_REF",
    "DOD_VERIFY_ALLOW_STALE_OCC_REF",
    "occ_repo_path",
    "occ_governance_ref",
)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _command_for(ticket: str, occ: Path) -> ModelDurableEvidenceGateRunCommand:
    """The run command, naming the OCC clone only where the model still has a field."""
    fields: dict[str, Any] = {
        "ticket_id": ticket,
        "contract": {"ticket_id": ticket, "dod_evidence": []},
        "merged_prs": (),
    }
    if "occ_repo_path" in ModelDurableEvidenceGateRunCommand.model_fields:
        fields["occ_repo_path"] = str(occ)
    return ModelDurableEvidenceGateRunCommand.model_validate(fields)


def test_not_engaged_verdict_refuses_and_reads_no_occ_path(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = "OMN-90501"
    contract_text = contract(ticket, "dod-occ", ["AC1"])
    occ = world.occ({ticket: contract_text})
    receipt = occ / "drift" / "dod_receipts" / ticket / "dod-occ" / "receipt.yaml"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("status: PASS\n")
    _git(occ, "add", ".")
    _git(occ, "commit", "-q", "-m", "receipt")
    _git(occ, "push", "-q", "origin", "dev")
    assert (occ / "contracts" / f"{ticket}.yaml").is_file()

    world.install(monkeypatch)
    monkeypatch.setenv("CONTRACT_REPO_DIR", str(occ))
    monkeypatch.setattr(
        HandlerDurableEvidenceGateEffect,
        "read_ticket",
        staticmethod(lambda _ticket: ("", frozenset(), "")),
    )

    real_run: Callable[..., Any] = subprocess.run
    touched_occ: list[list[str]] = []

    def spy(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(cmd, list) and str(occ) in [str(c) for c in cmd]:
            touched_occ.append([str(c) for c in cmd])
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)

    run = HandlerDurableEvidenceGateEffect().handle(_command_for(ticket, occ))

    assert touched_occ == []
    assert run.result.status is EnumDurableEvidenceStatus.FAIL
    refusals = [
        c
        for c in run.result.checks
        if c.check is EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN
    ]
    assert len(refusals) == 1
    assert not refusals[0].passed
    messages = " ".join(c.message for c in run.result.checks)
    assert "product-repository" in messages
    assert "OCC PR" not in messages
    assert "onex_change_control before" not in messages


def test_gate_names_no_occ_clone_or_ref_in_the_node() -> None:
    offenders = sorted(
        f"{path.relative_to(_NODE_DIR)}: {name}"
        for path in _NODE_DIR.rglob("*")
        if path.suffix in {".py", ".yaml"}
        for name in _RETIRED_NAMES
        if name in path.read_text(encoding="utf-8")
    )
    assert offenders == []


def test_grep_positive_control_finds_a_name_it_is_given(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text("x = 'occ_repo_path'\n")
    assert [name for name in _RETIRED_NAMES if name in probe.read_text()] == [
        "occ_repo_path"
    ]
