# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams scenario R9 leans on (OMN-24554): R8's adapter, the committed corpus payloads and the corpus bus request.

R9 publishes its corpus through R8's adapter pieces: the command and terminal topics come from the command node's
contract via ``load_node_topics``, the failed terminal's wording comes from ``failed_terminal_reason``, and the bus is
opened exactly as R8's tenant reads open it. These tests hold each of those seams: a change on either side that drops a
key or renames a topic turns them red.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from importlib import resources
from typing import NoReturn
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import JsonValue

from omnimarket.models.model_delegation_scenario_settings import (
    ModelScenarioTenantReadOverlay,
)
from omnimarket.models.model_tenant_read_bus import (
    EnumTenantReadKind,
    ModelTenantReadCommand,
)
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal, ModelScmTopics
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers import (
    adapter_r8_bus_request,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_bus_request import (
    build_bus_request,
    failed_terminal_reason,
    load_node_topics,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    load_lane_settings,
)
from omnimarket.nodes.node_delegation_scenario_r9_effect.handlers import (
    adapter_r9_corpus,
)
from omnimarket.nodes.node_delegation_scenario_r9_effect.handlers.adapter_r9_corpus import (
    CorpusRequest,
    ModelCorpusPayload,
    build_corpus_request,
)
from omnimarket.nodes.node_delegation_scenario_r9_effect.handlers.handler_scenario_r9 import (
    NODE_DIR,
    ModelR9Case,
    load_bus_settings,
    load_inputs,
)

pytestmark = pytest.mark.unit

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OVERLAY = ModelScenarioTenantReadOverlay(
    bus="inmemory", bus_lane="dev", probe_tenant_id=TENANT, timeout_seconds=5.0
)
INPUTS = load_inputs(NODE_DIR / "inputs/r9_run.yaml")
#: The refusal terminal one malformed payload is answered with (the shape the runtime's envelope validation returns).
REFUSAL_PAYLOAD: dict[str, JsonValue] = {
    "failure_code": INPUTS.refusal_failure_code,
    "retryable": INPUTS.retryable,
    "failure_reason": "prompt",
}
NODE_FAILURE_PAYLOAD: dict[str, JsonValue] = {
    "reason": "not_configured",
    "detail": "analytics source",
}
KEYS_ONLY_PAYLOAD: dict[str, JsonValue] = {"status": "failed", "origin_topic": "t"}


def _patched_corpus_bus(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failed: bool = True,
    payload: Mapping[str, JsonValue] = REFUSAL_PAYLOAD,
    error: Exception | None = None,
) -> dict[str, object]:
    """Patch the corpus bus open and caller as the R8 bus-request tests do; return what the request published."""

    seen: dict[str, object] = {}

    @asynccontextmanager
    async def fake_open_bus(bus_opts: dict[str, object]) -> AsyncIterator[object]:
        seen["bus_opts"] = dict(bus_opts)
        yield object()

    class _Caller:
        def __init__(self, bus: object) -> None:
            self._bus = bus

        async def call_topics(
            self,
            *,
            topics: ModelScmTopics,
            request: object,
            correlation_id: UUID,
            timeout_s: float,
        ) -> ModelScmTerminal:
            seen["topics"] = topics
            seen["request"] = request
            seen["timeout_s"] = timeout_s
            if error is not None:
                raise error
            return ModelScmTerminal(
                receipt=ModelScmBusReceipt(
                    correlation_id=correlation_id,
                    command_topic=topics.command,
                    command_envelope_id=uuid4(),
                    terminal_topic=topics.failure if failed else topics.success,
                    terminal_envelope_id=uuid4(),
                ),
                failed=failed,
                payload=dict(payload),
            )

    monkeypatch.setattr(adapter_r9_corpus, "_open_bus", fake_open_bus)
    monkeypatch.setattr(adapter_r9_corpus, "ScmBusCaller", _Caller)
    return seen


def _corpus_request() -> CorpusRequest:
    """The real corpus request, built from the contract's bus block, with no bus behind it."""
    settings = load_bus_settings()
    return build_corpus_request(
        OVERLAY,
        command_node=settings.command_node,
        workspace_env=settings.workspace_env,
        environment={},
    )


# -- the seam to the command node's contract ------------------------------------------------------------------------


def test_seam_the_corpus_topics_are_the_command_nodes_contracts() -> None:
    """No topic is hard-coded: the topics come from the command node's contract, read here with yaml."""
    package = load_bus_settings().command_node
    contract = yaml.safe_load(
        resources.files(package).joinpath("contract.yaml").read_text(encoding="utf-8")
    )
    dispatch = contract["runtime_dispatch"]
    assert load_node_topics(package) == ModelScmTopics(
        command=dispatch["command_topic"], **dispatch["terminal_events"]
    )


@pytest.mark.parametrize("case", INPUTS.cases, ids=lambda case: case.id)
def test_seam_every_committed_case_is_publishable_as_the_corpus_payload(
    case: ModelR9Case,
) -> None:
    """A case's payload is exactly what is published: no field is named, dropped or coerced."""
    assert ModelCorpusPayload(**case.payload).model_dump(mode="json") == case.payload


# -- the corpus bus request -----------------------------------------------------------------------------------------


def test_the_corpus_request_answers_a_failed_terminal_and_its_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patched_corpus_bus(monkeypatch, failed=True, payload=REFUSAL_PAYLOAD)
    answer = _corpus_request()(INPUTS.cases[0].payload, 12.0)
    assert answer.answered is True
    assert answer.failed is True
    assert dict(answer.payload) == REFUSAL_PAYLOAD
    assert seen["topics"] == load_node_topics(load_bus_settings().command_node)
    assert seen["timeout_s"] == 12.0
    published = seen["request"]
    assert isinstance(published, ModelCorpusPayload)
    assert published.model_dump(mode="json") == INPUTS.cases[0].payload


def test_the_corpus_request_answers_no_terminal_when_the_bus_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_corpus_bus(monkeypatch, error=TimeoutError())
    answer = _corpus_request()(INPUTS.cases[0].payload, 12.0)
    assert answer.answered is False
    assert answer.reason is not None
    assert answer.reason.startswith("no terminal")


@pytest.mark.parametrize(
    "payload",
    [REFUSAL_PAYLOAD, NODE_FAILURE_PAYLOAD, KEYS_ONLY_PAYLOAD],
    ids=["refusal", "node_failure", "keys_only"],
)
def test_seam_a_failed_terminals_reason_is_the_shared_failed_terminal_wording(
    monkeypatch: pytest.MonkeyPatch,
    payload: Mapping[str, JsonValue],
) -> None:
    """The wording seam to R8: whatever failed shape answers, the reason is the shared failed-terminal reason."""
    _patched_corpus_bus(monkeypatch, failed=True, payload=payload)
    answer = _corpus_request()(INPUTS.cases[0].payload, 12.0)
    assert answer.answered is True
    assert answer.failed is True
    assert answer.reason == failed_terminal_reason(dict(answer.payload))


def test_seam_both_adapters_open_the_bus_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9's corpus request opens the bus with exactly the options R8's tenant reads pass: the same keys and values."""
    r9_opts: dict[str, object] = {}
    r8_opts: dict[str, object] = {}

    class _StopError(Exception):
        """Stops each request at the bus open, after its options were captured."""

    def r9_open_bus(bus_opts: dict[str, object]) -> NoReturn:
        r9_opts.update(bus_opts)
        raise _StopError

    def r8_open_bus(bus_opts: dict[str, object]) -> NoReturn:
        r8_opts.update(bus_opts)
        raise _StopError

    monkeypatch.setattr(adapter_r9_corpus, "_open_bus", r9_open_bus)
    monkeypatch.setattr(adapter_r8_bus_request, "_open_bus", r8_open_bus)

    settings = load_lane_settings()
    assert settings.bus is not None
    r8_request = build_bus_request(
        OVERLAY,
        savings_node=settings.bus.savings_node,
        delegations_node=settings.bus.delegations_node,
        workspace_env="WS",
        environment={},
    )
    with pytest.raises(_StopError):
        r8_request(
            ModelTenantReadCommand(
                correlation_id=uuid4(),
                kind=EnumTenantReadKind.SAVINGS,
                tenant_id=TENANT,
            ),
            1.0,
        )

    with pytest.raises(_StopError):
        _corpus_request()(INPUTS.cases[0].payload, 1.0)

    assert set(r9_opts) == set(r8_opts)
    assert set(r9_opts) == {"bus", "bus_lane", "kafka_bootstrap", "omnibase_path"}
    assert r9_opts == r8_opts
    assert r9_opts["bus"] == OVERLAY.bus
    assert r9_opts["bus_lane"] == OVERLAY.bus_lane
    assert r9_opts["omnibase_path"] is None
