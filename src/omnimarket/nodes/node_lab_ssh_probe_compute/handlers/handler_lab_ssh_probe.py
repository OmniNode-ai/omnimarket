# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Renders the lab-ssh half of the lab host parity probe, with each peer's fallback targets (OMN-24279).

The parity probe prints ``PARITY-CAP lab-ssh <ok|missing|unknown> <detail>``: whether the host can ssh to the other lab hosts. It
used to be handed one ssh destination per peer, so on 2026-11-13 h202, which cannot resolve the names ``h051`` and ``h053``, read
``missing`` though their declared fallbacks (``hp-gen8``, ``hp-gen9``) answer, and a parked host (``available: false``) was still
named. The script now tries each peer's target and then its declared fallback targets in order, and a parked host is never a peer.

The caller injects every bound and the shell that bounds a command; nothing here names a host or a bound. The block is
self-contained: it caches its verdict the way the other capability checks do and prints exactly one line.
"""

from __future__ import annotations

import hashlib
import re

from ..models import ModelLabSshProbeRequest, ModelLabSshProbeScript

LAB_SSH = "lab-ssh"

LAB_SSH_SH = r"""
__BOUND__
CAP_DIR="$HOME/__CACHE_DIR__"; mkdir -p "$CAP_DIR" 2>/dev/null
# Each peer is one word ``<label>=<destination>,<destination>``; every part arrives as octal escapes (``printf %b``), so the
# script never carries another host's name in plain text.
CAP_GROUPS='__GROUPS__'
cap_ssh() {
  __PATH_EXPORT__
  [ -n "$CAP_GROUPS" ] || { echo "unknown no-peers-named"; return 0; }
  n=0; sp=""
  for g in $CAP_GROUPS; do
    n=$((n + 1)); out="$CAP_DIR/.ssh.$$.$n"
    (
      label=$(printf '%b' "${g%%=*}"); got=""
      for e in $(printf '%s\n' "${g#*=}" | tr ',' ' '); do
        d=$(printf '%b' "$e")
        if probe_bound __CHECK_S__ ssh -n -o BatchMode=yes -o ConnectTimeout=__CONNECT_S__ -o ConnectionAttempts=1 "$d" true >/dev/null 2>&1; then got="$d"; break; fi
      done
      if [ -n "$got" ]; then echo "ok $got"; else echo "missing $label"; fi
    ) > "$out" &
    sp="$sp $!"
  done
  for p in $sp; do wait "$p"; done
  sb=""; sr=""; i=1
  while [ "$i" -le "$n" ]; do
    out="$CAP_DIR/.ssh.$$.$i"; v=$(head -n 1 "$out" 2>/dev/null); rm -f "$out"
    case "$v" in ok\ *) sr="$sr,${v#ok }";; missing\ *) sb="$sb,${v#missing }";; *) sb="$sb,unread";; esac
    i=$((i + 1))
  done
  if [ -n "$sb" ]; then echo "missing no-ssh-route-to:${sb#,}"; else echo "ok reached:${sr#,}"; fi
}
# The verdict runs detached and is never waited on; the probe prints the last one the host wrote (``unknown not-read`` before the
# first) and starts a refresh once it is older than the fresh window. The lock keeps one refresh, and a lock left by a killed
# refresh is cleared after __LOCK_MIN__ minutes.
cn="__NAME__"
if ! { [ -s "$CAP_DIR/$cn" ] && [ -n "$(find "$CAP_DIR/$cn" -mmin -__FRESH_MIN__ 2>/dev/null)" ]; }; then
  find "$CAP_DIR/$cn.lock" -maxdepth 0 -mmin +__LOCK_MIN__ -exec rmdir {} \; 2>/dev/null
  if mkdir "$CAP_DIR/$cn.lock" 2>/dev/null; then
    ( trap '' HUP; cap_ssh > "$CAP_DIR/$cn.tmp" 2>/dev/null; mv -f "$CAP_DIR/$cn.tmp" "$CAP_DIR/$cn"; rmdir "$CAP_DIR/$cn.lock" ) </dev/null >/dev/null 2>&1 &
  fi
fi
cv=$(head -n 1 "$CAP_DIR/$cn" 2>/dev/null); echo "PARITY-CAP __CAPABILITY__ ${cv:-unknown not-read}"
"""


def octal(word: str) -> str:
    """``word`` as ``printf %b`` octal escapes."""
    return "".join(f"\\0{ord(ch):03o}" for ch in word)


class HandlerLabSshProbe:
    """Render the ssh route check for one host from the host table; never run it."""

    def handle(self, request: ModelLabSshProbeRequest) -> ModelLabSshProbeScript:
        valid = re.compile(request.destination_pattern)
        groups: list[tuple[str, tuple[str, ...]]] = []
        for row in request.rows:
            if row.name == request.host or not row.available:
                continue
            if not valid.match(row.target):
                continue
            destinations = tuple(
                dict.fromkeys(
                    d for d in (row.target, *row.fallback_targets) if valid.match(d)
                )
            )
            groups.append((row.target, destinations))
        groups = list(dict.fromkeys(groups))
        encoded = " ".join(
            octal(label) + "=" + ",".join(octal(d) for d in dests)
            for label, dests in groups
        )
        key = hashlib.sha256(
            ";".join(f"{label}={','.join(dests)}" for label, dests in groups).encode()
        ).hexdigest()[:12]
        name = f"{LAB_SSH}-{key}" if groups else f"{LAB_SSH}-none"
        swaps = {
            "__BOUND__": request.bound_sh,
            "__PATH_EXPORT__": request.path_export,
            "__CACHE_DIR__": request.cache_dir,
            "__GROUPS__": encoded,
            "__CHECK_S__": str(request.check_s),
            "__CONNECT_S__": str(request.connect_s),
            "__FRESH_MIN__": str(request.fresh_min),
            "__LOCK_MIN__": str(request.lock_min),
            "__NAME__": name,
            "__CAPABILITY__": LAB_SSH,
        }
        script = LAB_SSH_SH
        for placeholder, value in swaps.items():
            script = script.replace(placeholder, value)
        return ModelLabSshProbeScript(
            script=script, peers=tuple(label for label, _ in groups)
        )
