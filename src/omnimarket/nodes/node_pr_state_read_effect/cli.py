# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex pr-state``: read or wait on pull request state from the projection.

A shell over the PR-state read node. It parses arguments, builds the projection
client, calls the node's handlers and prints their typed results as JSON. The
projection API address comes from OMNIDASH_API_URL, as for ``onex trace``; with
no value set the command fails closed. It makes no GitHub request.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import click
import httpx
from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    resolve_setting,
)
from omnimarket.models.pr_state.model_pr_state_page_fetch import (
    ModelPrStatePageRequest,
)
from omnimarket.models.pr_state.model_pr_state_projection_page import (
    ModelPrStateProjectionRefusal,
)
from omnimarket.models.pr_state.model_pr_state_read_request import (
    ModelPrStateReadRequest,
    ModelPrStateWaitRequest,
)
from omnimarket.models.pr_state.model_pr_state_read_result import (
    EnumPrStateReadOutcome,
    ModelPrStateReadResult,
    ModelPrStateReadUnavailable,
)
from omnimarket.nodes.node_pr_state_read_effect.handlers.handler_pr_state_page_source import (
    HandlerPrStatePageSource,
    open_projection_client,
)
from omnimarket.nodes.node_pr_state_read_effect.handlers.handler_pr_state_read import (
    HandlerPrStateRead,
)
from omnimarket.nodes.node_pr_state_read_effect.handlers.handler_pr_state_wait import (
    HandlerPrStateWait,
)
from omnimarket.nodes.node_pr_state_read_effect.policy import (
    load_pr_state_read_policy,
)

_REQUEST_TIMEOUT_SECONDS = 10.0
_DEFAULT_WAIT_TIMEOUT_SECONDS = 600.0

# 0 is reserved for every pull request FRESH; the rest are in order of how
# little the caller can rely on the answer.
_EXIT_CODES: dict[EnumPrStateReadOutcome, int] = {
    EnumPrStateReadOutcome.FRESH: 0,
    EnumPrStateReadOutcome.STALE: 3,
    EnumPrStateReadOutcome.ABSENT: 4,
    EnumPrStateReadOutcome.TIMEOUT: 5,
    EnumPrStateReadOutcome.UNAVAILABLE: 6,
}


class ModelPrStateCliOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    results: tuple[ModelPrStateReadResult, ...]


@dataclass(frozen=True)
class PrStateCliRuntime:
    """What the command reaches outside itself with; tests supply their own."""

    client: httpx.Client
    clock: Callable[[], datetime]
    sleep: Callable[[float], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


_NODE_NAME = "node_pr_state_read_effect"
_API_URL_SETTING = "projection_api_url"
_API_URL_META_KEY = "pr_state_api_url"


def _runtime_for(explicit_url: str | None) -> PrStateCliRuntime:
    try:
        url = explicit_url or resolve_setting(
            _NODE_NAME,
            _API_URL_SETTING,
            fields=frozenset({_API_URL_SETTING}),
            env_vars=("OMNIDASH_API_URL",),
        )
    except NodeOverlayError as exc:
        raise click.ClickException(str(exc)) from None
    return PrStateCliRuntime(
        client=open_projection_client(url),
        clock=_utc_now,
        sleep=time.sleep,
    )


def _runtime(ctx: click.Context) -> PrStateCliRuntime:
    if isinstance(ctx.obj, PrStateCliRuntime):
        return ctx.obj
    return _runtime_for(ctx.meta.get(_API_URL_META_KEY))


def _parse_ref(ref: str) -> tuple[str, int]:
    repo, separator, number = ref.rpartition("#")
    if not separator or not repo or not number.isdecimal() or int(number) < 1:
        raise click.BadParameter(
            f"{ref!r} is not <owner>/<repo>#<number>", param_hint="REF"
        )
    return repo, int(number)


def _emit(results: tuple[ModelPrStateReadResult, ...]) -> None:
    click.echo(ModelPrStateCliOutput(results=results).model_dump_json(indent=2))
    code = max((_EXIT_CODES[r.result.outcome] for r in results), default=0)
    raise SystemExit(code)


@click.group(name="pr-state")
@click.option(
    "--api-url",
    default=None,
    help="Projection API address. Else OMNIDASH_API_URL, else the node overlay.",
)
@click.pass_context
def pr_state_group(ctx: click.Context, api_url: str | None) -> None:
    """Read pull request state from the projection, never from GitHub."""
    ctx.meta[_API_URL_META_KEY] = api_url


@pr_state_group.command("read")
@click.argument("refs", nargs=-1, required=True)
@click.option("--head", "head_sha", default=None, help="Only a row at this head sha.")
@click.pass_context
def read_command(
    ctx: click.Context, refs: tuple[str, ...], head_sha: str | None
) -> None:
    """Read each REF (<owner>/<repo>#<number>); a row older than the limit is refused."""
    parsed = [_parse_ref(ref) for ref in refs]
    if head_sha is not None and len(parsed) != 1:
        raise click.UsageError("--head applies to exactly one REF")
    runtime = _runtime(ctx)
    policy = load_pr_state_read_policy()
    page = (
        HandlerPrStatePageSource(runtime.client, _REQUEST_TIMEOUT_SECONDS)
        .handle(ModelPrStatePageRequest(topic=policy.projection_topic))
        .result
    )
    if isinstance(page, ModelPrStateProjectionRefusal):
        unavailable = ModelPrStateReadResult(
            result=ModelPrStateReadUnavailable(
                status_code=page.status_code, error=page.error
            )
        )
        _emit(tuple(unavailable for _ in parsed))
        return
    handler = HandlerPrStateRead()
    now = runtime.clock()
    _emit(
        tuple(
            handler.handle(
                ModelPrStateReadRequest(
                    repo=repo,
                    pr_number=number,
                    head_sha=head_sha,
                    page=page,
                    now=now,
                    policy=policy,
                )
            )
            for repo, number in parsed
        )
    )


@pr_state_group.command("wait")
@click.argument("ref")
@click.option("--head", "head_sha", default=None, help="Only a row at this head sha.")
@click.option(
    "--timeout",
    "timeout_seconds",
    type=click.FloatRange(min=0, min_open=True),
    default=_DEFAULT_WAIT_TIMEOUT_SECONDS,
    show_default=True,
    help="Seconds to wait for a terminal verdict.",
)
@click.pass_context
def wait_command(
    ctx: click.Context, ref: str, head_sha: str | None, timeout_seconds: float
) -> None:
    """Wait until REF's row reaches a terminal verdict, or the timeout passes."""
    repo, number = _parse_ref(ref)
    runtime = _runtime(ctx)
    policy = load_pr_state_read_policy()
    source = HandlerPrStatePageSource(runtime.client, _REQUEST_TIMEOUT_SECONDS)
    result = HandlerPrStateWait(source, runtime.clock, runtime.sleep).handle(
        ModelPrStateWaitRequest(
            repo=repo,
            pr_number=number,
            head_sha=head_sha,
            timeout_seconds=timeout_seconds,
            policy=policy,
        )
    )
    _emit((result,))
