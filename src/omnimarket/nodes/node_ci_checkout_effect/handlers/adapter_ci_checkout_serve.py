# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-host serve process of node_ci_checkout_effect (OMN-24287).

``ci-checkout serve`` runs on every runner host the deployed overlay declares. It subscribes the checkout
command topic under a consumer group of its own host, answers each request addressed to this host through
``HandlerCiCheckout`` and publishes the typed result on the completed terminal for every status (a sha no
source holds is a ``not_found`` result, not a failure), and a request it cannot read, or a handler that
raises, on the failed terminal. Every answer carries the command's envelope id as ``parent_envelope_id``,
the same way ``ScmBusCaller.call_topics`` matches a terminal to its command.

Topics come from the node's ``contract.yaml``; settings from its deployed overlay. The ``unit`` group renders,
installs, removes and reads the systemd user unit the overlay declares for a host.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

import click
from omnibase_core.event_bus.util_consumer_group import derive_service_group_id
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    ProtocolLabRunBus,
    event_type_for,
)
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.scm.scm_cli import _open_bus
from omnimarket.lab_work.bus import _bytes, _subscribe, _uuid_or_none
from omnimarket.models.scm import ModelScmTopics
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_cli import (
    load_topics,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_unit import (
    CiCheckoutUnitInstaller,
    ModelCiCheckoutUnitResult,
)
from omnimarket.nodes.node_ci_checkout_effect.handlers.handler_ci_checkout import (
    HandlerCiCheckout,
    load_deployment,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    OVERLAY_FILE_ENV,
    ModelCiCheckoutRequest,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout_deployment import (
    ModelCiCheckoutDeployment,
)

logger = logging.getLogger(__name__)

NODE: Final = "node_ci_checkout_effect"
GROUP_SERVICE: Final = "omnimarket"
GROUP_ROLE: Final = "checkout"
EXIT_BUS: Final = 69
EXIT_CONFIG: Final = 78


def host_group_id(host_name: str) -> str:
    """One consumer group per host: every host sees every request and answers only its own."""
    return f"{derive_service_group_id(NODE, service=GROUP_SERVICE)}.{GROUP_ROLE}.{host_name}"


class CiCheckoutHost:
    """Answer this host's checkout requests, each on its own task."""

    def __init__(
        self,
        bus: ProtocolLabRunBus,
        handler: HandlerCiCheckout,
        host_name: str,
        *,
        topics: ModelScmTopics | None = None,
    ) -> None:
        self._bus = bus
        self._handler = handler
        self._host_name = host_name
        self._topics = topics or load_topics()
        if self._topics.failure is None:
            raise ValueError("the checkout contract declares no failure terminal")
        self._failure: str = self._topics.failure
        self._unsubscribe: Callable[[], Awaitable[None]] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.skipped = 0

    @property
    def topics(self) -> ModelScmTopics:
        return self._topics

    async def start(self) -> None:
        self._unsubscribe = await _subscribe(
            self._bus,
            self._topics.command,
            self._on_request,
            host_group_id(self._host_name),
            "latest",
        )

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            await self._unsubscribe()
            self._unsubscribe = None
        await self.drain()

    async def drain(self) -> None:
        pending = list(self._tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.difference_update(pending)

    async def _on_request(self, message: ProtocolBusMessage) -> None:
        try:
            raw = json.loads(message.value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.skipped += 1
            return
        payload = raw.get("payload", raw) if isinstance(raw, dict) else None
        if not isinstance(payload, dict):
            self.skipped += 1
            return
        if payload.get("host") != self._host_name:
            return  # another host's request: seen by every host, answered by its own
        command_id = _uuid_or_none(raw.get("envelope_id"))
        try:
            request = ModelCiCheckoutRequest.model_validate(payload)
        except ValidationError as exc:
            await self._publish(
                self._failure,
                {
                    "host": self._host_name,
                    "ok": False,
                    "error": f"not a checkout request: {exc.error_count()} error(s)",
                },
                command_id,
                None,
            )
            return
        task = asyncio.create_task(self._answer(request, command_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(
        self, request: ModelCiCheckoutRequest, command_id: uuid.UUID | None
    ) -> None:
        try:
            # The handler runs git and the host's mirror sync as subprocesses: off the event loop.
            result = await asyncio.to_thread(self._handler.handle, request)
        except Exception as exc:
            # A handler crash is answered on the failure terminal, never dropped.
            logger.exception("ci-checkout handler raised")
            await self._publish(
                self._failure,
                {
                    "host": self._host_name,
                    "repo": request.repo,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"[-2000:],
                },
                command_id,
                request.correlation_id,
            )
            return
        logger.info(
            "ci-checkout %s %s@%s status=%s source=%s upstream_fetches=%s lane=%s",
            self._host_name,
            request.repo,
            request.sha[:12],
            result.status.value,
            result.source_name,
            result.upstream_fetches,
            request.requested_by_lane,
        )
        await self._publish(
            self._topics.success,
            result.model_dump(mode="json"),
            command_id,
            request.correlation_id,
        )

    async def _publish(
        self,
        topic: str,
        payload: dict[str, Any],
        command_id: uuid.UUID | None,
        correlation_id: uuid.UUID | None,
    ) -> None:
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=payload,
            correlation_id=correlation_id or uuid.uuid4(),
            parent_envelope_id=command_id,
            event_type=event_type_for(topic),
        )
        key = str(payload.get("repo") or payload.get("host") or "").encode("utf-8")
        await self._bus.publish(topic, key, _bytes(envelope))


def bus_options(
    deployment: ModelCiCheckoutDeployment,
    profile_name: str | None,
    environ: dict[str, str],
) -> dict[str, object]:
    """The options ``_open_bus`` reads, taken from the overlay's profile and the named environment variables."""
    profile = deployment.profile(profile_name)
    bootstrap = (
        environ.get(profile.kafka_bootstrap_env, "").strip()
        if profile.kafka_bootstrap_env
        else ""
    )
    root = (
        environ.get(profile.workspace_root_env, "").strip()
        if profile.workspace_root_env
        else ""
    )
    return {
        "bus": profile.kind,
        "bus_lane": profile.lane,
        "kafka_bootstrap": bootstrap or None,
        "omnibase_path": root or None,
    }


@click.group("ci-checkout")
@click.pass_context
def serve_group(ctx: click.Context) -> None:
    """Serve this host's bus checkout requests and manage the unit that runs the serve."""
    ctx.ensure_object(dict)


_overlay_option = click.option(
    "--overlay",
    "overlay_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help=f"Deployed overlay file; else {OVERLAY_FILE_ENV}, else the overlay roots.",
)
_profile_option = click.option(
    "--profile",
    default=None,
    help="Bus profile from the overlay; else its default_profile.",
)


@serve_group.command("serve")
@_overlay_option
@_profile_option
@click.option(
    "--host-name", required=True, help="This host's name (a deployment fact)."
)
def serve_command(
    overlay_path: Path | None, profile: str | None, host_name: str
) -> None:
    """Serve this host's checkout requests until SIGTERM."""
    import os

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        deployment = load_deployment(overlay_path)
        overlay = deployment.resolve(host_name, home=str(Path.home()))
        handler = HandlerCiCheckout(overlay=overlay)
        opts = bus_options(deployment, profile, dict(os.environ))
    except (NodeOverlayError, ValueError) as exc:
        click.echo(f"overlay: {exc}", err=True)
        sys.exit(EXIT_CONFIG)

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        async with _open_bus(opts) as opened:
            host = CiCheckoutHost(opened, handler, host_name)
            await host.start()
            logger.info("ci-checkout serve %s on %s", host_name, host.topics.command)
            try:
                await stop.wait()
            finally:
                await host.stop()

    try:
        asyncio.run(main())
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        sys.exit(EXIT_BUS)


@serve_group.group("unit")
@click.pass_context
def unit_group(ctx: click.Context) -> None:
    """Render, install, remove and read the serve unit declared in the overlay."""
    ctx.ensure_object(dict)


_host_option = click.option(
    "--host", "host", required=True, help="The declared host to act on."
)
_omni_root_option = click.option(
    "--omni-root",
    type=click.Path(path_type=Path, file_okay=False),
    required=True,
    help="The registry root on the host ($OMNI_HOME in the overlay).",
)
_overlay_root_option = click.option(
    "--overlay-root",
    type=click.Path(path_type=Path, file_okay=False),
    required=True,
    help="The overlay root the unit reads the overlay from (ONEX_SKILL_OVERLAY_ROOTS).",
)


def _installer(overlay_path: Path | None) -> CiCheckoutUnitInstaller:
    try:
        deployment = load_deployment(overlay_path)
    except (NodeOverlayError, ValueError) as exc:
        click.echo(f"overlay: {exc}", err=True)
        sys.exit(EXIT_CONFIG)
    return CiCheckoutUnitInstaller(overlay=deployment, home=Path.home())


def _finish(result: ModelCiCheckoutUnitResult) -> None:
    click.echo(result.stdout, nl=False)
    click.echo(result.stderr, err=True, nl=False)
    sys.exit(result.exit_code)


@unit_group.command("render")
@_overlay_option
@_host_option
@_omni_root_option
@_overlay_root_option
def unit_render(
    overlay_path: Path | None, host: str, omni_root: Path, overlay_root: Path
) -> None:
    """Print the unit file for a declared host."""
    _finish(
        _installer(overlay_path).render(
            host, omni_root=omni_root, overlay_root=overlay_root
        )
    )


@unit_group.command("install")
@_overlay_option
@_host_option
@_omni_root_option
@_overlay_root_option
@click.option("--dry-run", is_flag=True, help="Render and check; write nothing.")
def unit_install(
    overlay_path: Path | None,
    host: str,
    omni_root: Path,
    overlay_root: Path,
    dry_run: bool,
) -> None:
    """Write the unit, reload the manager and enable the service now."""
    _finish(
        _installer(overlay_path).install(
            host, omni_root=omni_root, overlay_root=overlay_root, dry_run=dry_run
        )
    )


@unit_group.command("uninstall")
@_overlay_option
@_host_option
def unit_uninstall(overlay_path: Path | None, host: str) -> None:
    """The rollback: stop and disable the service and remove its unit file."""
    _finish(_installer(overlay_path).uninstall(host))


@unit_group.command("status")
@_overlay_option
@_host_option
def unit_status(overlay_path: Path | None, host: str) -> None:
    """Read whether the service is enabled and active."""
    _finish(_installer(overlay_path).status(host))


def main() -> None:
    serve_group()


__all__ = [
    "CiCheckoutHost",
    "bus_options",
    "host_group_id",
    "main",
    "serve_group",
]
