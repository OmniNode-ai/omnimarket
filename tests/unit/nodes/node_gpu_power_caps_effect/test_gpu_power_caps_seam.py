# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests of the GPU power caps node (OMN-24042): each fails when either side of a seam changes shape.

Seams: the gpu_power_caps settings document (the overlays repository declares it, node_host_installer_net_units reads it
as its units file, this node reads the same keys), the node's own overlay, the net-units model, and the storage node's
transport. Shape followed: tests/unit/nodes/node_host_storage_effect/test_host_storage.py (a failing control per reason).
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.lab_work.bus import load_lab_work_topics
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.models.model_net_units_overlay import (
    ModelNetUnitDeclaration,
    ModelNetUnitsOverlay,
)
from omnimarket.nodes.node_gpu_power_caps_effect import runtime_gpu_power_caps
from omnimarket.nodes.node_gpu_power_caps_effect.handlers import (
    backend_subprocess,
    handler_gpu_power_caps,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.handler_gpu_power_caps import (
    HandlerGpuPowerCaps,
    caps_of_unit,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    RESULT_LINE_PREFIX,
    EnumGpuPowerCapsOutcome,
    ModelGpuPowerCapsRequest,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    ModelGpuPowerCapsOverlay,
)
from omnimarket.nodes.node_host_storage_effect.handlers import (
    backend_subprocess as storage_transport,
)
from tests.unit.nodes.node_gpu_power_caps_effect.fixtures import (
    BIG,
    NODE_DIR,
    SMALL,
    caps_document,
    fake_smi,
    in_memory_overlay,
    state_limits,
    subprocess_overlay,
    unit_entry,
)
from tests.unit.nodes.node_gpu_power_caps_effect.test_gpu_power_caps_direct_bootstrap import (
    runtime_overlay,
)
from tests.unit.nodes.node_gpu_power_caps_effect.test_gpu_power_caps_lab_work import (
    FakeSender,
    lab_work_overlay,
    overlay_document,
    receipt,
)

pytestmark = pytest.mark.unit

BIG_CARD = "NVIDIA GeForce RTX 5090"
SMALL_CARD = "NVIDIA GeForce RTX 4090"


def settings_document() -> dict[str, Any]:
    """The gpu_power_caps document as the overlays repository declares it: one unit per host, the caps as values."""
    h201 = unit_entry(
        "h201",
        {
            "GPU_NAME_0": BIG_CARD,
            "WATTS_0": "450",
            "GPU_NAME_1": SMALL_CARD,
            "WATTS_1": "300",
        },
    )
    h202 = unit_entry("h202", {"GPU_NAME_0": SMALL_CARD, "WATTS_0": "300"})
    for entry in (h201, h202):
        entry["files"][0]["content"] = "ExecStart=cap __GPU_NAME_0__ __WATTS_0__\n"
        entry["files"][0]["sha256_by_host"] = {entry["hosts"][0]: "0" * 64}
        del entry["files"][0]["sha256"]
    return {
        "schema": "host_net_units.v1",
        "units": {"gpu-power-caps-h201": h201, "gpu-power-caps-h202": h202},
    }


def prefixes() -> tuple[str, str]:
    overlay = in_memory_overlay()
    return overlay.name_value_prefix, overlay.watts_value_prefix


def read_caps(doc: dict[str, Any], unit: str) -> dict[str, int]:
    declared = ModelNetUnitsOverlay.model_validate(doc).units[unit]
    name_prefix, watts_prefix = prefixes()
    return {c.name: c.watts for c in caps_of_unit(declared, name_prefix, watts_prefix)}


def test_seam_settings_shape_the_node_reads_the_caps_the_settings_declare() -> None:
    doc = settings_document()
    assert read_caps(doc, "gpu-power-caps-h201") == {BIG_CARD: 450, SMALL_CARD: 300}
    assert read_caps(doc, "gpu-power-caps-h202") == {SMALL_CARD: 300}


@pytest.mark.parametrize("which", ["name", "watts"])
def test_seam_settings_shape_a_renamed_key_breaks_the_reading_visibly(
    which: str,
) -> None:
    doc = settings_document()
    values = doc["units"]["gpu-power-caps-h201"]["files"][0]["values"]
    old = "GPU_NAME_0" if which == "name" else "WATTS_0"
    values[old.replace("_0", "__0")] = values.pop(old)
    declared = ModelNetUnitsOverlay.model_validate(doc).units["gpu-power-caps-h201"]
    with pytest.raises(ValueError, match="no matching"):
        caps_of_unit(declared, *prefixes())


def test_seam_settings_shape_a_key_the_installer_model_does_not_know_is_refused() -> (
    None
):
    doc = copy.deepcopy(settings_document())
    doc["units"]["gpu-power-caps-h201"]["unexpected"] = 1
    with pytest.raises(ValidationError):
        ModelNetUnitsOverlay.model_validate(doc)


def test_seam_node_overlay_the_declared_units_are_the_units_of_the_settings() -> None:
    overlay = in_memory_overlay()
    assert overlay.caps_overlay_id == "gpu_power_caps"
    real = {"gpu-power-caps-h201", "gpu-power-caps-h202"}
    # The in-memory profile names simulated units; the shape rule is the same: one unit key per host row.
    assert len({h.caps_unit for h in overlay.hosts}) == len(overlay.hosts)
    assert real == set(settings_document()["units"])


def test_seam_installer_the_default_loader_reads_the_settings_through_the_installer_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, type]] = []

    def recorder(overlay_id: str, model: type, *args: Any, **kwargs: Any) -> Any:
        calls.append((overlay_id, model))
        return ModelNetUnitsOverlay.model_validate(settings_document())

    monkeypatch.setattr(handler_gpu_power_caps, "load_structured_overlay", recorder)
    overlay = in_memory_overlay()
    handler = HandlerGpuPowerCaps(overlay)
    handler.handle(ModelGpuPowerCapsRequest(host="sim-host-a", requested_by="lane-a"))
    assert calls == [(overlay.caps_overlay_id, ModelNetUnitsOverlay)]


