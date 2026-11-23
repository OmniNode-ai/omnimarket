# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Read each declared lab model server's waiting-request count (OMN-26534).

The routing overlay declares the lab model endpoints (``local_endpoints``, bound
onto the contract's local rungs) and ``placement`` names which of them share a
rung's traffic. Nothing here names a host: the endpoints to read are the ones
those declarations already carry, and :func:`endpoint_load_targets` derives them.

The read is the caller's I/O boundary. :func:`read_endpoint_load` scrapes each
server's Prometheus ``/metrics`` once, in parallel, and returns a
``ModelEndpointLoadSnapshot`` that is then passed INTO routing as an input, so
the pick itself stays a pure function of that snapshot. It never raises: a
server that cannot be read is recorded as unreadable with the reason, and the
pick skips it (or, when every server is unreadable, keeps its existing choice).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from omnimarket.models.delegation.model_endpoint_load import ModelEndpointLoad
from omnimarket.models.delegation.model_endpoint_load_snapshot import (
    ModelEndpointLoadSnapshot,
)

#: The scrape's own read budget. A server that does not answer inside it is
#: recorded unreadable, so one hung host cannot delay a decision by more.
LOAD_READ_TIMEOUT_SECONDS = 2.0

#: vLLM's counter of requests accepted but not yet scheduled.
VLLM_WAITING_METRIC = "vllm:num_requests_waiting"
#: llama.cpp's counter of requests deferred because every slot was busy.
LLAMACPP_WAITING_METRIC = "llamacpp:requests_deferred"

#: One scrape sample line: name, optional label set, value.
_SAMPLE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{[^}]*\})?\s+(?P<value>\S+)\s*$"
)

#: Fetch a scrape body for a URL; raises ``httpx.HTTPError`` or ``OSError`` when
#: the server cannot be read. Tests pass their own.
MetricsFetch = Callable[[str], str]


def parse_waiting_count(scrape: str) -> int | None:
    """The waiting-request count in a Prometheus scrape, or ``None`` when absent.

    vLLM's counter is read first, llama.cpp's when the scrape carries no vLLM
    one. A counter reported once per engine (label set) is summed.
    """
    totals: dict[str, float] = {}
    for line in scrape.splitlines():
        match = _SAMPLE.match(line)
        if match is None or match["name"] not in (
            VLLM_WAITING_METRIC,
            LLAMACPP_WAITING_METRIC,
        ):
            continue
        try:
            value = float(match["value"])
        except ValueError:
            continue
        totals[match["name"]] = totals.get(match["name"], 0.0) + value
    for name in (VLLM_WAITING_METRIC, LLAMACPP_WAITING_METRIC):
        if name in totals:
            return max(0, round(totals[name]))
    return None


def metrics_url(endpoint_url: str) -> str:
    """The server's ``/metrics`` URL: the endpoint's own origin, the scrape path."""
    parts = urlsplit(endpoint_url)
    return f"{parts.scheme}://{parts.netloc}/metrics"


def endpoint_load_targets(
    spread_peers: Mapping[str, Sequence[str]],
    endpoints: Mapping[str, object],
) -> dict[str, str]:
    """``backend_ref -> endpoint_url`` of every member of every spread group.

    ``spread_peers`` is the routing config's spread groups (rung to its peers, in
    declaration order) and ``endpoints`` the resolved bifrost backends. A member
    with no complete endpoint is left out. Empty when nothing is spread, so a
    deployment that declares one lab host reads nothing.
    """
    targets: dict[str, str] = {}
    for rung, peers in spread_peers.items():
        for ref in (rung, *peers):
            endpoint_url = getattr(endpoints.get(ref), "endpoint_url", None)
            if isinstance(endpoint_url, str) and endpoint_url:
                targets.setdefault(ref, endpoint_url)
    return targets


def _fetch_scrape(url: str) -> str:
    response = httpx.get(url, timeout=LOAD_READ_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def _read_one(ref: str, endpoint_url: str, fetch: MetricsFetch) -> ModelEndpointLoad:
    try:
        scrape = fetch(metrics_url(endpoint_url))
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return ModelEndpointLoad(
            backend_ref=ref, reason=f"{type(exc).__name__}: {exc}"[:200]
        )
    waiting = parse_waiting_count(scrape)
    if waiting is None:
        return ModelEndpointLoad(
            backend_ref=ref,
            reason=(
                f"scrape carries neither {VLLM_WAITING_METRIC} "
                f"nor {LLAMACPP_WAITING_METRIC}"
            ),
        )
    return ModelEndpointLoad(backend_ref=ref, waiting=waiting)


def read_endpoint_load(
    targets: Mapping[str, str],
    *,
    as_of: datetime,
    fetch: MetricsFetch | None = None,
) -> ModelEndpointLoadSnapshot:
    """Scrape every target's waiting-request count, in parallel, as of ``as_of``.

    Readings keep the order of ``targets`` (declaration order). Never raises on a
    server that cannot be read; the reading says so and the pick skips it.
    """
    if not targets:
        return ModelEndpointLoadSnapshot(as_of=as_of)
    scrape = fetch if fetch is not None else _fetch_scrape
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        loads = tuple(
            pool.map(lambda item: _read_one(item[0], item[1], scrape), targets.items())
        )
    return ModelEndpointLoadSnapshot(as_of=as_of, loads=loads)


__all__: list[str] = [
    "LLAMACPP_WAITING_METRIC",
    "LOAD_READ_TIMEOUT_SECONDS",
    "VLLM_WAITING_METRIC",
    "MetricsFetch",
    "endpoint_load_targets",
    "metrics_url",
    "parse_waiting_count",
    "read_endpoint_load",
]
