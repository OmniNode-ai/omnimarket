# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Platform support is shared; selected capability owners check real requirements."""

from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_preflight import (
    HandlerSetupPreflight,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_machine_facts import (
    ModelSetupMachineFacts,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit
GIB = 1024**3


def facts(**changes: object) -> ModelSetupMachineFacts:
    return ModelSetupMachineFacts.model_validate(
        {
            "platform": "Darwin",
            "architecture": "arm64",
            "macos_version": "26.1",
            "cpus": 10,
            "ram_bytes": 32 * GIB,
            "free_disk_bytes": 50 * GIB,
            "administrator": True,
            "virtual_machine": False,
            "rosetta": False,
            "docker_installed": False,
            "stack_running": False,
            "occupied_ports": [],
            "missing_tools": [],
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("changes", "remedy"),
    [
        ({"platform": "Linux"}, "macOS"),
        ({"macos_version": "13.6"}, "14"),
        ({"architecture": "aarch32"}, "architecture"),
        ({"rosetta": True}, "native"),
    ],
)
def test_native_refuses_failed_requirement(
    changes: dict[str, object], remedy: str, tmp_path: Path
) -> None:
    handler = HandlerSetupPreflight(collect=lambda _: facts(**changes))
    result = handler.handle(ModelSetupPlan(workspace=tmp_path))
    assert result.status != "success"
    assert remedy in (result.fix or "")
    assert not list(tmp_path.iterdir())


def test_native_vm_needs_no_docker_or_free_ports(tmp_path: Path) -> None:
    result = HandlerSetupPreflight(
        collect=lambda _: facts(virtual_machine=True, occupied_ports=[5436])
    ).handle(ModelSetupPlan(workspace=tmp_path))
    assert result.status == "success"


@pytest.mark.parametrize(
    "changes",
    [
        {"virtual_machine": True},
        {"cpus": 4},
        {"ram_bytes": 8 * GIB},
        {"free_disk_bytes": 29 * GIB},
        {"occupied_ports": [5436]},
        {"administrator": False, "missing_tools": ["git", "brew", "gh"]},
    ],
)
@pytest.mark.parametrize("component", ["local-stack", "docker"])
def test_component_name_never_activates_retired_blanket_floors(
    changes: dict[str, object], tmp_path: Path, component: str
) -> None:
    result = HandlerSetupPreflight(collect=lambda _: facts(**changes)).handle(
        ModelSetupPlan(workspace=tmp_path, components=(component,))
    )
    assert result.status == "success", result.fix
    assert set(result.evidence) == {
        "platform",
        "architecture",
        "macos_version",
        "rosetta",
        "virtual_machine",
    }


def test_selected_stack_owns_its_ports_and_allocation_checks(tmp_path: Path) -> None:
    result = HandlerSetupPreflight(
        collect=lambda _: facts(
            docker_installed=True,
            stack_running=True,
            free_disk_bytes=20 * GIB,
            occupied_ports=[5436],
        )
    ).handle(ModelSetupPlan(workspace=tmp_path, components=("local-stack",)))
    assert result.status == "success"


def test_endpoint_client_does_not_require_developer_tools(tmp_path: Path) -> None:
    result = HandlerSetupPreflight(
        collect=lambda _: facts(missing_tools=["gh", "uv"])
    ).handle(ModelSetupPlan(workspace=tmp_path))
    assert result.status == "success"
    assert "missing_tools" not in result.evidence


def test_probe_error_is_not_a_success_or_zero_measurement(tmp_path: Path) -> None:
    def fail(_: Path) -> ModelSetupMachineFacts:
        raise OSError("credential-SENTINEL")

    result = HandlerSetupPreflight(collect=fail).handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "failure"
    assert "SENTINEL" not in result.model_dump_json()
    assert "free_disk_bytes" not in result.evidence


def _stack_plan(tmp_path: Path) -> ModelSetupPlan:
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_local_stack import (
        ModelSetupLocalStackSelection,
    )

    return ModelSetupPlan(
        workspace=tmp_path,
        local_stack=ModelSetupLocalStackSelection(
            compose_file=tmp_path / "compose.yaml",
            topology_file=tmp_path / "topology.yaml",
        ),
    )


def test_vm_notice_stops_only_the_local_stack(tmp_path: Path) -> None:
    """OMN-23413 inventory row vm_notice: a macOS VM cannot give Docker the
    nested virtualization the local stack needs, so selecting the stack in a VM
    is refused with that reason, while endpoint-only setup in the same VM runs."""
    handler = HandlerSetupPreflight(collect=lambda _: facts(virtual_machine=True))
    stack = handler.handle(_stack_plan(tmp_path))
    assert stack.status == "unavailable"
    assert "virtual machine" in (stack.fix or "")
    endpoint_only = handler.handle(ModelSetupPlan(workspace=tmp_path))
    assert endpoint_only.status == "success"
    assert endpoint_only.evidence["virtual_machine"] == "true"
    physical = HandlerSetupPreflight(collect=lambda _: facts()).handle(
        _stack_plan(tmp_path)
    )
    assert physical.status == "success"


@pytest.mark.parametrize(
    ("answers", "rosetta", "vm"),
    [
        (
            {"sysctl.proc_translated": (0, "1"), "kern.hv_vmm_present": (0, "0")},
            True,
            False,
        ),
        (
            {"sysctl.proc_translated": (0, "0"), "kern.hv_vmm_present": (0, "1")},
            False,
            True,
        ),
        (
            {"sysctl.proc_translated": (1, ""), "kern.hv_vmm_present": (0, "garbage")},
            None,
            None,
        ),
    ],
)
def test_collector_reads_rosetta_and_vm_from_sysctl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, tuple[int, str]],
    rosetta: bool | None,
    vm: bool | None,
) -> None:
    """Inventory rows is_rosetta and is_vm: the production collector, not
    supplied facts, asks sysctl; an error or an unexpected answer stays unknown."""
    from types import SimpleNamespace

    from omnimarket.nodes.node_onex_setup_effect.handlers import (
        handler_setup_preflight as preflight,
    )

    asked: list[tuple[str, ...]] = []

    def answer(_self: object, request: object) -> SimpleNamespace:
        argv = request.argv  # type: ignore[attr-defined]
        asked.append(argv)
        code, out = answers[argv[-1]]
        return SimpleNamespace(returncode=code, stdout=out)

    monkeypatch.setattr(preflight.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(preflight.HandlerSetupCommand, "handle", answer)
    collected = preflight.collect_machine_facts(tmp_path)
    assert (collected.rosetta, collected.virtual_machine) == (rosetta, vm)
    assert {argv[-1] for argv in asked} == set(answers)


@pytest.mark.skipif(
    __import__("platform").system() != "Darwin", reason="reads this Mac's sysctl"
)
def test_collector_answers_on_a_real_mac(tmp_path: Path) -> None:
    from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_preflight import (
        collect_machine_facts,
    )

    collected = collect_machine_facts(tmp_path)
    assert isinstance(collected.virtual_machine, bool)
    assert collected.rosetta in (True, False, None)
