# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Execute the five local G2 nodes through their packaged typed contracts."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from typing import Any, get_type_hints

import pytest
import yaml
from pydantic import BaseModel, ValidationError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("node", "payload", "expected"),
    [
        (
            "node_pool_read_share_effect",
            {"argv": ("probe",), "timeout_s": 20.0, "ttl_s": 30.0},
            {"returncode": 0, "stdout": "answer", "stderr": "", "shared": False},
        ),
        (
            "node_brief_compact_compute",
            {"operation": "expand", "brief": "Brief without references."},
            {"brief": "Brief without references."},
        ),
        (
            "node_standup_retrospective_compute",
            {"until": "2026-10-01T17:50:00Z"},
            {"retrospective": {}, "ticket_drafts": [], "errors": [], "notes": []},
        ),
        (
            "node_lane_host_discovery_compute",
            {
                "knowledge_base": "kb_repo_example",
                "knowledge_base_home_env": "KB_EXAMPLE_PATH",
                "registry_root_var": "EXAMPLE_REGISTRY",
            },
            None,
        ),
        (
            "node_umbrella_ticket_compute",
            {
                "tickets": ("CHILD-1",),
                "text": "Fix the test.",
                "opens_pr": True,
                "umbrella_tickets": ("PARENT-1",),
            },
            {"refusals": ()},
        ),
    ],
)
def test_g2_local_invocation(
    node: str,
    payload: dict[str, Any],
    expected: dict[str, Any] | None,
    tmp_path: Path,
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
    assert metadata["dependencies"] == []
    effect = node.endswith("_effect")
    assert contract["node_type"] == ("EFFECT_GENERIC" if effect else "COMPUTE_GENERIC")
    assert contract["descriptor"]["node_archetype"] == (
        "effect" if effect else "compute"
    )
    # the retrospective reads only its request; the others read a file or the environment
    assert contract["descriptor"]["purity"] == (
        "pure" if node == "node_standup_retrospective_compute" else "impure"
    )
    assert contract["descriptor"]["idempotent"] is not effect
    assert metadata["capabilities"]["side_effect_class"] == (
        "filesystem_write" if effect else "none"
    )
    assert metadata["pack"] == (
        "operator_tooling"
        if node == "node_standup_retrospective_compute"
        else "pr_lifecycle"
    )
    assert not (package_dir / "node.py").exists()

    binding = contract["handler"]
    handler = getattr(importlib.import_module(binding["module"]), binding["class"])

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        candidate = getattr(importlib.import_module(module), name)
        assert isinstance(candidate, type)
        assert issubclass(candidate, BaseModel)
        assert candidate.model_config["frozen"] is True
        assert candidate.model_config["extra"] == "forbid"
        return candidate

    request_model = model(binding["input_model"])
    result_model = model(binding["output_model"])
    hints = get_type_hints(handler.handle)
    assert hints["request"] is request_model
    assert hints["return"] is result_model
    inputs = dict(payload)
    handler_args: dict[str, Any] = {}
    if effect:
        inputs["directory"] = str(tmp_path / "cache")

        def capture(
            argv: list[str], *, timeout: float
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "answer", "")

        handler_args["capture"] = capture
    if node == "node_brief_compact_compute":
        inputs["render"] = lambda _name, _cells: "unused"
    wrapper_name = "Node" + "".join(
        word.title() for word in node.removeprefix("node_").split("_")
    )
    wrapper = getattr(package, wrapper_name)
    assert issubclass(wrapper, handler)
    result = wrapper(**handler_args).handle(request_model.model_validate(inputs))
    assert isinstance(result, result_model)
    if expected is None:
        from omnimarket.nodes.node_lane_host_discovery_compute.handlers.handler_lane_host_discovery import (
            kb_lines,
            prrp_snapshot_lines,
        )
        from omnimarket.nodes.node_lane_host_discovery_compute.models.model_lane_host_discovery import (
            PRRP_SNAPSHOT_DESTINATION,
        )

        expected = {
            "kb_lines": kb_lines(
                "kb_repo_example", "KB_EXAMPLE_PATH", "EXAMPLE_REGISTRY"
            ),
            "prrp_snapshot_lines": prrp_snapshot_lines(PRRP_SNAPSHOT_DESTINATION),
        }
    assert result.model_dump() == expected
    if any(field.is_required() for field in request_model.model_fields.values()):
        with pytest.raises(ValidationError):
            request_model.model_validate({})
    with pytest.raises(ValidationError):
        request_model.model_validate({**inputs, "undeclared": True})
