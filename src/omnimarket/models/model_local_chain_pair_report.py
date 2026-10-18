# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local-path chain-pair registry and absence report (OMN-18698).

The registry names each local-path goal row and its declared golden/error
pair. Rows without both halves remain visible with their owning ticket.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_L4_MODULE = "tests.chains.local.test_chain_l4_customer_key_route_omn18698"
_L5_MODULE = "tests.chains.local.test_chain_l5_store_resolution_omn18698"
_L6_MODULE = "tests.chains.local.test_chain_l6_typed_credential_refusals_omn18698"
_L11_MODULE = "tests.chains.local.test_chain_l11_response_contract_omn18698"
_DELEGATE_MODULE = "tests.test_golden_chain_node_delegate_skill_orchestrator"


@dataclass(frozen=True)
class ModelLocalChainRow:
    """One row of the local-path MVP and the chain pair that proves it."""

    row_id: str
    title: str
    ticket: str
    #: Module holding the pair, or ``None`` when no pair exists yet.
    module: str | None = None
    #: Test names inside that module, pytest-style: ``test_x`` for a module
    #: level test, ``TestClass::test_x`` for a method.
    golden: tuple[str, ...] = field(default_factory=tuple)
    error: tuple[str, ...] = field(default_factory=tuple)
    #: Why no pair exists yet. Required when one does not; forbidden when it does.
    absent_reason: str | None = None

    @property
    def has_pair(self) -> bool:
        """A pair is BOTH halves. A golden chain alone is not a pair."""
        return bool(self.golden) and bool(self.error)


#: Every row of the local-path MVP, in order. The identity of a row is its
#: ticket; the title is the human handle.
LOCAL_ROWS: tuple[ModelLocalChainRow, ...] = (
    ModelLocalChainRow(
        row_id="L1",
        title="Install the client on a machine that is not the operator's",
        ticket="OMN-16200",
        absent_reason=(
            "an installation is not a dispatch; proving it needs a second "
            "machine, not a chain pair on this one"
        ),
    ),
    ModelLocalChainRow(
        row_id="L2",
        title="The packaged drift guard asserts without blocking dispatch",
        ticket="OMN-17255",
        absent_reason="the skip line the ruling asks for does not exist yet",
    ),
    ModelLocalChainRow(
        row_id="L3",
        title="The skill dispatches to a local model on the customer's machine",
        ticket="OMN-18626",
        module=_DELEGATE_MODULE,
        golden=(
            "TestDelegateSkillGoldenChain::"
            "test_completed_delegation_preserves_route_evidence",
            "TestDelegateSkillGoldenChain::"
            "test_unit_test_prompt_suppresses_reasoning_and_persists_evidence",
        ),
        error=(
            "TestDelegateSkillGoldenChain::test_failed_dispatch_stays_typed_failure",
        ),
    ),
    ModelLocalChainRow(
        row_id="L4",
        title="The skill dispatches to OpenRouter on the CUSTOMER's key, no gateway",
        ticket="OMN-18694",
        module=_L4_MODULE,
        golden=("test_golden_chain_customer_key_routes_and_pays",),
        error=(
            "test_error_chain_no_registered_key_refuses_rather_than_using_"
            "the_house_key",
        ),
    ),
    ModelLocalChainRow(
        row_id="L5",
        title="The customer's key resolves from the local SQLite store, never env",
        ticket="OMN-18695",
        module=_L5_MODULE,
        golden=("test_golden_chain_store_value_wins_over_every_environment_name",),
        error=("test_error_chain_an_empty_store_refuses_rather_than_reading_env",),
    ),
    ModelLocalChainRow(
        row_id="L6",
        title="Wrong key and absent key produce typed refusals on the local path",
        ticket="OMN-18696",
        module=_L6_MODULE,
        golden=("test_golden_chain_a_resolvable_key_completes_with_no_refusal",),
        error=(
            "test_error_chain_a_rejected_key_is_typed_and_does_not_climb",
            "test_error_chain_an_absent_value_is_typed_and_does_not_climb",
            "test_error_chain_a_provider_outage_is_not_a_credential_refusal",
        ),
    ),
    ModelLocalChainRow(
        row_id="L7",
        title="Per-call metering and a savings figure, local-only for the proof",
        ticket="OMN-18697",
        absent_reason="no command surfaces the figure yet; the surface is in flight",
    ),
    ModelLocalChainRow(
        row_id="L8",
        title="Receipts and evidence visible to the customer on their own disk",
        ticket="OMN-16999",
        absent_reason=(
            "measured green on live runs rather than pinned by a pair; the "
            "pair is worth adding and nobody has"
        ),
    ),
    ModelLocalChainRow(
        row_id="L9",
        title="The local deployment mints and carries its own tenant identity",
        ticket="OMN-18699",
        absent_reason="landed 2026-09-18; its pair has not been written",
    ),
    ModelLocalChainRow(
        row_id="L10",
        title="A golden chain AND an error chain exist for each row above",
        ticket="OMN-18698",
        module="tests.chains.local.test_local_chain_pair_registry_omn18698",
        golden=("test_every_declared_pair_resolves",),
        error=("test_rows_without_a_pair_are_named",),
    ),
    ModelLocalChainRow(
        row_id="L11",
        title="The served local model honours the response contract it is handed",
        ticket="OMN-18700",
        module=_L11_MODULE,
        golden=(
            "test_golden_chain_the_contract_reaches_the_model_and_the_caller_"
            "gets_the_object",
        ),
        error=("test_error_chain_guessed_key_names_fail_rather_than_pass",),
    ),
    ModelLocalChainRow(
        row_id="R4",
        title="A second person completes the local path on their own machine",
        ticket="OMN-18701",
        absent_reason="a person, not a process; no test can stand in for it",
    ),
)


def absence_report() -> str:
    """One line per row with no pair, naming the row and what owns it."""
    lines = [
        f"{row.row_id}: ABSENT -- {row.title} ({row.ticket}): {row.absent_reason}"
        for row in LOCAL_ROWS
        if not row.has_pair
    ]
    if not lines:
        return "every local-path row carries a golden and an error chain"
    return "\n".join(lines)
