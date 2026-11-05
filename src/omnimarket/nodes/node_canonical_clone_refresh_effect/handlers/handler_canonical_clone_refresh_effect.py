# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCanonicalCloneRefreshEffect -- sync this host's canonical clones of one repository
(OMN-20496).

Canonical def-B handler: ``handle(request: ModelCanonicalCloneRefreshRequest) ->
ModelCanonicalCloneRefreshReceipt``. No ``Plugin*`` base, no envelope type.

The handler runs the host's own sync command, a deployment fact given on the serve command line
with a ``{repo}`` placeholder (on the operator Mac and the lab hosts, omniclaude's
``canonical_clone_sync.py sync --repo {repo}``). That engine fetches over the git transport and
only fast-forwards a clean clone on its tracking branch; everything else it refuses and logs. This
handler reads the result from the
engine's own output lines (``RESULT clone branch before->after reason``), one per clone of the
repository. When the scan finds no clone, the host table supplies the declared clone outside the
scan. The existing declared-clone handler refreshes only this host's exact repository declaration.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.node_timeouts_reader import load_node_timeouts
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.nodes.node_canonical_clone_refresh_effect.handlers.handler_declared_clone_refresh import (
    HandlerDeclaredCloneRefresh,
    TransportSubprocess,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_canonical_clone_refresh import (
    EnumCanonicalCloneRefreshStatus,
    ModelCanonicalCloneRefreshReceipt,
    ModelCanonicalCloneRefreshRequest,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_declared_clone_refresh import (
    EnumDeclaredCloneState,
    ModelDeclaredCloneRefreshRequest,
)

REPO_PLACEHOLDER = "{repo}"
SYNC_TIMEOUT_SECONDS: float = load_node_timeouts(
    "node_canonical_clone_refresh_effect"
).get("sync_timeout_seconds")
_RESULTS = {
    "ADVANCED": EnumCanonicalCloneRefreshStatus.ADVANCED,
    "UP_TO_DATE": EnumCanonicalCloneRefreshStatus.UP_TO_DATE,
    "REFUSED": EnumCanonicalCloneRefreshStatus.REFUSED,
    "FAILED": EnumCanonicalCloneRefreshStatus.FAILED,
    "NO_CLONE": EnumCanonicalCloneRefreshStatus.NO_CLONE,
}
# A run's status is the worst of its clones' results, in this order.
_SEVERITY = (
    EnumCanonicalCloneRefreshStatus.FAILED,
    EnumCanonicalCloneRefreshStatus.REFUSED,
    EnumCanonicalCloneRefreshStatus.ADVANCED,
    EnumCanonicalCloneRefreshStatus.UP_TO_DATE,
    EnumCanonicalCloneRefreshStatus.NO_CLONE,
)


class HandlerCanonicalCloneRefreshEffect:
    """Run the host's sync command for one repository and read its result."""

    def __init__(
        self,
        sync_argv: Sequence[str],
        *,
        host_name: str,
        run: Callable[..., Any] = subprocess.run,
        timeout_seconds: float = SYNC_TIMEOUT_SECONDS,
    ) -> None:
        if not any(REPO_PLACEHOLDER in part for part in sync_argv):
            raise ValueError(
                f"the sync command must name the repository as {REPO_PLACEHOLDER}"
            )
        self._argv = list(sync_argv)
        self._run = run
        self._timeout = timeout_seconds
        self.host_name = host_name

    def handle(
        self, request: ModelCanonicalCloneRefreshRequest
    ) -> ModelCanonicalCloneRefreshReceipt:
        argv = [part.replace(REPO_PLACEHOLDER, request.repo) for part in self._argv]
        started = time.monotonic()
        try:
            done = self._run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return self._receipt(
                request,
                EnumCanonicalCloneRefreshStatus.FAILED,
                started,
                message=f"sync command did not run: {type(exc).__name__}: {exc}",
            )
        lines = [
            line.split(None, 4)
            for line in (done.stdout or "").splitlines()
            if line.split(None, 1)[:1] and line.split(None, 1)[0] in _RESULTS
        ]
        if not lines:
            if done.returncode == 0:
                return self._refresh_declared(request, started)
            tail = (done.stderr or "").strip()[-500:]
            return self._receipt(
                request,
                EnumCanonicalCloneRefreshStatus.FAILED,
                started,
                message=tail or "no canonical clone of the repository on this host",
            )
        statuses = {_RESULTS[parts[0]] for parts in lines}
        status = next(s for s in _SEVERITY if s in statuses)
        if status is EnumCanonicalCloneRefreshStatus.NO_CLONE and done.returncode == 0:
            return self._refresh_declared(request, started)
        first = next(parts for parts in lines if _RESULTS[parts[0]] is status)
        before, _, after = (first[3] if len(first) > 3 else "").partition("->")
        reason = first[4] if len(first) > 4 else ""
        return self._receipt(
            request,
            status,
            started,
            before=before,
            after=after,
            message=f"{len(lines)} clone(s); {reason}".rstrip("; "),
        )

    def _refresh_declared(
        self, request: ModelCanonicalCloneRefreshRequest, started: float
    ) -> ModelCanonicalCloneRefreshReceipt:
        try:
            table = resolve_lab_hosts_table()
        except NodeOverlayError as exc:
            return self._receipt(
                request,
                EnumCanonicalCloneRefreshStatus.REFUSED,
                started,
                message=f"declared clone coverage unreadable: {exc}",
            )
        row = next((h for h in table.hosts if h.name == self.host_name), None)
        if row is None:
            return self._receipt(
                request,
                EnumCanonicalCloneRefreshStatus.REFUSED,
                started,
                message=f"host {self.host_name} is absent from the declared host table",
            )
        if not any(c.repo == request.repo for c in row.clones):
            return self._receipt(
                request,
                EnumCanonicalCloneRefreshStatus.NO_CLONE,
                started,
                message="scan found no clone and this host declares none of the exact repository",
            )
        declared = HandlerDeclaredCloneRefresh(
            table, request.repo, TransportSubprocess(local_host_name=self.host_name)
        ).handle(ModelDeclaredCloneRefreshRequest(hosts=(self.host_name,)))
        outcome = declared.outcomes[0]
        status = {
            EnumDeclaredCloneState.REFRESHED: EnumCanonicalCloneRefreshStatus.ADVANCED,
            EnumDeclaredCloneState.UP_TO_DATE: EnumCanonicalCloneRefreshStatus.UP_TO_DATE,
            EnumDeclaredCloneState.REFUSED: EnumCanonicalCloneRefreshStatus.REFUSED,
        }[outcome.state]
        reason = outcome.refusal.value if outcome.refusal else outcome.state.value
        return self._receipt(
            request,
            status,
            started,
            before=outcome.before_sha,
            after=outcome.after_sha,
            message=f"declared clone {outcome.path}: {reason}; {outcome.message}".rstrip(
                "; "
            ),
        )

    def _receipt(
        self,
        request: ModelCanonicalCloneRefreshRequest,
        status: EnumCanonicalCloneRefreshStatus,
        started: float,
        *,
        before: str = "",
        after: str = "",
        message: str = "",
    ) -> ModelCanonicalCloneRefreshReceipt:
        return ModelCanonicalCloneRefreshReceipt(
            host=self.host_name,
            repo=request.repo,
            status=status,
            target_sha=request.target_sha,
            before_sha=before,
            after_sha=after,
            events_coalesced=request.events,
            duration_ms=int((time.monotonic() - started) * 1000),
            message=message[-2000:],
        )
