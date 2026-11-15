# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review keys' standing credit check writes one alarm row when a key flips (OMN-24562).

Every key is a fixed fake string; no test reaches a provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.handlers.handler_review_key_credit_probe import (
    PROBE_PAYLOAD,
    flips,
    read_states,
    record,
)
from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
    ClaudeReviewGateway,
)
from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
    ModelClaudeReviewGatewayRequest,
    ModelReviewGatewayOverlay,
)
from tests.unit.nodes.node_claude_review_gateway_effect.test_claude_review_gateway import (
    _HOST,
    _MODEL,
    _TENANT,
    _URL,
    FakeCall,
    FakeCatalog,
    FakeValues,
    _fail,
    _ok,
    _row,
    _value,
)

pytestmark = pytest.mark.unit
_NOW = datetime(2026, 11, 15, 23, 0, tzinfo=UTC)


def _gateway(call: FakeCall, names: list[str]) -> ClaudeReviewGateway:
    refs = [str(_row(n)["api_key_ref"]) for n in names]
    return ClaudeReviewGateway(
        ModelClaudeReviewGatewayRequest(
            host=_HOST,
            tenant_id=_TENANT,
            model=_MODEL,
            cloud_refused_repositories=("acme/private-notes",),
        ),
        catalog=FakeCatalog([_row(n) for n in names]),
        key_values=FakeValues(refs),
        call=call,
        endpoint_url=_URL,
        overlay=ModelReviewGatewayOverlay(key_set_aside_ttl_seconds=1),
        event_bus=None,
        clock=lambda: _NOW,
    )


async def test_probe_reads_every_bound_key_and_names_none_by_reference() -> None:
    call = FakeCall(
        [
            _fail(
                EnumDelegationFailureClass.PROVIDER_BILLING, "credit balance is too low"
            ),
            _ok(),
            _fail(EnumDelegationFailureClass.PROVIDER_AUTH_FAILED),
            _fail(EnumDelegationFailureClass.UNKNOWN),
        ]
    )
    states = await _gateway(call, ["key-a", "key-b", "key-c", "key-d"]).probe_keys(
        PROBE_PAYLOAD
    )
    assert states == [
        ("key-a", "out_of_credit"),
        ("key-b", "funded"),
        ("key-c", "refused"),
        ("key-d", "unreachable"),
    ]
    assert all(request.max_tokens == 1 for request in call.calls)
    text = repr(states)
    assert str(_row("key-a")["api_key_ref"]) not in text
    assert _value(str(_row("key-a")["api_key_ref"])) not in text


def test_a_flip_is_a_change_from_a_recorded_state_never_a_baseline_or_an_unreachable_read() -> (
    None
):
    assert flips({}, {"a": "funded"}) == []
    assert flips({"a": "funded"}, {"a": "funded"}) == []
    assert flips({"a": "funded"}, {"a": "unreachable"}) == []
    assert flips({"a": "unreachable"}, {"a": "out_of_credit"}) == []
    assert flips(
        {"a": "funded", "b": "out_of_credit"}, {"a": "out_of_credit", "b": "funded"}
    ) == [
        ("a", "funded", "out_of_credit"),
        ("b", "out_of_credit", "funded"),
    ]


def test_first_run_is_a_baseline_and_a_flip_writes_one_row_each(tmp_path: Path) -> None:
    rows: list[str] = []

    def appender(ledger: Path, row: str) -> tuple[bool, str]:
        rows.append(row)
        return True, "ok"

    def run(states: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
        return record(
            states,
            state_dir=tmp_path / "state",
            ledger=tmp_path / "ledger.md",
            host="h200",
            model=_MODEL,
            appender=appender,
            clock=lambda: _NOW,
        )

    assert run([("a", "funded"), ("b", "out_of_credit")]) == []
    assert rows == []
    assert run([("a", "out_of_credit"), ("b", "out_of_credit")]) == [
        ("a", "funded", "out_of_credit")
    ]
    assert len(rows) == 1
    assert "| STATUS |" in rows[0]
    assert "alarm=review-key-credit" in rows[0]
    for field in ("key=a", "from=funded", "to=out_of_credit"):
        assert field in rows[0]
    assert run([("a", "out_of_credit"), ("b", "out_of_credit")]) == []
    assert len(rows) == 1
    assert read_states(tmp_path / "state") == {
        "a": "out_of_credit",
        "b": "out_of_credit",
    }


def test_a_ledger_that_refuses_keeps_the_old_state_so_the_flip_is_retried(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    record(
        [("a", "funded")],
        state_dir=state,
        ledger=tmp_path / "l",
        host="h",
        model="m",
        appender=lambda *_: (True, "ok"),
        clock=lambda: _NOW,
    )
    with pytest.raises(RuntimeError, match="not recorded"):
        record(
            [("a", "out_of_credit")],
            state_dir=state,
            ledger=tmp_path / "l",
            host="h",
            model="m",
            appender=lambda *_: (False, "refused"),
            clock=lambda: _NOW,
        )
    assert read_states(state) == {"a": "funded"}


def test_an_unreachable_read_keeps_the_last_known_state(tmp_path: Path) -> None:
    state = tmp_path / "state"

    def run(reading: str) -> None:
        record(
            [("a", reading)],
            state_dir=state,
            ledger=tmp_path / "l",
            host="h",
            model="m",
            appender=lambda *_: (True, "ok"),
            clock=lambda: _NOW,
        )

    run("funded")
    run("unreachable")
    assert read_states(state) == {"a": "funded"}
