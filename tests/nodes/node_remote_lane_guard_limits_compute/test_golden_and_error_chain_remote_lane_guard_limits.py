# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of node_remote_lane_guard_limits_compute (OMN-22848, stopped-line fill plan T15).

The expected values in ORACLE were produced by running the runner script's own ``lane_guard.resolve_limits`` (plugin omni, scripts)
on the same asks at its frozen commit, so a handler that drifts from the script fails a row by name.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, get_type_hints

import pytest
import yaml
from pydantic import BaseModel, ValidationError

pytestmark = pytest.mark.unit

NODE = "omnimarket.nodes.node_remote_lane_guard_limits_compute"


def _package() -> Any:
    return importlib.import_module(NODE)


def _overlay_bounds() -> dict[str, Any]:
    package = _package()
    overlay = yaml.safe_load(
        (Path(package.__file__).parent / "overlay.example.yaml").read_text()
    )
    return dict(overlay["remote_lane_guard_limits"])


def _verdict(
    timeout_s: float,
    mem_max: str | None = None,
    cpu_quota: int | None = None,
    kind: str = "lane",
) -> Any:
    models = importlib.import_module(f"{NODE}.models.model_remote_lane_guard_limits")
    handler = _package().HandlerRemoteLaneGuardLimits()
    return handler.handle(
        models.ModelRemoteLaneGuardLimitsRequest(
            timeout_s=timeout_s,
            mem_max=mem_max,
            cpu_quota=cpu_quota,
            kind=kind,
            bounds=models.ModelGuardBounds(**_overlay_bounds()),
        )
    )


GIB = 1024**3
# (timeout_s, mem_max, cpu_quota, kind) -> the limits the script resolved.
ORACLE_OK: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
    (
        (5400, None, None, "lane"),
        {
            "runtime_s": 5520,
            "mem_bytes": 8 * GIB,
            "cpu_pct": 400,
            "tasks_max": 8192,
            "kind": "lane",
        },
    ),
    (
        (300, "16G", 800, "tests"),
        {
            "runtime_s": 420,
            "mem_bytes": 16 * GIB,
            "cpu_pct": 800,
            "tasks_max": 8192,
            "kind": "tests",
        },
    ),
    (
        (10680, None, None, "lane"),
        {
            "runtime_s": 10800,
            "mem_bytes": 8 * GIB,
            "cpu_pct": 400,
            "tasks_max": 8192,
            "kind": "lane",
        },
    ),
    (
        (0, None, None, "lane"),
        {
            "runtime_s": 120,
            "mem_bytes": 8 * GIB,
            "cpu_pct": 400,
            "tasks_max": 8192,
            "kind": "lane",
        },
    ),
    (
        (5400, "512M", None, "lane"),
        {
            "runtime_s": 5520,
            "mem_bytes": 512 * 1024**2,
            "cpu_pct": 400,
            "tasks_max": 8192,
            "kind": "lane",
        },
    ),
    (
        (5400, "1.5G", 0, "lane"),
        {
            "runtime_s": 5520,
            "mem_bytes": 1610612736,
            "cpu_pct": 400,
            "tasks_max": 8192,
            "kind": "lane",
        },
    ),
]
ORACLE_REFUSED: list[tuple[tuple[Any, ...], str]] = [
    (
        (10681, None, None, "lane"),
        "a timeout of 10681s plus the 120s grace passes the 10800s (3 h) hard cap; the longest allowed timeout is 178 minutes",
    ),
    ((5400, "abc", None, "lane"), "'abc' is not a size (use 8G, 512M or a byte count)"),
    (
        (5400, "33G", None, "lane"),
        "memory limit 35433480192 bytes is outside 64M..32G (the hard ceiling)",
    ),
    (
        (5400, "32M", None, "lane"),
        "memory limit 33554432 bytes is outside 64M..32G (the hard ceiling)",
    ),
    ((5400, None, 5, "lane"), "CPU quota 5% is outside 10..1600%"),
    ((5400, None, 1601, "lane"), "CPU quota 1601% is outside 10..1600%"),
    ((5400, None, None, "bogus"), "kind 'bogus' is not one of ('lane', 'tests')"),
    (
        (-130, None, None, "lane"),
        "runtime -10s is outside 1..10800s (the hard cap is 3 h)",
    ),
]


