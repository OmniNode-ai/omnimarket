# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review keys' standing credit check writes one alarm row when a key flips (OMN-24658).

Every key is a fixed fake string; no test reaches a provider.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli import cli_secret
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
    ClaudeReviewGateway,
)
from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_review_key_credit_probe import (
    LEDGER_COMMAND_VARIABLE,
    PROBE_PAYLOAD,
    NoReadingError,
    flips,
    ledger_appender_from_environment,
    read_states,
    record,
    render_alarm_row,
)
from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
    ModelClaudeReviewGatewayRequest,
    ModelReviewGatewayOverlay,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    InMemoryLedgerAppender,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    apply_credential_registered,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
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


_KEY_REFS = {name: str(_row(name)["api_key_ref"]) for name in ("key-a", "key-b")}


def _record(
    tmp_path: Path, states: list[tuple[str, str]], appender: InMemoryLedgerAppender
) -> list[tuple[str, str, str]]:
    return record(
        states,
        state_dir=tmp_path / "state",
        ledger=tmp_path / "ledger.md",
        host="h200",
        model=_MODEL,
        appender=appender,
        clock=lambda: _NOW,
    )


def test_first_run_is_a_baseline_and_a_flip_writes_one_row_each(tmp_path: Path) -> None:
    ledger = InMemoryLedgerAppender()
    assert _record(tmp_path, [("a", "funded"), ("b", "out_of_credit")], ledger) == []
    assert ledger.rows == []
    states = [("a", "out_of_credit"), ("b", "out_of_credit")]
    assert _record(tmp_path, states, ledger) == [("a", "funded", "out_of_credit")]
    (_, row) = ledger.rows[0]
    assert len(ledger.rows) == 1
    assert "| STATUS |" in row
    assert "alarm=review-key-credit" in row
    for field in ("key=a", "from=funded", "to=out_of_credit"):
        assert field in row
    assert _record(tmp_path, states, ledger) == []
    assert len(ledger.rows) == 1
    assert read_states(tmp_path / "state") == {
        "a": "out_of_credit",
        "b": "out_of_credit",
    }


def test_a_ledger_that_refuses_keeps_the_old_state_so_the_flip_is_retried(
    tmp_path: Path,
) -> None:
    _record(tmp_path, [("a", "funded")], InMemoryLedgerAppender())
    with pytest.raises(RuntimeError, match="not recorded"):
        _record(tmp_path, [("a", "out_of_credit")], InMemoryLedgerAppender(refuse="no"))
    assert read_states(tmp_path / "state") == {"a": "funded"}


def test_a_refused_second_row_does_not_write_the_first_row_again(
    tmp_path: Path,
) -> None:
    class RefuseSecond(InMemoryLedgerAppender):
        def append(self, ledger: Path, row: str) -> Any:
            if len(self.rows) == 1:
                return InMemoryLedgerAppender(refuse="contention").append(ledger, row)
            return super().append(ledger, row)

    _record(tmp_path, [("a", "funded"), ("b", "funded")], InMemoryLedgerAppender())
    flipped = [("a", "out_of_credit"), ("b", "out_of_credit")]
    ledger = RefuseSecond()
    with pytest.raises(RuntimeError, match="not recorded"):
        _record(tmp_path, flipped, ledger)
    assert len(ledger.rows) == 1
    assert read_states(tmp_path / "state") == {"a": "out_of_credit", "b": "funded"}
    retried = InMemoryLedgerAppender()
    assert _record(tmp_path, flipped, retried) == [("b", "funded", "out_of_credit")]
    assert [row for _, row in retried.rows if "key=a" in row] == []


def test_an_unreachable_read_keeps_the_last_known_state(tmp_path: Path) -> None:
    _record(tmp_path, [("a", "funded"), ("b", "funded")], InMemoryLedgerAppender())
    _record(tmp_path, [("a", "unreachable"), ("b", "funded")], InMemoryLedgerAppender())
    assert read_states(tmp_path / "state") == {"a": "funded", "b": "funded"}


