# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deliver a credential env file to a declared folder on a host, over ssh or locally (OMN-24256).

One fixed bash script does the whole delivery. Every variable (folder, file name, owner, mode, apply flag, the privilege
prefix) and the file content travel on standard input, never on a command line, so the content is on no process list.
The file is written next to its destination with the declared owner and mode and renamed into place, then read back:
the reading names the owner and mode the host holds, never the content. Shape followed:
node_lab_host_key_distribution_effect's adapter_ssh_host_key_transport.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from typing import Literal

from pydantic import SecretStr

from ..models.model_service_principal_mint import (
    ModelServicePrincipalDelivery,
    ModelServicePrincipalDeliveryReading,
)

_TAIL_CHARS = 400

_DELIVERY_SCRIPT = r"""set -eu
IFS= read -r folder
IFS= read -r filename
IFS= read -r owner
IFS= read -r mode
IFS= read -r apply
IFS= read -r nprefix
privs=()
i=0
while [ "$i" -lt "$nprefix" ]; do
    IFS= read -r word
    privs+=("$word")
    i=$((i + 1))
done
tmp="$(mktemp)"
trap 'rm -f -- "$tmp"' EXIT
cat > "$tmp"
case "$folder" in
    /*) dir="$folder" ;;
    *) dir="$HOME/$folder" ;;
esac
dest="$dir/$filename"
want_mode="$(printf '%o' "0$mode")"
run() { ${privs[@]+"${privs[@]}"} "$@"; }
state=would-write
cur=""
if run test -f "$dest" && run cmp -s -- "$dest" "$tmp"; then
    cur="$(run stat -c '%u:%g %a' "$dest")"
    if [ "$cur" = "$owner $want_mode" ]; then state=unchanged; fi
fi
if [ "$state" = unchanged ]; then
    echo "unchanged $cur"
    exit 0
fi
if [ "$apply" != yes ]; then
    echo "would-write $owner $want_mode"
    exit 0
fi
run mkdir -p -- "$dir"
run install -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" -T -- "$tmp" "$dest.new"
run mv -f -- "$dest.new" "$dest"
run cmp -s -- "$dest" "$tmp" || { echo "readback differs: $dest" >&2; exit 4; }
echo "written $(run stat -c '%u:%g %a' "$dest")"
"""


def _scrub(text: str, content: SecretStr) -> str:
    """Remove the content, and every value after an '=' in it, from a text that may echo it."""
    raw = content.get_secret_value()
    out = text.replace(raw, "***")
    for line in raw.splitlines():
        _, sep, value = line.partition("=")
        if sep and value:
            out = out.replace(value, "***")
    return out


def _normal_mode(mode: str) -> str:
    return format(int(mode, 8), "o")


class SshCredentialDelivery:
    """Implements ProtocolCredentialDelivery: ssh to each target's ssh_target, or bash here for a local host name."""

    def __init__(
        self, local_hosts: frozenset[str] = frozenset(), timeout_s: int = 30
    ) -> None:
        self._local_hosts = local_hosts
        self._timeout_s = timeout_s

    def _argv(self, target: ModelServicePrincipalDelivery) -> list[str]:
        if target.host in self._local_hosts:
            return ["bash", "-c", _DELIVERY_SCRIPT]
        return [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            target.ssh_target,
            "bash -c " + shlex.quote(_DELIVERY_SCRIPT),
        ]

    def _run(
        self, target: ModelServicePrincipalDelivery, content: SecretStr, apply: bool
    ) -> subprocess.CompletedProcess[str]:
        header = [
            target.folder,
            target.filename,
            target.owner,
            target.mode,
            "yes" if apply else "no",
            str(len(target.privilege_argv)),
            *target.privilege_argv,
        ]
        return subprocess.run(
            self._argv(target),
            input="\n".join(header) + "\n" + content.get_secret_value(),
            capture_output=True,
            text=True,
            timeout=self._timeout_s,
            check=False,
        )

    async def deliver(
        self,
        target: ModelServicePrincipalDelivery,
        content: SecretStr,
        *,
        apply: bool,
    ) -> ModelServicePrincipalDeliveryReading:
        def failed(detail: str) -> ModelServicePrincipalDeliveryReading:
            return ModelServicePrincipalDeliveryReading(
                host=target.host,
                state="failed",
                owner=target.owner,
                mode=target.mode,
                detail=_scrub(detail, content)[-_TAIL_CHARS:],
            )

        try:
            done = await asyncio.to_thread(self._run, target, content, apply)
        except subprocess.TimeoutExpired:
            return failed("rc=124 timeout")
        except OSError as exc:
            return failed(f"rc={exc.errno}: {type(exc).__name__}")
        if done.returncode != 0:
            return failed(f"rc={done.returncode}: {done.stderr.strip()}")
        lines = done.stdout.strip().splitlines()
        parts = lines[-1].split() if lines else []
        if len(parts) != 3 or parts[0] not in ("unchanged", "written", "would-write"):
            return failed("the host's answer was not '<state> <uid:gid> <mode>'")
        state, owner, mode = parts
        reading_state: Literal["unchanged", "written", "would-write"]
        if state == "unchanged":
            reading_state = "unchanged"
        elif state == "written":
            reading_state = "written"
        else:
            reading_state = "would-write"
        if owner != target.owner or mode != _normal_mode(target.mode):
            return failed(f"the host holds owner {owner} mode {mode}, not the declared")
        return ModelServicePrincipalDeliveryReading(
            host=target.host,
            state=reading_state,
            owner=owner,
            mode=target.mode,
        )
