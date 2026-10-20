# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20844: the contract carries the ticket's criteria and nothing else.

Earlier lanes recorded AC7 to AC32 only in contracts/OMN-20844.yaml; the ticket
carries AC1 to AC6. dod_verify compares the contract with the live ticket and
refused it with CRITERIA_DRIFT ("AC7 deleted from the ticket ..."). The amended
contract declares AC1 to AC6 and retires every claim on the others with a typed
supersedes_ac_binding entry, leaving the merged items themselves unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.ac_binding_retirements import (
    resolve_retirements,
)
from omnimarket.nodes.node_dod_verify.services.criteria_drift import (
    compare_criteria,
    extract_criteria_pins,
)

_CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "OMN-20844.yaml"
_TICKET_LABELS = ("AC1", "AC2", "AC3", "AC4", "AC5", "AC6")

# The ticket's acceptance block, verbatim (Linear OMN-20844, read 2026-10-20).
_TICKET_CRITERIA = """## Acceptance criteria

- [ ] AC1: onex models add and key registration take the customer's model, validated against the provider's model list and stored in the customer's own config -- falsifier: uv run pytest on a registration test that passes a model absent from the provider list and expects a refusal, and one that passes a listed model and reads it back from the customer config
- [ ] AC2: onex delegate --model overrides the model per call, through the infra CLI and an optional model field on ModelDelegateSkillRequest, landed consumers-first -- falsifier: uv run pytest on a request-model test that accepts the model field and a CLI test that delegates with --model and reads the model back from the request; the same suite fails today because the flag is unknown
- [ ] AC3: onboarding selects the default OpenRouter model declared in the onboarding overlay, with no model id in code, and the customer can change it with one documented command (`onex models add openrouter --model <id>`) -- falsifier: uv run pytest on an onboarding test that supplies an overlay declaring a fixture model and expects that model to reach the OpenRouter registration, one with no overlay declaring a default and expects a refusal naming the overlay (never a stored model), a scan of the onboarding script finding no model id, and a test that the documented command replaces the stored model
- [ ] AC4: the delegation receipt names the chosen model -- falsifier: uv run pytest on a receipt test that delegates with --model X and asserts the receipt model field equals X
- [ ] AC5: the OpenRouter catalogue row's billing describes paid models, not free tier -- falsifier: uv run pytest on a catalogue test asserting the OpenRouter row billing is not the free-tier value
- [ ] AC6: the C29 probe passes --model and asserts the receipt names it, and the C29 workflow passes one inexpensive paid model -- falsifier: uv run pytest on the probe test that asserts the --model argument and receipt assertion, plus a read of c29-customer-byo-key-delegation.yml for a paid model id and no :free suffix
"""


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT.read_text())
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
def test_the_contract_declares_exactly_the_ticket_criteria(
    contract: dict[str, Any],
) -> None:
    declared = [
        criterion["id"]
        for requirement in contract["requirements"]
        for criterion in requirement["acceptance"]
    ]
    assert declared == list(_TICKET_LABELS)


@pytest.mark.unit
def test_every_retirement_applies_and_none_is_refused(contract: dict[str, Any]) -> None:
    resolution = resolve_retirements(contract["dod_evidence"])
    assert resolution.refused == ()
    retired = {(entry.item, entry.label) for entry in resolution.applied}
    claimed_elsewhere = {
        (item["id"], label)
        for item in contract["dod_evidence"]
        for label in item.get("binds_ac") or []
        if label not in _TICKET_LABELS
    }
    assert claimed_elsewhere
    assert retired == claimed_elsewhere


@pytest.mark.unit
def test_the_live_ticket_reads_no_criteria_drift(contract: dict[str, Any]) -> None:
    pins = extract_criteria_pins(contract, contract["dod_evidence"])
    assert pins is not None
    drift, amendment = compare_criteria(pins, "OMN-20844", _TICKET_CRITERIA)
    assert [(d.kind.value, d.label) for d in drift] == []
    assert amendment is None


@pytest.mark.unit
def test_an_unretired_contract_only_criterion_still_drifts(
    contract: dict[str, Any],
) -> None:
    """Negative control: drop one retirement and the drift comes back."""
    items = [dict(item) for item in contract["dod_evidence"]]
    carrier = next(item for item in items if item.get("supersedes_ac_binding"))
    carrier["supersedes_ac_binding"] = carrier["supersedes_ac_binding"][1:]
    dropped = contract["dod_evidence"][-1]["supersedes_ac_binding"][0]["label"]
    pins = extract_criteria_pins(contract, items)
    assert pins is not None
    drift, _ = compare_criteria(pins, "OMN-20844", _TICKET_CRITERIA)
    assert ("deleted", dropped) in [(d.kind.value, d.label) for d in drift]


@pytest.mark.unit
def test_every_ticket_criterion_stays_bound(contract: dict[str, Any]) -> None:
    bound = {
        label
        for item in contract["dod_evidence"]
        for label in item.get("binds_ac") or []
    }
    assert set(_TICKET_LABELS) <= bound
