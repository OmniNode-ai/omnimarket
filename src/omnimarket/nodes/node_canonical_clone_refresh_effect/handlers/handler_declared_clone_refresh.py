# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDeclaredCloneRefresh -- refresh the clone each lab host row declares (OMN-22149).

Canonical def-B handler: ``handle(request: ModelDeclaredCloneRefreshRequest) ->
ModelDeclaredCloneRefreshReceipt``.

The scan-based sync engine lists clones under the registry roots, so a clone it does not list is
skipped with a clean exit. This handler starts from the other end: the lab host table (injected as a
typed ``ModelLabHostsTable``) says which clone of the declared repository each host carries and where
(its ``clones`` column), and the handler brings each declared clone to the tip of its declared ref.
No host name and no path is written here: the table, the repository's directory name (the
``repos.knowledge_base`` overlay key, resolved by the composition root) and the transport are all
injected.

A declared clone whose path is missing, is not a clone, cannot be reached or is not fast-forwarded
after the run is a refusal that names the host and the path. The receipt's ``refused`` is true then,
and the caller exits non-zero.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.node_timeouts_reader import load_node_timeouts
from omnimarket.models.overlay_keys import (
    ModelLabHostClone,
    ModelLabHostRow,
    ModelLabHostsTable,
)
from omnimarket.nodes.node_canonical_clone_refresh_effect.models.model_declared_clone_refresh import (
    EnumDeclaredCloneRefusal,
    EnumDeclaredCloneState,
    ModelDeclaredCloneOutcome,
    ModelDeclaredCloneRefreshReceipt,
    ModelDeclaredCloneRefreshRequest,
)

DEFAULT_TIMEOUT_SECONDS: float = load_node_timeouts(
    "node_canonical_clone_refresh_effect"
).get("declared_clone_timeout_seconds")

# Runs on the host that holds the clone: argv is (path, ref). Prints KEY=value lines and always
# exits 0, so a transport failure (nonzero exit) is told apart from a refusal. A path may be written
# `$HOME/...`, `~/...` or `$OMNI_HOME/...`, expanded on that host.
HOST_SCRIPT = r"""
p=$1; ref=$2
case "$p" in
  "~/"*) p="$HOME/${p#"~/"}" ;;
  '$HOME/'*) p="$HOME/${p#'$HOME/'}" ;;
  '$OMNI_HOME/'*)
    if [ -z "${OMNI_HOME:-}" ]; then echo STATE=PATH_UNRESOLVED; exit 0; fi
    p="$OMNI_HOME/${p#'$OMNI_HOME/'}" ;;
esac
case "$p" in /*) ;; *) echo STATE=PATH_UNRESOLVED; exit 0 ;; esac
[ -d "$p" ] || { echo STATE=PATH_MISSING; exit 0; }
top=$(git -C "$p" rev-parse --show-toplevel 2>/dev/null) || { echo STATE=NOT_A_CLONE; exit 0; }
here=$(cd "$p" && pwd -P); there=$(cd "$top" && pwd -P)
[ "$here" = "$there" ] || { echo STATE=NOT_A_CLONE; exit 0; }
echo BEFORE=$(git -C "$p" rev-parse HEAD 2>/dev/null)
if ! err=$(git -C "$p" fetch --quiet origin "+refs/heads/$ref:refs/remotes/origin/$ref" 2>&1); then
  echo STATE=FETCH_FAILED; echo "MESSAGE=$err" | tr '\n' ' '; echo; exit 0
fi
echo ORIGIN=$(git -C "$p" rev-parse "refs/remotes/origin/$ref")
branch=$(git -C "$p" symbolic-ref --short -q HEAD || echo "")
if [ "$branch" = "$ref" ]; then
  git -C "$p" merge --quiet --ff-only "refs/remotes/origin/$ref" >/dev/null 2>&1
else
  echo "MESSAGE=clone is on '${branch:-a detached head}', not '$ref'"
fi
echo AFTER=$(git -C "$p" rev-parse HEAD 2>/dev/null)
echo STATE=READ
"""


class TransportUnreachableError(Exception):
    """The host could not be reached at all (no route, no login)."""


class ModelTransportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int
    stdout: str = ""
    stderr: str = ""


class ProtocolCloneHostTransport(Protocol):
    """Runs one command on the host a table row names; raises ``TransportUnreachableError``."""

    def run(
        self, host: ModelLabHostRow, argv: Sequence[str], timeout_seconds: float
    ) -> ModelTransportResult: ...


class TransportSubprocess:
    """The deployment transport: this machine's own shell for the local host, ssh for the others.

    ``local_host_name`` is a deployment fact (the table row that names this machine)."""

    def __init__(self, local_host_name: str = "") -> None:
        self._local = local_host_name

    def run(
        self, host: ModelLabHostRow, argv: Sequence[str], timeout_seconds: float
    ) -> ModelTransportResult:
        if host.local or (self._local and host.name == self._local):
            command = list(argv)
        else:
            command = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                host.target,
                shlex.join(argv),
            ]
        try:
            done = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TransportUnreachableError(f"{type(exc).__name__}: {exc}") from exc
        if command[0] == "ssh" and done.returncode == 255:
            raise TransportUnreachableError(
                (done.stderr or "ssh failed").strip()[-300:]
            )
        return ModelTransportResult(
            returncode=done.returncode, stdout=done.stdout, stderr=done.stderr
        )