def test_seam_transport_the_node_uses_the_storage_nodes_runner_and_prefix_join() -> (
    None
):
    assert backend_subprocess.__dict__["_run_bounded"] is storage_transport._run_bounded
    assert (
        backend_subprocess.__dict__["_transport_argv"]
        is storage_transport._transport_argv
    )


def test_seam_unit_declaration_type_is_the_installers() -> None:
    declared = ModelNetUnitDeclaration.model_validate(
        unit_entry("h", {"GPU_NAME_0": "A", "WATTS_0": "1"})
    )
    assert caps_of_unit(declared, *prefixes())[0].watts == 1


def test_seam_lab_work_unit_the_nodes_unit_is_the_lab_work_request_shape() -> None:
    sender = FakeSender()
    sender.receipt = ModelLabWorkUnitReceipt(
        work_unit_id="gpc-seam0001",
        target_host="sim-host-a",
        host="",
        repo="o/r",
        commit_sha="0" * 40,
        status=EnumLabWorkUnitStatus.INFRA_ERROR,
    )
    handler_gpu_power_caps.HandlerGpuPowerCaps(
        overlay=lab_work_overlay(), sender=sender
    ).handle(ModelGpuPowerCapsRequest(host="sim-host-a", requested_by="lane-a"))
    wire = sender.calls[0].model_dump(mode="json", exclude_none=True)
    assert set(wire) <= set(ModelLabWorkUnitRequest.model_fields)
    assert ModelLabWorkUnitRequest.model_validate(wire) == sender.calls[0]


def test_seam_direct_bootstrap_the_runtime_profile_unit_is_the_lab_work_request_shape() -> (
    None
):
    sender = FakeSender(receipt(EnumLabWorkUnitStatus.INFRA_ERROR, exit_code=None))
    overlay = runtime_overlay()
    handler_gpu_power_caps.HandlerGpuPowerCaps(overlay=overlay, sender=sender).handle(
        ModelGpuPowerCapsRequest(host=overlay.hosts[0].name, requested_by="lane-a")
    )
    wire = sender.calls[0].model_dump(mode="json", exclude_none=True)
    assert set(wire) <= set(ModelLabWorkUnitRequest.model_fields)
    assert ModelLabWorkUnitRequest.model_validate(wire) == sender.calls[0]


def test_seam_direct_bootstrap_the_overlay_profiles_of_the_contract_exist() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    profiles = contract["overlay_profiles"]
    assert "lab_work_runtime" in profiles
    for path in profiles.values():
        assert (NODE_DIR / path).exists()
    runtime = ModelGpuPowerCapsOverlay.model_validate(
        yaml.safe_load(
            (NODE_DIR / profiles["lab_work_runtime"]).read_text(encoding="utf-8")
        )
    )
    assert runtime.lab_work is not None
    assert runtime.lab_work.kafka_bootstrap is not None


