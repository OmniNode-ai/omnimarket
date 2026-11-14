# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's own entry sends the signed rebuild command from outside
the lane's runtime (OMN-19998).

* golden: with the dev runtime stopped, the entry signs and publishes
  one message to the deploy agent's contract topic through the bus its
  overlay declares — no runtime, run-node or consumer anywhere;
* error: a signing key unset refuses, a failing bus fails, an
  unresolvable broker and a malformed input are INDETERMINATE;
* seam: the example overlay's ``event_bus`` validates through the
  overlay model, a kafka declaration without a lane is refused, and an
  overlay written to the workspace config root is read by
  ``resolve_node_event_bus`` the same way the entry reads it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.bus_binding.node_broker import NodeBrokerResolutionError
from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
    NODE_OVERLAYS_RELATIVE_PATH,
    resolve_node_event_bus,
)
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_branch_deploy_command_effect import __main__ as entry
from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    HandlerBranchDeployCommand,
    load_command_topic,
)
from omnimarket.nodes.node_branch_deploy_command_effect.overlay import (
    ModelBranchDeployCommandOverlay,
)
from tests.nodes.node_branch_deploy_command_effect.support import (
    NODE_DIR,
    RUN_ID,
    SIGNING_ENV,
    SIGNING_KEY,
    FailingBus,
    FakeBus,
    overlay,
    request,
)

pytestmark = pytest.mark.unit


def _expected_signature(body: dict) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(SIGNING_KEY.encode(), raw, hashlib.sha256).hexdigest()


def _hermetic_build(
    bus: FakeBus | FailingBus,
) -> Callable[..., HandlerBranchDeployCommand]:
    """A build_handler that assembles the handler on ``bus`` with the example overlay."""

    def _build(*, environ: Mapping[str, str]) -> HandlerBranchDeployCommand:
        return HandlerBranchDeployCommand(
            event_bus=bus, overlay=overlay(), environ=environ
        )

    return _build


def test_golden_chain_publishes_signed_command_with_runtime_stopped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dev-runtime-stopped case: no runtime, run-node or consumer is
    involved anywhere — the node's own entry dials the broker its
    overlay names and sends the signed command itself."""
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)
    bus = FakeBus()
    monkeypatch.setattr(entry, "build_handler", _hermetic_build(bus))

    code = entry.main(["--input", request(mode="deploy").model_dump_json()])

    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "published"
    assert len(bus.published) == 1
    topic, key, value = bus.published[0]
    assert topic == load_command_topic()
    assert key == str(RUN_ID).encode()
    payload = json.loads(value)
    body = {k: v for k, v in payload.items() if k != "_signature"}
    assert payload["_signature"] == _expected_signature(body)


def test_error_chain_signing_key_unset_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(SIGNING_ENV, raising=False)
    bus = FakeBus()
    monkeypatch.setattr(entry, "build_handler", _hermetic_build(bus))

    code = entry.main(["--input", request(mode="deploy").model_dump_json()])

    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "refused"
    assert bus.published == []


def test_error_chain_bus_failure_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)
    monkeypatch.setattr(entry, "build_handler", _hermetic_build(FailingBus()))

    code = entry.main(["--input", request(mode="deploy").model_dump_json()])

    assert code == 1
    out = capsys.readouterr()
    assert json.loads(out.out)["status"] == "failed"
    assert json.loads(out.out)["detail"] == "ConnectionError"


def test_error_chain_unresolvable_broker_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(
        node: str, *, environ: Mapping[str, str] | None = None
    ) -> ModelNodeBroker:
        raise NodeBrokerResolutionError(
            f"{node} declares no event_bus in its contract overlay"
        )

    monkeypatch.setattr(entry, "resolve_dialable_broker", _raise)

    assert entry.main(["--input", request(mode="deploy").model_dump_json()]) == 3
    assert capsys.readouterr().err.startswith("INDETERMINATE")


def test_error_chain_malformed_input_is_indeterminate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert entry.main(["--input", "{ not json"]) == 3
    assert capsys.readouterr().err.startswith("INDETERMINATE")


def test_seam_example_overlay_event_bus_validates_through_the_model() -> None:
    bus = overlay().event_bus

    assert bus is not None
    assert bus.model_dump() == {"type": "kafka", "lane": "example-lane"}


def test_seam_kafka_event_bus_without_lane_is_refused_by_the_model() -> None:
    document: Any = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    document["event_bus"] = {"type": "kafka"}

    with pytest.raises(ValidationError):
        ModelBranchDeployCommandOverlay.model_validate(document)


def test_seam_config_root_overlay_is_read_as_the_entry_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_root = tmp_path / "config-owner"
    config_file = (
        config_root / NODE_OVERLAYS_RELATIVE_PATH / entry.NODE_NAME / "overlay.yaml"
    )
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        "event_bus:\n  type: kafka\n  lane: lane-a\n", encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(config_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    bus, path = resolve_node_event_bus(
        entry.NODE_NAME, workspace, {"HOME": str(tmp_path / "home")}
    )

    assert bus is not None
    assert (bus.type, bus.lane) == ("kafka", "lane-a")
    assert path == config_file
