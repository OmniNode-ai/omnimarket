# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live five-leg I/O boundary for the hook->cloud chain probe (OMN-17202).

This is the ONLY module in the node that touches a broker, a filesystem or the
network. It produces structural observations; it never classifies. Two rules it
holds to, because both were violated by the per-leg proofs this node replaces:

  * **Never fabricate a blocker.** When a fact cannot be established (the
    forwarder's lane attachment is not measurable from this machine, say), the
    observation records the benign value and carries the reason in ``detail``
    -- so the classifier reports the blockers it can PROVE (the
    contract-declared allowlist denial is provable offline) rather than
    inventing a lane mismatch it did not measure.
  * **Never inherit the lane.** The hook edge lane comes from the hooks' own
    env authority, never from the ambient shell and never from
    ``ModelKafkaEventBusConfig.default()`` (which applies environment
    overrides). An unresolvable lane refuses; it does not guess.
  * **Never reach for the cluster.** Legs 4 and 5 are read over the cloud
    gateway's HTTPS routes. The operator runs this from the Mac with no
    kubeconfig (AC4); a probe that needs cluster access is not the probe the
    ticket asked for.

Leg 2 sequencing: the consumer is subscribed and primed to the log end BEFORE
leg 1 produces, so the readback does not have to replay the ~61k-record backlog
on the hook topic to find one correlation id.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import yaml

from omnimarket.nodes.node_hook_chain_probe_effect.models.model_hook_chain_probe import (
    ModelCloudGatewayObservation,
    ModelCloudProjectionObservation,
    ModelForwarderObservation,
    ModelHookChainAddress,
    ModelLocalBusObservation,
    ModelLocalEmitObservation,
)

_CONTRACT_PATH: Final[Path] = Path(__file__).parent / "contract.yaml"
_CORRELATION_HEADER: Final[str] = "correlation-id"
_POLL_SLICE_MS: Final[int] = 2000
_HTTP_TIMEOUT_SECONDS: Final[float] = 15.0

#: ``KEY=value`` / ``export KEY="value"`` as the hook env authorities write it.
_ENV_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$"
)


class _TopicAbsentError(LookupError):
    """The topic does not exist on the probed lane."""


class HookEdgeLaneUnresolvedError(RuntimeError):
    """No hook env authority declared the lane the hooks publish to.

    Raised rather than falling back to the ambient shell. The fallback IS the
    defect: ``ModelKafkaEventBusConfig.default()`` applies environment
    overrides, so a probe built on it silently emits to whatever lane the
    calling shell carries. On this Mac the hooks' authority declares the
    stability lane while an interactive shell carries the dev lane, and that
    ambiguity is what produced the wrong-broker verdict that flipped OMN-16162
    out of Done and the falsified OMN-16996 (OMN-17010).
    """


def default_hook_edge_env_files() -> list[Path]:
    """The hook env authorities, in the hooks' OWN precedence order.

    Read from this node's contract and expanded against the environment;
    entries whose variables do not expand are dropped rather than guessed at.
    Order matters and mirrors the installed hook runtime (``common.sh``: global
    ``~/.omnibase/.env`` first, project ``.env`` overrides), so a later file
    wins.
    """
    declared = _load_probe_config().get("hook_edge_env_files", [])
    resolved: list[Path] = []
    for entry in declared:
        expanded = os.path.expanduser(os.path.expandvars(str(entry)))
        if "$" in expanded:
            continue
        resolved.append(Path(expanded))
    return resolved


def resolve_hook_edge_lane(env_files: Sequence[Path]) -> tuple[str, str]:
    """Resolve the hook EDGE lane from the hooks' own env authorities.

    Returns ``(lane, authority)`` where ``authority`` names the file the value
    came from -- recorded in the result so a reader can see WHICH authority the
    probe trusted rather than having to assume. The ambient environment is
    never consulted.

    Raises ``HookEdgeLaneUnresolvedError`` when no authority declares the variable.
    """
    variable = str(_load_probe_config()["hook_edge_lane_var"])
    lane: str | None = None
    authority: str | None = None
    for path in env_files:
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            if line.lstrip().startswith("#"):
                continue
            match = _ENV_ASSIGNMENT.match(line)
            if match is None or match.group(1) != variable:
                continue
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                lane, authority = value, str(path)
    if lane is None or authority is None:
        searched = ", ".join(str(path) for path in env_files) or "<no authorities>"
        raise HookEdgeLaneUnresolvedError(
            f"no hook env authority declares {variable} (searched: {searched}); "
            "refusing to fall back to the ambient shell"
        )
    return (lane, authority)