@pytest.mark.parametrize(
    "states",
    [[], [("a", "unreachable")], [("a", "unreachable"), ("b", "unreachable")]],
    ids=["no-key-bound", "one-unreachable", "all-unreachable"],
)
def test_a_run_in_which_no_key_gave_a_reading_fails_and_keeps_no_state(
    tmp_path: Path, states: list[tuple[str, str]]
) -> None:
    ledger = InMemoryLedgerAppender()
    with pytest.raises(NoReadingError, match="no key gave a reading"):
        _record(tmp_path, states, ledger)
    assert ledger.rows == []
    assert read_states(tmp_path / "state") == {}


def test_the_rendered_row_carries_the_ticket_and_the_key_name_never_a_reference() -> (
    None
):
    row = render_alarm_row(
        "2026-11-16T01:00:00Z", "h200", _MODEL, "key-a", "funded", "out_of_credit"
    )
    cells = row.split(" | ")
    assert cells[1] == "STATUS"
    assert "ticket=OMN-24658" in cells
    assert "key=key-a" in cells
    assert "24562" not in row
    for ref in _KEY_REFS.values():
        assert ref not in row
        assert _value(ref) not in row
    piped = render_alarm_row("t", "h|1", "m", "k|x", "a|b", "c|d")
    assert len(piped.split(" | ")) == len(row.split(" | "))


def _stub_ledger_command(tmp_path: Path, exit_code: int) -> tuple[str, Path]:
    """A stand-in onex-ledger: records its argument list, optionally refuses."""
    script = tmp_path / "stub_ledger.py"
    seen = tmp_path / "ledger-argv.json"
    script.write_text(
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({str(seen)!r}, 'w'))\n"
        f"sys.stderr.write('stub refused') if {exit_code} else None\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {script}", seen


def test_the_appender_runs_the_command_the_host_units_carry_and_reads_its_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LEDGER_COMMAND_VARIABLE, raising=False)
    with pytest.raises(RuntimeError, match=LEDGER_COMMAND_VARIABLE):
        ledger_appender_from_environment()
    command, seen = _stub_ledger_command(tmp_path, 0)
    monkeypatch.setenv(LEDGER_COMMAND_VARIABLE, command)
    row = render_alarm_row("t", "h200", _MODEL, "key-a", "funded", "out_of_credit")
    outcome = ledger_appender_from_environment().append(tmp_path / "l.md", row)
    assert outcome.ok
    assert json.loads(seen.read_text(encoding="utf-8")) == [
        str(tmp_path / "l.md"),
        "--append",
        row,
    ]
    command, seen = _stub_ledger_command(tmp_path, 75)
    monkeypatch.setenv(LEDGER_COMMAND_VARIABLE, command)
    refused = ledger_appender_from_environment().append(tmp_path / "l.md", row)
    assert not refused.ok
    assert "rc=75" in refused.detail
    assert "stub refused" in refused.detail


class _ProbeWorld:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.store = LocalByokCredentialStore(db_path=tmp_path / "delegation.sqlite")
        self.refs: list[str] = []
        for index, name in enumerate(("key-a", "key-b")):
            ref = f"cred_{_TENANT}_anthropic_" + str(index) * 32
            self.refs.append(ref)
            asyncio.run(
                self.store.set_secret(ref, f"sk-ant-fake-{index}-never-printed")
            )
            apply_credential_registered(
                {
                    "tenant_id": _TENANT,
                    "provider": "anthropic",
                    "name": name,
                    "api_key_ref": ref,
                    "bound_host": _HOST,
                    "expires_at": "2099-01-01T00:00:00+00:00",
                },
                SqliteDatabaseAdapter(self.store.db_path),
            )
        table = tmp_path / "hosts.yaml"
        table.write_text(
            f"hosts:\n  - name: {_HOST}\n    target: lab-host\n", encoding="utf-8"
        )
        monkeypatch.setenv("ONEX_LAB_RUN_HOSTS", str(table))
        monkeypatch.setattr(cli_secret, "_tenant_key_store", lambda: self.store)
        self.outcomes: list[Any] = []
        self.requests: list[Any] = []
        outcomes = self.outcomes
        requests = self.requests

        def _call(_self: object, request: object) -> Any:
            requests.append(request)
            return outcomes.pop(0)

        monkeypatch.setattr(
            "omnimarket.nodes.node_llm_delegation_call_effect.handlers."
            "handler_llm_delegation_call.HandlerLlmDelegationCall.__call__",
            _call,
        )
        self.command, self.seen = _stub_ledger_command(tmp_path, 0)
        monkeypatch.setenv(LEDGER_COMMAND_VARIABLE, self.command)

    def run(self, *extra: str, model: str = _MODEL) -> Any:
        return CliRunner().invoke(
            cli_secret.models_group,
            [
                "review-gateway-key-probe",
                "--tenant",
                _TENANT,
                "--model",
                model,
                "--host",
                _HOST,
                "--state-dir",
                str(self.tmp / "state"),
                "--ledger",
                str(self.tmp / "ledger.md"),
                *extra,
            ],
        )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ProbeWorld:
    return _ProbeWorld(tmp_path, monkeypatch)


