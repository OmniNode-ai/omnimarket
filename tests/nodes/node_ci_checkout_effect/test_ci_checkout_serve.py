# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-host serve of node_ci_checkout_effect: golden and error chains over the bus (OMN-24287).

Exemplar followed: tests/nodes/node_repo_state_query_effect (the serve test) and this node's golden chain test.
The serve is the seam between the bus and the handler: it answers its own host's requests, ignores another
host's, and ends an unreadable request on the failed terminal.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import event_type_for
from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.handlers.bus_binding.select import positional_opener
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.lab_work.bus import _bytes
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_cli import (
    load_topics,
    result_from_terminal,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_serve import (
    CiCheckoutHost,
    bus_options,
    host_group_id,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.handler_ci_checkout import (
    HandlerCiCheckout,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    EnumCiCheckoutStatus,
    ModelCiCheckoutRequest,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout_deployment import (
    ModelCiCheckoutDeployment,
)
from tests.nodes.node_ci_checkout_effect.planted import (
    HOST,
    deployment_document,
    plant_bare_mirror,
)

pytestmark = pytest.mark.unit
OTHER_HOST = "another-host"


def _handler(mirrors: Path) -> HandlerCiCheckout:
    deployment = ModelCiCheckoutDeployment.model_validate(deployment_document(mirrors))
    return HandlerCiCheckout(overlay=deployment.resolve(HOST, home="/unused"))


def _request(sha: str, host: str = HOST) -> ModelCiCheckoutRequest:
    return ModelCiCheckoutRequest(
        correlation_id=uuid.uuid4(),
        repo="OmniNode-ai/omnimarket",
        sha=sha,
        ref=None,
        requested_by_lane="test-lane",
        host=host,
    )


async def _ask(bus: object, request: ModelCiCheckoutRequest, timeout_s: float = 10):
    return await ScmBusCaller(bus).call_topics(  # type: ignore[arg-type]
        topics=load_topics(),
        request=request,
        correlation_id=request.correlation_id,
        timeout_s=timeout_s,
    )


@pytest.mark.asyncio
async def test_golden_chain_a_held_sha_is_served_on_the_completed_topic(
    tmp_path: Path,
) -> None:
    mirrors, sha = plant_bare_mirror(tmp_path)
    async with positional_opener(open_lab_run_bus)("inmemory", None, None, None) as bus:
        host = CiCheckoutHost(bus, _handler(mirrors), HOST)
        await host.start()
        request = _request(sha)
        terminal = await _ask(bus, request)
        await host.stop()
    result = result_from_terminal(terminal.payload, request)
    assert not terminal.failed
    assert terminal.receipt.terminal_topic == load_topics().success
    assert result.status is EnumCiCheckoutStatus.SERVED
    assert result.upstream_fetches == 0
    assert result.serve_url is not None
    assert result.serve_url.startswith("file://")
    assert result.host == HOST


@pytest.mark.asyncio
async def test_error_chain_a_sha_no_source_holds_is_answered_not_found(
    tmp_path: Path,
) -> None:
    mirrors, _sha = plant_bare_mirror(tmp_path)
    async with positional_opener(open_lab_run_bus)("inmemory", None, None, None) as bus:
        host = CiCheckoutHost(bus, _handler(mirrors), HOST)
        await host.start()
        request = _request("b" * 40)
        terminal = await _ask(bus, request)
        await host.stop()
    result = result_from_terminal(terminal.payload, request)
    assert terminal.receipt.terminal_topic == load_topics().success
    assert result.status is EnumCiCheckoutStatus.NOT_FOUND
    assert result.serve_url is None


@pytest.mark.asyncio
async def test_error_chain_an_unreadable_request_ends_on_the_failed_topic(
    tmp_path: Path,
) -> None:
    mirrors, _sha = plant_bare_mirror(tmp_path)
    topics = load_topics()
    async with positional_opener(open_lab_run_bus)("inmemory", None, None, None) as bus:
        host = CiCheckoutHost(bus, _handler(mirrors), HOST)
        await host.start()
        command = ModelEventEnvelope[dict[str, object]](
            payload={"host": HOST, "junk": 1},
            correlation_id=uuid.uuid4(),
            event_type=event_type_for(topics.command),
        )

        failures: list[bytes] = []

        async def on_failed(message: object) -> None:
            failures.append(message.value)  # type: ignore[attr-defined]

        unsubscribe = await bus.subscribe_ungrouped(topics.failure, on_failed)  # type: ignore[attr-defined]
        await bus.publish(topics.command, b"k", _bytes(command))
        for _ in range(50):
            if failures:
                break
            await asyncio.sleep(0.1)
        await unsubscribe()
        await host.stop()
    assert failures, "no terminal on the failed topic"
    assert b'"ok": false' in failures[0]
    assert str(command.envelope_id).encode() in failures[0]


@pytest.mark.asyncio
async def test_a_request_for_another_host_is_ignored(tmp_path: Path) -> None:
    """Failing control: the host filter. The same sha asked of this host is answered; asked of another host, nothing is."""
    mirrors, sha = plant_bare_mirror(tmp_path)
    async with positional_opener(open_lab_run_bus)("inmemory", None, None, None) as bus:
        host = CiCheckoutHost(bus, _handler(mirrors), HOST)
        await host.start()
        with pytest.raises(TimeoutError):
            await _ask(bus, _request(sha, host=OTHER_HOST), timeout_s=1)
        answered = await _ask(bus, _request(sha))
        await host.stop()
    assert answered.receipt.terminal_topic == load_topics().success


def test_the_consumer_group_names_the_host_and_the_acl_prefix() -> None:
    group = host_group_id("h051")
    assert group.endswith(".h051")
    # The broker rows are prefixed on this family; a renamed service would leave the serve with no group row.
    assert group.startswith("local.omnimarket.node_ci_checkout_effect.consume.v1.")


def test_the_bus_options_come_from_the_overlay_profile_and_the_named_variable() -> None:
    document = deployment_document(Path("/unused"))
    document["profiles"]["dev"] = {
        "kind": "kafka",
        "lane": "dev",
        "workspace_root_env": "TEST_WORKSPACE_ROOT",
    }
    document["default_profile"] = "dev"
    deployment = ModelCiCheckoutDeployment.model_validate(document)
    opts = bus_options(deployment, None, {"TEST_WORKSPACE_ROOT": "/workspace"})
    assert opts == {
        "bus": "kafka",
        "bus_lane": "dev",
        "kafka_bootstrap": None,
        "omnibase_path": Path("/workspace"),
    }
    assert bus_options(deployment, "inmemory", {})["bus"] == "inmemory"
