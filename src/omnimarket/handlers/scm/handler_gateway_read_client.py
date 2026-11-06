# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synchronous reads through node_github_repo_gateway_effect (OMN-21146).

A caller that needs a PR's facts or a commit's check runs asks the gateway node
instead of shelling ``gh``. The gateway runs in process when its contract-declared
GitHub token resolves on this host (a hosted runner). Otherwise the typed request
is published on the gateway's command topic and the deployed gateway answers on
its terminal topic (a lab lane, where the ``gh`` shim refuses every read).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final, cast

from pydantic import JsonValue, TypeAdapter, ValidationError

from omnimarket.models.model_github_gateway_io import (
    GithubGatewayResult,
    ModelGithubGatewayRequest,
)
from omnimarket.models.scm import EnumScmNode
from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.token_resolver import (
    resolve_github_token,
)
from omnimarket.nodes.node_github_repo_gateway_effect.transport import (
    RealGitHubReadTransport,
)
from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError

#: The declared bus lane the deployed gateway serves; a host may name another.
GATEWAY_BUS_LANE_ENV: Final[str] = "ONEX_GITHUB_GATEWAY_BUS_LANE"
DEFAULT_GATEWAY_BUS_LANE: Final[str] = "dev"
DEFAULT_GATEWAY_TIMEOUT_S: Final[float] = 45.0

_RESULT: Final[TypeAdapter[GithubGatewayResult]] = TypeAdapter(GithubGatewayResult)

GatewayRoute = Callable[[ModelGithubGatewayRequest], GithubGatewayResult]


# A node's refusal of a request field reads as "the node is older than this client":
# a client that adds a field before the deployed node carries it gets every request
# refused by the node's own validation, with pydantic's ``extra_forbidden`` text that
# names the field and nothing about the cause.
_VALIDATION_CLASS: Final[str] = "ValidationError"
# pydantic prints "<field>\n  Extra inputs are not permitted [type=extra_forbidden".
_REFUSED_FIELD: Final[re.Pattern[str]] = re.compile(
    r"^(?P<field>[A-Za-z_][\w.]*)\n[ \t]+Extra inputs are not permitted "
    r"\[type=extra_forbidden",
    re.MULTILINE,
)


def refused_fields(payload: dict[str, JsonValue]) -> tuple[str, ...]:
    """The request fields the node's validation refused as unknown, in order.

    Empty unless the failure is a validation error whose every reported error is
    an ``extra_forbidden`` one: a mixed failure has another cause and is not skew.
    """
    if payload.get("failure_class") != _VALIDATION_CLASS:
        return ()
    reason = payload.get("failure_reason")
    if not isinstance(reason, str):
        return ()
    count = re.match(r"(?P<n>\d+) validation errors? for ", reason)
    fields = tuple(dict.fromkeys(m["field"] for m in _REFUSED_FIELD.finditer(reason)))
    if count is None or int(count["n"]) != len(_REFUSED_FIELD.findall(reason)):
        return ()
    return fields


def plain_sentence(what: str, fields: tuple[str, ...]) -> str:
    """One sentence for the operator: cause, the field names and the fix."""
    names = ", ".join(fields)
    return (
        f"{what}: the deployed node does not know the request field(s) {names} "
        "that this client sends, so the node is older than this client. "
        "Redeploy the node at a dev-merge tag that carries the field; "
        "retrying cannot change the answer."
    )


class GatewayReadError(RuntimeError):
    """The gateway gave no typed result for a request."""


class GatewayNodeOlderError(GatewayReadError):
    """The deployed gateway refused a request field this client sends."""


def _resolve_token_off_loop() -> str:
    """Resolve the gateway's token, on a worker thread under a running loop.

    The sync resolver refuses to run under a running event loop, which is where
    dod_verify calls it; a refusal there read as "no token" and sent every read
    to the bus, even on a host whose token resolves.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return resolve_github_token()
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(resolve_github_token).result()


def read_in_process(request: ModelGithubGatewayRequest) -> GithubGatewayResult:
    """Run the gateway's dispatch here, with the token the gateway resolves."""
    transport = RealGitHubReadTransport(_resolve_token_off_loop())
    try:
        return dispatch(request, transport)
    except (GitHubTransportError, RuntimeError, ValueError) as exc:
        raise GatewayReadError(f"gateway {request.operation.value}: {exc}") from exc


