# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Open a lane's bus through its declared addresses in order, and say which one answered (OMN-20540).

A lane declaration may carry ``broker_fallbacks``: other addresses of the same broker listener. The bus
opens through the declared ``broker`` first; when that address does not answer within a bounded wait the
next declared address is tried, and when none answers the open fails closed with every address named.

The address that carried the bus is announced when it opens. A switch from the first address is a warning
that carries a count, never a quiet retry. The credentials are the lane's: the address chosen never changes
whose login is used (the caller resolves the lane's transport for each address from the lane, not from
the address).

A broker that advertises one name in its metadata sends a client that bootstrapped elsewhere back to that
name for every produce and fetch. Each fallback therefore declares ``resolve``: the advertised names to dial
at a given address while the connection runs through that fallback. The mapping lives on the running event
loop of the process that opened the bus and is lifted when the bus closes; it touches no host file.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    contextmanager,
)
from pathlib import Path
from typing import Final

import yaml
from omnibase_infra.errors import InfraConnectionError, InfraTimeoutError
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.models.bus_binding import ModelBrokerFallback

#: How long one declared address may take to open before the next is tried (only when a lane declares more
#: than one address; a lane with one address keeps the broker client's own connect policy).
CONNECT_TIMEOUT_S: Final[float] = 30.0

_LOG: Final = logging.getLogger("omnimarket.bus.lane_address")
#: Switches from the first declared address in this process, counted from 1.
_SWITCHES: Final = itertools.count(1)

OpenOne = Callable[
    [ModelBrokerFallback], AbstractAsyncContextManager[ProtocolLabRunBus]
]

#: The declaration beside the lane file that already holds each lane's LAN address; an entry with
#: ``source: hook_edge_lane`` takes its address from there so the address is declared in one place.
EDGE_LANE_FILE: Final = "hook_edge_lane.yaml"


