# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the placement snapshot publisher."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_prrp_snapshot_publish_effect.handlers import (
    HandlerPrrpSnapshotPublish,
)
from omnimarket.nodes.node_prrp_snapshot_publish_effect.models import (
    ModelPrrpSnapshotHost,
    ModelPrrpSnapshotPublishRequest,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_prrp_snapshot_publish_effect"
)


def test_golden_chain_failed_read_then_complete_publish(tmp_path: Path) -> None:
    """A failed read records failure; recovery publishes and clears that failure."""
    gate = tmp_path / "gate.py"
    gate.write_text("raise SystemExit(2)\n")
    repos = tmp_path / "repos.yaml"
    repos.write_text("repositories: [{repo: example/alpha}]\n")
    request = ModelPrrpSnapshotPublishRequest(
        gate_script=gate,
        repos_file=repos,
        hosts=(
            ModelPrrpSnapshotHost(
                name="host-a", target="user@host-a.example.invalid", local=True
            ),
        ),
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        destination="snapshots/variables.json",
        python=Path(sys.executable),
    )
    handler = HandlerPrrpSnapshotPublish()
    failed = handler.handle(request)
    assert failed.exit_code == 1
    assert (request.state_dir / "failure.json").is_file()
    assert not (request.state_dir / "heartbeat.json").exists()
    assert not (request.home / request.destination).exists()

    snapshot = {
        "schema": 1,
        "captured_at": "2026-10-14T12:00:00Z",
        "visibility": {"example/alpha": "private"},
        "scopes": {"repo:example/alpha": {}},
    }
    gate.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"Path(sys.argv[-1]).write_text({json.dumps(snapshot)!r})\n"
    )
    complete = handler.handle(request)
    assert complete.exit_code == 0, complete.failures
    assert complete.repos == 1
    assert complete.hosts[0].status == "written"
    assert json.loads((request.home / request.destination).read_text()) == snapshot
    heartbeat_path = request.state_dir / "heartbeat.json"
    heartbeat = heartbeat_path.read_bytes()
    assert json.loads(heartbeat)["captured_at"] == snapshot["captured_at"]
    assert not (request.state_dir / "failure.json").exists()

    gate.write_text("raise SystemExit(2)\n")
    assert handler.handle(request).exit_code == 1
    assert heartbeat_path.read_bytes() == heartbeat
    assert json.loads((request.home / request.destination).read_text()) == snapshot
    assert (request.state_dir / "failure.json").is_file()


@pytest.mark.parametrize("destination", ["/absolute.json", "../outside.json", ""])
def test_error_chain_request_refuses_unsafe_destination(
    tmp_path: Path, destination: str
) -> None:
    """A destination outside the host home is refused before the handler runs."""
    with pytest.raises(ValidationError, match="relative to the host home"):
        ModelPrrpSnapshotPublishRequest(
            gate_script=tmp_path / "gate.py",
            repos_file=tmp_path / "repos.yaml",
            hosts=(),
            state_dir=tmp_path / "state",
            home=tmp_path / "home",
            destination=destination,
            python=Path(sys.executable),
        )


def test_contract_declares_its_topics_and_resolvable_handler() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.prrp-snapshot-publish-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.prrp-snapshot-publish-completed.v1"
    ]
    assert contract["runtime_dispatch"]["command_topic"] == bus["subscribe_topics"][0]
    assert contract["terminal_event"] == bus["publish_topics"][0]
    assert contract["node_type"] == "effect"
    assert contract["descriptor"]["purity"] == "effectful"
    assert contract["metadata"] == {"ticket": "OMN-19998", "transport_type": "kafka"}
    assert contract["handler"]["class"] == HandlerPrrpSnapshotPublish.__name__
    assert HandlerPrrpSnapshotPublish.__module__ == contract["handler"]["module"]
    route = contract["handler_routing"]["handlers"][0]
    assert route["operation"] == "publish"
    assert route["handler"]["module"] == contract["handler"]["module"]
    assert route["handler"]["name"] == contract["handler"]["class"]
    assert route["input_model"] == contract["input_model"]
    assert route["output_model"] == contract["output_model"]
