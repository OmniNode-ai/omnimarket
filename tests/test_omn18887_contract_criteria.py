# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18887 evidence declares and claims every acceptance criterion of the ticket."""

from pathlib import Path

from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "OMN-18887.yaml"
CRITERIA = {"AC1", "AC2", "AC3", "AC4", "AC5"}
GOLDEN_CHAIN = "tests/test_golden_chain_node_delegate_skill_orchestrator.py"
CLAIM_STORE = (
    "tests/unit/nodes/node_delegate_skill_orchestrator/"
    "test_claim_store_state_root_omn19654.py"
)
PROBES = {
    "AC1": (
        f"{GOLDEN_CHAIN}::TestDelegateSkillRedeliveryIsIdempotent::"
        "test_a_redelivered_record_dispatches_the_inference_once",
        f"{GOLDEN_CHAIN}::TestDelegateSkillRedeliveryIsIdempotent::"
        "test_a_reused_correlation_on_a_new_record_still_dispatches",
    ),
    "AC2": (f"{GOLDEN_CHAIN}::test_a_redelivered_record_calls_the_provider_once",),
    "AC3": (
        f"{GOLDEN_CHAIN}::TestDelegateSkillRedeliveryIsIdempotent::"
        "test_the_redelivered_record_still_answers_its_caller",
    ),
    "AC4": (
        f"{GOLDEN_CHAIN}::test_a_redelivered_record_calls_the_provider_once"
        "[restarted-runtime]",
        f"{CLAIM_STORE}::"
        "test_omninode_readonly_rootfs_claim_store_is_durable_across_ports",
    ),
    "AC5": (
        f"{GOLDEN_CHAIN}::test_a_redelivered_record_calls_the_provider_once",
        f"{GOLDEN_CHAIN}::"
        "test_a_reused_correlation_on_a_new_record_still_calls_the_provider",
    ),
}


def _load() -> ModelTicketContract:
    return ModelTicketContract.from_yaml(CONTRACT.read_text(encoding="utf-8"))


def test_contract_declares_every_ticket_criterion() -> None:
    contract = _load()
    declared = {
        criterion.id
        for requirement in contract.requirements
        for criterion in requirement.acceptance
    }
    assert declared == CRITERIA


def test_every_criterion_is_claimed_by_one_item_with_a_check() -> None:
    contract = _load()
    claimed: dict[str, int] = {}
    for item in contract.dod_evidence:
        for label in item.binds_ac:
            claimed[label] = claimed.get(label, 0) + 1
            assert item.checks, item.id
    assert claimed == dict.fromkeys(CRITERIA, 1)


def test_each_criterion_runs_its_own_probe_nodes() -> None:
    contract = _load()
    for item in contract.dod_evidence:
        for label in item.binds_ac:
            command = " ".join(check.check_value for check in item.checks)
            for node_id in PROBES[label]:
                assert node_id in command, (item.id, node_id)


def test_every_bound_probe_node_exists() -> None:
    for node_ids in PROBES.values():
        for node_id in node_ids:
            path, _, rest = node_id.partition("::")
            source = (ROOT / path).read_text(encoding="utf-8")
            leaf = rest.split("::")[-1].split("[")[0]
            assert f"def {leaf}(" in source, node_id