def _edge_address(declaration: Path, lane: str) -> str:
    """The lane's LAN broker address from the hook edge lane declaration beside ``declaration``."""
    edge = declaration.with_name(EDGE_LANE_FILE)
    try:
        raw = yaml.safe_load(edge.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(
            f"lane {lane!r} broker_fallbacks source hook_edge_lane: cannot read {edge}: {exc}"
        ) from exc
    known = raw.get("known_lanes") if isinstance(raw, dict) else None
    entry = known.get(lane) if isinstance(known, dict) else None
    address = entry.get("bootstrap_servers") if isinstance(entry, dict) else None
    if not isinstance(address, str) or not address.strip():
        raise ValueError(
            f"lane {lane!r} broker_fallbacks source hook_edge_lane: {edge} declares no "
            f"known_lanes.{lane}.bootstrap_servers"
        )
    return address.strip()


def _entry(entry: object, declaration: Path, lane: str) -> ModelBrokerFallback:
    """One ``broker_fallbacks`` entry: written out (``bootstrap``, ``resolve``) or sourced from the edge lane file.

    A sourced entry is ``{source: hook_edge_lane, advertised_names: [name, ...]}``: each advertised name
    is dialled at the host of the edge lane's address while the connection runs through it.
    """
    if isinstance(entry, dict) and "source" in entry:
        if (
            set(entry) - {"source", "advertised_names"}
            or entry["source"] != "hook_edge_lane"
        ):
            raise ValueError(
                f"lane {lane!r} broker_fallbacks sourced entry takes source: hook_edge_lane "
                f"and advertised_names only, got {sorted(entry)}"
            )
        names = entry.get("advertised_names", [])
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError(
                f"lane {lane!r} broker_fallbacks advertised_names must be a list of names"
            )
        bootstrap = _edge_address(declaration, lane)
        host = bootstrap.rpartition(":")[0]
        return ModelBrokerFallback(
            bootstrap=bootstrap, resolve=dict.fromkeys(names, host)
        )
    return ModelBrokerFallback.model_validate(entry)


def load_broker_fallbacks(
    declaration: Path, lane: str, primary: str
) -> tuple[ModelBrokerFallback, ...]:
    """The lane's declared ``broker_fallbacks`` in order; empty when it declares none.

    Raises:
        ValueError: the entry is malformed. A fallback that cannot be read is refused, never ignored: a
            lane that believes it has a second address and has none is the failure this exists for.
    """
    try:
        raw = yaml.safe_load(declaration.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read {declaration}: {exc}") from exc
    lanes = raw.get("lanes") if isinstance(raw, dict) else None
    declared = lanes.get(lane) if isinstance(lanes, dict) else None
    entries = declared.get("broker_fallbacks") if isinstance(declared, dict) else None
    if entries is None:
        return ()
    if not isinstance(entries, list) or not entries:
        raise ValueError(
            f"lane {lane!r} broker_fallbacks must be a non-empty list of mappings"
        )
    try:
        parsed = tuple(_entry(entry, declaration, lane) for entry in entries)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ValueError(
            f"lane {lane!r} broker_fallbacks is invalid: {problems}"
        ) from exc
    addresses = [primary, *(entry.bootstrap for entry in parsed)]
    if len(set(addresses)) != len(addresses):
        raise ValueError(
            f"lane {lane!r} broker_fallbacks repeats an address: {addresses}"
        )
    return parsed


@contextmanager
def advertised_names_resolved(
    resolve: Mapping[str, str], *, lane: str, via: str
) -> Iterator[None]:
    """For the life of the block, dial the declared address wherever the broker advertises a declared name.

    The mapping is on this process's running event loop and nowhere else. Every dial it changes is a
    warning that names the lane, the name, the address and the bootstrap address in use, so a reconnect
    through a fallback is never silent.
    """
    if not resolve:
        yield
        return
    loop = asyncio.get_running_loop()
    previous = vars(loop).get("getaddrinfo")
    original = loop.getaddrinfo
    dials = itertools.count(1)

    async def getaddrinfo(host: object, port: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        address = resolve.get(host.lower()) if isinstance(host, str) else None
        if address is None:
            return await original(host, port, *args, **kwargs)  # type: ignore[arg-type]
        _LOG.warning(
            "bus lane=%s dialled name=%s at address=%s via=%s dials=%d",
            lane,
            host,
            address,
            via,
            next(dials),
        )
        return await original(address, port, *args, **kwargs)  # type: ignore[arg-type]

    loop.getaddrinfo = getaddrinfo  # type: ignore[method-assign]
    try:
        yield
    finally:
        if vars(loop).get("getaddrinfo") is getaddrinfo:
            if previous is None:
                del loop.getaddrinfo
            else:
                loop.getaddrinfo = previous  # type: ignore[method-assign]


@asynccontextmanager
async def open_in_order(
    *,
    lane: str,
    candidates: Sequence[ModelBrokerFallback],
    open_one: OpenOne,
    timeout_s: float | None = None,
) -> AsyncIterator[ProtocolLabRunBus]:
    """Open ``open_one`` for each candidate in order and yield the first bus that opens.

    ``candidates[0]`` is the lane's declared ``broker``; the rest are its ``broker_fallbacks``. With one
    candidate the call is exactly ``open_one(candidate)``. With more, each is bounded by ``timeout_s``.

    Raises:
        omnimarket.delegated_test_loop.lane_bus.LabRunBusError: no address opened. Every address is named
            with the failure it gave.
    """
    from omnimarket.delegated_test_loop.lane_bus import LabRunBusError

    timeout_s = CONNECT_TIMEOUT_S if timeout_s is None else timeout_s
    failures: list[str] = []
    async with AsyncExitStack() as stack:
        bus: ProtocolLabRunBus | None = None
        for index, candidate in enumerate(candidates):

            @asynccontextmanager
            async def dialled(
                candidate: ModelBrokerFallback = candidate,
            ) -> AsyncIterator[ProtocolLabRunBus]:
                with advertised_names_resolved(
                    candidate.resolve, lane=lane, via=candidate.bootstrap
                ):
                    async with open_one(candidate) as opened:
                        yield opened

            opening = stack.enter_async_context(dialled())
            try:
                bus = await (
                    asyncio.wait_for(opening, timeout_s)
                    if len(candidates) > 1
                    else opening
                )
            except (
                LabRunBusError,
                InfraConnectionError,
                InfraTimeoutError,
                TimeoutError,
                OSError,
            ) as exc:
                detail = str(exc).replace("\n", " ")[:200]
                failures.append(
                    f"{candidate.bootstrap}: {type(exc).__name__}: {detail}"
                )
                continue
            if index == 0:
                _LOG.info(
                    "bus lane=%s address=%s via=primary",
                    lane,
                    candidate.bootstrap,
                )
            else:
                _LOG.warning(
                    "bus address switch lane=%s from=%s to=%s count=%d reason=%s",
                    lane,
                    candidates[0].bootstrap,
                    candidate.bootstrap,
                    next(_SWITCHES),
                    failures[0],
                )
            break
        if bus is None:
            raise LabRunBusError(
                f"lane {lane!r}: no declared broker address answered "
                f"({len(candidates)} tried, {timeout_s:g}s each): "
                + " | ".join(failures)
            )
        yield bus