def grants_of_the_contract() -> set[tuple[str, str]]:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    (block,) = contract["broker_acl_grants"]
    assert block["principal"] == "${ONEX_ACL_RUNTIME_PRINCIPAL}"
    return {
        (row["name"], row["operation"])
        for row in block["grants"]
        if row["resource_type"] == "TOPIC"
    }


def test_seam_lab_work_topics_the_contract_grants_the_lab_work_callers_topics() -> None:
    topics = load_lab_work_topics()
    granted = grants_of_the_contract()
    assert (topics.command, "WRITE") in granted
    for read_topic in (topics.success, topics.failure, topics.capacity):
        assert (read_topic, "READ") in granted
    for topic in (topics.command, topics.success, topics.failure, topics.capacity):
        assert (topic, "DESCRIBE") in granted


def test_seam_lab_work_topics_a_renamed_topic_breaks_the_grant_reading() -> None:
    granted = grants_of_the_contract()
    topics = load_lab_work_topics()
    renamed = topics.command.replace("requested", "asked")
    assert (renamed, "WRITE") not in granted


def run_host_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    words: list[str],
    smi: Path,
) -> tuple[int, str]:
    """What the host's lab-work serve does with the unit's words: run the node's command line there."""
    doc = overlay_document()
    local = subprocess_overlay(smi).model_dump(mode="json")
    local.update(backend="lab_work", lab_work=doc["lab_work"], hosts=doc["hosts"])
    overlay = ModelGpuPowerCapsOverlay.model_validate(local)
    caps = ModelNetUnitsOverlay.model_validate(caps_document())
    monkeypatch.setattr(
        runtime_gpu_power_caps,
        "load_structured_overlay",
        lambda *_args, **_kwargs: overlay,
    )
    monkeypatch.setattr(
        handler_gpu_power_caps,
        "load_structured_overlay",
        lambda *_args, **_kwargs: caps,
    )
    module_at = words.index("-m")
    assert words[module_at + 1] == runtime_gpu_power_caps.__name__
    code = runtime_gpu_power_caps.main(words[module_at + 2 :])
    return code, capsys.readouterr().out


def test_seam_lab_work_host_command_the_overlays_argv_runs_the_nodes_command_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    smi = fake_smi(tmp_path)
    unit_words = [
        word.replace("{host}", "sim-host-a").replace("{requested_by}", "lane-a")
        for word in overlay_document()["lab_work"]["host_argv"]
    ]
    code, out = run_host_command(monkeypatch, capsys, unit_words, smi)
    assert code == 0
    # The host's own limits moved to the caps; the dispatcher reads the same result the host printed.
    assert state_limits(smi) == {0: 500.0, 1: 300.0}
    sender = FakeSender(
        ModelLabWorkUnitReceipt(
            work_unit_id="gpc-seam0002",
            target_host="sim-host-a",
            host="sim-host-a",
            repo="o/r",
            commit_sha="0" * 40,
            status=EnumLabWorkUnitStatus.COMPLETED,
            exit_code=code,
            output_tail=out,
        )
    )
    result = handler_gpu_power_caps.HandlerGpuPowerCaps(
        overlay=lab_work_overlay(), sender=sender
    ).handle(ModelGpuPowerCapsRequest(host="sim-host-a", requested_by="lane-a"))
    assert result.outcome is EnumGpuPowerCapsOutcome.OK
    assert [c.after_watts for c in result.cards] == [500, 300]
    assert [c.name for c in result.cards] == [BIG, SMALL]
    assert out.startswith(RESULT_LINE_PREFIX)
    assert out.count("\n") == 1


def test_seam_lab_work_host_command_without_local_the_lab_work_overlay_has_no_backend(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    smi = fake_smi(tmp_path)
    words = [
        "uv",
        "-m",
        runtime_gpu_power_caps.__name__,
        "--host",
        "sim-host-a",
        "--requested-by",
        "lane-a",
    ]
    with pytest.raises(ValueError, match="no local backend"):
        run_host_command(monkeypatch, capsys, words, smi)
    assert sys.executable
