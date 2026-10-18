# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Independent section 7.2 oracle and contract invariants (OMN-21098)."""

from itertools import product

import pytest
from pydantic import ValidationError

from omnimarket.models.claude_code_binding import (
    EnumClientMaxMode,
    EnumClientVerdictAction,
)
from omnimarket.nodes.node_delegation_client_verdict_compute import (
    HandlerDelegationClientVerdict,
    NodeDelegationClientVerdictCompute,
)
from omnimarket.nodes.node_delegation_client_verdict_compute.handlers.handler_delegation_client_verdict import (
    load_verdict_table,
    validate_verdict_table,
)
from omnimarket.nodes.node_delegation_client_verdict_compute.models.model_delegation_client_verdict import (
    EnumDelegationClientOutcome,
    EnumDelegationClientVerdictRow,
    ModelDelegationClientVerdictRequest,
    ModelDelegationClientVerdictRow,
)

_CASES = tuple(
    product(
        EnumDelegationClientOutcome,
        (EnumClientMaxMode.SELECTIVE, EnumClientMaxMode.REQUIRED),
        (None, *EnumClientMaxMode),
        (True, False),
        (True, False),
        (True, False),
    )
)


def expected_action(
    outcome: EnumDelegationClientOutcome,
    effective_mode: EnumClientMaxMode | None,
    client_max_mode: EnumClientMaxMode,
    allowlisted: bool,
    echo_matches: bool,
    terminal_ok: bool,
) -> EnumClientVerdictAction:
    """Section 7.2, written independently of contract rows and handler helpers."""
    mode = client_max_mode if effective_mode is None else effective_mode
    # Refused before publish and admission unknown carry no echo; every other
    # outcome follows an admission event, so it carries one. Anything else refuses.
    unechoed = outcome in (
        EnumDelegationClientOutcome.REFUSED_BEFORE_PUBLISH,
        EnumDelegationClientOutcome.ADMISSION_UNKNOWN,
    )
    if unechoed != (effective_mode is None):
        return EnumClientVerdictAction.REFUSE
    # In the tested domain only selective -> required exceeds the client maximum.
    if (
        client_max_mode == EnumClientMaxMode.SELECTIVE
        and effective_mode == EnumClientMaxMode.REQUIRED
    ):
        return EnumClientVerdictAction.REFUSE
    if outcome == EnumDelegationClientOutcome.REFUSED_BEFORE_PUBLISH:
        if mode == EnumClientMaxMode.SELECTIVE:
            return EnumClientVerdictAction.PROCEED_NATIVE
        return EnumClientVerdictAction.REFUSE
    if outcome == EnumDelegationClientOutcome.ADMISSION_UNKNOWN:
        return EnumClientVerdictAction.REFUSE
    if outcome == EnumDelegationClientOutcome.NOT_ADMITTED:
        if mode in (EnumClientMaxMode.OBSERVE, EnumClientMaxMode.SELECTIVE):
            return EnumClientVerdictAction.PROCEED_NATIVE
        if mode == EnumClientMaxMode.REQUIRED and not allowlisted:
            return EnumClientVerdictAction.PROCEED_NATIVE
        return EnumClientVerdictAction.REFUSE
    if outcome == EnumDelegationClientOutcome.ADMISSION_OBSERVE:
        if mode == EnumClientMaxMode.OBSERVE:
            return EnumClientVerdictAction.PROCEED_NATIVE
        return EnumClientVerdictAction.REFUSE
    if outcome == EnumDelegationClientOutcome.TERMINAL:
        if (
            mode in (EnumClientMaxMode.SELECTIVE, EnumClientMaxMode.REQUIRED)
            and echo_matches
            and terminal_ok
        ):
            return EnumClientVerdictAction.ANSWER
        return EnumClientVerdictAction.REFUSE
    # Terminal timeout and all n/a cells refuse.
    return EnumClientVerdictAction.REFUSE


