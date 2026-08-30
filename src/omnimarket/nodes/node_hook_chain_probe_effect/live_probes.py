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
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

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


def build_cloud_read_url(*, base_url: str, path: str, correlation_id: str) -> str:
    """Build the cloud read URL the supplier route actually serves.

    The correlation id rides as a QUERY parameter because that is the signature
    OMN-17205 deployed (``GET /v1/projections/hook-events/by-correlation?
    correlation_id=...``). Posting it as a path segment produces an unmatched
    ``/v1`` path, which onex-api answers 401 -- the probe would then report a
    credential problem for a URL it built wrong.
    """
    return (
        f"{base_url.rstrip('/')}{path}"
        f"?correlation_id={urllib.parse.quote(correlation_id, safe='')}"
    )


def parse_projection_body(body: str) -> tuple[bool, str | None]:
    """Read the supplier route's own three-state answer out of a 200 body.

    Returns ``(row_found, data_state)``. A row counts as found ONLY when the
    route says ``data_state == "found"`` and returns at least one row. The
    previous "any non-empty body" heuristic would have read the route's honest
    ``{"data_state": "not_found", "rows": []}`` as a successful chain -- the
    exact silent-blinding class (OMN-15797) the supplier's three states exist
    to prevent, re-opened on the reading side.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return (False, None)
    if not isinstance(parsed, dict):
        return (False, None)
    data_state = parsed.get("data_state")
    state = str(data_state) if isinstance(data_state, str) else None
    rows = parsed.get("rows")
    has_rows = isinstance(rows, list) and len(rows) > 0
    return (state == "found" and has_rows, state)


def route_is_served(*, openapi_body: str, path: str) -> bool | None:
    """Is ``path`` in the gateway's own published route list?

    ``None`` when the list could not be parsed -- deliberately NOT ``False``.
    Claiming a route is absent because its inventory was unreadable would
    fabricate a blocker from a failed measurement, which is the error class
    this whole node exists to end.
    """
    try:
        parsed = json.loads(openapi_body)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    paths = parsed.get("paths")
    if not isinstance(paths, dict):
        return None
    return path in paths


def _resolve_secret_resolver_config_path() -> str:
    """Indirection over the infra resolver path so tests can pin the empty case.

    Imported lazily: this module is imported by the operator entry point on a
    Mac with no runtime installed, and an import-time infra dependency would
    turn a missing config into an import error.
    """
    from omnibase_infra.runtime.runtime_profile import (
        resolve_secret_resolver_config_path,
    )

    return str(resolve_secret_resolver_config_path())


class LiveHookChainProbes:
    """Live implementation of ``ProtocolHookChainProbes``."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout_seconds = timeout_seconds
        self._config = _load_probe_config()
        self._consumer: Any | None = None
        self._emit_offset: int | None = None
        #: The gateway's published route list, fetched at most once per run.
        self._served_paths: tuple[str, ...] | None = None

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
        config_path = _resolve_secret_resolver_config_path()
        if not config_path:
            # The path resolver answers "" when nothing is configured, and
            # reading "" raises IsADirectoryError -- reporting that verbatim
            # sends the reader hunting for a corrupt file when the fact is that
            # this host carries no secret-resolver config at all.
            return f"unresolved:{ref}:no_secret_resolver_config"
        try:
            from omnibase_infra.runtime.models.model_secret_resolver_config import (
                ModelSecretResolverConfig,
            )
            from omnibase_infra.runtime.secret_resolver import SecretResolver

            config = ModelSecretResolverConfig.model_validate(
                yaml.safe_load(Path(config_path).read_text())
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
            present = await self._liveness_records_present(
                lane=address.emit_lane, topic=liveness_topic
            )
        except (Exception, asyncio.CancelledError) as exc:
            return (
                True,
                f"forwarder lane attachment not measurable from this host "
                f"({type(exc).__name__}); lane mismatch NOT asserted",
            )
        if present:
            return (True, None)
        return (
            False,
            f"forwarder liveness topic {liveness_topic} carries no record on "
            f"lane {address.emit_lane} (absent or empty -- the same verdict "
            "here, not the same evidence) -- the forwarder is not attached to "
            "the lane the hooks publish to",
        )

    async def _liveness_records_present(self, *, lane: str, topic: str) -> bool:
        """Does ``topic`` carry any record on ``lane``?

        Read through ``KafkaTransport`` -- the sanctioned bus surface -- rather
        than a raw ``AIOKafkaConsumer``. A raw client would give this node its
        own private, unaddressed path to a broker, which is exactly the shape
        the imperative-contract guard (OMN-12515/12540) blocks and exactly the
        shape a probe defending the sanctioned path must not take.

        The read is from ``earliest`` over a bounded window: any record proves
        the forwarder writes its canary HERE. An empty window is reported as
        not-present, and the caller records that "absent topic" and "topic with
        no records" are the same verdict on this lane even though they are not
        the same evidence.
        """
        from omnibase_infra.event_bus.kafka_transport import KafkaTransport

        consumer = KafkaTransport.from_bootstrap(
            lane,
            group=f"onex.probe.hook-chain.liveness.{uuid4().hex}",
            topics=(topic,),
            auto_offset_reset="earliest",
        )
        await consumer.start()
        try:
            deadline = time.monotonic() + min(self._timeout_seconds, 15.0)
            while time.monotonic() < deadline:
                messages = await consumer.poll(
                    max_messages=1, timeout_ms=_POLL_SLICE_MS
                )
                if messages:
                    return True
            return False
        finally:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await consumer.close()

    def _forwarder_consumer_group(self) -> str | None:
        """The forwarder's OWN declared consumer group, or None if it declares none."""
        contract = _read_forwarder_contract(
            str(self._config["forwarder_contract_module"])
        )
        if isinstance(contract, str):
            return None
        node: Any = contract.get("config", {}).get("gateway_forwarder", {})
        for key in str(self._config["forwarder_consumer_group_key"]).split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return str(node) if isinstance(node, str) and node else None

    async def _forwarder_group_advanced(
        self, address: ModelHookChainAddress
    ) -> tuple[bool, str | None]:
        """Did the forwarder's consumer group advance past the probe's offset.

        The group is read from the FORWARDER's own contract. When the forwarder
        declares none -- which it does not today -- nothing is asserted: a
        NO_CONSUMER verdict built on a guessed group name is a blocker
        manufactured from a name nobody promised, and this node exists to end
        exactly that. The cloud legs then supply the terminal evidence instead,
        which is stronger than a group offset in any case.

        Consulted only after the allowlist, lane-attachment and transport facts
        all pass, so it can never masquerade as one of those three.
        """
        group = self._forwarder_consumer_group()
        if group is None:
            return (
                True,
                "forwarder contract declares no consumer group; consumption NOT "
                "asserted (the cloud legs carry the terminal evidence)",
            )
        if self._emit_offset is None:
            return (False, "no emit offset to compare the forwarder group against")
        try:
            # Committed group offsets have no KafkaTransport equivalent -- the
            # transport exposes consumption, not another group's commit
            # position. This is a READ-ONLY admin call, it is consulted LAST
            # (only after the allowlist, lane-attachment and transport facts
            # all pass), and it publishes nothing.
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
        """Leg 4: is the correlated event observable past the cloud gateway?

        The observation is taken from the gateway's EXISTING surfaces only --
        its published route list and the declared ingest route's own response.
        The probe never acquires an ingest, echo or debug route of its own
        (operator ruling 2026-08-30: the cloud leg is one ingress, nothing new),
        so when the OMN-16459 ingest route is not yet deployed the honest
        verdict is that the route is absent, not that the gateway refused us.
        """
        path = str(self._config["cloud_gateway_ingest_path"])
        served = await self._route_is_served(
            base_url=address.cloud_gateway_base_url, path=path
        )
        reachable, status, body, detail = await self._read_cloud_route(
            base_url=address.cloud_gateway_base_url,
            path=path,
            correlation_id=correlation_id,
        )
        found, _state = (
            parse_projection_body(body) if body is not None else (False, None)
        )
        if served is False:
            detail = (
                f"{path} is absent from the {len(self._served_paths or ())} routes "
                "the gateway publishes -- the relay ingress is not deployed "
                "(OMN-16459), so the 401 is an unmatched path, not a refusal"
            )
        return ModelCloudGatewayObservation(
            reachable=reachable,
            status_code=status,
            correlation_found=found,
            route_served=served,
            detail=detail,
        )

    async def read_cloud_projection(
        self, *, correlation_id: str, address: ModelHookChainAddress
    ) -> ModelCloudProjectionObservation:
        """Leg 5: read the correlated row back through the supplier route.

        The route (OMN-17205) answers HTTP 200 for ``found``, ``not_found`` and
        ``projection_absent`` alike, so the body -- not the status -- carries
        the verdict.
        """
        path = str(self._config["cloud_projection_path"])
        served = await self._route_is_served(
            base_url=address.cloud_gateway_base_url, path=path
        )
        reachable, status, body, detail = await self._read_cloud_route(
            base_url=address.cloud_gateway_base_url,
            path=path,
            correlation_id=correlation_id,
        )
        found, data_state = (
            parse_projection_body(body) if body is not None else (False, None)
        )
        if served is False:
            detail = (
                f"{path} is absent from the {len(self._served_paths or ())} routes "
                "the gateway publishes -- the projection read route is not deployed "
                "on this plane"
            )
        return ModelCloudProjectionObservation(
            reachable=reachable,
            status_code=status,
            row_found=found,
            route_served=served,
            data_state=data_state,
            detail=detail,
        )

    async def _route_is_served(self, *, base_url: str, path: str) -> bool | None:
        """Consult the gateway's own published route list, once per run.

        onex-api 401s every unmatched ``/v1`` path, so a status code alone
        cannot tell a refused read from a route that was never deployed -- the
        ambiguity that cost OMN-17205 a session. The route list is public and
        needs no credential and no cluster access (AC4).
        """
        if base_url.startswith("unresolved:"):
            return None
        if self._served_paths is None:
            openapi_url = (
                f"{base_url.rstrip('/')}{self._config['cloud_gateway_openapi_path']!s}"
            )
            _reachable, _status, body, _detail = await self._http_get(openapi_url)
            if body is None:
                return None
            parsed = json.loads(body) if body.lstrip().startswith("{") else {}
            paths = parsed.get("paths") if isinstance(parsed, dict) else None
            self._served_paths = (
                tuple(str(key) for key in paths) if isinstance(paths, dict) else ()
            )
            if not self._served_paths:
                self._served_paths = None
                return None
        return path in self._served_paths

    async def _read_cloud_route(
        self, *, base_url: str, path: str, correlation_id: str
    ) -> tuple[bool, int | None, str | None, str | None]:
        if base_url.startswith("unresolved:"):
            return (False, None, None, f"cloud gateway base URL {base_url}")
        url = build_cloud_read_url(
            base_url=base_url, path=path, correlation_id=correlation_id
        )
        return await self._http_get(url)

    async def _http_get(
        self, url: str
    ) -> tuple[bool, int | None, str | None, str | None]:
        """One GET through the runtime's HTTP client. ``(reachable, status, body, detail)``.

        The client comes from ``ProviderHttpClient`` -- the same materialized
        dependency the runtime injects into contract-declared ``http_client``
        consumers -- rather than a raw ``urlopen``/``httpx.get``. Reaching the
        network through the sanctioned surface is the point: a node that opens
        its own socket is unaddressed and unauthenticated by construction.

        A definitive status is REACHABLE: refused (401/403) and absent (404) are
        different facts and both are answers, so neither is folded into the
        unreachable case that means "the network never got there".
        """
        import httpx
        from omnibase_infra.runtime.models.model_http_client_config import (
            ModelHttpClientConfig,
        )
        from omnibase_infra.runtime.providers.provider_http_client import (
            ProviderHttpClient,
        )

        provider = ProviderHttpClient(
            ModelHttpClientConfig(timeout_seconds=_HTTP_TIMEOUT_SECONDS)
        )
        client = await provider.create()
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            return (False, None, None, type(exc).__name__)
        finally:
            await ProviderHttpClient.close(client)
        status = int(response.status_code)
        if status >= 400:
            # Still an ANSWER, not a transport failure: the classifier needs to
            # see 401 (refused) and 404 (absent) as the distinct facts they are.
            return (True, status, None, f"HTTP {status}")
        return (True, status, response.text, None)


__all__: list[str] = [
    "HookEdgeLaneUnresolvedError",
    "LiveHookChainProbes",
    "build_cloud_read_url",
    "default_hook_edge_env_files",
    "load_forwarder_liveness_topic",
    "parse_projection_body",
    "resolve_hook_edge_lane",
    "route_is_served",
]
