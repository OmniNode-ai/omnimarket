# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Standing per-key credit check for the review gateway's keys (OMN-24658).

A review key that runs out of credit makes a voter answer 503 until someone notices. The gateway
skips such a key on its own, but nothing told the operator that a key had flipped. This module
keeps the last state of each key in a state file and, when a key's state differs from the last
run, appends one ledger STATUS row carrying ``alarm=review-key-credit``. The first run records
the baseline and writes no row. Only a key's display name is ever written, never its credential
reference, its value or a provider message. A run in which no key gave a reading (no key bound,
or every call failed for a reason that says nothing about a key) is a failed run, never a quiet one.
Rows go through the ledger command the host units already carry (``ONEX_LEDGER_COMMAND``).

The probe's token budget and prompt are declared in the node contract's ``config.probe`` and
read from it (OMN-24727); the per-key read is the gateway's bus-free ``ReviewKeyProbe``, so no
gateway and no event bus is built here.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import click
from pydantic import ValidationError

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    ProtocolLedgerAppender,
    SubprocessLedgerAppender,
)
from omnimarket.nodes.node_review_key_credit_probe_effect.models.model_review_key_credit_probe import (
    ModelReviewKeyCreditProbeRequest,
    ModelReviewKeyCreditProbeResult,
    ModelReviewKeyProbeConfig,
)

_NODE_NAME = "node_review_key_credit_probe_effect"
_STATE_FILE = "key-states.json"
LEDGER_COMMAND_VARIABLE = "ONEX_LEDGER_COMMAND"
_APPEND_TIMEOUT_SECONDS = 60


def load_probe_config() -> ModelReviewKeyProbeConfig:
    """The probe's request as the node contract declares it (``config.probe``)."""
    config = load_packaged_contract_section(_NODE_NAME, "config")
    if not isinstance(config, Mapping):
        raise ValueError(f"{_NODE_NAME}: contract config must be a mapping")
    return ModelReviewKeyProbeConfig.model_validate(config["probe"])


def probe_payload(config: ModelReviewKeyProbeConfig) -> dict[str, Any]:
    """The chat payload one probe call carries, its token budget taken from the config."""
    return {
        "messages": [{"role": "user", "content": config.prompt}],
        "max_tokens": config.max_tokens,
    }


