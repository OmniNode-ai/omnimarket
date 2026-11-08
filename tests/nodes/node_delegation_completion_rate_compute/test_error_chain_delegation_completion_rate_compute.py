# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain (OMN-23108): a below-bound hour, a dead deployed lane and an empty window read NOT WORKING, rows that could not be
read read UNKNOWN, a request the node cannot read as asked is refused by name, and each named mutation turns the chain red.

Every mutation in NAMED_MUTATIONS replaces one rule in the handler module and runs the same error assertions; the mutated chain
must raise AssertionError.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_delegation_completion_rate_compute.handlers import (
    handler_delegation_completion_rate as module,
)
from omnimarket.nodes.node_delegation_completion_rate_compute.models.model_delegation_completion_rate import (
    ModelDelegationCompletionRateRequest,
)
from tests.nodes.node_delegation_completion_rate_compute.completion_rate_support import (
    HARNESS,
    above_bound_hour,
    below_bound_hour,
    event,
    hour,
    request,
    resolve_handler,
)

pytestmark = pytest.mark.unit

NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "r-dep-bound-ignored": (
        "error",
        "test_mutation_r_dep_bound_ignored_turns_the_error_chain_red",
    ),
    "failed-rate-bound-ignored": (
        "error",
        "test_mutation_failed_rate_bound_ignored_turns_the_error_chain_red",
    ),
    "empty-window-passes": (
        "error",
        "test_mutation_empty_window_passes_turns_the_error_chain_red",
    ),
    "unreadable-rows-read-as-zero-rows": (
        "error",
        "test_mutation_unreadable_rows_read_as_zero_rows_turns_the_error_chain_red",
    ),
    "shadow-rows-counted": (
        "error",
        "test_mutation_shadow_rows_counted_turns_the_error_chain_red",
    ),
}


def run_error_chain() -> None:
    """Each broken request ends NOT WORKING (or UNKNOWN, or is refused by name)."""
    handler = resolve_handler()()
    broken: dict[str, dict[str, Any]] = {
        "below-bound-hour": {"events": below_bound_hour()},
        "dead-deployed-lane": {
            "events": tuple(event(1 + i * 0.4, backend=HARNESS) for i in range(94))
        },
        "failures-only": {"events": hour(40, 40, failed=40)},
        "empty-window": {"events": ()},
        "only-shadow-rows": {
            "events": tuple(event(i, shadow=True) for i in range(1, 60))
        },
    }
    for name, edits in broken.items():
        reading = handler.handle(request(**edits))
        assert reading.verdict is V.NOT_WORKING, name
        assert reading.reasons, name
    unreadable = handler.handle(
        request(events=(), events_unread_reason="delegation_events-down")
    )
    assert unreadable.verdict is V.UNKNOWN
    assert (unreadable.r_all, unreadable.r_dep, unreadable.failed_rate) == (None,) * 3
    with pytest.raises(ValidationError, match="cannot both be given"):
        request(events=above_bound_hour(), events_unread_reason="down")
    with pytest.raises(ValidationError, match="names no reason"):
        request(events=(), events_unread_reason="  ")
    with pytest.raises(ValidationError):
        request(now="2026-11-08T07:00:00")
    with pytest.raises(ValidationError, match="not UTC"):
        request(now="2026-11-08T07:00:00+02:00")


def test_error_chain_a_well_formed_request_is_accepted() -> None:
    """Positive control: the refused shapes differ from this one in one field each."""
    parsed = ModelDelegationCompletionRateRequest.model_validate(request().model_dump())
    assert resolve_handler()().handle(parsed).verdict is V.WORKING


def test_error_chain_broken_requests_end_not_working_unknown_or_refused() -> None:
    run_error_chain()


def _drop_failures(real: Callable[..., Any], prefix: str) -> Callable[..., Any]:
    def mutated(*args: Any) -> list[str]:
        return [f for f in real(*args) if not f.startswith(prefix)]

    return mutated


def test_mutation_r_dep_bound_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module, "judge", _drop_failures(module.judge, "r_dep-below-bound")
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_failed_rate_bound_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module, "judge", _drop_failures(module.judge, "failed_rate-above-bound")
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_empty_window_passes_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.judge

    def mutated(overlay: Any, r_all: int, r_dep: int, rate: float | None) -> list[str]:
        return [] if rate is None else real(overlay, r_all, r_dep, rate)

    monkeypatch.setattr(module, "judge", mutated)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_unreadable_rows_read_as_zero_rows_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.HandlerDelegationCompletionRate.handle

    def mutated(self: Any, req: ModelDelegationCompletionRateRequest) -> Any:
        return real(self, req.model_copy(update={"events_unread_reason": None}))

    monkeypatch.setattr(module.HandlerDelegationCompletionRate, "handle", mutated)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_shadow_rows_counted_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.count_rates

    def mutated(overlay: Any, rows: Any) -> Any:
        return real(overlay, [r.model_copy(update={"is_shadow": False}) for r in rows])

    monkeypatch.setattr(module, "count_rates", mutated)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_the_error_chain_is_green_again_after_each_mutation_is_undone() -> None:
    """Positive control: the chain the mutations break is the one that passes unmutated."""
    run_error_chain()


def test_every_named_error_mutation_has_its_test() -> None:
    names = {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names
    assert {kind for kind, _ in NAMED_MUTATIONS.values()} == {"error"}
