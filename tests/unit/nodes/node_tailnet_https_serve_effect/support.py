# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory seams of the tailnet https listener, with named injected breaks."""

from __future__ import annotations

from omnimarket.nodes.node_tailnet_https_serve_effect.adapters.adapters_memory import (
    InMemoryReporter,
    InMemoryServeBackend,
    InMemoryUpstreamProbe,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.handlers.handler_tailnet_https_serve import (
    HandlerTailnetHttpsServe,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.models import (
    EnumServeMode,
    ModelServeListener,
    ModelServeRequest,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.protocols import ServeStepError

UPSTREAM = "http://127.0.0.1:18080"
FOREIGN = "http://127.0.0.1:19999"
PORT = 8443
OTHER_PORT = 9443
ENV_FILE = "/etc/example/tailnet-https/serve.env"

LISTENER = ModelServeListener(
    https_port=PORT,
    upstream_url=UPSTREAM,
    probe_path="/health",
    probe_timeout_seconds=5,
    env_file=ENV_FILE,
    env_port_name="EXAMPLE_HTTPS_PORT",
    env_upstream_name="EXAMPLE_UPSTREAM",
)


def request(mode: EnumServeMode, run_id: str = "20261106T160000Z") -> ModelServeRequest:
    return ModelServeRequest(run_id=run_id, mode=mode)


class BreakableBackend(InMemoryServeBackend):
    """The in-memory backend with the named injected breaks of the error chain."""

    def __init__(
        self,
        served: dict[int, str] | None = None,
        *,
        serve_lands_wrong_target: bool = False,
        serve_fails: bool = False,
        off_does_nothing: bool = False,
        status_fails: bool = False,
    ) -> None:
        super().__init__(served)
        self.serve_lands_wrong_target = serve_lands_wrong_target
        self.serve_fails = serve_fails
        self.off_does_nothing = off_does_nothing
        self.status_fails = status_fails

    def served_target(self, https_port: int) -> str:
        if self.status_fails:
            raise ServeStepError("serve", "serve status exited 1: tailscaled is down")
        return super().served_target(https_port)

    def serve(self, https_port: int, upstream_url: str) -> None:
        if self.serve_fails:
            raise ServeStepError("serve", "serve exited 1: access denied")
        super().serve(
            https_port, FOREIGN if self.serve_lands_wrong_target else upstream_url
        )

    def off(self, https_port: int) -> None:
        self.calls.append(f"off {https_port}")
        if not self.off_does_nothing:
            self.served.pop(https_port, None)


def build(
    *,
    alive: bool = True,
    backend: BreakableBackend | None = None,
) -> tuple[
    HandlerTailnetHttpsServe,
    InMemoryUpstreamProbe,
    BreakableBackend,
    InMemoryReporter,
]:
    probe = InMemoryUpstreamProbe(alive)
    held = backend or BreakableBackend()
    reporter = InMemoryReporter()
    handler = HandlerTailnetHttpsServe(
        listener=LISTENER, probe=probe, backend=held, reporter=reporter
    )
    return handler, probe, held, reporter
