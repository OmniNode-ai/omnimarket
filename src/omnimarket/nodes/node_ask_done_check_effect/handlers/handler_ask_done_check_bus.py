# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus entry of the ask done check: a command on the request topic runs the declared checks.

The runtime builds a handler with no arguments, but ``HandlerAskDoneCheck`` takes its overlay, the
floor read, the ledger row source and the appender as constructor parameters. This handler builds
them when it is called, from the deployment overlay and the environment, then hands the request on.

Named break: a deployment that holds no overlay, or whose overlay names a floor URL that is not set,
cannot read anything. Every ask then reads UNKNOWN with the reason, never TRUE and never FALSE, no row
is appended and the ask stays open.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_ask_done_check_effect.adapters import HttpFloorRead
from omnimarket.nodes.node_ask_done_check_effect.handlers.handler_ask_done_check import (
    HandlerAskDoneCheck,
)
from omnimarket.nodes.node_ask_done_check_effect.models import (
    EnumCheckVerdict,
    ModelAskCheckVerdict,
    ModelAskDoneCheckOverlay,
    ModelAskDoneCheckRequest,
    ModelAskDoneCheckResult,
)
from omnimarket.nodes.node_ask_done_check_effect.overlay import load_overlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    build_appender,
    build_row_source,
)

HandlerFactory = Callable[
    [ModelAskDoneCheckOverlay, Mapping[str, str]], HandlerAskDoneCheck
]


def build_handler(
    overlay: ModelAskDoneCheckOverlay, env: Mapping[str, str]
) -> HandlerAskDoneCheck:
    """Build the checking handler from the overlay's routes; the floor URL variable must be set."""
    url = env.get(overlay.floor.url_env, "").strip()
    if not url:
        raise NodeOverlayError(
            f"{overlay.floor.url_env} is not set; the overlay names it as the floor URL"
        )
    return HandlerAskDoneCheck(
        overlay,
        HttpFloorRead(url, overlay.floor.timeout_seconds),
        build_row_source(overlay.read, env),
        build_appender(overlay.append, overlay.check_lane, env),
    )


class HandlerAskDoneCheckBus:
    """``handle(ModelAskDoneCheckRequest) -> ModelAskDoneCheckResult``, built by the runtime with no arguments."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        factory: HandlerFactory = build_handler,
    ) -> None:
        self._environ = environ
        self._factory = factory

    def handle(self, request: ModelAskDoneCheckRequest) -> ModelAskDoneCheckResult:
        env = os.environ if self._environ is None else self._environ
        try:
            handler = self._factory(load_overlay(env), env)
        except ValueError as exc:
            return self._unreadable(request, str(exc))
        return handler.handle(request)

    @staticmethod
    def _unreadable(
        request: ModelAskDoneCheckRequest, cause: str
    ) -> ModelAskDoneCheckResult:
        reason = f"deployment-unreadable:{' '.join(cause.split())[:200]}"
        return ModelAskDoneCheckResult(
            terminal_failure_cause="deployment-unreadable",
            verdicts=tuple(
                ModelAskCheckVerdict(
                    ask_id=ask.ask_id,
                    done_check=ask.done_check,
                    verdict=EnumCheckVerdict.UNKNOWN,
                    reason=reason,
                    row="",
                    written="no",
                )
                for ask in request.asks
            ),
        )


__all__ = ["HandlerAskDoneCheckBus", "build_handler"]
