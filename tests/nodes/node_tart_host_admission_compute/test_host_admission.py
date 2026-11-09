# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tart host admission: the credential route, its grade and the receipt (OMN-23204).

A host is admitted only when the host, the tart version, the image digest and all seven probe
results are present and admitting, and the probe is dated after the go. Each named break below
flips a passing input to a failing one with its own reason.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.nodes.node_tart_host_admission_compute.handlers.handler_host_admission import (
    CREDENTIAL_ROUTE_OVERLAY_ID,
    HandlerCredentialRouteGrade,
    HandlerCredentialRoutePlan,
    HandlerHostAdmission,
    load_credential_route_overlay,
)
from omnimarket.nodes.node_tart_host_admission_compute.models.model_host_admission import (
    EnumDeliveryStepKind,
    ModelAdmissionReceipt,
    ModelAdmissionRequest,
    ModelCredentialEvidence,
    ModelCredentialGrade,
    ModelCredentialGradeRequest,
    ModelCredentialPlanRequest,
    ModelCredentialRouteSettings,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

GO = datetime(2026, 11, 8, 19, 31, 9, tzinfo=UTC)
DIGEST = "sha256:" + "ab" * 32
ROUTE_FIELDS = {
    "credentials": [
        {
            "name": "probe_key",
            "secret_ref": "ONEX_PROBE_API_KEY",
            "guest_file": "probe_key",
        },
        {
            "name": "plugin_credential",
            "secret_ref": "ONEX_PLUGIN_PROBE_CREDENTIAL",
            "guest_file": "plugin_credential",
        },
    ],
    "ramdisk_volume": "walk-cred",
    "ramdisk_mount_point": "/tmp/walk-cred",
    "ramdisk_megabytes": 4,
    "tart_path": "tart",
}


def _settings() -> ModelCredentialRouteSettings:
    return ModelCredentialRouteSettings.model_validate(ROUTE_FIELDS)


def _evidence(name: str, **update: object) -> ModelCredentialEvidence:
    base: dict[str, object] = {
        "name": name,
        "delivered_exit_code": 0,
        "guest_length": 40,
        "expected_length": 40,
        "on_ramdisk": True,
        "disk_scan_hits": 0,
        "disk_scan_control_hits": 2,
        "value_in_walk_log": False,
    }
    base.update(update)
    return ModelCredentialEvidence.model_validate(base)


def _grade(*evidence: ModelCredentialEvidence) -> ModelCredentialGrade:
    return HandlerCredentialRouteGrade().handle(
        ModelCredentialGradeRequest(
            expected_names=("probe_key", "plugin_credential"), evidence=evidence
        )
    )


def _both(**update: object) -> tuple[ModelCredentialEvidence, ...]:
    return (_evidence("probe_key", **update), _evidence("plugin_credential"))


def _admission(**update: object) -> ModelAdmissionRequest:
    base: dict[str, object] = {
        "host": "h101",
        "go_at": GO,
        "probed_at": GO + timedelta(hours=5),
        "tart_version": "2.32.1",
        "image_digest": DIGEST,
        "guest_agent": "answered",
        "guest_capture": "answered",
        "lifecycle_observer": "answered",
        "lifecycle_controls": "passed",
        "probe_credentials": "reachable_not_in_image",
        "vm_deleted": True,
    }
    base.update(update)
    return ModelAdmissionRequest.model_validate(base)


def test_credential_route_plan_names_every_credential_and_holds_no_value() -> None:
    plan = HandlerCredentialRoutePlan().handle(
        ModelCredentialPlanRequest(vm_name="walk-1", settings=_settings())
    )

    kinds = [s.kind for s in plan.steps]
    assert kinds[0] is EnumDeliveryStepKind.RAMDISK_CREATE
    assert kinds[-1] is EnumDeliveryStepKind.RAMDISK_DETACH
    delivered = [
        s for s in plan.steps if s.kind is EnumDeliveryStepKind.CREDENTIAL_DELIVER
    ]
    assert [s.stdin_credential for s in delivered] == ["probe_key", "plugin_credential"]
    assert plan.guest_mount == "/tmp/walk-cred"
    # Every delivery writes under the RAM disk mount point and reads its value from stdin (tart exec -i).
    for step in delivered:
        assert "-i" in step.argv
        assert "/tmp/walk-cred/" in step.argv[-1]
    assert "ONEX_PROBE_API_KEY" not in " ".join(" ".join(s.argv) for s in plan.steps)


def test_credential_route_plan_mounts_the_ram_disk_at_the_declared_mount_point() -> (
    None
):
    plan = HandlerCredentialRoutePlan().handle(
        ModelCredentialPlanRequest(vm_name="walk-1", settings=_settings())
    )

    create = plan.steps[0].argv[-1]
    # A RAM disk (not the data volume), formatted, mounted at the overlay's path, owned by the guest user.
    assert "ram://8192" in create
    assert "newfs_hfs -v walk-cred" in create
    assert "mount -t hfs -o nobrowse" in create
    assert create.count("/tmp/walk-cred") == 3
    # Proof and detach address the same mount point.
    assert "/tmp/walk-cred (hfs" in plan.steps[-2].argv[-1]
    assert "/tmp/walk-cred" in plan.steps[-1].argv[-1]


def test_credential_route_settings_come_from_the_overlay_with_no_packaged_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayNotFoundError):
        load_credential_route_overlay()

    install_node_overlay(
        monkeypatch, tmp_path, CREDENTIAL_ROUTE_OVERLAY_ID, ROUTE_FIELDS
    )

    assert load_credential_route_overlay().ramdisk_volume == "walk-cred"


