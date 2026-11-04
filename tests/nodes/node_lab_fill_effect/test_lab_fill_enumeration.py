# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A bounded enumeration answers UNREAD instead of timing out silently."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import time
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_enumeration import (
    HandlerLabFillEnumeration,
)
from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_enumeration import (
    ModelLabFillEnumerationRequest,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
    HandlerLabFillCandidateChoice,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillCandidateChoiceRequest,
    ModelLabFillPlanConfig,
)

FIXTURE = (
    Path(__file__).parents[2] / "fixtures/lab_fill_enumeration/recorded_timeout.json"
)


def _request(program: str, **kwargs: object) -> ModelLabFillEnumerationRequest:
    values: dict[str, object] = {
        "reader_argv": (sys.executable, "-c", program),
        "kinds": ("ticket",),
        "read_budget_s": 2.0,
        "outer_deadline_epoch_s": time.time() + 5,
        "return_margin_s": 1.0,
    }
    values.update(kwargs)
    return ModelLabFillEnumerationRequest.model_validate(values)


def _snapshot(**kwargs: object) -> dict[str, object]:
    values: dict[str, object] = {
        "source": "linear",
        "candidates": [],
        "fenced_tickets": [],
        "source_diagnostics": {"ticket": {"read": "empty", "seen": 0, "eligible": 0}},
    }
    values.update(kwargs)
    return values


def _print(snapshot: dict[str, object]) -> str:
    return f"print({json.dumps(snapshot)!r})"


def test_recorded_exit_124_no_json_no_stderr_is_unread() -> None:
    recorded = json.loads(FIXTURE.read_text())
    assert recorded["free_slots"] == 19
    result = HandlerLabFillEnumeration().handle(
        _request(
            f"raise SystemExit({recorded['returncode']})",
            kinds=recorded["kinds"],
            source=recorded["source"],
        )
    )
    assert result.outcome == "UNREAD"
    assert result.reader_returncode == 124
    assert result.candidates == ()
    assert result.read_error == "reader-exited:124; no-json; stderr-empty"
    assert result.enumeration_read.outcome == "failed"
    assert result.enumeration_read.empty_is_evidence is False
    assert set(result.source_diagnostics) == set(recorded["kinds"])
    assert all(
        d.read.startswith("unreadable:") for d in result.source_diagnostics.values()
    )
    assert result.finished_utc
    assert result.model_dump_json()
    chosen = HandlerLabFillCandidateChoice().handle(
        ModelLabFillCandidateChoiceRequest(
            enumerated=result.model_dump(mode="json"),
            config=ModelLabFillPlanConfig(
                parent_ticket="OMN-19998", milestone="test", run_key="2026-11-03T1950Z"
            ),
        )
    )
    assert chosen.candidates == ()
    assert chosen.source == "enumerate-partial"
    assert chosen.source_diagnostics["ticket"]["read"].startswith("unreadable:")


def test_complete_empty_read_is_positive_evidence() -> None:
    result = HandlerLabFillEnumeration().handle(_request(_print(_snapshot())))
    assert result.outcome == "READ"
    assert result.enumeration_read.outcome == "complete"
    assert result.enumeration_read.empty_is_evidence is True
    assert not result.truncated
    assert not result.read_error


def test_complete_read_preserves_candidates_fences_and_auxiliary_facts() -> None:
    snapshot = _snapshot(
        candidates=[{"kind": "ticket", "ticket": "OMN-19998", "repo": "omnimarket"}],
        fenced_tickets=["OMN-19998"],
        source_diagnostics={"ticket": {"read": "ok", "seen": 1, "eligible": 1}},
        filtered={"owned": 1},
        open_count_gate={"gated": True, "trend": "growing"},
    )
    result = HandlerLabFillEnumeration().handle(_request(_print(snapshot)))
    assert result.outcome == "READ"
    wire = result.model_dump(mode="json")
    assert wire["candidates"] == snapshot["candidates"]
    assert wire["fenced_tickets"] == snapshot["fenced_tickets"]
    assert wire["filtered"] == snapshot["filtered"]
    assert wire["open_count_gate"] == snapshot["open_count_gate"]


