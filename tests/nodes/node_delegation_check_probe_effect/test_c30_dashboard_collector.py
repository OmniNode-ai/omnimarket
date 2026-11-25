# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C30 dashboard collector: a customer's path to the dashboard, graded (OMN-26044).

A bare ``onex dashboard`` keeps its one-time token to itself, so no check can learn it. The customer path is
``onex dashboard start`` (the token lands in the local credential store, an owner-only file), then a Bearer read on loopback.
A bare dashboard reads PRECONDITION-ABSENT with the reason, a started one passes, a wrong token fails, and no token value ever
reaches an observation, a clause or a run record.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from _gateway_fake import FakeClock, FakeGateway

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelChainResolution,
    ModelResponderReading,
)
from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers import (
    clean_dashboard_driver as driver,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_clean_install_machine import (
    CleanInstallCustomerMachine,
    CommandResult,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    InMemoryCheckPublisher,
    InMemoryCustomerMachine,
    InMemoryResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    GatewayRunSettings,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_dashboard_session import (
    TOKEN_UNLEARNABLE,
    grade_c30_dashboard,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
    contract_topics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    EnumCheckVerdict,
    ModelDashboardObservation,
    ModelDelegationCheckRequest,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)

SECRET = str(uuid.UUID(int=0x3030C30))
CLOCK = FakeClock()


def started(**over: Any) -> ModelDashboardObservation:
    base: dict[str, Any] = {
        "setup_rc": 0,
        "start_rc": 0,
        "token_in_store": True,
        "store_owner_only": True,
        "delegation_rc": 0,
        "delegation_correlation_id": "run-1",
        "projections_status": 200,
        "run_visible": True,
        "wrong_token_status": 401,
        "no_token_status": 401,
    }
    base.update(over)
    return ModelDashboardObservation(**base)


def bare() -> ModelDashboardObservation:
    return ModelDashboardObservation(setup_rc=0, start_rc=None, token_in_store=False)


def _verdict(obs: ModelDashboardObservation) -> EnumCheckVerdict:
    clauses, preconditions = grade_c30_dashboard(obs)
    from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
        ModelDelegationCheckReading,
    )

    return ModelDelegationCheckReading(
        clauses=clauses, preconditions=preconditions
    ).verdict()


# ----- the grader ---------------------------------------------------------------------------------------------------


def test_a_bare_dashboard_is_precondition_absent_with_the_reason() -> None:
    clauses, preconditions = grade_c30_dashboard(bare())
    assert _verdict(bare()) is EnumCheckVerdict.PRECONDITION_ABSENT
    absent = [p for p in preconditions if not p.present]
    assert [p.name for p in absent] == ["dashboard_token_learnable"]
    assert absent[0].detail == TOKEN_UNLEARNABLE
    assert "onex dashboard start" in TOKEN_UNLEARNABLE
    assert clauses == ()


def test_a_started_dashboard_passes_every_clause() -> None:
    clauses, preconditions = grade_c30_dashboard(started())
    assert all(p.present for p in preconditions)
    assert {c.name for c in clauses} == {
        "dashboard_started_by_customer_command",
        "store_token_owner_only",
        "delegation_ran_once",
        "projections_read_with_store_token",
        "run_visible_on_projections",
        "wrong_token_refused",
        "absent_token_refused",
    }
    assert all(c.ok for c in clauses), [c for c in clauses if not c.ok]
    assert _verdict(started()) is EnumCheckVerdict.PASS


def test_a_wrong_token_fails_the_read_and_the_run_is_not_visible() -> None:
    obs = started(projections_status=401, run_visible=False)
    failed = {c.name for c in grade_c30_dashboard(obs)[0] if not c.ok}
    assert {"projections_read_with_store_token", "run_visible_on_projections"} <= failed
    assert _verdict(obs) is EnumCheckVerdict.FAIL


def test_a_dashboard_that_accepts_a_wrong_or_absent_token_fails() -> None:
    for over in ({"wrong_token_status": 200}, {"no_token_status": 200}):
        assert _verdict(started(**over)) is EnumCheckVerdict.FAIL


def test_a_group_readable_store_fails() -> None:
    assert _verdict(started(store_owner_only=False)) is EnumCheckVerdict.FAIL


def test_a_start_that_failed_with_a_stored_token_fails_not_absent() -> None:
    assert _verdict(started(start_rc=1)) is EnumCheckVerdict.FAIL


def test_a_setup_that_failed_fails() -> None:
    assert _verdict(started(setup_rc=2, start_rc=None)) is EnumCheckVerdict.FAIL


# ----- the in-machine driver ----------------------------------------------------------------------------------------


def test_the_driver_record_never_carries_the_token() -> None:
    record = driver.build_record(
        setup=_ok(),
        start=_ok(
            json.dumps({"status": "success", "evidence": {"url": "http://127.0.0.1:9"}})
        ),
        delegation=_ok(json.dumps({"correlation_id": "run-1"})),
        store_token=SECRET,
        store_mode=0o600,
        reads={
            "bearer": (
                200,
                {
                    "topics": [
                        {"topic": "t", "table": "delegation_events", "status": "ok"}
                    ]
                },
            ),
            "run": (200, {"rows": [{"correlation_id": "run-1"}]}),
            "wrong": (401, {}),
            "absent": (401, {}),
        },
    )
    assert SECRET not in json.dumps(record)
    assert record["token_in_store"] is True
    assert record["store_owner_only"] is True
    assert record["run_visible"] is True


def test_the_driver_reads_a_group_readable_store_as_not_owner_only() -> None:
    record = driver.build_record(
        setup=_ok(),
        start=_ok("{}"),
        delegation=_ok("{}"),
        store_token=SECRET,
        store_mode=0o644,
        reads={"bearer": (200, {"topics": []})},
    )
    assert record["store_owner_only"] is False


def test_the_driver_reports_no_token_when_the_store_holds_none() -> None:
    record = driver.build_record(
        setup=_ok(),
        start=None,
        delegation=None,
        store_token=None,
        store_mode=None,
        reads={},
    )
    assert record["token_in_store"] is False
    assert record["start_rc"] is None


def _ok(stdout: str = "") -> dict[str, Any]:
    return {"rc": 0, "stdout": stdout, "stderr": ""}


# ----- the collector and the handler --------------------------------------------------------------------------------


class _Runner:
    def __init__(self, document: Mapping[str, Any], install_rc: int = 0) -> None:
        self.calls: list[tuple[Sequence[str], Mapping[str, str]]] = []
        self._document = document
        self._install_rc = install_rc

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
        stdin: str | None = None,
    ) -> CommandResult:
        self.calls.append((list(argv), dict(env)))
        if argv[0] == "uv":
            return CommandResult(self._install_rc, "", "")
        return CommandResult(0, json.dumps(self._document), "")