def test_credential_route_overlay_refuses_a_value_shaped_secret_ref() -> None:
    bad = {
        **ROUTE_FIELDS,
        "credentials": [
            {
                "name": "probe_key",
                "secret_ref": "sk-live abc",
                "guest_file": "probe_key",
            }
        ],
    }

    with pytest.raises(ValidationError):
        ModelCredentialRouteSettings.model_validate(bad)


def test_credential_route_grade_reads_reachable_not_in_image_when_every_check_holds() -> (
    None
):
    grade = _grade(*_both())

    assert grade.probe_credentials == "reachable_not_in_image"
    assert grade.reasons == ()
    assert grade.status == "passed"


@pytest.mark.parametrize(
    ("update", "reason", "verdict"),
    [
        ({"delivered_exit_code": 1}, "credential-not-delivered", "not_reachable"),
        ({"guest_length": 39}, "credential-length-mismatch", "not_reachable"),
        ({"on_ramdisk": False}, "credential-not-on-ramdisk", "not_reachable"),
        ({"disk_scan_hits": 1}, "credential-in-image", "in_image"),
        ({"disk_scan_control_hits": 0}, "disk-scan-blind", "not_proven"),
        ({"value_in_walk_log": True}, "credential-in-log", "in_image"),
    ],
)
def test_credential_route_injected_break_flips_the_grade(
    update: dict[str, object], reason: str, verdict: str
) -> None:
    assert _grade(*_both()).probe_credentials == "reachable_not_in_image"

    grade = _grade(*_both(**update))

    assert grade.probe_credentials == verdict
    assert reason in grade.reasons
    assert grade.status == "failed"


def test_credential_route_a_credential_with_no_evidence_is_not_reachable() -> None:
    grade = _grade(_evidence("probe_key"))

    assert grade.probe_credentials == "not_reachable"
    assert grade.reasons == ("credential-evidence-missing",)


def test_host_admission_admits_a_host_whose_every_result_admits() -> None:
    receipt = HandlerHostAdmission().handle(_admission())

    assert receipt.admitted is True
    assert receipt.role == "tart"
    assert receipt.missing == ()
    assert receipt.failed == ()


@pytest.mark.parametrize(
    "field",
    [
        "tart_version",
        "image_digest",
        "guest_agent",
        "guest_capture",
        "lifecycle_observer",
        "lifecycle_controls",
        "probe_credentials",
        "vm_deleted",
    ],
)
def test_host_admission_a_receipt_with_a_result_missing_does_not_admit(
    field: str,
) -> None:
    receipt = HandlerHostAdmission().handle(_admission(**{field: None}))

    assert receipt.admitted is False
    assert receipt.role is None
    assert receipt.missing == (field,)


@pytest.mark.parametrize(
    ("update", "failed"),
    [
        ({"guest_agent": "timeout"}, "guest_agent"),
        ({"guest_capture": "not_answered"}, "guest_capture"),
        ({"lifecycle_observer": "not_answered"}, "lifecycle_observer"),
        ({"lifecycle_controls": "failed"}, "lifecycle_controls"),
        ({"probe_credentials": "not_proven"}, "probe_credentials"),
        ({"vm_deleted": False}, "vm_deleted"),
        ({"image_digest": "latest"}, "image_digest"),
        ({"tart_version": "tart"}, "tart_version"),
        ({"probed_at": GO - timedelta(seconds=1)}, "probed_before_go"),
        ({"probed_at": GO}, "probed_before_go"),
    ],
)
def test_host_admission_injected_break_names_the_failed_result(
    update: dict[str, object], failed: str
) -> None:
    assert HandlerHostAdmission().handle(_admission()).admitted is True

    receipt = HandlerHostAdmission().handle(_admission(**update))

    assert receipt.admitted is False
    assert receipt.failed == (failed,)
    assert receipt.status == "failed"


def test_host_admission_a_naive_time_is_refused() -> None:
    with pytest.raises(ValidationError):
        _admission(go_at=datetime(2026, 11, 8, 19, 31, 9))


def _contract_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_tart_host_admission_compute/contract.yaml"
    )


def test_host_admission_the_contract_routes_the_three_operations() -> None:
    import yaml

    contract = yaml.safe_load(_contract_path().read_text())
    routes = {e["operation"]: e for e in contract["handler_routing"]["handlers"]}

    assert set(routes) == {
        "admit_host",
        "plan_credential_route",
        "grade_credential_route",
    }
    assert routes["grade_credential_route"]["output_model"].endswith(
        ".ModelCredentialGrade"
    )


def _run_hosted(
    tmp_path: Path, request: ModelAdmissionRequest
) -> tuple[object, ModelAdmissionReceipt]:
    from tests.runtime_local_compat import RuntimeLocal

    input_path = tmp_path / "input.json"
    input_path.write_text(request.model_dump_json())
    hosted = RuntimeLocal(
        workflow_path=_contract_path(),
        state_root=tmp_path / "state",
        input_path=input_path,
        timeout=5,
    )
    result = hosted.run()
    receipt = hosted.handler_result
    assert isinstance(receipt, ModelAdmissionReceipt)
    return result, receipt


def test_host_admission_golden_chain_runtime_admits_the_host(tmp_path: Path) -> None:
    from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

    result, receipt = _run_hosted(tmp_path, _admission())

    assert result == EnumWorkflowResult.COMPLETED
    assert receipt.admitted is True


def test_host_admission_error_chain_runtime_refuses_a_host_without_lifecycle_controls(
    tmp_path: Path,
) -> None:
    from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

    result, receipt = _run_hosted(tmp_path, _admission(lifecycle_controls="failed"))

    assert result == EnumWorkflowResult.FAILED
    assert receipt.admitted is False
    assert receipt.failed == ("lifecycle_controls",)
