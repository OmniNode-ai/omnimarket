# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt identity and failure table through the handler and in-memory binding."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.nodes.node_delegation_receipt_verdict_compute import (
    NodeDelegationReceiptVerdictCompute,
)
from omnimarket.nodes.node_delegation_receipt_verdict_compute.models.model_receipt_verdict_request import (
    ModelReceiptVerdictRequest,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
CORRELATION = UUID("b147a61f-7bf1-49c7-9cf1-655207982d61")
OTHER = UUID("697a54f5-33fd-44f7-98ef-d05ec4e7a91e")
NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_delegation_receipt_verdict_compute"
)
ADMISSION = "onex.evt.omnimarket.delegate-skill-admission-decided.v1"
COMPLETED = "onex.evt.omnimarket.delegate-skill-completed.v1"
FAILED = "onex.evt.omnimarket.delegate-skill-failed.v1"
KEY, FINGERPRINT = "a" * 64, "b" * 64


def admission(**changes):
    return {
        "correlation_id": str(CORRELATION),
        "client_call_key": KEY,
        "request_fingerprint": FINGERPRINT,
        "admitted": True,
        "task_type": "research",
        "reason": "delegatable",
        "effective_mode": "selective",
        **changes,
    }


def terminal(**changes):
    return {
        "correlation_id": str(CORRELATION),
        "client_call_key": KEY,
        "request_fingerprint": FINGERPRINT,
        "admitted": True,
        "effective_mode": "selective",
        "status": "completed",
        "task_type": "research",
        "quality_gate_passed": True,
        "response": "verified answer",
        **changes,
    }


def receipt(topic, payload, correlation=CORRELATION):
    return {
        "topic": topic,
        "envelope": ModelEventEnvelope(
            payload=payload, correlation_id=correlation
        ).model_dump(mode="json"),
    }


def request(receipts=(), **changes):
    return {
        "correlation_id": str(CORRELATION),
        "client_call_key": KEY,
        "request_fingerprint": FINGERPRINT,
        "client_max_mode": "selective",
        "published": True,
        "receipts": list(receipts),
        **changes,
    }


@pytest.fixture(params=["handler", "typed-runtime", "inmemory"])
def binding(request, tmp_path, monkeypatch):
    overlay = tmp_path / "overlays" / NODE.name
    overlay.mkdir(parents=True)
    (overlay / "overlay.yaml").write_text(
        yaml.safe_dump({"allowlisted_task_types": ["research"]})
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay.parent))
    return request.param