class _Sources:
    def guide(self) -> tuple[str, str]:
        return (
            "guide.md",
            "```\nuv tool install --python 3.12 'omnibase-core>=0.46.8'\n```\n",
        )

    def registry(self) -> Mapping[str, Any]:
        return {}


BASE = "http://127.0.0.12:8000"
RESPONDER = ModelChainResolution(
    tier="local",
    base_url=BASE,
    served_model="served-model",
    window=8192,
    attempts=(),
)


def _uv(tmp_path: Path) -> str:
    path = tmp_path / "uv"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def test_the_collector_runs_the_guide_install_in_a_clean_profile_and_reads_the_record(
    tmp_path: Path,
) -> None:
    runner = _Runner(
        driver.build_record(
            setup=_ok(),
            start=_ok("{}"),
            delegation=_ok(json.dumps({"correlation_id": "run-1"})),
            store_token=SECRET,
            store_mode=0o600,
            reads={
                "bearer": (
                    200,
                    {
                        "topics": [
                            {"topic": "t", "table": "delegation_events", "status": "ok"}
                        ]
                    },
                ),
                "run": (200, {"rows": [{"correlation_id": "run-1"}]}),
                "wrong": (401, {}),
                "absent": (401, {}),
            },
        )
    )
    machine = CleanInstallCustomerMachine(
        sources=_Sources(), runner=runner, uv=_uv(tmp_path), host_env={}
    )
    obs = machine.collect_dashboard(RESPONDER)
    assert obs.token_in_store
    assert obs.projections_status == 200
    assert obs.run_visible
    install_argv, env = runner.calls[0]
    assert "uv" in install_argv[0]
    assert env["HOME"].endswith("/home")
    driver_argv, _ = runner.calls[1]
    assert "--url" in driver_argv
    assert RESPONDER.base_url in driver_argv
    assert SECRET not in repr(obs)