def test_startup_and_unbounded_local_read_share_the_full_budget(tmp_path: Path) -> None:
    marker = tmp_path / "late"
    program = f"import time; time.sleep(5); open({str(marker)!r}, 'w').write('late')"
    started = time.monotonic()
    result = HandlerLabFillEnumeration().handle(_request(program, read_budget_s=0.15))
    assert time.monotonic() - started < 2
    assert result.outcome == "UNREAD"
    assert result.read_error.startswith("read-deadline-exceeded")
    assert not marker.exists()
    assert not result.enumeration_read.empty_is_evidence


def test_absolute_outer_deadline_caps_the_read_without_a_floor() -> None:
    started = time.monotonic()
    result = HandlerLabFillEnumeration().handle(
        _request(
            "import time; time.sleep(5)",
            read_budget_s=180,
            outer_deadline_epoch_s=time.time() + 0.5,
            return_margin_s=0.3,
        )
    )
    assert time.monotonic() - started < 2
    assert result.outcome == "UNREAD"
    assert result.read_error.startswith("read-deadline-exceeded")


def test_no_remaining_budget_never_starts_the_reader(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    result = HandlerLabFillEnumeration().handle(
        _request(
            f"open({str(marker)!r}, 'w').write('started')",
            outer_deadline_epoch_s=time.time() - 1,
        )
    )
    assert result.outcome == "UNREAD"
    assert result.reader_returncode is None
    assert not marker.exists()


@pytest.mark.parametrize(
    "program", ["pass", "print('not json')", "print('[]')", "print('{}')"]
)
def test_missing_or_malformed_json_is_not_an_empty_queue(program: str) -> None:
    result = HandlerLabFillEnumeration().handle(_request(program))
    assert result.outcome == "UNREAD"
    assert result.candidates == ()
    assert not result.enumeration_read.empty_is_evidence
    assert result.read_error


def test_failed_command_cannot_smuggle_candidates_in_valid_json() -> None:
    snapshot = _snapshot(candidates=[{"kind": "ticket", "ticket": "OMN-19998"}])
    result = HandlerLabFillEnumeration().handle(
        _request(_print(snapshot) + "; raise SystemExit(124)")
    )
    assert result.outcome == "UNREAD"
    assert result.candidates == ()


def test_unread_kind_never_selects_work_and_partial_alarm_survives() -> None:
    snapshot = _snapshot(
        candidates=[
            {"kind": "ticket", "ticket": "OMN-19998"},
            {"kind": "wiring", "ticket": "OMN-19998"},
        ],
        source_diagnostics={
            "ticket": {
                "read": "unreadable:request timed out",
                "seen": 1,
                "eligible": 1,
            },
            "wiring": {"read": "ok", "seen": 1, "eligible": 1},
        },
    )
    result = HandlerLabFillEnumeration().handle(
        _request(_print(snapshot), kinds=("ticket", "wiring"))
    )
    assert result.outcome == "PARTIAL"
    assert [c["kind"] for c in result.candidates] == ["wiring"]
    assert result.source_diagnostics["ticket"].eligible == 0
    assert not result.enumeration_read.empty_is_evidence
    assert result.enumeration_read.failed_reads


def test_missing_kind_diagnostic_is_unread() -> None:
    result = HandlerLabFillEnumeration().handle(
        _request(_print(_snapshot()), kinds=("ticket", "wiring"))
    )
    assert result.outcome == "PARTIAL"
    assert result.source_diagnostics["wiring"].read == "unreadable:missing-diagnostic"
    assert not result.enumeration_read.empty_is_evidence


def test_child_cannot_claim_complete_over_failed_diagnostics() -> None:
    snapshot = _snapshot(
        source_diagnostics={"ticket": {"read": "unreadable:timeout", "seen": 0}},
        enumeration_read={"outcome": "complete", "empty_is_evidence": True},
    )
    result = HandlerLabFillEnumeration().handle(_request(_print(snapshot)))
    assert result.outcome == "UNREAD"
    assert not result.enumeration_read.empty_is_evidence


def test_oversized_result_is_bounded() -> None:
    result = HandlerLabFillEnumeration().handle(
        _request("print('x' * 100000)", max_output_bytes=1024)
    )
    assert result.outcome == "UNREAD"
    assert result.read_error == "reader-output-limit-exceeded"


def test_stdin_and_stderr_are_handled_without_shell_expansion(tmp_path: Path) -> None:
    # The literal shell syntax reaches stdin; it must never execute on the host.
    marker = tmp_path / "shell"
    payload = _snapshot(notes=f"$(touch {marker})")
    result = HandlerLabFillEnumeration().handle(
        _request(
            "import sys; print(sys.stdin.read())", reader_stdin=json.dumps(payload)
        )
    )
    assert result.outcome == "READ"
    assert result.notes == payload["notes"]
    assert not marker.exists()


def test_reader_failure_retains_stderr_diagnostic() -> None:
    result = HandlerLabFillEnumeration().handle(
        _request(
            "import sys; print('reader refused', file=sys.stderr); raise SystemExit(2)"
        )
    )
    assert result.outcome == "UNREAD"
    assert result.reader_stderr == "reader refused\n"
    assert "stderr-empty" not in result.read_error


def test_reader_failure_redacts_environment_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENUMERATION_TEST_API_KEY", "fixture-sensitive-value")
    result = HandlerLabFillEnumeration().handle(
        _request(
            "import os, sys; print(os.environ['ENUMERATION_TEST_API_KEY'], file=sys.stderr); raise SystemExit(2)"
        )
    )
    assert "fixture-sensitive-value" not in result.model_dump_json()
    assert "[redacted]" in result.reader_stderr


def test_contract_routes_enumeration_to_the_typed_handler() -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_lab_fill_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    entry = next(
        e
        for e in contract["handler_routing"]["handlers"]
        if e["operation"] == "read_lab_fill_enumeration"
    )
    module = importlib.import_module(entry["handler"]["module"])
    assert getattr(module, entry["handler"]["name"]) is HandlerLabFillEnumeration
    assert entry["input_model"].endswith("ModelLabFillEnumerationRequest")
    assert entry["output_model"].endswith("ModelLabFillEnumerationResult")
    assert contract["runtime_dispatch"]["command_topic"]
    assert contract["runtime_dispatch"]["terminal_events"]["success"]


@pytest.mark.parametrize("budget", [float("nan"), float("inf"), -1, 0])
def test_non_finite_or_non_positive_budgets_are_refused(budget: float) -> None:
    with pytest.raises(ValidationError):
        _request("pass", read_budget_s=budget)


def test_unknown_candidate_kind_is_never_selected() -> None:
    result = HandlerLabFillEnumeration().handle(
        _request(
            _print(
                _snapshot(candidates=[{"kind": "unreported", "ticket": "OMN-19998"}])
            )
        )
    )
    assert result.outcome == "UNREAD"
    assert result.candidates == ()


@pytest.mark.parametrize("declared", ["failed", "partial"])
def test_reader_declared_failure_cannot_be_upgraded(declared: str) -> None:
    snapshot = _snapshot(
        enumeration_read={
            "outcome": declared,
            "empty_is_evidence": False,
            "detail": "source-not-complete",
        },
    )
    result = HandlerLabFillEnumeration().handle(_request(_print(snapshot)))
    assert result.outcome == ("UNREAD" if declared == "failed" else "PARTIAL")
    assert not result.enumeration_read.empty_is_evidence


def test_empty_kind_cannot_supply_candidates() -> None:
    snapshot = _snapshot(candidates=[{"kind": "ticket", "ticket": "OMN-19998"}])
    result = HandlerLabFillEnumeration().handle(_request(_print(snapshot)))
    assert result.outcome == "UNREAD"
    assert result.candidates == ()
    assert result.read_error == "reader-empty-kind-has-candidates"


def test_inherited_output_descriptors_do_not_delay_return_or_leave_a_child(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "child.pid"
    program = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"open({str(pid_file)!r}, 'w').write(str(child.pid)); " + _print(_snapshot())
    )
    started = time.monotonic()
    result = HandlerLabFillEnumeration().handle(_request(program))
    assert result.outcome == "READ"
    assert time.monotonic() - started < 2
    pid = int(pid_file.read_text())
    child = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    assert not child.stdout.strip() or child.stdout.lstrip().startswith("Z")


def test_recorded_unread_result_preserves_the_existing_idle_alarm() -> None:
    from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_status import (
        HandlerLabFillStatus,
    )
    from omnimarket.nodes.node_lab_fill_plan_compute.models.model_lab_fill_status import (
        ModelLabFillStatusRequest,
    )

    recorded = json.loads(FIXTURE.read_text())
    enumeration = HandlerLabFillEnumeration().handle(
        _request(
            "raise SystemExit(124)", kinds=recorded["kinds"], source=recorded["source"]
        )
    )
    config = {
        "pillar": "lab",
        "parent_lane": "pfix-32-r-1405",
        "parent_ticket": "OMN-19998",
        "run_key": recorded["run"],
        "authority_ruling": "2026-11-03T18:40:27Z",
        "authority_lane": "orchestrator-31d9",
    }
    result = HandlerLabFillStatus().handle(
        ModelLabFillStatusRequest.model_validate(
            {
                "config": config,
                "capacity": {
                    "hosts": [
                        {
                            "name": "h053",
                            "lanes": recorded["free_slots"],
                            "running": 0,
                            "idle_lanes": recorded["free_slots"],
                            "runner_slots": recorded["free_slots"],
                            "cap_bound": False,
                            "codex": True,
                            "claude": True,
                            "load_per_core": 0.0,
                            "reason": "",
                        }
                    ],
                    "free": recorded["free_slots"],
                    "budget": recorded["free_slots"],
                },
                "selection": {"budget": recorded["free_slots"]},
                "candidates": {
                    "source": "enumerate-partial",
                    "source_diagnostics": enumeration.model_dump(mode="json")[
                        "source_diagnostics"
                    ],
                },
                "observed_at": recorded["finished_utc"],
            }
        )
    )
    assert result.idle["idle_alarm"] is True
    assert result.idle["friction_cells"]
    assert "unreadable:" in result.idle["friction_cells"]


@pytest.mark.parametrize(
    ("program", "expected"),
    [("raise SystemExit(124)", "UNREAD"), (_print(_snapshot()), "READ")],
)
async def test_bus_adapter_publishes_terminal_on_the_declared_topic(
    program: str, expected: str
) -> None:
    import asyncio

    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

    from tests.runtime_local_compat import LocalRuntimeBusAdapter

    contract = yaml.safe_load(
        files("omnimarket.nodes.node_lab_fill_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    entry = next(
        e
        for e in contract["handler_routing"]["handlers"]
        if e["operation"] == "read_lab_fill_enumeration"
    )
    handler_type = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    module, _, name = entry["input_model"].rpartition(".")
    request_type = getattr(importlib.import_module(module), name)
    command_topic = contract["runtime_dispatch"]["command_topic"]
    completed_topic = contract["runtime_dispatch"]["terminal_events"]["success"]
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    bus = EventBusInmemory(environment="local", group="enumeration-boundary")
    await bus.start()

    async def collect(message: ModelEventMessage) -> None:
        await queue.put(json.loads(message.value))

    adapter = LocalRuntimeBusAdapter(
        handler_type(), entry["handler"]["name"], request_type, completed_topic, bus
    )
    try:
        await bus.subscribe(
            command_topic, group_id="enumeration-reader", on_message=adapter.on_message
        )
        await bus.subscribe(
            completed_topic, group_id="enumeration-results", on_message=collect
        )
        await bus.publish(
            command_topic, None, _request(program).model_dump_json().encode()
        )
        terminal = await asyncio.wait_for(queue.get(), timeout=5)
        assert terminal["outcome"] == expected
        assert terminal["enumeration_read"]["empty_is_evidence"] is (expected == "READ")
    finally:
        await bus.close()
