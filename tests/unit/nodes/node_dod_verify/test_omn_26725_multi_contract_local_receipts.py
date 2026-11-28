# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-26725: a local Done gate run over a multi-contract ticket writes one
receipt per governing contract, keyed as the hosted lookup reads it.

OMN-26118 is governed by two contracts, one in omnibase_infra and one in
omnimarket, each carrying a ``local_done_gate`` integration-proof item. The
local run's aggregate contract subject is UNBOUND (no single revision), so the
single receipt it used to build was None and the hosted gate, which looks a
skipped item up per contract by its unprefixed id at that contract's revision,
found nothing and refused on the skipped item.

The world is the registry-root fixture the repo-first lookup suite uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.models.model_dod_verify_state import ModelDodVerifyState
from omnimarket.models.model_local_done_gate_receipt_store import (
    FilesystemLocalDoneGateReceiptStore,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from omnimarket.nodes.node_local_done_gate_receipt_store_effect.handlers.handler_local_done_gate_receipt_store import (
    HandlerLocalDoneGateReceiptStore,
)
from tests.unit.nodes.node_dod_verify.omn_20886_repo_world import World

pytestmark = pytest.mark.unit

TICKET = "OMN-90301"
PROOF = "dod-integration-proof"


def _contract(unit_item: str) -> str:
    return yaml.safe_dump(
        {
            "schema_version": "1.0.0",
            "ticket_id": TICKET,
            "dod_evidence": [
                {
                    "id": unit_item,
                    "description": "a unit item",
                    "source": "manual",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                    "binds_ac": ["AC1"],
                },
                {
                    "id": PROOF,
                    "description": "lab-only integration proof",
                    "source": "manual",
                    "execution_scope": "local_done_gate",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                },
            ],
        },
        sort_keys=False,
    )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, str]]:
    w = World(tmp_path)
    merged = {
        "omnibase_infra": w.merge(
            "omnibase_infra",
            71,
            f"feat({TICKET}): infra",
            {f"contracts/{TICKET}.yaml": _contract("dod-infra-unit")},
        ),
        "omnimarket": w.merge(
            "omnimarket",
            72,
            f"feat({TICKET}): market",
            {f"contracts/{TICKET}.yaml": _contract("dod-market-unit")},
        ),
    }
    w.install(monkeypatch)
    return merged


def _local_run(monkeypatch: pytest.MonkeyPatch) -> ModelDodVerifyState:
    collector = EvidenceCollector()
    monkeypatch.setattr(
        HandlerDodVerify, "_make_collector", staticmethod(lambda: collector)
    )
    state = HandlerDodVerify().handle(
        ModelDodVerifyStartCommand(
            ticket_id=TICKET,
            execution_audience=EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE,
        )
    )
    assert isinstance(state, ModelDodVerifyState)
    return state


def test_local_run_writes_one_receipt_per_governing_contract(
    world: dict[str, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _local_run(monkeypatch)

    by_sha = {r.contract_sha: r for r in state.local_done_gate_receipts}
    assert set(by_sha) == {
        world["omnibase_infra"]["merge_sha"],
        world["omnimarket"]["merge_sha"],
    }
    # Items are keyed the way the hosted lookup reads them: unprefixed.
    infra = by_sha[world["omnibase_infra"]["merge_sha"]]
    assert sorted((i.item_id, i.verdict) for i in infra.items) == [
        ("dod-infra-unit", "VERIFIED"),
        (PROOF, "VERIFIED"),
    ]
    market = by_sha[world["omnimarket"]["merge_sha"]]
    assert sorted(i.item_id for i in market.items) == [PROOF, "dod-market-unit"]


def test_hosted_gate_counts_each_skipped_item_from_its_own_contract_receipt(
    world: dict[str, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _local_run(monkeypatch)
    store = FilesystemLocalDoneGateReceiptStore(tmp_path / "receipts")
    assert HandlerLocalDoneGateReceiptStore(store=store).handle(state).stored is True

    class _Source:
        def receipts_for(self, ticket_id: str) -> list:  # type: ignore[type-arg]
            return store.receipts_for(ticket_id)

    hosted = EvidenceCollector(local_done_gate_receipts=_Source())
    results = hosted.collect(
        TICKET, execution_audience=EnumDodVerifyExecutionAudience.HOSTED
    )
    proofs = [r for r in results if r.evidence_id.endswith(PROOF)]
    assert [r.evidence_id for r in proofs] == [
        f"omnibase_infra:{PROOF}",
        f"omnimarket:{PROOF}",
    ]
    assert all(r.status is EnumEvidenceCheckStatus.VERIFIED for r in proofs)


def test_hosted_run_writes_no_receipts(
    world: dict[str, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = EvidenceCollector()
    monkeypatch.setattr(
        HandlerDodVerify, "_make_collector", staticmethod(lambda: collector)
    )
    state = HandlerDodVerify().handle(
        ModelDodVerifyStartCommand(
            ticket_id=TICKET,
            execution_audience=EnumDodVerifyExecutionAudience.HOSTED,
        )
    )
    assert isinstance(state, ModelDodVerifyState)
    assert state.local_done_gate_receipts == ()
