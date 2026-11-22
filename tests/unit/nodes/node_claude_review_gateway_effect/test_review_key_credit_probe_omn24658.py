# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review keys' standing credit check writes one alarm row when a key flips (OMN-24658).

Every key is a fixed fake string; no test reaches a provider.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.cli import cli_secret
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
    ClaudeReviewGateway,
    ReviewKeyProbe,
)
from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
    ModelClaudeReviewGatewayRequest,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    apply_credential_registered,
)
from omnimarket.nodes.node_review_key_credit_probe_effect.handlers import (
    handler_review_key_credit_probe as probe_handler,
)
from omnimarket.nodes.node_review_key_credit_probe_effect.handlers.handler_review_key_credit_probe import (
    HandlerReviewKeyCreditProbe,
    NoReadingError,
    flips,
    load_probe_config,
    probe_payload,
    read_states,
    record,
    render_alarm_row,
)
from omnimarket.nodes.node_review_key_credit_probe_effect.models.model_review_key_credit_probe import (
    ModelReviewKeyCreditProbeRequest,
)
from omnimarket.nodes.node_review_key_credit_probe_effect.protocols import (
    BusReviewKeyLedgerAppender,
    InMemoryReviewKeyLedgerAppender,
    ledger_appender_for_lane,
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
_BUS_LANE = "lane-x"


def _gateway(call: FakeCall, names: list[str]) -> ReviewKeyProbe:
    refs = [str(_row(n)["api_key_ref"]) for n in names]
    return ReviewKeyProbe(
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
        probe_payload(load_probe_config())
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
    tmp_path: Path,
    states: list[tuple[str, str]],
    appender: InMemoryReviewKeyLedgerAppender,
) -> list[tuple[str, str, str]]:
    return asyncio.run(
        record(
            states,
            state_dir=tmp_path / "state",
            host="h200",
            model=_MODEL,
            appender=appender,
            clock=lambda: _NOW,
        )
    )


def test_first_run_is_a_baseline_and_a_flip_writes_one_row_each(tmp_path: Path) -> None:
    ledger = InMemoryReviewKeyLedgerAppender()
    assert _record(tmp_path, [("a", "funded"), ("b", "out_of_credit")], ledger) == []
    assert ledger.rows == []
    states = [("a", "out_of_credit"), ("b", "out_of_credit")]
    assert _record(tmp_path, states, ledger) == [("a", "funded", "out_of_credit")]
    row = ledger.rows[0]
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
    _record(tmp_path, [("a", "funded")], InMemoryReviewKeyLedgerAppender())
    with pytest.raises(RuntimeError, match="not recorded"):
        _record(
            tmp_path,
            [("a", "out_of_credit")],
            InMemoryReviewKeyLedgerAppender(refuse="no"),
        )
    assert read_states(tmp_path / "state") == {"a": "funded"}


def test_a_refused_second_row_does_not_write_the_first_row_again(
    tmp_path: Path,
) -> None:
    class RefuseSecond(InMemoryReviewKeyLedgerAppender):
        async def append(self, row: str) -> Any:
            if len(self.rows) == 1:
                return await InMemoryReviewKeyLedgerAppender(
                    refuse="contention"
                ).append(row)
            return await super().append(row)

    _record(
        tmp_path, [("a", "funded"), ("b", "funded")], InMemoryReviewKeyLedgerAppender()
    )
    flipped = [("a", "out_of_credit"), ("b", "out_of_credit")]
    ledger = RefuseSecond()
    with pytest.raises(RuntimeError, match="not recorded"):
        _record(tmp_path, flipped, ledger)
    assert len(ledger.rows) == 1
    assert read_states(tmp_path / "state") == {"a": "out_of_credit", "b": "funded"}
    retried = InMemoryReviewKeyLedgerAppender()
    assert _record(tmp_path, flipped, retried) == [("b", "funded", "out_of_credit")]
    assert [row for row in retried.rows if "key=a" in row] == []


def test_an_unreachable_read_keeps_the_last_known_state(tmp_path: Path) -> None:
    _record(
        tmp_path, [("a", "funded"), ("b", "funded")], InMemoryReviewKeyLedgerAppender()
    )
    _record(
        tmp_path,
        [("a", "unreachable"), ("b", "funded")],
        InMemoryReviewKeyLedgerAppender(),
    )
    assert read_states(tmp_path / "state") == {"a": "funded", "b": "funded"}


@pytest.mark.parametrize(
    "states",
    [[], [("a", "unreachable")], [("a", "unreachable"), ("b", "unreachable")]],
    ids=["no-key-bound", "one-unreachable", "all-unreachable"],
)
def test_a_run_in_which_no_key_gave_a_reading_fails_and_keeps_no_state(
    tmp_path: Path, states: list[tuple[str, str]]
) -> None:
    ledger = InMemoryReviewKeyLedgerAppender()
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


class _FakeSend:
    """A stand-in for the bus: records each request and answers with the status it was given."""

    def __init__(self, status: EnumWorkLedgerAppendStatus, message: str = "") -> None:
        self.status = status
        self.message = message
        self.requests: list[ModelWorkLedgerAppendRequest] = []

    async def __call__(
        self, request: ModelWorkLedgerAppendRequest, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        self.requests.append(request)
        return ModelWorkLedgerAppendReceipt(
            request_id=request.request_id,
            status=self.status,
            exit_code=0,
            message=self.message,
            ledger_lines=[],
            ledger_host="ledger-host",
            duration_ms=1,
            ledger_seq=1,
        )


def _bus_appender(send: _FakeSend) -> BusReviewKeyLedgerAppender:
    return BusReviewKeyLedgerAppender(
        "lane-x", Path("."), host="h200", clock=lambda: _NOW, send=send
    )


def test_a_flip_row_reaches_the_ledger_as_a_bus_append_request_naming_no_file() -> None:
    send = _FakeSend(EnumWorkLedgerAppendStatus.ACCEPTED)
    row = render_alarm_row("t", "h200", _MODEL, "key-a", "funded", "out_of_credit")
    outcome = asyncio.run(_bus_appender(send).append(row))
    assert outcome.ok
    (request,) = send.requests
    assert request.ledger_id == "rolling-work-ledger"
    assert request.requested_by_lane == "review-key-credit-probe"
    assert request.requesting_host == "h200"
    cells = request.rows.split(" | ")
    assert f"req={request.request_id}" in cells
    assert "via=bus:h200" in cells
    assert cells[-1] == row.split(" | ")[-1]
    assert "alarm=review-key-credit" in cells
    repeated = asyncio.run(_bus_appender(send).append(row))
    assert repeated.ok
    assert send.requests[1].request_id == request.request_id


def test_a_refusal_or_silence_from_the_ledger_is_a_refused_append() -> None:
    row = render_alarm_row("t", "h200", _MODEL, "key-a", "funded", "out_of_credit")
    refused = asyncio.run(
        _bus_appender(_FakeSend(EnumWorkLedgerAppendStatus.REFUSED, "no")).append(row)
    )
    assert not refused.ok
    assert "refused" in refused.detail

    async def _silent(
        _request: ModelWorkLedgerAppendRequest, _timeout: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        return None

    silent = asyncio.run(
        BusReviewKeyLedgerAppender(
            "lane-x", Path("."), host="h200", clock=lambda: _NOW, send=_silent
        ).append(row)
    )
    assert not silent.ok
    assert "no receipt" in silent.detail


def test_the_appender_needs_a_workspace_root_and_has_no_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("OMNIBASE_PATH", "OMNI_HOME"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="OMNI_HOME"):
        ledger_appender_for_lane("lane-x")
    monkeypatch.setenv("OMNI_HOME", ".")
    assert isinstance(ledger_appender_for_lane("lane-x"), BusReviewKeyLedgerAppender)


def test_the_probe_runs_no_subprocess_so_no_ledger_command_stands_between_it_and_the_bus() -> (
    None
):
    for path in _PROBE_NODE.rglob("*.py"):
        assert "subprocess" not in path.read_text("utf-8"), path


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
            f"hosts:\n  - name: {_HOST}\n    target: lab-host\n"
            f"    ledger_bus_lane: {_BUS_LANE}\n",
            encoding="utf-8",
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
        self.ledger = InMemoryReviewKeyLedgerAppender()
        self.lanes: list[str] = []
        ledger = self.ledger
        lanes = self.lanes

        def _appender_for_lane(lane: str, host: str | None = None) -> Any:
            lanes.append(lane)
            return ledger

        monkeypatch.setattr(
            probe_handler, "ledger_appender_for_lane", _appender_for_lane
        )

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
                *extra,
            ],
        )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ProbeWorld:
    return _ProbeWorld(tmp_path, monkeypatch)


def test_the_command_baselines_then_ledgers_a_flip_over_the_hosts_bus_lane(
    world: _ProbeWorld,
) -> None:
    world.outcomes[:] = [_ok(), _ok()]
    first = world.run()
    assert first.exit_code == 0, first.output
    assert "0 flip(s) written" in first.output
    assert world.ledger.rows == []
    world.outcomes[:] = [_fail(EnumDelegationFailureClass.PROVIDER_BILLING), _ok()]
    second = world.run()
    assert second.exit_code == 0, second.output
    assert "1 flip(s) written" in second.output
    assert world.lanes == [_BUS_LANE, _BUS_LANE]
    (row,) = world.ledger.rows
    assert "key=key-a" in row
    assert "ticket=OMN-24658" in row
    for ref in world.refs:
        assert ref not in row + first.output + second.output
    for value in ("sk-ant-fake-0-never-printed", "sk-ant-fake-1-never-printed"):
        assert value not in row + first.output + second.output


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
    assert world.ledger.rows == []


def test_the_command_exits_nonzero_when_no_key_is_bound_to_this_host(
    world: _ProbeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_secret, "_resolve_bound_host", lambda _host: "h999")
    (world.tmp / "hosts.yaml").write_text(
        f"hosts:\n  - name: h999\n    target: lab-host\n    ledger_bus_lane: {_BUS_LANE}\n",
        encoding="utf-8",
    )
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


def test_the_command_refuses_to_run_on_a_host_that_declares_no_bus_lane(
    world: _ProbeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    (world.tmp / "hosts.yaml").write_text(
        f"hosts:\n  - name: {_HOST}\n    target: lab-host\n", encoding="utf-8"
    )
    world.outcomes[:] = [_ok(), _ok()]
    result = world.run()
    assert result.exit_code != 0
    assert "ledger_bus_lane" in result.output
    assert world.requests == []
    assert world.ledger.rows == []


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


_PROBE_NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_review_key_credit_probe_effect"
)


def test_the_probe_builds_no_gateway_and_so_no_gateway_without_a_bus(
    world: _ProbeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[object] = []
    original = ClaudeReviewGateway.__init__

    def _watch(self: ClaudeReviewGateway, *args: Any, **kwargs: Any) -> None:
        built.append(kwargs.get("event_bus"))
        original(self, *args, **kwargs)

    monkeypatch.setattr(ClaudeReviewGateway, "__init__", _watch)
    world.outcomes[:] = [_ok(), _ok()]
    result = world.run()
    assert result.exit_code == 0, result.output
    assert built == []


def test_no_source_file_the_probe_touches_passes_event_bus_none() -> None:
    root = _PROBE_NODE.parents[2]
    for path in (
        *_PROBE_NODE.rglob("*.py"),
        root / "omnimarket/cli/cli_secret.py",
        root / "omnimarket/nodes/node_claude_review_gateway_effect/handlers",
    ):
        files = list(path.rglob("*.py")) if path.is_dir() else [path]
        for file in files:
            assert not re.search(r"event_bus\s*=\s*None", file.read_text("utf-8")), file


def test_the_probe_token_budget_is_declared_in_the_node_contract() -> None:
    contract = yaml.safe_load((_PROBE_NODE / "contract.yaml").read_text("utf-8"))
    assert contract["config"]["probe"]["max_tokens"] == 1
    assert load_probe_config().max_tokens == contract["config"]["probe"]["max_tokens"]
    source = (_PROBE_NODE / "handlers/handler_review_key_credit_probe.py").read_text(
        "utf-8"
    )
    assert not re.search(r"max_tokens\W{1,4}\d", source)


def test_a_budget_the_contract_declares_is_the_one_the_request_carries(
    world: _ProbeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _other_contract(node: str, section: str) -> object:
        assert (node, section) == ("node_review_key_credit_probe_effect", "config")
        return {"probe": {"max_tokens": 7, "prompt": "Say OK."}}

    monkeypatch.setattr(
        probe_handler, "load_packaged_contract_section", _other_contract
    )
    world.outcomes[:] = [_ok(), _ok()]
    result = world.run()
    assert result.exit_code == 0, result.output
    assert [request.max_tokens for request in world.requests] == [7, 7]


@pytest.mark.asyncio
async def test_the_handler_result_counts_the_flips_it_wrote(tmp_path: Path) -> None:
    ledger = InMemoryReviewKeyLedgerAppender()
    request = ModelReviewKeyCreditProbeRequest(
        tenant_id=_TENANT,
        model=_MODEL,
        host=_HOST,
        state_dir=tmp_path / "state",
    )

    async def run(call: FakeCall) -> Any:
        gateway = _gateway(call, ["key-a"])
        return await HandlerReviewKeyCreditProbe(
            catalog=gateway._catalog,
            key_values=gateway._key_values,
            call=call,
            endpoint_url=_URL,
            config=load_probe_config(),
            appender=ledger,
            clock=lambda: _NOW,
        ).handle(request)

    first = await run(FakeCall([_ok()]))
    assert first.flips_written == 0
    second = await run(
        FakeCall(
            [
                _fail(
                    EnumDelegationFailureClass.PROVIDER_BILLING,
                    "credit balance is too low",
                )
            ]
        )
    )
    assert second.flips_written == 1
    assert second.states == (("key-a", "out_of_credit"),)