def read_over_bus(
    request: ModelGithubGatewayRequest,
    *,
    timeout_s: float = DEFAULT_GATEWAY_TIMEOUT_S,
) -> GithubGatewayResult:
    """Publish the request to the deployed gateway and validate its answer."""
    # Imported here: an in-process read must not need a bus client.
    from omnibase_infra.errors import InfraConnectionError

    from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
    from omnimarket.delegated_test_loop.lane_bus import (
        LabRunBusError,
        open_lab_run_bus,
    )
    from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller

    workspace = os.environ.get("OMNIBASE_PATH") or os.environ.get("OMNI_HOME")
    lane = os.environ.get(GATEWAY_BUS_LANE_ENV) or DEFAULT_GATEWAY_BUS_LANE
    # The opener's fourth keyword (the workspace root) carries the private
    # registry clone's name, which the public-repo hygiene gate refuses; its
    # parameters are bound from its signature in declared order, as
    # ``onex scm`` binds them.
    names = tuple(inspect.signature(open_lab_run_bus).parameters)
    if names[:3] != ("bus", "lane", "kafka_bootstrap") or len(names) != 4:
        raise GatewayReadError(f"unexpected bus opener parameters: {names}")
    opener = cast(
        Callable[..., contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]],
        open_lab_run_bus,
    )
    values = ("kafka", lane, None, Path(workspace) if workspace else None)

    async def call() -> dict[str, JsonValue]:
        async with opener(**dict(zip(names, values, strict=True))) as bus:
            terminal = await ScmBusCaller(bus).call(
                node=EnumScmNode.GATEWAY,
                request=request,
                correlation_id=request.correlation_id,
                timeout_s=timeout_s,
            )
        # The runtime answers a handler fault on the success topic with
        # status=failed and the reason; a gateway that predates an operation
        # refuses the request that way.
        if terminal.failed or terminal.payload.get("status") == "failed":
            if skew := refused_fields(terminal.payload):
                raise GatewayNodeOlderError(
                    plain_sentence(f"gateway {request.operation.value}", skew)
                )
            reason = str(terminal.payload.get("failure_reason") or "no reason")
            raise GatewayReadError(
                f"gateway {request.operation.value} failed: {reason[:300]}"
            )
        return terminal.payload

    def run_call() -> dict[str, JsonValue]:
        return asyncio.run(call())

    try:
        # A caller under a running event loop (dod_verify inside the runtime)
        # cannot start a second loop on its own thread; the bus call runs on one
        # worker thread of its own instead.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            payload = run_call()
        else:
            with ThreadPoolExecutor(max_workers=1) as executor:
                payload = executor.submit(run_call).result()
    except TimeoutError as exc:
        raise GatewayReadError(
            f"gateway {request.operation.value}: no answer on lane {lane!r} "
            f"within {timeout_s:.0f}s"
        ) from exc
    except (LabRunBusError, InfraConnectionError) as exc:
        raise GatewayReadError(f"gateway bus lane {lane!r}: {exc}") from exc
    try:
        return _RESULT.validate_python(payload.get("result"))
    except ValidationError as exc:
        raise GatewayReadError(
            f"gateway {request.operation.value}: untyped answer: {exc}"
        ) from exc


def _token_resolves() -> bool:
    try:
        _resolve_token_off_loop()
    except RuntimeError:
        return False
    return True


class HandlerGatewayReadClient:
    """Run one typed gateway read, in process or over the bus, and return it."""

    def __init__(
        self,
        route: GatewayRoute | None = None,
    ) -> None:
        self._route = route

    def read(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult:
        """Raises :class:`GatewayReadError` when no typed result comes back."""
        route = self._route or (read_in_process if _token_resolves() else read_over_bus)
        return route(request)


__all__: list[str] = [
    "DEFAULT_GATEWAY_BUS_LANE",
    "GATEWAY_BUS_LANE_ENV",
    "GatewayNodeOlderError",
    "GatewayReadError",
    "GatewayRoute",
    "HandlerGatewayReadClient",
    "plain_sentence",
    "read_in_process",
    "read_over_bus",
    "refused_fields",
]
