# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the host drift orchestrator (OMN-24197): each fails when either side changes shape.

S1 orchestrator -> lab-work serve: the command and terminal topics, the unit model, the executable allow list, every receipt status.
S2 lab-work serve -> orchestrator: the receipt fields the verdict reads exist on the receipt model.
S3 orchestrator -> the drift check: the module and flags the argv names are accepted by the check's entry point, and its
   output lines are the ones the verdict reads.
S4 orchestrator -> schedule verdict: the schedule run topic the reducer consumes.
S5 orchestrator -> run readers: the run model and topics the contract publishes.
"""

from __future__ import annotations

import importlib
import importlib.util
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.models import model_host_drift_orchestrator as run_models
from omnimarket.models.model_host_drift_orchestrator import (
    EnumHostDriftHostVerdict,
    ModelHostDriftOrchestratorOverlay,
)
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.dispatch_host_drift import (
    DispatchHostDriftUnits,
    verdict_from_receipt,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.sender_in_memory import (
    InMemoryHostDriftSender,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes"
NODE = "node_host_drift_orchestrator"


def _contract(node: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODES / node / "contract.yaml").read_text()
    )
    return loaded


ORCHESTRATOR = _contract(NODE)
OVERLAY = ModelHostDriftOrchestratorOverlay.model_validate(
    yaml.safe_load((NODES / NODE / "overlays" / "in_memory.yaml").read_text())
)


def _acl_names(operation: str) -> set[str]:
    rows = ORCHESTRATOR["broker_acl_grants"][0]["grants"]
    return {
        r["name"]
        for r in rows
        if r["resource_type"] == "TOPIC" and r["operation"] == operation
    }


def test_s1_the_acl_rows_name_the_topics_of_the_lab_work_serve() -> None:
    serve = _contract("node_lab_work_unit_effect")
    command = serve["runtime_dispatch"]["command_topic"]
    terminals = set(serve["runtime_dispatch"]["terminal_events"].values())
    assert command in _acl_names("WRITE")
    assert terminals <= _acl_names("READ")


def test_s1_a_built_unit_is_a_valid_lab_work_unit_with_an_allowed_executable() -> None:
    sender = InMemoryHostDriftSender()
    DispatchHostDriftUnits(OVERLAY, sender).dispatch(OVERLAY.hosts)

    from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_lab_work_unit_effect import (
        DEFAULT_ALLOWED_EXECUTABLES,
    )

    assert len(sender.sent) == len(OVERLAY.hosts)
    for unit in sender.sent:
        assert ModelLabWorkUnitRequest.model_validate(unit.model_dump())
        assert Path(unit.argv[0]).name in DEFAULT_ALLOWED_EXECUTABLES


EXPECTED_VERDICT = {
    EnumLabWorkUnitStatus.COMPLETED: EnumHostDriftHostVerdict.IN_SYNC,
    EnumLabWorkUnitStatus.TIMED_OUT: EnumHostDriftHostVerdict.NO_RESULT,
    EnumLabWorkUnitStatus.REFUSED: EnumHostDriftHostVerdict.REFUSED,
    EnumLabWorkUnitStatus.INFRA_ERROR: EnumHostDriftHostVerdict.NO_RESULT,
}


@pytest.mark.parametrize("status", list(EnumLabWorkUnitStatus))
def test_s1_every_receipt_status_of_the_serve_maps_to_a_typed_verdict(
    status: EnumLabWorkUnitStatus,
) -> None:
    host = OVERLAY.hosts[0].name
    sender = InMemoryHostDriftSender(
        {host: (status, 0, f"VERDICT {host}: IN_SYNC findings=0")}
    )
    (verdict, *_) = DispatchHostDriftUnits(OVERLAY, sender).dispatch(OVERLAY.hosts)
    assert verdict.verdict is EXPECTED_VERDICT[status]


def test_s2_the_receipt_fields_the_verdict_reads_exist_on_the_receipt_model() -> None:
    fields = ModelLabWorkUnitReceipt.model_fields
    for name in ("status", "exit_code", "output_tail", "detail"):
        assert name in fields, name
    receipt = ModelLabWorkUnitReceipt(
        work_unit_id="hd-00000001",
        target_host="h",
        host="h",
        repo="o/r",
        commit_sha="0" * 40,
        status=EnumLabWorkUnitStatus.COMPLETED,
        exit_code=0,
        output_tail="VERDICT h: IN_SYNC findings=0",
    )
    assert (
        verdict_from_receipt("h", "hd-00000001", receipt).verdict
        is EnumHostDriftHostVerdict.IN_SYNC
    )


def _module_and_flags(argv: tuple[str, ...]) -> tuple[str, list[str]]:
    index = argv.index("-m")
    return argv[index + 1], [w for w in argv[index + 2 :] if w.startswith("--")]


def test_s3_the_argv_module_exists_and_its_entry_point_accepts_every_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, flags = _module_and_flags(OVERLAY.check.argv)
    assert importlib.util.find_spec(module) is not None, module
    entry = importlib.import_module(module)
    with pytest.raises(SystemExit) as stop:
        entry.main(["--help"])
    assert stop.value.code == 0
    usage = capsys.readouterr().out
    assert flags
    for flag in flags:
        assert flag in usage, flag


@pytest.mark.parametrize(
    ("ok", "exit_code", "verdict"),
    [
        (True, 0, EnumHostDriftHostVerdict.IN_SYNC),
        (False, 1, EnumHostDriftHostVerdict.DRIFTED),
    ],
)
def test_s3_the_check_output_is_what_the_verdict_reads(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    ok: bool,
    exit_code: int,
    verdict: EnumHostDriftHostVerdict,
) -> None:
    module, _ = _module_and_flags(OVERLAY.check.argv)
    entry = importlib.import_module(module)
    host = OVERLAY.hosts[0].name

    class _Profile:
        def model_copy(self, **_: Any) -> _Profile:
            return self

    profile = _Profile()

    class _Check:
        def __init__(self, _shell: object) -> None:
            pass

        def handle(self, _request: object) -> object:
            return types.SimpleNamespace(host=host, ok=ok, findings=())

    monkeypatch.setattr(entry, "parse_host_profile", lambda _text, _host: profile)
    monkeypatch.setattr(entry, "HandlerHostDriftCheck", _Check)
    monkeypatch.setattr(entry, "HandlerHostShell", lambda: object())
    monkeypatch.setattr(
        entry, "ModelHostDriftCheckRequest", lambda **_: types.SimpleNamespace()
    )
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text("hosts: {}\n")

    code = entry.main(["--host", host, "--profiles", str(profiles), "--local"])

    assert code == exit_code
    tail = capsys.readouterr().out
    receipt = ModelLabWorkUnitReceipt(
        work_unit_id="hd-00000001",
        target_host=host,
        host=host,
        repo="o/r",
        commit_sha="0" * 40,
        status=EnumLabWorkUnitStatus.COMPLETED,
        exit_code=code,
        output_tail=tail,
    )
    assert verdict_from_receipt(host, "hd-00000001", receipt).verdict is verdict


def test_s4_the_schedule_run_topic_is_the_one_the_schedule_reducer_consumes() -> None:
    reducer = _contract("node_schedule_run_reducer")
    published = ORCHESTRATOR["event_bus"]["publish_topics"]
    assert any(topic in reducer["event_bus"]["subscribe_topics"] for topic in published)
    assert (
        ScheduleGate(NODE, declaration=OVERLAY).declaration().job_id == OVERLAY.job_id
    )


def test_s5_the_published_events_name_models_that_exist_and_the_two_topics() -> None:
    assert ORCHESTRATOR["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.host-drift-run-completed.v1",
        "onex.evt.omnimarket.schedule-run.v1",
    ]
    assert (
        ORCHESTRATOR["terminal_event"] == ORCHESTRATOR["event_bus"]["publish_topics"][0]
    )
    assert hasattr(run_models, ORCHESTRATOR["published_events"][0]["event_type"])
    overlay_spec = ORCHESTRATOR["runtime"]["schedule_overlay"]["model"]
    module, _, name = overlay_spec.rpartition(".")
    assert (
        getattr(importlib.import_module(module), name)
        is ModelHostDriftOrchestratorOverlay
    )