def test_the_command_baselines_then_ledgers_a_flip_through_the_ledger_command(
    world: _ProbeWorld,
) -> None:
    world.outcomes[:] = [_ok(), _ok()]
    first = world.run()
    assert first.exit_code == 0, first.output
    assert "0 flip(s) written" in first.output
    assert not world.seen.exists()
    world.outcomes[:] = [_fail(EnumDelegationFailureClass.PROVIDER_BILLING), _ok()]
    second = world.run()
    assert second.exit_code == 0, second.output
    assert "1 flip(s) written" in second.output
    argv = json.loads(world.seen.read_text(encoding="utf-8"))
    assert argv[:2] == [str(world.tmp / "ledger.md"), "--append"]
    assert "key=key-a" in argv[2]
    assert "ticket=OMN-24658" in argv[2]
    for ref in world.refs:
        assert ref not in argv[2] + first.output + second.output
    for value in ("sk-ant-fake-0-never-printed", "sk-ant-fake-1-never-printed"):
        assert value not in argv[2] + first.output + second.output


def test_the_command_exits_nonzero_when_every_key_is_unreachable(
    world: _ProbeWorld,
) -> None:
    world.outcomes[:] = [
        _fail(EnumDelegationFailureClass.UNKNOWN),
        _fail(EnumDelegationFailureClass.UNKNOWN),
    ]
    result = world.run()
    assert result.exit_code != 0
    assert "no key gave a reading" in result.output
    assert not world.seen.exists()


def test_the_command_exits_nonzero_when_no_key_is_bound_to_this_host(
    world: _ProbeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_secret, "_resolve_bound_host", lambda _host: "h999")
    result = world.run()
    assert result.exit_code != 0
    assert "no key gave a reading" in result.output


def test_the_command_refuses_a_model_name_the_request_refuses_without_a_traceback(
    world: _ProbeWorld,
) -> None:
    result = world.run(model="")
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "review-gateway-key-probe refused" in result.output


def test_the_command_refuses_to_run_without_the_ledger_command(
    world: _ProbeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(LEDGER_COMMAND_VARIABLE)
    world.outcomes[:] = [_ok(), _ok()]
    result = world.run()
    assert result.exit_code != 0
    assert LEDGER_COMMAND_VARIABLE in result.output


def test_the_command_passes_the_thinking_mode_to_every_probe_request(
    world: _ProbeWorld,
) -> None:
    world.outcomes[:] = [_ok(), _ok()]
    adaptive = world.run("--thinking-mode", "adaptive")
    assert adaptive.exit_code == 0, adaptive.output
    assert len(world.requests) == 2
    assert all(
        request.provider_request_options["thinking"] == {"type": "adaptive"}
        for request in world.requests
    )
    world.requests.clear()
    world.outcomes[:] = [_ok(), _ok()]
    default = world.run()
    assert default.exit_code == 0, default.output
    assert all(
        "thinking" not in request.provider_request_options for request in world.requests
    )


def test_the_command_refuses_a_thinking_mode_the_gateway_does_not_know(
    world: _ProbeWorld,
) -> None:
    result = world.run("--thinking-mode", "always")
    assert result.exit_code != 0
    assert not world.requests