def load_forwarder_liveness_topic(module_path: str) -> tuple[str, str | None]:
    """Read the forwarder's own contract-declared liveness/canary topic.

    This topic is the evidence of WHICH lane the forwarder is attached to: the
    forwarder produces and reads it back on its own local bus, so its presence
    on the hook edge lane is a live, un-forgeable answer to "is the forwarder
    even listening where the hooks publish?" -- readable from the operator Mac
    with no cluster access (AC4).
    """
    contract = _read_forwarder_contract(module_path)
    if isinstance(contract, str):
        return ("", contract)
    canary = contract.get("config", {}).get("gateway_forwarder", {}).get("canary", {})
    topic = str(canary.get("topic", ""))
    if not topic:
        return ("", "forwarder contract declares no canary topic")
    return (topic, None)


def _read_forwarder_contract(module_path: str) -> dict[str, Any] | str:
    """Load the installed forwarder contract, or return the reason it could not be."""
    import importlib.util

    spec = importlib.util.find_spec(module_path)
    if spec is None or not spec.submodule_search_locations:
        return f"forwarder package {module_path} not importable"
    contract_path = Path(next(iter(spec.submodule_search_locations))) / "contract.yaml"
    if not contract_path.is_file():
        return f"forwarder contract absent at {contract_path}"
    loaded: dict[str, Any] = yaml.safe_load(contract_path.read_text())
    return loaded


def _load_probe_config() -> dict[str, Any]:
    """Read this node's contract-declared addressing block."""
    contract: dict[str, Any] = yaml.safe_load(_CONTRACT_PATH.read_text())
    config = contract.get("config", {}).get("chain_probe", {})
    if not isinstance(config, dict) or not config:
        raise ValueError(
            "node_hook_chain_probe_effect contract is missing config.chain_probe; "
            "the probe refuses to run on hardcoded addressing"
        )
    return config


def _load_forwarder_policy(module_path: str) -> tuple[tuple[str, ...], str, str | None]:
    """Read the forwarder's declared outbound mirror set and cloud-leg transport.

    Source of truth is the forwarder's OWN contract in the installed
    ``omnibase_infra`` package -- never a copy kept here, which would drift into
    a second, wrong allowlist the moment OMN-16979 widens the real one.

    Returns ``(outbound_topics, cloud_leg_transport, detail)`` where ``detail``
    is non-None only when the policy could not be read.
    """
    contract = _read_forwarder_contract(module_path)
    if isinstance(contract, str):
        return ((), "unknown", contract)

    forwarder = contract.get("config", {}).get("gateway_forwarder", {})
    mirror = forwarder.get("mirror_topics", {})
    outbound = tuple(str(topic) for topic in mirror.get("outbound", []))
    transport = str(forwarder.get("cloud_leg", {}).get("transport", "unknown"))
    if not outbound:
        return ((), transport, "forwarder contract declares no outbound mirror set")
    return (outbound, transport, None)


