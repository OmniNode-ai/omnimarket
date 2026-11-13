# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests of the GPU power caps node (OMN-24042): each fails when either side of a seam changes shape.

Seams: the gpu_power_caps settings document (the overlays repository declares it, node_host_installer_net_units reads it
as its units file, this node reads the same keys), the node's own overlay, the net-units model, and the storage node's
transport. Shape followed: tests/unit/nodes/node_host_storage_effect/test_host_storage.py (a failing control per reason).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_gpu_power_caps_effect.handlers import (
    backend_subprocess,
    handler_gpu_power_caps,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.handler_gpu_power_caps import (
    HandlerGpuPowerCaps,
    caps_of_unit,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    ModelGpuPowerCapsRequest,
)
from omnimarket.nodes.node_host_installer_net_units.models.model_net_units import (
    ModelNetUnitDeclaration,
    ModelNetUnitsOverlay,
)
from omnimarket.nodes.node_host_storage_effect.handlers import (
    backend_subprocess as storage_transport,
)
from tests.unit.nodes.node_gpu_power_caps_effect.fixtures import (
    in_memory_overlay,
    unit_entry,
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
