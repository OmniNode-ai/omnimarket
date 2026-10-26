# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Execute the five local G1 nodes through their packaged typed contracts."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_type_hints

import pytest
import yaml
from pydantic import BaseModel, ValidationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("node", "payload", "expected"),
    [
        (
            "node_host_memory_admission_compute",
            {
                "mem_avail_gb": 1.0,
                "mem_psi_avg60": None,
                "swap_in_kb_s": 30000.0,
                "swap_out_kb_s": 0.0,
                "min_free_gb": 4.0,
                "max_mem_psi": 10.0,
                "max_swap_kb_s": 102400.0,
                "max_swap_in_kb_s": 20480.0,
            },
            {
                "refusals": (
                    "mem_avail 1.0G < 4G",
                    "swap-in 30000 KB/s > 20480 KB/s (paging)",
                )
            },
        ),
        (
            "node_landing_worker_engine_compute",
            {
                "readings": (),
                "requested_engine": "claude_opus",
                "codex_first": False,
                "cause": False,
                "codex_allowed": frozenset(),
                "env": {"LANDING_CODEX_FALLBACK": "0"},
            },
            {"pick": None, "record": {}},
        ),
        (
            "node_landing_tick_bounds_compute",
            {"timing": {"total_s": 150.0, "work_s": 130.0}, "target_s": 120.0},
            {
                "target_s": 120.0,
                "total_s": 150.0,
                "over": True,
                "top": [["work_s", 130.0]],
            },
        ),
        (
            "node_landing_upstream_gate_compute",
            {
                "pr": "example#1",
                "head": "current-head",
                "judged": {
                    "example#2": {"head": "current-head", "kind": "upstream_open"}
                },
                "records": {"example#2": {"facts": {"state": "OPEN"}}},
            },
            {
                "standing": ("example#2",),
                "unbounded": ("example#2",),
                "gate_ref": "example#2",
            },
        ),
        (
            "node_merge_sweep_same_head_compute",
            {
                "head": {
                    "state": "OPEN",
                    "head": "current-head",
                    "queued": {"reason": "queued"},
                },
                "key": "example#1",
                "scope": {"example"},
            },
            {
                "skipped": {
                    "pr": "example#1",
                    "why": "queued-or-handed-off-same-head",
                    "head": "current-head",
                    "reason": "queued",
                }
            },
        ),
    ],
)
def test_g1_local_invocation(
    node: str, payload: dict[str, Any], expected: dict[str, Any]
) -> None:
    package = importlib.import_module(f"omnimarket.nodes.{node}")
    assert package.__file__ is not None
    package_dir = Path(package.__file__).parent
    contract = yaml.safe_load((package_dir / "contract.yaml").read_text())
    metadata = yaml.safe_load((package_dir / "metadata.yaml").read_text())
    assert contract["metadata"]["transport_type"] == "local"
    assert contract["runtime_profiles"] == []
    assert contract["descriptor"]["runtime_profiles"] == []
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["runtime_dispatch"]["external_trigger"].strip()
    assert metadata["capabilities"]["full_runtime"] is False

    binding = contract["handler"]
    handler = getattr(importlib.import_module(binding["module"]), binding["class"])

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        candidate = getattr(importlib.import_module(module), name)
        assert isinstance(candidate, type)
        assert issubclass(candidate, BaseModel)
        return candidate

    request_model = model(binding["input_model"])
    result_model = model(binding["output_model"])
    hints = get_type_hints(handler.handle)
    assert hints["request"] is request_model
    assert hints["return"] is result_model
    inputs = dict(payload)
    if node == "node_landing_worker_engine_compute":
        inputs["placement"] = SimpleNamespace(choose=lambda _engine, _readings: None)
    result = handler().handle(request_model.model_validate(inputs))
    assert isinstance(result, result_model)
    assert result.model_dump() == expected
    with pytest.raises(ValidationError):
        request_model.model_validate({})