def make_request(
    outcome: EnumDelegationClientOutcome = EnumDelegationClientOutcome.TERMINAL,
    client_max_mode: EnumClientMaxMode = EnumClientMaxMode.REQUIRED,
    effective_mode: EnumClientMaxMode | None = EnumClientMaxMode.REQUIRED,
    allowlisted: bool = True,
    echo_matches: bool = True,
    terminal_ok: bool = True,
) -> ModelDelegationClientVerdictRequest:
    return ModelDelegationClientVerdictRequest(
        correlation_id="correlation-21098",
        client_max_mode=client_max_mode,
        effective_mode=effective_mode,
        outcome=outcome,
        task_type_allowlisted=allowlisted,
        admission_reason="capacity exhausted",
        terminal_status="completed" if terminal_ok else "failed",
        terminal_quality_gate_passed=terminal_ok,
        terminal_failure_cause="" if terminal_ok else "quality gate failed",
        client_call_key="call-21098",
        request_fingerprint="fingerprint-21098",
        echoed_client_call_key="call-21098" if echo_matches else "another-call",
        echoed_request_fingerprint="fingerprint-21098",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    (
        "outcome",
        "client_max_mode",
        "effective_mode",
        "allowlisted",
        "echo_matches",
        "terminal_ok",
    ),
    _CASES,
)
def test_full_product_matches_independent_oracle(
    outcome: EnumDelegationClientOutcome,
    client_max_mode: EnumClientMaxMode,
    effective_mode: EnumClientMaxMode | None,
    allowlisted: bool,
    echo_matches: bool,
    terminal_ok: bool,
) -> None:
    request = make_request(
        outcome, client_max_mode, effective_mode, allowlisted, echo_matches, terminal_ok
    )
    result = HandlerDelegationClientVerdict().handle(request)
    assert result.action == expected_action(
        outcome, effective_mode, client_max_mode, allowlisted, echo_matches, terminal_ok
    )
    assert result.mode == (
        client_max_mode if effective_mode is None else effective_mode
    )
    assert result.correlation_id == request.correlation_id


@pytest.mark.unit
@pytest.mark.parametrize(
    "mode", [EnumClientMaxMode.SELECTIVE, EnumClientMaxMode.REQUIRED]
)
@pytest.mark.parametrize(
    ("outcome", "echo_matches", "terminal_ok", "row"),
    [
        (
            EnumDelegationClientOutcome.ADMISSION_UNKNOWN,
            True,
            True,
            EnumDelegationClientVerdictRow.ADMISSION_UNKNOWN,
        ),
        (
            EnumDelegationClientOutcome.TERMINAL,
            True,
            False,
            EnumDelegationClientVerdictRow.TERMINAL_REJECTED,
        ),
        (
            EnumDelegationClientOutcome.TERMINAL,
            False,
            True,
            EnumDelegationClientVerdictRow.TERMINAL_IDENTITY_MISMATCH,
        ),
        (
            EnumDelegationClientOutcome.TERMINAL_TIMEOUT,
            True,
            True,
            EnumDelegationClientVerdictRow.TERMINAL_TIMEOUT,
        ),
    ],
)
def test_refuse_reasons_name_correlation_id(
    mode: EnumClientMaxMode,
    outcome: EnumDelegationClientOutcome,
    echo_matches: bool,
    terminal_ok: bool,
    row: EnumDelegationClientVerdictRow,
) -> None:
    request = make_request(
        outcome=outcome,
        client_max_mode=mode,
        effective_mode=(
            None if outcome == EnumDelegationClientOutcome.ADMISSION_UNKNOWN else mode
        ),
        echo_matches=echo_matches,
        terminal_ok=terminal_ok,
    )
    result = HandlerDelegationClientVerdict().handle(request)
    assert result.action == EnumClientVerdictAction.REFUSE
    assert result.row == row
    assert request.correlation_id in result.reason


