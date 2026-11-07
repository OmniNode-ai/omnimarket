# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the pilot manifest check (OMN-22979).

The chain is the one the contract declares: a check command arrives as JSON, the
compute returns the typed result, and the result serialises onto the declared
terminal event topic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_pilot_manifest_check_compute.handlers.handler_pilot_manifest_check import (
    HandlerPilotManifestCheck,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_status import (
    EnumPilotStatus,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_overlay import (
    ModelPilotManifestCheckOverlay,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_request import (
    ModelPilotManifestCheckRequest,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_manifest_check_result import (
    ModelPilotManifestCheckResult,
)
from tests.nodes.node_pilot_manifest_check_compute.support import (
    frozen_manifest,
    request,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pilot_manifest_check_compute"
)


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_golden_chain_command_json_to_terminal_event_json() -> None:
    command_json = request(
        frozen_manifest(), results={"overall_accuracy": 0.9, "planted_recall": 1.0}
    ).model_dump_json()
    result = HandlerPilotManifestCheck().handle(
        ModelPilotManifestCheckRequest.model_validate_json(command_json)
    )
    event = ModelPilotManifestCheckResult.model_validate_json(result.model_dump_json())
    assert event.status is EnumPilotStatus.ACCEPTED
    assert event.correlation_id == result.correlation_id
    contract = _contract()
    assert contract["terminal_event"] == "onex.evt.omnimarket.pilot-manifest-checked.v1"
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]  # type: ignore[index]
    assert (
        contract["runtime_dispatch"]["command_topic"]  # type: ignore[index]
        in contract["event_bus"]["subscribe_topics"]  # type: ignore[index]
    )


def test_error_chain_refused_manifest_reaches_the_terminal_event() -> None:
    command_json = request(frozen_manifest(bar=(), bar_hash=None)).model_dump_json()
    result = HandlerPilotManifestCheck().handle(
        ModelPilotManifestCheckRequest.model_validate_json(command_json)
    )
    event = ModelPilotManifestCheckResult.model_validate_json(result.model_dump_json())
    assert event.check_passed is False
    assert event.status is EnumPilotStatus.REFUSED
    assert [f.value for f in event.findings] == ["bar_missing", "hash_missing"]


def test_error_chain_malformed_command_is_refused_before_the_handler() -> None:
    command_json = request(frozen_manifest()).model_dump_json()
    broken = command_json.replace('"pilot"', '"pilote"')
    with pytest.raises(ValidationError):
        ModelPilotManifestCheckRequest.model_validate_json(broken)


def test_overlay_default_file_parses_to_the_in_memory_default() -> None:
    loaded = yaml.safe_load(
        (NODE_DIR / "overlay.default.yaml").read_text(encoding="utf-8")
    )
    assert (
        ModelPilotManifestCheckOverlay.model_validate(loaded)
        == ModelPilotManifestCheckOverlay()
    )


def test_overlay_setting_changes_the_check() -> None:
    lenient = ModelPilotManifestCheckOverlay(require_frozen_before_start=False)
    manifest = frozen_manifest(frozen_at=None)
    assert HandlerPilotManifestCheck(lenient).handle(request(manifest)).check_passed
    assert not HandlerPilotManifestCheck().handle(request(manifest)).check_passed