def _safe(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def read_states(state_dir: Path) -> dict[str, str]:
    """The last run's key states, empty when there was no readable run."""
    try:
        raw = json.loads((state_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def write_states(state_dir: Path, states: dict[str, str]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / _STATE_FILE
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(states, sort_keys=True), encoding="utf-8")
    temp.replace(target)


def flips(
    previous: dict[str, str], current: dict[str, str]
) -> list[tuple[str, str, str]]:
    """(key, from, to) for each key whose state changed since a recorded run.

    A key with no recorded state is a baseline, not a flip, and an ``unreachable`` reading
    (the call failed for a reason that says nothing about the key) never flips a key.
    """
    return [
        (name, previous[name], state)
        for name, state in sorted(current.items())
        if name in previous
        and state != "unreachable"
        and previous[name] != "unreachable"
        and previous[name] != state
    ]


def render_alarm_row(
    stamp: str, host: str, model: str, name: str, before: str, after: str
) -> str:
    """One rolling-ledger STATUS row, no data-supplied pipe delimiters."""
    return (
        f"{_safe(stamp)} | STATUS | lane=review-key-credit-probe | ticket=OMN-24658 | "
        f"actor=launchd:review-key-credit-probe | alarm=review-key-credit | "
        f"host={_safe(host)} | model={_safe(model)} | key={_safe(name)} | "
        f"from={_safe(before)} | to={_safe(after)} | review key {_safe(name)} on host "
        f"{_safe(host)} went from {_safe(before)} to {_safe(after)}"
    )


def ledger_appender_from_environment() -> ProtocolLedgerAppender:
    """The existing subprocess appender on the command the host units carry; no default."""
    command = shlex.split(os.environ.get(LEDGER_COMMAND_VARIABLE, ""))
    if not command:
        raise RuntimeError(
            f"set {LEDGER_COMMAND_VARIABLE} to the onex-ledger command line (no default)"
        )
    return SubprocessLedgerAppender(command, _APPEND_TIMEOUT_SECONDS)


class NoReadingError(RuntimeError):
    """No key gave a reading, so the run saw nothing and must not look like a quiet one."""


def record(
    states: Sequence[tuple[str, str]],
    *,
    state_dir: Path,
    ledger: Path,
    host: str,
    model: str,
    appender: ProtocolLedgerAppender | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[tuple[str, str, str]]:
    """Compare this run with the last, write one row per flip, then keep the new states.

    Raises ``NoReadingError`` when there is no key or every read was ``unreachable``.
    A ledger that refuses a row keeps the state of every row already written and the old state
    of the refused one, so the next run retries that flip and writes no row twice.
    """
    ledger_appender = (
        appender if appender is not None else ledger_appender_from_environment()
    )
    if all(state == "unreachable" for _, state in states):
        raise NoReadingError(
            f"no key gave a reading ({len(states)} bound to host {host}); "
            "no state was kept and no row was written"
        )
    current = dict(states)
    previous = read_states(state_dir)
    changed = flips(previous, current)
    stamp = clock().strftime("%Y-%m-%dT%H:%M:%SZ")
    kept = {name: previous[name] for name in current if name in previous}
    for name, before, after in changed:
        outcome = ledger_appender.append(
            ledger, render_alarm_row(stamp, host, model, name, before, after)
        )
        if not outcome.ok:
            # Keep what was written so far: the next run retries only the refused flip.
            write_states(state_dir, kept)
            raise RuntimeError(f"review key flip not recorded: {outcome.detail}")
        kept[name] = after
    for name, state in current.items():
        if state != "unreachable" or name not in kept:
            kept[name] = state
    write_states(state_dir, kept)
    return changed


class HandlerReviewKeyCreditProbe:
    """Probe each review key bound to the host, then ledger the keys whose state flipped."""

    def __init__(
        self,
        *,
        catalog: Any,
        key_values: Any,
        call: Any,
        endpoint_url: str,
        config: ModelReviewKeyProbeConfig,
        appender: ProtocolLedgerAppender | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._catalog = catalog
        self._key_values = key_values
        self._call = call
        self._endpoint_url = endpoint_url
        self._config = config
        self._appender = appender
        self._clock = clock

    async def handle(
        self, request: ModelReviewKeyCreditProbeRequest
    ) -> ModelReviewKeyCreditProbeResult:
        from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
            ReviewKeyProbe,
        )
        from omnimarket.nodes.node_claude_review_gateway_effect.models.model_claude_review_gateway import (
            ModelClaudeReviewGatewayRequest,
        )

        probe = ReviewKeyProbe(
            ModelClaudeReviewGatewayRequest(
                host=request.host,
                tenant_id=request.tenant_id,
                model=request.model,
                cloud_refused_repositories=("none",),
                thinking_mode=request.thinking_mode,
            ),
            catalog=self._catalog,
            key_values=self._key_values,
            call=self._call,
            endpoint_url=self._endpoint_url,
            clock=self._clock,
        )
        states = await probe.probe_keys(probe_payload(self._config))
        changed = record(
            states,
            state_dir=request.state_dir,
            ledger=request.ledger,
            host=request.host,
            model=request.model,
            appender=self._appender,
            clock=self._clock,
        )
        return ModelReviewKeyCreditProbeResult(
            states=tuple(states), flips_written=len(changed)
        )


def review_key_probe_command(
    *,
    resolve_host: Callable[[str | None], str | None],
    key_store: Callable[[], LocalByokCredentialStore],
) -> click.Command:
    """The ``models review-gateway-key-probe`` command, the body of the standing probe (OMN-24658).

    The CLI owns how this machine's lab host row and credential store are found and passes
    them in; the provider backend, the call effect and the ledger writer are imported on call.
    """

    @click.command("review-gateway-key-probe")
    @click.option("--tenant", required=True, help="The tenant whose keys are probed.")
    @click.option(
        "--model", "model_option", required=True, help="The Claude model probed."
    )
    @click.option(
        "--thinking-mode",
        type=click.Choice(["disabled", "adaptive"]),
        default="disabled",
        show_default=True,
        help="Native thinking protocol the gateway's host overlay declares for this model.",
    )
    @click.option("--host", default=None, help="This machine's lab host table row.")
    @click.option(
        "--state-dir",
        type=click.Path(file_okay=False, path_type=Path),
        required=True,
        help="Where the last state of each key is kept.",
    )
    @click.option(
        "--ledger",
        type=click.Path(dir_okay=False, path_type=Path),
        envvar="ONEX_LEDGER_PATH",
        required=True,
        help="The ledger a key's credit flip is appended to.",
    )
    def probe(
        tenant: str,
        model_option: str,
        thinking_mode: str,
        host: str | None,
        state_dir: Path,
        ledger: Path,
    ) -> None:
        """Probe each review key bound to this host with one token; ledger a credit flip."""
        from omnimarket.handlers.routing.byok_provider_backends import (
            resolve_byok_provider_backend,
        )
        from omnimarket.nodes.node_claude_review_gateway_effect.handlers.handler_claude_review_gateway import (
            GATEWAY_PLAN,
        )
        from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
            HandlerLlmDelegationCall,
        )
        from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

        bound_host = resolve_host(host)
        if bound_host is None:
            raise click.ClickException("the probe needs this machine's lab host row.")
        backend = resolve_byok_provider_backend("anthropic", GATEWAY_PLAN)
        if backend is None:
            raise click.ClickException(
                "the provider catalogue offers no anthropic api_native backend."
            )
        store = key_store()
        try:
            handler = HandlerReviewKeyCreditProbe(
                catalog=SqliteDatabaseAdapter(store.db_path),
                key_values=store,
                call=HandlerLlmDelegationCall(),
                endpoint_url=backend.endpoint_url,
                config=load_probe_config(),
            )
            result = asyncio.run(
                handler.handle(
                    ModelReviewKeyCreditProbeRequest(
                        host=bound_host,
                        tenant_id=tenant,
                        model=model_option,
                        thinking_mode=cast(
                            Literal["disabled", "adaptive"], thinking_mode
                        ),
                        state_dir=state_dir,
                        ledger=ledger,
                    )
                )
            )
        except (ValidationError, ValueError) as exc:
            raise click.ClickException(
                f"review-gateway-key-probe refused: {exc}"
            ) from None
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from None
        for name, state in result.states:
            click.echo(f"review-key-probe: {name} {state}")
        click.echo(f"review-key-probe: {result.flips_written} flip(s) written")

    return probe