@pytest.mark.unit
def test_terminal_rejected_reason_names_status_and_failure() -> None:
    request = make_request(terminal_ok=False)
    result = HandlerDelegationClientVerdict().handle(request)
    assert result.reason == (
        "delegation failed quality gate failed; correlation_id correlation-21098"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("echoed_client_call_key", "different-key"),
        ("echoed_request_fingerprint", "different-fingerprint"),
        ("client_call_key", ""),
    ],
)
def test_identity_mismatch_overrides_accepted_terminal(field: str, value: str) -> None:
    data = make_request().model_dump()
    data[field] = value
    if field == "client_call_key":
        data["echoed_client_call_key"] = ""
    request = ModelDelegationClientVerdictRequest.model_validate(data)
    result = HandlerDelegationClientVerdict().handle(request)
    assert result.row == EnumDelegationClientVerdictRow.TERMINAL_IDENTITY_MISMATCH
    assert result.action == EnumClientVerdictAction.REFUSE


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("terminal_status", "failed"),
        ("terminal_quality_gate_passed", False),
        ("terminal_failure_cause", "execution failed"),
    ],
)
def test_each_terminal_acceptance_condition_is_required(
    field: str, value: str | bool
) -> None:
    data = make_request().model_dump()
    data[field] = value
    result = HandlerDelegationClientVerdict().handle(
        ModelDelegationClientVerdictRequest.model_validate(data)
    )
    assert result.row == EnumDelegationClientVerdictRow.TERMINAL_REJECTED
    assert result.action == EnumClientVerdictAction.REFUSE


@pytest.mark.unit
def test_contract_loads_22_rows_and_is_cached() -> None:
    table = load_verdict_table()
    assert len(table) == 22
    assert load_verdict_table() is table
    assert all(isinstance(row, ModelDelegationClientVerdictRow) for row in table)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (None, None),
        (True, True),
        (False, False),
        (None, True),
        (True, None),
        (None, False),
        (False, None),
    ],
)
def test_validation_rejects_duplicate_or_overlapping_selectors(
    first: bool | None, second: bool | None
) -> None:
    rows = tuple(
        ModelDelegationClientVerdictRow(
            row=EnumDelegationClientVerdictRow.NOT_ADMITTED,
            mode=EnumClientMaxMode.REQUIRED,
            allowlisted=selector,
            action=EnumClientVerdictAction.REFUSE,
        )
        for selector in (first, second)
    )
    with pytest.raises(ValueError, match="ambiguous verdict row"):
        validate_verdict_table(rows)
    with pytest.raises(ValueError, match="ambiguous verdict row"):
        HandlerDelegationClientVerdict(table=rows)


@pytest.mark.unit
def test_answer_requires_terminal_over_full_product() -> None:
    handler = HandlerDelegationClientVerdict()
    for case in _CASES:
        request = make_request(*case)
        if request.outcome != EnumDelegationClientOutcome.TERMINAL:
            assert handler.handle(request).action != EnumClientVerdictAction.ANSWER


@pytest.mark.unit
def test_required_allowlisted_never_proceeds_native_over_full_product() -> None:
    handler = HandlerDelegationClientVerdict()
    for case in _CASES:
        request = make_request(*case)
        result = handler.handle(request)
        if result.mode == EnumClientMaxMode.REQUIRED and request.task_type_allowlisted:
            assert result.action != EnumClientVerdictAction.PROCEED_NATIVE


@pytest.mark.unit
def test_effective_mode_above_max_precedes_terminal_identity_check() -> None:
    request = make_request(
        client_max_mode=EnumClientMaxMode.SELECTIVE,
        effective_mode=EnumClientMaxMode.REQUIRED,
        echo_matches=False,
    )
    result = HandlerDelegationClientVerdict().handle(request)
    assert result.row == EnumDelegationClientVerdictRow.EFFECTIVE_MODE_ABOVE_MAX
    assert result.mode == EnumClientMaxMode.REQUIRED
    assert result.reason == (
        "effective mode above client max; correlation_id correlation-21098"
    )