class LiveHookChainProbes:
    """Live implementation of ``ProtocolHookChainProbes``."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout_seconds = timeout_seconds
        self._config = _load_probe_config()
        self._consumer: Any | None = None
        self._emit_offset: int | None = None

    # -- addressing ---------------------------------------------------------

    async def resolve_address(self) -> ModelHookChainAddress:
        """Resolve the hook topic and cloud base URL from contracts, lane from the hooks.

        The lane deliberately does NOT come from ``ModelKafkaEventBusConfig.default()``:
        that applies environment overrides, so the probe would emit to whatever
        lane the calling shell carries rather than the one the hooks publish to.
        Raises ``HookEdgeLaneUnresolvedError`` when no hook authority declares it.
        """
        try:
            emit_lane, authority = resolve_hook_edge_lane(default_hook_edge_env_files())
        except HookEdgeLaneUnresolvedError as exc:
            emit_lane, authority = "", f"unresolved: {exc}"
        return ModelHookChainAddress(
            hook_topic=str(self._config["hook_topic"]),
            emit_lane=emit_lane,
            emit_lane_authority=authority,
            cloud_gateway_base_url=self._resolve_cloud_base_url(),
        )

    def _resolve_cloud_base_url(self) -> str:
        """Resolve the cloud gateway base URL through the contract's secret ref.

        An unresolvable ref returns ``unresolved:<ref>`` rather than a guessed
        URL: legs 4 and 5 then report GATEWAY_UNREACHABLE carrying the ref that
        could not be resolved, which is an actionable fact. Guessing a hostname
        here would turn a config gap into a fake network failure.
        """
        ref = str(self._config["cloud_gateway_base_url_ref"])
        try:
            from omnibase_infra.runtime.models.model_secret_resolver_config import (
                ModelSecretResolverConfig,
            )
            from omnibase_infra.runtime.runtime_profile import (
                resolve_secret_resolver_config_path,
            )
            from omnibase_infra.runtime.secret_resolver import SecretResolver

            config = ModelSecretResolverConfig.model_validate(
                yaml.safe_load(Path(resolve_secret_resolver_config_path()).read_text())
            )
            secret = SecretResolver(config).get_secret(ref, required=True)
        except Exception as exc:
            return f"unresolved:{ref}:{type(exc).__name__}"
        if secret is None:
            return f"unresolved:{ref}"
        return str(secret.get_secret_value())

    # -- leg 1 + 2 ----------------------------------------------------------

    async def emit(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ) -> ModelLocalEmitObservation:
        """Subscribe at the log end, then publish one correlated hook-shaped event."""
        from omnibase_infra.event_bus.kafka_transport import KafkaTransport

        if not address.emit_lane:
            return ModelLocalEmitObservation(
                emitted=False,
                lane="",
                topic=address.hook_topic,
                lane_resolved=False,
                detail=address.emit_lane_authority,
            )

        try:
            consumer = KafkaTransport.from_bootstrap(
                address.emit_lane,
                group=f"onex.probe.hook-chain.{correlation_id}",
                topics=(address.hook_topic,),
                auto_offset_reset="latest",
            )
            await consumer.start()
            self._consumer = consumer

            producer = KafkaTransport.from_bootstrap(address.emit_lane)
            await producer.start()
            try:
                await producer.send(
                    address.hook_topic,
                    key=correlation_id.encode(),
                    value=json.dumps(
                        {
                            "correlation_id": correlation_id,
                            "probe": "node_hook_chain_probe_effect",
                            "ticket": "OMN-17202",
                            "emitted_at": time.time(),
                        }
                    ).encode(),
                    headers={_CORRELATION_HEADER: correlation_id.encode()},
                )
            finally:
                await producer.close()
        except Exception as exc:
            return ModelLocalEmitObservation(
                emitted=False,
                lane=address.emit_lane,
                topic=address.hook_topic,
                detail=f"{type(exc).__name__}: {exc}",
            )

        return ModelLocalEmitObservation(
            emitted=True,
            lane=address.emit_lane,
            topic=address.hook_topic,
            detail=None,
        )

    async def read_local_bus(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ) -> ModelLocalBusObservation:
        """Poll the primed consumer for the correlated record until the deadline."""
        consumer = self._consumer
        if consumer is None:
            return ModelLocalBusObservation(
                observed=False,
                lane=address.emit_lane,
                topic=address.hook_topic,
                detail="no consumer was primed before emit",
            )

        deadline = time.monotonic() + self._timeout_seconds
        try:
            while time.monotonic() < deadline:
                messages = await consumer.poll(
                    max_messages=200, timeout_ms=_POLL_SLICE_MS
                )
                for message in messages:
                    if self._matches(message, correlation_id):
                        offset = getattr(message, "offset", None)
                        self._emit_offset = offset
                        return ModelLocalBusObservation(
                            observed=True,
                            lane=address.emit_lane,
                            topic=address.hook_topic,
                            offset=offset,
                        )
        except Exception as exc:
            return ModelLocalBusObservation(
                observed=False,
                lane=address.emit_lane,
                topic=address.hook_topic,
                detail=f"{type(exc).__name__}: {exc}",
            )
        finally:
            await self._close_consumer()

        return ModelLocalBusObservation(
            observed=False,
            lane=address.emit_lane,
            topic=address.hook_topic,
            detail=f"correlation id absent after {self._timeout_seconds:.0f}s of readback",
        )

    @staticmethod
    def _matches(message: Any, correlation_id: str) -> bool:
        headers = dict(getattr(message, "headers", ()) or ())
        header_value = headers.get(_CORRELATION_HEADER)
        if isinstance(header_value, bytes) and header_value.decode() == correlation_id:
            return True
        key = getattr(message, "key", None)
        return isinstance(key, bytes) and key.decode() == correlation_id

    async def _close_consumer(self) -> None:
        consumer, self._consumer = self._consumer, None
        if consumer is not None:
            await consumer.close()

    # -- leg 3 --------------------------------------------------------------

    async def read_forwarder(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ) -> ModelForwarderObservation:
        """Read the forwarder's declared policy and measure its lane attachment."""
        outbound, transport, policy_detail = _load_forwarder_policy(
            str(self._config["forwarder_contract_module"])
        )
        liveness_topic, liveness_detail = load_forwarder_liveness_topic(
            str(self._config["forwarder_contract_module"])
        )
        present, present_detail = await self._forwarder_present_on_lane(
            address=address, liveness_topic=liveness_topic
        )
        advanced, advance_detail = await self._forwarder_group_advanced(address)

        details = [
            text
            for text in (
                policy_detail,
                liveness_detail,
                present_detail,
                advance_detail,
            )
            if text
        ]
        return ModelForwarderObservation(
            mirror_outbound_topics=outbound,
            cloud_leg_transport=transport,
            liveness_topic=liveness_topic,
            forwarder_present_on_emit_lane=present,
            consumer_group_advanced=advanced,
            detail="; ".join(details) or None,
        )

    async def _forwarder_present_on_lane(
        self, *, address: ModelHookChainAddress, liveness_topic: str
    ) -> tuple[bool, str | None]:
        """Is the forwarder attached to the lane the hooks publish to?

        Measured, not asserted: the forwarder writes its own contract-declared
        canary topic onto whichever local bus it is bound to, so that topic
        carrying records on the hook edge lane is the evidence that it is bound
        HERE. An unmeasurable answer returns ``True`` -- i.e. NO lane-mismatch
        claim -- because inventing a mismatch from a failed measurement is the
        same error class the probe exists to end.
        """
        if not liveness_topic:
            return (
                True,
                "forwarder liveness topic unknown; lane attachment NOT asserted",
            )
        try:
            total = await self._liveness_high_watermark(
                lane=address.emit_lane, topic=liveness_topic
            )
        except _TopicAbsentError:
            return (
                False,
                f"forwarder liveness topic {liveness_topic} does not exist on "
                f"lane {address.emit_lane} -- the forwarder is not attached to "
                "the lane the hooks publish to",
            )
        except (Exception, asyncio.CancelledError) as exc:
            return (
                True,
                f"forwarder lane attachment not measurable from this host "
                f"({type(exc).__name__}); lane mismatch NOT asserted",
            )
        if total > 0:
            return (True, None)
        return (
            False,
            f"forwarder liveness topic {liveness_topic} has high-watermark 0 on "
            f"lane {address.emit_lane} -- the forwarder is not attached to the "
            "lane the hooks publish to",
        )

    @staticmethod
    async def _liveness_high_watermark(*, lane: str, topic: str) -> int:
        """Summed end offset of ``topic`` on ``lane``.

        Raises ``_TopicAbsentError`` when the topic does not exist there -- absence
        and emptiness are the same verdict here but not the same evidence, and
        the operator reading the result should see which one it was.

        ``stop()`` is suppressed separately: aiokafka's coordinator teardown
        raises ``CancelledError``, which is a ``BaseException`` in 3.12 and so
        escapes an ``except Exception`` around the whole block -- that exact
        escape crashed the first live run of this probe instead of reporting a
        leg.
        """
        from aiokafka import AIOKafkaConsumer
        from aiokafka.structs import TopicPartition
        from omnibase_infra.event_bus.kafka_auth import (
            build_aiokafka_auth_kwargs_from_env,
        )

        consumer = AIOKafkaConsumer(
            bootstrap_servers=lane, **build_aiokafka_auth_kwargs_from_env()
        )
        await consumer.start()
        try:
            await consumer.topics()  # force a metadata fetch before asking
            partition_ids = consumer.partitions_for_topic(topic)
            if not partition_ids:
                raise _TopicAbsentError(topic)
            end_offsets = await consumer.end_offsets(
                [TopicPartition(topic, pid) for pid in partition_ids]
            )
            return sum(int(offset) for offset in end_offsets.values())
        finally:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await consumer.stop()

    async def _forwarder_group_advanced(
        self, address: ModelHookChainAddress
    ) -> tuple[bool, str | None]:
        """Did the forwarder's consumer group advance past the probe's offset.

        The forwarder contract declares no consumer group, so the group name
        comes from this node's own contract and is the probe's assumption, not
        the forwarder's declaration. It is only ever consulted after the
        allowlist, lane-attachment and transport facts all pass, so a wrong
        guess here can never masquerade as one of those three.
        """
        if self._emit_offset is None:
            return (False, "no emit offset to compare the forwarder group against")
        group = str(self._config["forwarder_consumer_group"])
        try:
            from aiokafka.admin import AIOKafkaAdminClient
            from aiokafka.structs import TopicPartition
            from omnibase_infra.event_bus.kafka_auth import (
                build_aiokafka_auth_kwargs_from_env,
            )

            admin = AIOKafkaAdminClient(
                bootstrap_servers=address.emit_lane,
                **build_aiokafka_auth_kwargs_from_env(),
            )
            await admin.start()
            try:
                offsets = await admin.list_consumer_group_offsets(group)
            finally:
                await admin.close()
            for partition, metadata in offsets.items():
                if (
                    isinstance(partition, TopicPartition)
                    and partition.topic == address.hook_topic
                    and metadata.offset > self._emit_offset
                ):
                    return (True, None)
            return (
                False,
                f"consumer group {group} has not committed past the probe offset",
            )
        except Exception as exc:
            return (False, f"consumer group {group} unreadable: {type(exc).__name__}")

    # -- leg 4 + 5 ----------------------------------------------------------

    async def read_cloud_gateway(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ) -> ModelCloudGatewayObservation:
        reachable, status, found, detail = await self._read_cloud_route(
            base_url=address.cloud_gateway_base_url,
            path=str(self._config["cloud_gateway_ingest_path"]),
            correlation_id=correlation_id,
        )
        return ModelCloudGatewayObservation(
            reachable=reachable,
            status_code=status,
            correlation_found=found,
            detail=detail,
        )

    async def read_cloud_projection(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ) -> ModelCloudProjectionObservation:
        reachable, status, found, detail = await self._read_cloud_route(
            base_url=address.cloud_gateway_base_url,
            path=str(self._config["cloud_projection_path"]),
            correlation_id=correlation_id,
        )
        return ModelCloudProjectionObservation(
            reachable=reachable,
            status_code=status,
            row_found=found,
            detail=detail,
        )

    async def _read_cloud_route(
        self, *, base_url: str, path: str, correlation_id: str
    ) -> tuple[bool, int | None, bool, str | None]:
        if base_url.startswith("unresolved:"):
            return (False, None, False, f"cloud gateway base URL {base_url}")
        url = f"{base_url.rstrip('/')}{path}/{correlation_id}"
        return await asyncio.to_thread(self._read_cloud_route_sync, url)

    @staticmethod
    def _read_cloud_route_sync(url: str) -> tuple[bool, int | None, bool, str | None]:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=_HTTP_TIMEOUT_SECONDS
            ) as response:
                status = int(response.status)
                body = response.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            # A definitive status: refused (401/403) and absent (404) are
            # different facts and the classifier must see which one it was.
            return (True, int(exc.code), False, f"HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return (False, None, False, type(exc).__name__)
        return (
            True,
            status,
            bool(body.strip()) and body.strip() not in {"[]", "{}"},
            None,
        )


__all__: list[str] = [
    "HookEdgeLaneUnresolvedError",
    "LiveHookChainProbes",
    "default_hook_edge_env_files",
    "load_forwarder_liveness_topic",
    "resolve_hook_edge_lane",
]