def test_a_failed_install_is_a_failed_setup_not_an_absent_instrument(
    tmp_path: Path,
) -> None:
    machine = CleanInstallCustomerMachine(
        sources=_Sources(),
        runner=_Runner({}, install_rc=1),
        uv=_uv(tmp_path),
        host_env={},
    )
    obs = machine.collect_dashboard(RESPONDER)
    assert obs.install_rc == 1
    assert _verdict(obs) is EnumCheckVerdict.FAIL


def test_a_driver_with_no_record_is_an_absent_collector(tmp_path: Path) -> None:
    class Silent(_Runner):
        def run(
            self,
            argv: Sequence[str],
            *,
            env: Mapping[str, str],
            cwd: Path,
            timeout_s: float,
            stdin: str | None = None,
        ) -> CommandResult:
            if argv[0] == "uv":
                return CommandResult(0, "", "")
            return CommandResult(1, "", "boom")

    machine = CleanInstallCustomerMachine(
        sources=_Sources(), runner=Silent({}), uv=_uv(tmp_path), host_env={}
    )
    with pytest.raises(CollectorAbsentError) as raised:
        machine.collect_dashboard(RESPONDER)
    assert raised.value.name == "clean_dashboard_driver"


class _DashMachine(InMemoryCustomerMachine):
    def __init__(self, obs: ModelDashboardObservation) -> None:
        super().__init__()
        self.obs = obs

    def collect_dashboard(
        self, responder: ModelChainResolution
    ) -> ModelDashboardObservation:
        return self.obs


def _handler(machine: Any) -> HandlerDelegationCheckProbe:
    return HandlerDelegationCheckProbe(
        responders=(ModelDelegationCheckResponder(tier="local", base_url=BASE),),
        reader=InMemoryResponderReader(
            {
                BASE: ModelResponderReading(
                    base_url=BASE,
                    answered=True,
                    served_model="served-model",
                    max_model_len=8192,
                )
            }
        ),
        machine=machine,
        publisher=InMemoryCheckPublisher(),
        topic=contract_topics()[0],
        gateway=FakeGateway(),
        gateway_settings=GatewayRunSettings(run_budget_s=5.0, poll_interval_s=0.0),
        sleep=CLOCK.sleep,
        monotonic=CLOCK.monotonic,
    )


def _c30() -> ModelDelegationCheckRequest:
    return ModelDelegationCheckRequest(correlation_id="c30-test", check_id="C30")


def test_c30_without_a_dashboard_collector_keeps_the_gateway_reading() -> None:
    result = _handler(InMemoryCustomerMachine()).handle(_c30())
    assert result.verdict is EnumCheckVerdict.PASS


def test_c30_with_a_bare_dashboard_reads_precondition_absent_with_the_reason() -> None:
    result = _handler(_DashMachine(bare())).handle(_c30())
    assert result.verdict is EnumCheckVerdict.PRECONDITION_ABSENT
    assert [p.detail for p in result.preconditions if not p.present] == [
        TOKEN_UNLEARNABLE
    ]


def test_c30_with_a_started_dashboard_passes_and_adds_its_clauses() -> None:
    result = _handler(_DashMachine(started())).handle(_c30())
    assert result.verdict is EnumCheckVerdict.PASS
    assert "run_visible_on_projections" in {c.name for c in result.clauses}


def test_c30_with_a_wrong_token_fails() -> None:
    result = _handler(
        _DashMachine(started(projections_status=401, run_visible=False))
    ).handle(_c30())
    assert result.verdict is EnumCheckVerdict.FAIL


def test_the_driver_reads_a_degraded_listing_of_the_run_table_as_not_visible() -> None:
    record = driver.build_record(
        setup=_ok(),
        start=_ok("{}"),
        delegation=_ok(json.dumps({"correlation_id": "run-1"})),
        store_token=SECRET,
        store_mode=0o600,
        reads={"bearer": (200, {"topics": []}), "run": (503, {})},
    )
    assert record["run_visible"] is False