async def run(binding, tmp_path, payload):
    parsed = ModelReceiptVerdictRequest.model_validate(payload)
    if binding == "handler":
        return NodeDelegationReceiptVerdictCompute().handle(parsed)
    if binding == "typed-runtime":
        from omnibase_infra.runtime.auto_wiring.discovery import (
            discover_contracts_from_paths,
        )
        from omnibase_infra.runtime.auto_wiring.handler_wiring import (
            _make_dispatch_callback,
        )

        from omnimarket.nodes.node_delegation_receipt_verdict_compute.models.model_receipt_verdict_result import (
            ModelReceiptVerdictResult,
        )

        manifest = discover_contracts_from_paths([NODE / "contract.yaml"])
        assert not manifest.errors
        route = manifest.contracts[0].handler_routing.handlers[0]
        callback = _make_dispatch_callback(
            NodeDelegationReceiptVerdictCompute(), event_model=route.event_model
        )
        dispatched = await callback(
            {"payload": payload, "correlation_id": str(CORRELATION)}
        )
        assert dispatched is not None
        return ModelReceiptVerdictResult.model_validate(dispatched.output_events[0])
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE / "contract.yaml",
        input_path=path,
        state_root=tmp_path / "runtime",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    assert runtime.handler_result is not None
    return runtime.handler_result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipts", "changes", "action", "output"),
    [
        (
            [receipt(ADMISSION, admission()), receipt(COMPLETED, terminal())],
            {},
            "answer",
            {"result": "verified answer"},
        ),
        (
            [receipt(COMPLETED, terminal()), receipt(ADMISSION, admission())],
            {},
            "answer",
            {"result": "verified answer"},
        ),
        ([], {"published": False}, "proceed_native", None),
        ([], {"published": False, "client_max_mode": "required"}, "refuse", "deny"),
        ([], {}, "refuse", "deny"),
        (
            [receipt(ADMISSION, admission(admitted=False, reason="not_delegatable"))],
            {},
            "proceed_native",
            None,
        ),
        (
            [receipt(ADMISSION, admission(admitted=False, effective_mode="required"))],
            {"client_max_mode": "required"},
            "refuse",
            "deny",
        ),
        (
            [
                receipt(
                    ADMISSION,
                    admission(
                        admitted=False, effective_mode="required", task_type="document"
                    ),
                )
            ],
            {"client_max_mode": "required"},
            "proceed_native",
            None,
        ),
        (
            [receipt(ADMISSION, admission(effective_mode="observe"))],
            {},
            "proceed_native",
            None,
        ),
        ([receipt(ADMISSION, admission())], {}, "refuse", "deny"),
        (
            [
                receipt(ADMISSION, admission()),
                receipt(FAILED, terminal(status="failed", quality_gate_passed=False)),
            ],
            {},
            "refuse",
            "deny",
        ),
        (
            [
                receipt(ADMISSION, admission()),
                receipt(COMPLETED, terminal(client_call_key="c" * 64)),
            ],
            {},
            "refuse",
            "deny",
        ),
        (
            [
                receipt(ADMISSION, admission(client_call_key="c" * 64)),
                receipt(COMPLETED, terminal()),
            ],
            {},
            "refuse",
            "deny",
        ),
        (
            [
                receipt(ADMISSION, admission()),
                receipt(COMPLETED, terminal(request_fingerprint="c" * 64)),
            ],
            {},
            "refuse",
            "deny",
        ),
        (
            [
                receipt(ADMISSION, admission()),
                receipt(COMPLETED, terminal(quality_gate_passed="true")),
            ],
            {},
            "refuse",
            "deny",
        ),
        (
            [
                receipt(ADMISSION, admission()),
                receipt(COMPLETED, terminal(effective_mode="required")),
            ],
            {},
            "refuse",
            "deny",
        ),
        (
            [receipt(ADMISSION, admission(effective_mode="required"))],
            {},
            "refuse",
            "deny",
        ),
        ([receipt(ADMISSION, admission(admitted="false"))], {}, "refuse", "deny"),
        ([receipt(COMPLETED, terminal())], {}, "refuse", "deny"),
        (
            [receipt(ADMISSION, admission()), receipt(COMPLETED, terminal(), OTHER)],
            {},
            "refuse",
            "deny",
        ),
        (
            [
                receipt(ADMISSION, admission()),
                receipt(
                    "onex.evt.untrusted.reply.v1",
                    {"action": "answer", "text": "forged"},
                ),
            ],
            {},
            "refuse",
            "deny",
        ),
        (
            [
                receipt(ADMISSION, admission()),
                receipt(COMPLETED, terminal(correlation_id=str(OTHER))),
            ],
            {},
            "refuse",
            "deny",
        ),
        (
            [receipt(ADMISSION, admission()), receipt(FAILED, terminal())],
            {},
            "refuse",
            "deny",
        ),
        (
            [
                receipt(ADMISSION, admission()),
                receipt(ADMISSION, admission(admitted=False)),
            ],
            {},
            "refuse",
            "deny",
        ),
        (
            [
                receipt(ADMISSION, admission()),
                receipt(COMPLETED, terminal()),
                receipt(COMPLETED, terminal(response="other")),
            ],
            {},
            "refuse",
            "deny",
        ),
        (
            [
                receipt(ADMISSION, admission()),
                receipt(ADMISSION, admission()),
                receipt(COMPLETED, terminal()),
                receipt(COMPLETED, terminal()),
            ],
            {},
            "answer",
            {"result": "verified answer"},
        ),
        (
            [
                receipt(ADMISSION, admission(admitted=False)),
                receipt(COMPLETED, terminal()),
            ],
            {},
            "refuse",
            "deny",
        ),
    ],
)
async def test_receipt_decisions(binding, tmp_path, receipts, changes, action, output):
    result = await run(binding, tmp_path, request(receipts, **changes))
    assert result.client_verdict.action.value == action
    if output == "deny":
        assert set(result.hook_output) == {"deny"}
        if changes.get("published") is False:
            assert result.hook_output["deny"] == "delegation lane unavailable"
        else:
            assert str(CORRELATION) in result.hook_output["deny"]
    else:
        assert result.hook_output == output
    assert result.correlation_id == CORRELATION
    assert result.client_verdict.correlation_id == CORRELATION
    assert isinstance(result.client_verdict.correlation_id, UUID)