def _read(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in {"STATE", "BEFORE", "AFTER", "ORIGIN", "MESSAGE"}:
            values[key] = value.strip()
    return values


_SCRIPT_REFUSALS = {
    "PATH_UNRESOLVED": EnumDeclaredCloneRefusal.PATH_UNRESOLVED,
    "PATH_MISSING": EnumDeclaredCloneRefusal.PATH_MISSING,
    "NOT_A_CLONE": EnumDeclaredCloneRefusal.NOT_A_CLONE,
    "FETCH_FAILED": EnumDeclaredCloneRefusal.FETCH_FAILED,
}


class HandlerDeclaredCloneRefresh:
    """Refresh each named host's declared clone of one repository, as the host table says."""

    def __init__(
        self,
        table: ModelLabHostsTable,
        repo_dir_name: str,
        transport: ProtocolCloneHostTransport,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._table = table
        self._dir = repo_dir_name
        self._transport = transport
        self._timeout = timeout_seconds

    def _declared(self, host: ModelLabHostRow) -> ModelLabHostClone | None:
        return next(
            (c for c in host.clones if c.repo.rsplit("/", 1)[-1] == self._dir), None
        )

    def handle(
        self, request: ModelDeclaredCloneRefreshRequest
    ) -> ModelDeclaredCloneRefreshReceipt:
        started = time.monotonic()
        rows = {h.name: h for h in self._table.hosts}
        names = request.hosts or tuple(
            name for name, row in rows.items() if self._declared(row)
        )
        outcomes: list[ModelDeclaredCloneOutcome] = []
        for name in names:
            row = rows.get(name)
            clone = self._declared(row) if row else None
            if row is None or clone is None:
                outcomes.append(
                    self._refusal(
                        name,
                        "",
                        EnumDeclaredCloneRefusal.NOT_DECLARED,
                        f"{name}: the host table declares no clone of {self._dir}",
                    )
                )
                continue
            outcomes.append(self._refresh(row, clone))
        return ModelDeclaredCloneRefreshReceipt(
            outcomes=tuple(outcomes),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _refusal(
        host: str,
        path: str,
        why: EnumDeclaredCloneRefusal,
        message: str,
        *,
        before: str = "",
        after: str = "",
        origin: str = "",
    ) -> ModelDeclaredCloneOutcome:
        return ModelDeclaredCloneOutcome(
            host=host,
            path=path,
            state=EnumDeclaredCloneState.REFUSED,
            refusal=why,
            before_sha=before,
            after_sha=after,
            origin_sha=origin,
            message=message[-2000:],
        )

    def _refresh(
        self, row: ModelLabHostRow, clone: ModelLabHostClone
    ) -> ModelDeclaredCloneOutcome:
        argv = ["sh", "-c", HOST_SCRIPT, "sh", clone.path, clone.ref]
        try:
            done = self._transport.run(row, argv, self._timeout)
        except TransportUnreachableError as exc:
            return self._refusal(
                row.name,
                clone.path,
                EnumDeclaredCloneRefusal.HOST_UNREACHABLE,
                f"{row.name} ({clone.path}): host not reached: {exc}",
            )
        got = _read(done.stdout)
        state = got.get("STATE", "")
        if done.returncode != 0 or not state:
            tail = (done.stderr or done.stdout or "").strip()[-300:]
            return self._refusal(
                row.name,
                clone.path,
                EnumDeclaredCloneRefusal.UNREADABLE_RESULT,
                f"{row.name} ({clone.path}): exit {done.returncode}: {tail}",
            )
        before, after, origin = (
            got.get("BEFORE", ""),
            got.get("AFTER", ""),
            got.get("ORIGIN", ""),
        )
        if state in _SCRIPT_REFUSALS:
            return self._refusal(
                row.name,
                clone.path,
                _SCRIPT_REFUSALS[state],
                f"{row.name} ({clone.path}): {state.lower()} {got.get('MESSAGE', '')}".strip(),
                before=before,
            )
        if not origin or after != origin:
            return self._refusal(
                row.name,
                clone.path,
                EnumDeclaredCloneRefusal.NOT_FAST_FORWARDED,
                f"{row.name} ({clone.path}): at {after[:12]}, origin/{clone.ref} is "
                f"{origin[:12]}; {got.get('MESSAGE', 'fast-forward refused')}",
                before=before,
                after=after,
                origin=origin,
            )
        return ModelDeclaredCloneOutcome(
            host=row.name,
            path=clone.path,
            state=(
                EnumDeclaredCloneState.UP_TO_DATE
                if before == after
                else EnumDeclaredCloneState.REFRESHED
            ),
            before_sha=before,
            after_sha=after,
            origin_sha=origin,
        )
