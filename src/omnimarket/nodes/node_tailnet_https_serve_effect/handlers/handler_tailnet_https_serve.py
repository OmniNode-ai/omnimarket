# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Put a tailnet https listener in front of a loopback upstream, read it back, and take it away again.

Install refuses before it changes anything when the upstream does not answer, and when the https port is
already served by something other than this upstream (another listener is never overwritten). After it serves,
it reads the listener back and ends ``readback-failed`` when the port does not serve the upstream. Uninstall is the
rollback: it turns off the listener only when it still serves this upstream and leaves any other listener, on this
port or another, as it found it. Every end is a typed outcome and is reported once.
"""

from __future__ import annotations

from omnimarket.nodes.node_tailnet_https_serve_effect.models import (
    EnumServeMode,
    EnumServeOutcome,
    ModelServeListener,
    ModelServeRequest,
    ModelServeResult,
)
from omnimarket.nodes.node_tailnet_https_serve_effect.protocols import (
    ProtocolServeBackend,
    ProtocolServeReporter,
    ProtocolUpstreamProbe,
    ServeStepError,
)


def _same(target: str, upstream_url: str) -> bool:
    return target.rstrip("/") == upstream_url.rstrip("/")


class HandlerTailnetHttpsServe:
    def __init__(
        self,
        *,
        listener: ModelServeListener,
        probe: ProtocolUpstreamProbe,
        backend: ProtocolServeBackend,
        reporter: ProtocolServeReporter,
    ) -> None:
        self._listener = listener
        self._probe = probe
        self._backend = backend
        self._reporter = reporter

    def handle(self, request: ModelServeRequest) -> ModelServeResult:
        result = self._run(request)
        self._reporter.report(result)
        return result

    def _result(
        self,
        request: ModelServeRequest,
        outcome: EnumServeOutcome,
        detail: str,
        served: str = "",
        changed: bool = False,
    ) -> ModelServeResult:
        return ModelServeResult(
            run_id=request.run_id,
            mode=request.mode,
            outcome=outcome,
            detail=detail,
            https_port=self._listener.https_port,
            upstream_url=self._listener.upstream_url,
            served_target=served,
            changed=changed,
        )

    def _env_text(self) -> str:
        spec = self._listener
        return (
            "# Written by node_tailnet_https_serve_effect from its overlay. Run the node again; do not hand-edit.\n"
            f"{spec.env_port_name}={spec.https_port}\n"
            f"{spec.env_upstream_name}={spec.upstream_url}\n"
        )

    def _run(self, request: ModelServeRequest) -> ModelServeResult:
        try:
            if request.mode is EnumServeMode.INSTALL:
                return self._install(request)
            if request.mode is EnumServeMode.UNINSTALL:
                return self._uninstall(request)
            return self._status(request)
        except ServeStepError as exc:
            return self._result(
                request,
                EnumServeOutcome.BACKEND_FAILED,
                f"{exc.step} step failed: {exc}",
            )

    def _install(self, request: ModelServeRequest) -> ModelServeResult:
        spec = self._listener
        if not self._probe.answers(
            spec.upstream_url, spec.probe_path, spec.probe_timeout_seconds
        ):
            return self._result(
                request,
                EnumServeOutcome.REFUSED_DEAD_UPSTREAM,
                f"{spec.upstream_url}{spec.probe_path} does not answer; nothing was installed",
            )
        current = self._backend.served_target(spec.https_port)
        if current and not _same(current, spec.upstream_url):
            return self._result(
                request,
                EnumServeOutcome.REFUSED_FOREIGN_LISTENER,
                f":{spec.https_port} is already served by {current!r}; it was not overwritten",
                served=current,
            )
        already = bool(current)
        self._backend.place_env(spec.env_file, self._env_text())
        if not already:
            self._backend.serve(spec.https_port, spec.upstream_url)
        served = self._backend.served_target(spec.https_port)
        if not _same(served, spec.upstream_url):
            return self._result(
                request,
                EnumServeOutcome.READBACK_FAILED,
                f":{spec.https_port} serves {served!r} after the install, not {spec.upstream_url}",
                served=served,
                changed=not already,
            )
        return self._result(
            request,
            EnumServeOutcome.ALREADY_INSTALLED
            if already
            else EnumServeOutcome.INSTALLED,
            f":{spec.https_port} serves {spec.upstream_url}, read back",
            served=served,
            changed=not already,
        )

    def _uninstall(self, request: ModelServeRequest) -> ModelServeResult:
        spec = self._listener
        current = self._backend.served_target(spec.https_port)
        if current and not _same(current, spec.upstream_url):
            self._backend.remove_env(spec.env_file)
            return self._result(
                request,
                EnumServeOutcome.NOT_INSTALLED,
                f":{spec.https_port} is served by {current!r}, which is not this upstream; it was left alone",
                served=current,
            )
        if not current:
            self._backend.remove_env(spec.env_file)
            return self._result(
                request,
                EnumServeOutcome.NOT_INSTALLED,
                f"nothing is served on :{spec.https_port}",
            )
        self._backend.off(spec.https_port)
        served = self._backend.served_target(spec.https_port)
        if served:
            return self._result(
                request,
                EnumServeOutcome.READBACK_FAILED,
                f":{spec.https_port} still serves {served!r} after the listener was turned off",
                served=served,
                changed=True,
            )
        self._backend.remove_env(spec.env_file)
        return self._result(
            request,
            EnumServeOutcome.UNINSTALLED,
            f":{spec.https_port} no longer serves {spec.upstream_url}, read back",
            changed=True,
        )

    def _status(self, request: ModelServeRequest) -> ModelServeResult:
        spec = self._listener
        current = self._backend.served_target(spec.https_port)
        if current and _same(current, spec.upstream_url):
            return self._result(
                request,
                EnumServeOutcome.SERVING,
                f":{spec.https_port} serves {spec.upstream_url}",
                served=current,
            )
        if current:
            return self._result(
                request,
                EnumServeOutcome.NOT_SERVING,
                f":{spec.https_port} serves {current!r}, not this upstream",
                served=current,
            )
        return self._result(
            request,
            EnumServeOutcome.NOT_SERVING,
            f"nothing is served on :{spec.https_port}",
        )