def test_contract_golden_chain() -> None:
    """The packaged contract binds the handler, its typed models and an empty bus, and the handler answers the golden ask."""
    package = _package()
    assert package.__file__ is not None
    root = Path(package.__file__).parent
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    metadata = yaml.safe_load((root / "metadata.yaml").read_text())
    assert contract["name"] == "node_remote_lane_guard_limits_compute"
    assert contract["lifecycle"] == "experimental"
    assert contract["metadata"]["transport_type"] == "local"
    assert contract["metadata"]["related_tickets"] == ["OMN-22848"]
    assert (
        contract["runtime_profiles"] == contract["descriptor"]["runtime_profiles"] == []
    )
    assert metadata["name"] == contract["name"]
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )
    assert (
        contract["handler_routing"]["default_handler"]
        == binding["module"] + ":" + binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert issubclass(result, BaseModel)
        assert result.model_config == {"frozen": True, "extra": "forbid"}
        return result

    request_class = model(binding["input_model"])
    result_class = model(binding["output_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    golden = _verdict(5400)
    assert isinstance(golden, result_class)
    assert golden.refusal is None
    assert golden.limits is not None
    assert golden.limits.model_dump() == ORACLE_OK[0][1]


@pytest.mark.parametrize(
    ("ask", "expected"), ORACLE_OK, ids=[str(a) for a, _ in ORACLE_OK]
)
def test_resolved_limits_equal_the_script(
    ask: tuple[Any, ...], expected: dict[str, Any]
) -> None:
    verdict = _verdict(*ask)
    assert verdict.refusal is None
    assert verdict.limits is not None
    assert verdict.limits.model_dump() == expected


@pytest.mark.parametrize(
    ("ask", "text"), ORACLE_REFUSED, ids=[str(a) for a, _ in ORACLE_REFUSED]
)
def test_error_chain_refusal_text_equals_the_script(
    ask: tuple[Any, ...], text: str
) -> None:
    verdict = _verdict(*ask)
    assert verdict.limits is None
    assert verdict.refusal == text


def test_error_chain_a_missing_bound_is_refused_naming_it() -> None:
    """No bound has a default: an overlay missing one fails validation by that key (no value lives in the node)."""
    models = importlib.import_module(f"{NODE}.models.model_remote_lane_guard_limits")
    bounds = _overlay_bounds()
    del bounds["mem_ceiling_bytes"]
    with pytest.raises(ValidationError) as caught:
        models.ModelGuardBounds(**bounds)
    assert "mem_ceiling_bytes" in str(caught.value)


def test_error_chain_overlay_example_carries_every_bound() -> None:
    models = importlib.import_module(f"{NODE}.models.model_remote_lane_guard_limits")
    assert set(_overlay_bounds()) == set(models.ModelGuardBounds.model_fields)


def test_bounds_come_from_the_request_not_the_node() -> None:
    """A tighter hard cap in the request tightens the refusal: the node holds no cap of its own."""
    models = importlib.import_module(f"{NODE}.models.model_remote_lane_guard_limits")
    handler = _package().HandlerRemoteLaneGuardLimits()
    tight = {**_overlay_bounds(), "hard_cap_s": 3600}
    verdict = handler.handle(
        models.ModelRemoteLaneGuardLimitsRequest(
            timeout_s=5400, bounds=models.ModelGuardBounds(**tight)
        )
    )
    assert verdict.limits is None
    assert verdict.refusal is not None
    assert "passes the 3600s (1 h) hard cap" in verdict.refusal
