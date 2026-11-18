# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One command topic, three operations: a request reaches only the handler whose model it fits (OMN-25245).

Seam: the contract's `handler_routing` <-> the engine's `PayloadTypeMatcher` (omnibase_infra, the predicate the
dispatch engine asks before it calls a dispatcher). The render node declares three operations on ONE command topic.
Under `operation_match` every dispatcher on the topic is handed every command, so a render request was also handed
to HandlerBrokerAclDerive and HandlerBrokerAclCoverageGate, which raised ValidationError (live on the dev runtime on
h201, 2026-11-18T00:41:09Z, correlation 97b129ab-b071-4fa1-ba2e-78615f3320b9). With `payload_type_match` and a declared
`event_model` per handler, the matcher refuses a payload the handler's model does not accept, and the engine treats
that as "not my message", not as an error. The test drives the real matcher from the contract's own entries.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _make_payload_type_matcher,
)
from omnibase_infra.runtime.auto_wiring.models import ModelHandlerRef

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_broker_acl_overlay_render_compute"
)
RENDER = "HandlerBrokerAclOverlayRender"
DERIVE = "HandlerBrokerAclDerive"
COVERAGE = "HandlerBrokerAclCoverageGate"

_OVERLAY_ROW = {
    "principal": "User:example-login",
    "role": "what the login is",
    "grants": [
        {
            "resource_type": "TOPIC",
            "pattern": "LITERAL",
            "name": "onex.evt.omnimarket.example.v1",
            "operation": "READ",
        }
    ],
}
_TOPIC_USE = {
    "node": "node_example_compute",
    "topic": "onex.cmd.omnimarket.example-requested.v1",
    "direction": "subscribe",
}
PAYLOADS: dict[str, dict[str, Any]] = {
    RENDER: {
        "scope": "broker_acls_example",
        "overlay": {
            "scope": "broker_acls_example",
            "ticket": "OMN-00000",
            "why": "a test",
            "operator_ssh_target": "broker-host-alias",
            "operator_command": "apply {declaration} {ssh_target}",
            "declaration_path": "overlays/{scope}/overlay.yaml",
            "lane": "dev",
            "host": "dev-lane-broker",
            "compose_project": "omnibase-infra",
            "broker_container": "omnibase-infra-redpanda",
            "superuser_source_container": "omnibase-infra-redpanda-sasl-enable",
            "principals": [_OVERLAY_ROW],
        },
    },
    DERIVE: {
        "scope": "broker_acl_principals",
        "topic_uses": [_TOPIC_USE],
        "principals": {
            "scope": "broker_acl_principals",
            "ticket": "OMN-19998",
            "why": "a test",
            "roles": [
                {
                    "name": "runners",
                    "role": "a runner",
                    "principals": ["User:runner"],
                    "nodes": ["node_example_compute"],
                }
            ],
        },
    },
    COVERAGE: {"topic_uses": [_TOPIC_USE], "base_topic_uses": []},
}


def _entries() -> list[dict[str, Any]]:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    return list(contract["handler_routing"]["handlers"])


def _event_model(entry: dict[str, Any]) -> ModelHandlerRef:
    declared = entry.get("event_model")
    assert declared, (
        f"{entry['handler']['name']} declares no event_model: with no model to match, every "
        "command on the shared topic is handed to every dispatcher of the node"
    )
    return ModelHandlerRef(name=declared["name"], module=declared["module"])


def test_the_payload_fixtures_are_valid_for_their_own_models() -> None:
    """Positive control: each fixture validates as its own handler's input model, so a refusal below is real."""
    for entry in _entries():
        name = entry["handler"]["name"]
        module_path, _, class_name = entry["input_model"].rpartition(".")
        model_cls = getattr(importlib.import_module(module_path), class_name)
        model_cls.model_validate(PAYLOADS[name])


def test_each_handler_declares_the_event_model_its_input_model_names() -> None:
    entries = _entries()
    assert {e["handler"]["name"] for e in entries} == {RENDER, DERIVE, COVERAGE}
    for entry in entries:
        declared = _event_model(entry)
        assert f"{declared.module}.{declared.name}" == entry["input_model"]


def test_a_command_is_matched_by_exactly_the_handler_it_is_for() -> None:
    """The regression: the render request must not be offered to the derive and coverage handlers."""
    matchers = {
        entry["handler"]["name"]: _make_payload_type_matcher(_event_model(entry))
        for entry in _entries()
    }
    for payload_for, payload in PAYLOADS.items():
        matched = {name for name, matcher in matchers.items() if matcher(payload)}
        assert matched == {payload_for}, (
            f"a {payload_for} command was offered to {sorted(matched)}; "
            "only its own handler may take it"
        )


def test_the_matcher_also_reads_the_engines_wire_envelope() -> None:
    """The Kafka path hands the matcher {"payload": {...}, ...}; the decision must be the same there."""
    matchers = {
        entry["handler"]["name"]: _make_payload_type_matcher(_event_model(entry))
        for entry in _entries()
    }
    wire = {"payload": PAYLOADS[RENDER], "__bindings": {}, "__debug_trace": {}}
    assert matchers[RENDER](wire)
    assert not matchers[DERIVE](wire)
    assert not matchers[COVERAGE](wire)
