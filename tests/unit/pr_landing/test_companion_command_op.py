# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-19827): a command without ``op`` is read as ``derive``.

The companion seam of the PR landing workflow adds an ``op`` field (derive,
regenerate, verify) to the live occ-autobind command. Every publisher in the
product repos predates it, so a payload that carries no ``op`` must keep
validating exactly as before and read as ``derive``. The recorded payloads here
were read off the lab dev-lane bus on 2026-09-26, so the proof is against what
the publishers really send, not against a payload this test built.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.events.pr_landing_companion import EnumPrLandingCompanionOp
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_RECORDED = (
    _REPO_ROOT
    / "tests"
    / "fixtures"
    / "pr_landing"
    / "autobind_command"
    / "recorded_2026-09-26.json"
)
_PUBLISHER = _REPO_ROOT / "scripts" / "publish_occ_autobind_command.py"


def _recorded_payloads() -> list[dict[str, object]]:
    doc = json.loads(_RECORDED.read_text(encoding="utf-8"))
    return [record["value"] for record in doc["records"]]


def test_recorded_payloads_are_real_and_carry_no_op() -> None:
    payloads = _recorded_payloads()
    assert len(payloads) >= 2
    for payload in payloads:
        assert "op" not in payload
        assert payload["block_reason"] == "receipt_evidence_source_autobind"


@pytest.mark.parametrize("payload", _recorded_payloads())
def test_recorded_publisher_payload_reads_as_derive(payload: dict[str, object]) -> None:
    command = ModelPrLifecycleFixCommand.model_validate(payload)
    assert command.op is EnumPrLandingCompanionOp.DERIVE
    assert command.block_reason is EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("derive", EnumPrLandingCompanionOp.DERIVE),
        ("regenerate", EnumPrLandingCompanionOp.REGENERATE),
        ("verify", EnumPrLandingCompanionOp.VERIFY),
    ],
)
def test_explicit_op_is_read(raw: str, expected: EnumPrLandingCompanionOp) -> None:
    payload = dict(_recorded_payloads()[0], op=raw)
    assert ModelPrLifecycleFixCommand.model_validate(payload).op is expected


def test_unknown_op_is_refused() -> None:
    payload = dict(_recorded_payloads()[0], op="remint")
    with pytest.raises(ValidationError):
        ModelPrLifecycleFixCommand.model_validate(payload)


def test_a_companion_op_on_a_non_companion_block_reason_is_refused() -> None:
    payload = dict(
        _recorded_payloads()[0],
        block_reason=EnumPrBlockReason.CI_FAILURE.value,
        op="regenerate",
    )
    with pytest.raises(ValidationError, match="op"):
        ModelPrLifecycleFixCommand.model_validate(payload)


def test_a_non_companion_command_without_op_is_unchanged() -> None:
    payload = dict(
        _recorded_payloads()[0], block_reason=EnumPrBlockReason.CI_FAILURE.value
    )
    command = ModelPrLifecycleFixCommand.model_validate(payload)
    assert command.op is EnumPrLandingCompanionOp.DERIVE
