# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of the signed rebuild command's send from outside the runtime (OMN-19998).

Seam 1 (entry -> broker): the entry hands the command to a producer on the broker the overlay names, on the namespaced topic
the contract declares, keyed by the correlation id; the broker is resolved from the overlay and nothing else.
Seam 2 (contract -> broker rows): the topic the node publishes is the topic the permission derivation grants WRITE on for every
lab login the overlays repository's role ``dev_rebuild_command_sender`` names (peer: the overlays repository's
tests/test_dev_rebuild_command_sender_role.py spells the same two logins and the node name); a renamed publish topic changes the
rows, which the control shows. Exemplar followed: tests/nodes/node_broker_acl_overlay_render_compute/test_derive_roles.py.
Seam 3 (overlays repository -> overlay model): the document the overlays repository holds for this node loads through the model.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.models.broker_acl import (
    ModelBrokerAclDeriveRequest,
    ModelBrokerAclPrincipalsOverlay,
)
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_branch_deploy_command_effect import __main__ as entry
from omnimarket.nodes.node_branch_deploy_command_effect.handlers.handler_branch_deploy_command import (
    load_command_topic,
)
from omnimarket.nodes.node_branch_deploy_command_effect.overlay import (
    ModelBranchDeployCommandOverlay,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_derive import (
    HandlerBrokerAclDerive,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.contract_topics import (
    load_topic_uses,
)
from tests.nodes.node_branch_deploy_command_effect.support import (
    NODE_DIR,
    RUN_ID,
    SIGNING_ENV,
    SIGNING_KEY,
    request,
)

pytestmark = pytest.mark.unit

NODE = "node_branch_deploy_command_effect"
LAB_LOGINS = (
    "User:dev-jonah-hp-gen9",
    "User:dev-jonah-hp-gen8",
)  # h053, h051 (overlays repository)
# The document the overlays repository holds for this node (spelled here: that repository is never read from this one).
OVERLAYS_REPOSITORY_DOCUMENT = {
    "hmac_secret_env": "DEPLOY_AGENT_HMAC_SECRET",
    "scope": "runtime",
    "build_source": "workspace",
    "detail_max_chars": 4096,
    "event_bus": {"type": "kafka", "lane": "dev"},
}


class _RecordingProducer:
    """Stands in for aiokafka's producer; records what the entry sends and how it connected."""

    instances: list[_RecordingProducer] = []

    def __init__(self, **settings: Any) -> None:
        self.settings = settings
        self.sent: list[tuple[str, bytes, bytes]] = []
        self.stopped = False
        _RecordingProducer.instances.append(self)

    async def start(self) -> None:
        return None

    async def send_and_wait(self, topic: str, value: bytes, *, key: bytes) -> None:
        self.sent.append((topic, value, key))

    async def stop(self) -> None:
        self.stopped = True


def _broker() -> ModelNodeBroker:
    return ModelNodeBroker(
        kind="kafka",
        overlay_path=Path("overlay.yaml"),
        lane="lane-a",
        bootstrap_servers="broker-a:9092",
        security_protocol="SASL_PLAINTEXT",
        sasl_mechanism="SCRAM-SHA-256",
        sasl_username="login-a",
        sasl_password=SecretStr("pw-fixture"),  # fixture value, not a credential
    )


def test_seam_entry_publishes_on_the_declared_topic_through_the_overlay_broker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _RecordingProducer.instances = []
    aiokafka = ModuleType("aiokafka")
    vars(aiokafka)["AIOKafkaProducer"] = _RecordingProducer
    monkeypatch.setitem(sys.modules, "aiokafka", aiokafka)
    resolved: list[str] = []

    def _resolve(
        node: str, *, environ: Mapping[str, str] | None = None
    ) -> ModelNodeBroker:
        resolved.append(node)
        return _broker()

    monkeypatch.setattr(entry, "resolve_dialable_broker", _resolve)
    monkeypatch.setattr(
        entry,
        "load_overlay",
        lambda: ModelBranchDeployCommandOverlay.model_validate(
            {**OVERLAYS_REPOSITORY_DOCUMENT, "hmac_secret_env": SIGNING_ENV}
        ),
    )
    monkeypatch.setenv(SIGNING_ENV, SIGNING_KEY)

    assert entry.main(["--input", request(mode="deploy").model_dump_json()]) == 0

    assert resolved == [NODE]
    (producer,) = _RecordingProducer.instances
    assert producer.settings["bootstrap_servers"] == "broker-a:9092"
    assert producer.settings["sasl_plain_username"] == "login-a"
    assert producer.stopped
    ((topic, _value, key),) = producer.sent
    assert topic.endswith(load_command_topic())
    assert key == str(RUN_ID).encode()
    assert "pw-fixture" not in capsys.readouterr().out


def _derive_for(root: Path, nodes: list[str]) -> tuple[Any, ...]:
    overlay = ModelBrokerAclPrincipalsOverlay.model_validate(
        {
            "scope": "broker_acl_principals",
            "ticket": "OMN-24005",
            "why": "a test",
            "roles": [
                {
                    "name": "dev_rebuild_command_sender",
                    "role": "lab hosts that send the signed command",
                    "nodes": nodes,
                    "principals": list(LAB_LOGINS),
                }
            ],
        }
    )
    result = HandlerBrokerAclDerive().handle(
        ModelBrokerAclDeriveRequest(
            scope="broker_acl_principals",
            topic_uses=load_topic_uses(root),
            principals=overlay,
            only_node=None,
        )
    )
    assert result.findings == ()
    return tuple(result.rows)


def _contract_root(tmp_path: Path, publish_topic: str | None = None) -> Path:
    target = tmp_path / NODE
    target.mkdir(parents=True)
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    if publish_topic is not None:
        topics = contract["event_bus"]["publish_topics"]
        contract["event_bus"]["publish_topics"] = [
            publish_topic if t == load_command_topic() else t for t in topics
        ]
    (target / "contract.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")
    return tmp_path


def _writes(rows: tuple[Any, ...], login: str) -> set[str]:
    return {
        r.name
        for r in rows
        if r.principal == login
        and r.resource_type.value == "TOPIC"
        and r.operation.value == "WRITE"
    }


def test_seam_every_lab_login_holds_write_on_the_topic_the_node_publishes(
    tmp_path: Path,
) -> None:
    rows = _derive_for(_contract_root(tmp_path), [NODE])
    for login in LAB_LOGINS:
        assert load_command_topic() in _writes(rows, login)


def test_seam_control_a_renamed_publish_topic_changes_the_granted_rows(
    tmp_path: Path,
) -> None:
    """Failing control: the rows follow the contract, so the seam test above fails when the topic moves."""
    renamed = "onex.cmd.deploy.rebuild-requested-renamed.v1"
    rows = _derive_for(_contract_root(tmp_path, renamed), [NODE])
    for login in LAB_LOGINS:
        assert load_command_topic() not in _writes(rows, login)
        assert renamed in _writes(rows, login)


def test_seam_the_overlays_repository_document_loads_through_the_model() -> None:
    loaded = ModelBranchDeployCommandOverlay.model_validate(
        OVERLAYS_REPOSITORY_DOCUMENT
    )
    assert loaded.event_bus is not None
    assert (loaded.event_bus.type, loaded.event_bus.lane) == ("kafka", "dev")
