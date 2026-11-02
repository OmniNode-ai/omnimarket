# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SSH and local shell transport for public host key distribution."""

from __future__ import annotations

import re
import shlex
import subprocess

from omnimarket.models.overlay_keys import ModelLabHostRow

from ..models.model_lab_host_key_distribution import (
    EnumKeyFile,
    EnumLineState,
    ModelHostKeyMaterial,
    ModelLabHostKeyDistributionRequest,
)
from ..protocols.protocol_host_key_transport import HostTransportError

_SAFE_TARGET = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*")

_READ_SCRIPT = r"""set -eu
IFS= read -r identity_count
found=0
printf '__USER_KEY__\n'
for ((i=0; i<identity_count; i++)); do
    IFS= read -r name
    if [ "$found" = 0 ] && [ -e "$HOME/.ssh/$name" ]; then
        cat -- "$HOME/.ssh/$name"
        printf '\n'
        found=1
    fi
done
if [ "$found" = 0 ]; then
    echo 'no public identity key found' >&2
    exit 1
fi
printf '__HOST_KEYS__\n'
IFS= read -r host_count
found=0
for ((i=0; i<host_count; i++)); do
    IFS= read -r path
    if [ -r "$path" ]; then
        cat -- "$path"
        printf '\n'
        found=1
    fi
done
if [ "$found" = 0 ]; then
    echo 'no readable public host key found' >&2
    exit 1
fi
printf '__END__\n'
"""

_ENSURE_SCRIPT = r"""set -eu
IFS= read -r kind
IFS= read -r needle
IFS= read -r line
IFS= read -r apply
case "$kind" in
    authorized_keys|known_hosts) file="$HOME/.ssh/$kind" ;;
    *) echo 'invalid key file' >&2; exit 2 ;;
esac
if [ -e "$file" ]; then
    rc=0
    grep -F -q -- "$needle" "$file" || rc=$?
    if [ "$rc" = 0 ]; then printf 'present\n'; exit 0; fi
    if [ "$rc" != 1 ]; then exit "$rc"; fi
fi
if [ "$apply" != yes ]; then printf 'absent\n'; exit 0; fi
umask 077
mkdir -p -- "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
touch -- "$file"
chmod 600 "$file"
if [ -s "$file" ] && [ -n "$(tail -c 1 "$file")" ]; then
    printf '\n' >> "$file"
fi
printf '%s\n' "$line" >> "$file"
printf 'added\n'
"""


class SshHostKeyTransport:
    """Send fixed scripts with all variable data on standard input."""

    def __init__(self) -> None:
        self._requests: dict[str, ModelLabHostKeyDistributionRequest] = {}

    def _run(self, host: ModelLabHostRow, script: str, stdin: str) -> str:
        if _SAFE_TARGET.fullmatch(host.target) is None:
            raise HostTransportError("invalid SSH target")
        request = self._requests.get(host.name)
        local = host.local or (request is not None and request.local_host == host.name)
        argv = (
            ["bash", "-c", script]
            if local
            else [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=8",
                host.target,
                "bash -c " + shlex.quote(script),
            ]
        )
        try:
            done = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=request.timeout_s if request else 30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            raise HostTransportError(
                f"rc=124 timeout: {stderr.strip()[:300]}"
            ) from None
        except OSError as exc:
            raise HostTransportError(
                f"rc={exc.errno}: {str(exc).strip()[:300]}"
            ) from None
        if done.returncode != 0:
            raise HostTransportError(
                f"rc={done.returncode}: {done.stderr.strip()[:300]}"
            )
        return done.stdout

    def read_material(
        self, host: ModelLabHostRow, request: ModelLabHostKeyDistributionRequest
    ) -> ModelHostKeyMaterial:
        self._requests[host.name] = request
        stdin = "\n".join(
            (
                str(len(request.identity_public_files)),
                *request.identity_public_files,
                str(len(request.host_key_files)),
                *request.host_key_files,
                "",
            )
        )
        output = self._run(host, _READ_SCRIPT, stdin)
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        try:
            split = lines.index("__HOST_KEYS__")
            if lines[0] != "__USER_KEY__" or split != 2 or lines[-1] != "__END__":
                raise ValueError("invalid public material framing")
            return ModelHostKeyMaterial(user_key=lines[1], host_keys=tuple(lines[3:-1]))
        except (ValueError, IndexError):
            raise HostTransportError("invalid or missing public key material") from None

    def ensure_line(
        self,
        host: ModelLabHostRow,
        file: EnumKeyFile,
        needle: str,
        line: str,
        *,
        apply: bool,
    ) -> EnumLineState:
        if not needle or not line or any(char in needle + line for char in "\n\r\0"):
            raise HostTransportError("key lines must be nonempty single lines")
        stdin = "\n".join((file.value, needle, line, "yes" if apply else "no", ""))
        output = self._run(host, _ENSURE_SCRIPT, stdin).strip()
        try:
            return EnumLineState(output)
        except ValueError:
            raise HostTransportError("invalid append result from transport") from None