@pytest.mark.unit
def test_missing_cell_and_empty_injected_table_fail_closed() -> None:
    request = make_request(effective_mode=EnumClientMaxMode.OBSERVE)
    for handler in (
        HandlerDelegationClientVerdict(),
        HandlerDelegationClientVerdict(table=()),
    ):
        result = handler.handle(request)
        assert result.row == EnumDelegationClientVerdictRow.TERMINAL_ACCEPTED
        assert result.action == EnumClientVerdictAction.REFUSE
        assert result.reason == (
            "no verdict row for terminal_accepted at observe; correlation_id correlation-21098"
        )


@pytest.mark.unit
def test_injected_table_owns_action_and_formats_all_placeholders() -> None:
    cell = ModelDelegationClientVerdictRow(
        row=EnumDelegationClientVerdictRow.TERMINAL_REJECTED,
        mode=EnumClientMaxMode.REQUIRED,
        action=EnumClientVerdictAction.REFUSE,
        reason="{correlation_id}: {admission_reason}; {terminal_status}: {terminal_failure_cause}",
    )
    handler = NodeDelegationClientVerdictCompute(table=(cell,))
    result = handler.handle(make_request(terminal_ok=False))
    assert result.reason == (
        "correlation-21098: capacity exhausted; failed: quality gate failed"
    )
    # Changing a cell changes the decision without changing the handler.
    replacement = cell.model_copy(
        update={"action": EnumClientVerdictAction.PROCEED_NATIVE}
    )
    assert (
        HandlerDelegationClientVerdict(table=(replacement,))
        .handle(make_request(terminal_ok=False))
        .action
        == EnumClientVerdictAction.PROCEED_NATIVE
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason",
    [
        "{unknown}",
        "{correlation_id.upper}",
        "{correlation_id!r}",
        "{terminal_status:>10}",
        "{",
    ],
)
def test_reason_template_rejects_undeclared_placeholders(reason: str) -> None:
    with pytest.raises(ValidationError):
        ModelDelegationClientVerdictRow(
            row=EnumDelegationClientVerdictRow.ADMISSION_UNKNOWN,
            mode=EnumClientMaxMode.SELECTIVE,
            action=EnumClientVerdictAction.REFUSE,
            reason=reason,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("outcome", "effective_mode"),
    [
        (
            EnumDelegationClientOutcome.REFUSED_BEFORE_PUBLISH,
            EnumClientMaxMode.SELECTIVE,
        ),
        (EnumDelegationClientOutcome.ADMISSION_UNKNOWN, EnumClientMaxMode.OBSERVE),
        (EnumDelegationClientOutcome.NOT_ADMITTED, None),
        (EnumDelegationClientOutcome.ADMISSION_OBSERVE, None),
        (EnumDelegationClientOutcome.TERMINAL, None),
        (EnumDelegationClientOutcome.TERMINAL_TIMEOUT, None),
    ],
)
def test_self_contradicting_observation_refuses(
    outcome: EnumDelegationClientOutcome, effective_mode: EnumClientMaxMode | None
) -> None:
    # Max required lowered to selective on a refused-before-publish call would
    # otherwise run natively; an echo there contradicts the outcome itself.
    request = make_request(outcome=outcome, effective_mode=effective_mode)
    result = HandlerDelegationClientVerdict().handle(request)
    assert result.row == EnumDelegationClientVerdictRow.INCONSISTENT_OBSERVATION
    assert result.action == EnumClientVerdictAction.REFUSE
    assert request.correlation_id in result.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "action",
    [
        EnumClientVerdictAction.INSTALL,
        EnumClientVerdictAction.INJECT,
        EnumClientVerdictAction.NATIVE,
        EnumClientVerdictAction.NONE,
    ],
)
def test_row_rejects_non_delegation_actions(action: EnumClientVerdictAction) -> None:
    with pytest.raises(ValidationError):
        ModelDelegationClientVerdictRow(
            row=EnumDelegationClientVerdictRow.TERMINAL_ACCEPTED,
            mode=EnumClientMaxMode.REQUIRED,
            action=action,
        )