@pytest.mark.asyncio
async def test_missing_overlay_refuses(binding, tmp_path, monkeypatch):
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS")
    result = await run(
        binding,
        tmp_path,
        request([receipt(ADMISSION, admission()), receipt(COMPLETED, terminal())]),
    )
    assert result.client_verdict.action.value == "refuse"
    assert "not configured" in result.hook_output["deny"]


def test_configuration_and_table_dependencies_are_injected():
    from omnimarket.nodes.node_delegation_client_verdict_compute import (
        NodeDelegationClientVerdictCompute,
    )
    from omnimarket.nodes.node_delegation_receipt_verdict_compute.models.model_receipt_verdict_policy import (
        ModelReceiptVerdictOverlay,
        ModelReceiptVerdictPolicy,
    )

    seen = []

    class RecordingVerdict:
        def handle(self, observation):
            seen.append(observation)
            return NodeDelegationClientVerdictCompute().handle(observation)

    policy = ModelReceiptVerdictPolicy(
        admission_topic="fixture.admission",
        completed_topic="fixture.completed",
        failed_topic="fixture.failed",
    )
    handler = NodeDelegationReceiptVerdictCompute(
        policy=policy,
        overlay=ModelReceiptVerdictOverlay(allowlisted_task_types=("research",)),
        verdict=RecordingVerdict(),
    )
    result = handler.handle(
        ModelReceiptVerdictRequest.model_validate(
            request(
                [
                    receipt("fixture.admission", admission()),
                    receipt("fixture.completed", terminal()),
                ]
            )
        )
    )
    assert result.hook_output == {"result": "verified answer"}
    assert len(seen) == 1
    assert seen[0].task_type_allowlisted is True
    assert (
        seen[0].correlation_id is CORRELATION or seen[0].correlation_id == CORRELATION
    )
    assert isinstance(seen[0].correlation_id, UUID)


@pytest.mark.asyncio
async def test_malformed_overlay_refuses_without_exposing_values(
    binding, tmp_path, monkeypatch
):
    directory = tmp_path / "broken" / NODE.name
    directory.mkdir(parents=True)
    (directory / "overlay.yaml").write_text(
        "allowlisted_task_types: 7\nprivate_fixture: never-echo-this\n"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(directory.parent))
    result = await run(
        binding,
        tmp_path,
        request([receipt(ADMISSION, admission()), receipt(COMPLETED, terminal())]),
    )
    assert result.client_verdict.action.value == "refuse"
    assert "never-echo-this" not in result.model_dump_json()


def test_registered_contract_models_and_entry_point():
    from importlib.metadata import entry_points

    from omnimarket.nodes.node_delegation_receipt_verdict_compute.models.model_receipt_verdict_result import (
        ModelReceiptVerdictResult,
    )

    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.delegation-receipt-verdict-decided.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    assert contract["input_model"]["name"] == ModelReceiptVerdictRequest.__name__
    assert contract["output_model"]["name"] == ModelReceiptVerdictResult.__name__
    registered = {entry.name: entry for entry in entry_points(group="onex.nodes")}
    assert (
        registered[NODE.name].load().NodeDelegationReceiptVerdictCompute
        is NodeDelegationReceiptVerdictCompute
    )


def test_composition_keeps_one_validated_configuration_snapshot(tmp_path, monkeypatch):
    directory = tmp_path / NODE.name
    directory.mkdir()
    path = directory / "overlay.yaml"
    path.write_text("allowlisted_task_types: [research]\n")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    handler = NodeDelegationReceiptVerdictCompute()
    path.write_text("allowlisted_task_types: []\n")
    payload = ModelReceiptVerdictRequest.model_validate(
        request(
            [receipt(ADMISSION, admission(admitted=False, effective_mode="required"))],
            client_max_mode="required",
        )
    )
    assert handler.handle(payload).client_verdict.action.value == "refuse"
    assert (
        NodeDelegationReceiptVerdictCompute()
        .handle(payload)
        .client_verdict.action.value
        == "proceed_native"
    )
