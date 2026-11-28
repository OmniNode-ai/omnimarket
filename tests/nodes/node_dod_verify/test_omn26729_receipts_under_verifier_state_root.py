# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hosted verifier reads receipts from the runtime's state root (OMN-26729).

The Done-write guard runs the verifier as ``skill dod_verify --state-root <fresh
temp dir>``, and the skill CLI points ``ONEX_STATE_DIR`` at that temp dir while
``ONEX_STATE_ROOT`` still names the runtime's own root, where the receipt consumer
wrote ``local_done_gate_receipts/OMN-26115.jsonl``. ``ONEX_STATE_DIR`` wins in the
state-root resolver, so the verifier read an empty store and counted 10 of 11 on
dev even though its resolved contract revision (872e78) matched the receipts. The
fixture is that store file as the dev runtime held it on 2026-11-27.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnimarket.enums.enum_dod_contract_source import EnumDodContractSource
from omnimarket.handlers.runtime_paths import runtime_path
from omnimarket.nodes.node_dod_verify.models.model_dod_contract_subject import (
    ModelDodContractSubject,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "omn26115_local_done_gate_receipts_recorded_2026_11_27.jsonl"
)
_TICKET = "OMN-26115"
_SHA = "872e7808f71194200b1dba4769c980661b2e8a78"
_ITEM = "dod-omn26115-integration-proof"


def _collector() -> EvidenceCollector:
    collector = EvidenceCollector()
    collector.contract_subject = ModelDodContractSubject(
        source=EnumDodContractSource.PRODUCT_REPOSITORY,
        repository="OmniNode-ai/omnimarket",
        commit_sha=_SHA,
        repo_path=f"contracts/{_TICKET}.yaml",
    )
    return collector


def _runtime_root_with_recorded_receipts(tmp_path: Path) -> Path:
    root = tmp_path / "runtime_state"
    store = root / runtime_path("dod_evidence.home_subdir") / "local_done_gate_receipts"
    store.mkdir(parents=True)
    shutil.copy(_FIXTURE, store / f"{_TICKET}.jsonl")
    return root


def test_receipts_under_the_runtime_root_clear_the_item_when_the_state_dir_is_a_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = _runtime_root_with_recorded_receipts(tmp_path)
    verifier_temp = tmp_path / "onex-dod-verify-temp"
    verifier_temp.mkdir()
    monkeypatch.setenv("ONEX_STATE_ROOT", str(runtime_root))
    monkeypatch.setenv("ONEX_STATE_DIR", str(verifier_temp))

    receipt = _collector()._local_done_gate_receipt_for(_TICKET, _ITEM)

    assert receipt is not None
    assert receipt.contract_sha == _SHA


def test_a_state_root_with_no_receipts_still_leaves_the_item_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("ONEX_STATE_ROOT", str(empty))
    monkeypatch.setenv("ONEX_STATE_DIR", str(empty))

    assert _collector()._local_done_gate_receipt_for(_TICKET, _ITEM) is None


def test_the_same_receipts_do_not_clear_another_contract_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ONEX_STATE_ROOT", str(_runtime_root_with_recorded_receipts(tmp_path))
    )
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    collector = _collector()
    assert collector.contract_subject is not None
    collector.contract_subject = collector.contract_subject.model_copy(
        update={"commit_sha": "0" * 40}
    )

    assert collector._local_done_gate_receipt_for(_TICKET, _ITEM) is None
