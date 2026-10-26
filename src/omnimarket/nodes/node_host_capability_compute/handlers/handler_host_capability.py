# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The capability half of the lab host parity probe, its PARITY-CAP lines, and the refusals they give (OMN-17427).

On 2026-10-19 lanes placed on one lab host failed on what the parity record never read: ``onex scm pr-create`` exited 69 (the host's bus
login could not read the pr-create terminal topic), its git credential lacked the workflow scope (a push of a merge touching
``.github/workflows`` was refused), ``shellcheck`` was missing (the shell-hygiene hook failed), ``llm.openrouter.api_key`` was absent
(every delegation was refused) and it had no ssh route to the other lab hosts. Each is now a capability read live on the host by the
same probe in a detached refresh under ``$HOME/.cache/omni/capabilities`` (every ``CAPABILITY_FRESH_MIN`` minutes at most),
printed as ``PARITY-CAP <name> <ok|missing|unknown> <detail>``, and judged the way a plugin mismatch is: a unit whose needs name a
capability the host reads ``missing`` is refused with the reason and what the host owner adds. ``unknown`` (no dispatch venv, no
credential to judge, GitHub or the broker silent) and an older probe without the lines never refuse. Nothing here changes a login or
a credential on the host; the token the git check reads is passed to curl on stdin and never printed.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
    not_configured_message,
)

from ..models.model_host_capability import (
    ModelHostCapability,
    ModelHostCapabilityRequest,
    ModelHostCapabilityResult,
    ModelLabHostsRegistryRoots,
)

SCM_PR_CREATE = "scm-pr-create"
GIT_WORKFLOW_PUSH = "git-workflow-push"
SHELLCHECK = "shellcheck"
DELEGATION_KEY = "delegation-key"
LAB_SSH = "lab-ssh"
CAPABILITIES = (SCM_PR_CREATE, GIT_WORKFLOW_PUSH, SHELLCHECK, DELEGATION_KEY, LAB_SSH)
VERDICTS = ("ok", "missing", "unknown")
# Every remote lane opens its PR with ``onex scm pr-create``. The delegation key is read and recorded, not needed by default: on
# 2026-10-19 the bare probe read it absent on one lab host too, where lanes delegate, so a default need would refuse every host.
LANE_CAPABILITIES = frozenset({SCM_PR_CREATE})
# A dispatcher names further capabilities a unit needs (``git-workflow-push`` for a lane that merges a base touching workflows,
# ``lab-ssh`` for one that drives other lab hosts, ``delegation-key``) as a comma list in this variable of the runner's environment.
NEEDS_ENV = "ONEX_LANE_NEEDS_CAPABILITIES"
# The secret ref every delegation rung resolves its key through (omnimarket ``bifrost_delegation.yaml``).
DELEGATION_SECRET_REF = "llm.openrouter.api_key"
# The bus lane lanes open PRs over (``onex scm pr-create --bus-lane dev``).
SCM_BUS_LANE = "dev"
# Interpreter override (tests, a host with its venv elsewhere); else the dispatch venv under the registry root.
PYTHON_ENV = "ONEX_CAPABILITY_PY"
CAPABILITY_FRESH_MIN = 10
CAPABILITY_CHECK_S = 6
# The bus check's own bound, and the bound of the interpreter running it (it opens the bus and joins a consumer group).
CAPABILITY_SCM_S = 30
CAPABILITY_VENV_S = 45
CAPABILITY_LOCK_MIN = 3
SSH_CONNECT_S = 4
PEER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._-]*$")

# What the host lacks, and what its owner adds, per capability; the probe's detail follows.
OWNER_FIX = {
    SCM_PR_CREATE: "onex scm pr-create exits 69 on this host: its bus login cannot open the pr-create terminal topics; the host "
    "owner grants the host's broker principal read on node_github_repo_write_effect's terminal topics and its "
    "scm_command_client consumer group",
    GIT_WORKFLOW_PUSH: "the host's git credential lacks the workflow scope, so a push touching .github/workflows is refused; the "
    "host owner gives the host a GitHub credential with the workflow scope",
    SHELLCHECK: "shellcheck is not on the PATH lanes run with, so a system shellcheck hook (shell-hygiene) fails; the host owner "
    "installs shellcheck",
    DELEGATION_KEY: f"{DELEGATION_SECRET_REF} does not resolve from the host's secret store, so every delegation is refused; the "
    "host owner adds the key to the host's store",
    LAB_SSH: "the host has no ssh route to the other lab hosts; the host owner adds the host's key (and their host keys) for them",
}

CAPABILITY_PY = r"""import asyncio, sys
what = sys.argv[1]


def say(verdict, detail):
    print(verdict + " " + ("_".join(str(detail).split()) or "-")[:200])


if what == "delegation-key":
    ref = sys.argv[2]
    try:
        from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
        found = resolve_api_key_loop_safe(ref, required=False)
    except Exception as exc:
        say("unknown", "resolver-failed:" + type(exc).__name__)
    else:
        say("ok", ref + "-resolves") if found else say("missing", ref + "-is-absent-from-the-secret-store")
elif what == "scm-pr-create":
    lane, bound, root = sys.argv[2], float(sys.argv[3]), sys.argv[4] if len(sys.argv) > 4 else ""
    try:
        from pathlib import Path
        from omnimarket.handlers.scm import handler_scm_dispatch as dispatch, scm_cli
        from omnimarket.lab_work.bus import _subscribe
        topics = dispatch.load_topics(dispatch.verb("pr-create").node)
    except Exception as exc:
        say("unknown", "scm-unimportable:" + type(exc).__name__)
        sys.exit(0)

    async def drop(message):
        return None

    async def main():
        # The opening and the subscriptions ``onex scm pr-create`` makes before it publishes, with its consumer group prefix.
        opts = {"bus": "kafka", "bus_lane": lane, "kafka_bootstrap": None, "omnibase_path": Path(root) if root else None}
        async with scm_cli._open_bus(opts) as bus:
            group = dispatch.ScmBusCaller(bus)._group
            for topic in [t for t in (topics.success, topics.failure) if t]:
                stop = await _subscribe(bus, topic, drop, group, "latest")
                await stop()
        return topics.success

    try:
        topic = asyncio.run(asyncio.wait_for(main(), bound))
    except asyncio.TimeoutError:
        say("unknown", "bus-check-timed-out")
    except Exception as exc:
        # Only a broker that refuses the login or cannot be reached is the exit 69 a lane meets; a setup error of the check is not.
        name, said = type(exc).__name__, str(exc)
        refused = any(w in said.lower() for w in ("authoriz", "denied", "acl", "sasl", "authentication"))
        if name == "InfraConnectionError" or (name == "LabRunBusError" and refused):
            say("missing", "bus:" + name + ":" + said)
        else:
            say("unknown", "bus-check-failed:" + name + ":" + said)
    else:
        say("ok", "subscribed:" + str(topic))
else:
    say("unknown", "no-such-check:" + what)
"""

# Runs inside the parity probe after it has set ``PARITY_H`` (the registry root it found). ``__BOUND__`` defines ``probe_bound``; the
# git and ssh checks run in their own background subshell, so each exports the PATH lanes run with there.
CAPABILITY_SH = r"""
__BOUND__
CAP_DIR="$HOME/.cache/omni/capabilities"; mkdir -p "$CAP_DIR" 2>/dev/null
CAP_PYI=""
if [ -n "${__PYTHON_ENV__+x}" ]; then [ -x "$__PYTHON_ENV__" ] && CAP_PYI="$__PYTHON_ENV__"
else for r in "${PARITY_H:-}" "${ONEX_REGISTRY_ROOT:-}" __REGISTRY_ROOTS__; do
  if [ -n "$r" ] && [ -x "$r/.onex-dispatch-venv/bin/python" ]; then CAP_PYI="$r/.onex-dispatch-venv/bin/python"; break; fi
done; fi
CAP_PY='__CAPABILITY_PY__'
# The peers arrive as octal escapes (``peer_codes``) so a probe script never carries another host's target in plain text: callers
# and their tests tell which host a probe is for by its target in the argv.
CAP_CODES='__PEER_CODES__'; CAP_PEERS=""
for e in $CAP_CODES; do CAP_PEERS="$CAP_PEERS $(printf '%b' "$e")"; done
CAP_PEERS="${CAP_PEERS# }"
cap_line() { case "$1" in ok\ *|missing\ *|unknown\ *) printf '%s\n' "$1";; *) printf 'unknown %s\n' "$2";; esac; }
cap_venv() {
  [ -n "$CAP_PYI" ] || { echo "unknown no-dispatch-venv"; return 0; }
  cv=$(probe_bound __VENV_S__ "$CAP_PYI" -c "$CAP_PY" "$@" 2>/dev/null); cr=$?
  cap_line "$(printf '%s\n' "$cv" | tail -n 1)" "check-exit-$cr"
}
cap_git() {
  __FETCH_PATH_EXPORT__
  printf 'protocol=https\nhost=github.com\n\n' > "$CAP_DIR/.git-request"
  cg=$( probe_bound __CHECK_S__ sh -c 'GIT_TERMINAL_PROMPT=0 git -c credential.interactive=never credential fill < "$1"' sh "$CAP_DIR/.git-request" 2>/dev/null | sed -n 's/^password=//p' | head -n 1 )
  [ -n "$cg" ] || { echo "unknown no-github-credential-for-git"; return 0; }
  command -v curl >/dev/null 2>&1 || { cg=""; echo "unknown no-curl"; return 0; }
  ch=$(printf 'Authorization: token %s\n' "$cg" | curl -sS -o /dev/null -D - --max-time __CHECK_S__ -H @- https://api.github.com/rate_limit 2>/dev/null | tr -d '\r'); cg=""
  cs=$(printf '%s\n' "$ch" | sed -n 's/^HTTP\/[0-9.]* \([0-9][0-9]*\).*/\1/p' | tail -n 1)
  sc=$(printf '%s\n' "$ch" | sed -n 's/^[Xx]-[Oo][Aa][Uu][Tt][Hh]-[Ss][Cc][Oo][Pp][Ee][Ss]: *//p' | tr -d ' ' | tail -n 1)
  if [ "$cs" = 401 ]; then echo "missing github-answered-401-for-the-git-credential"
  elif [ -z "$cs" ]; then echo "unknown github-gave-no-answer"
  elif [ -z "$sc" ]; then echo "unknown no-oauth-scopes-header-http=$cs"
  else case ",$sc," in *,workflow,*) echo "ok scopes=$sc";; *) echo "missing scopes=$sc";; esac; fi
}
cap_ssh() {
  __FETCH_PATH_EXPORT__
  [ -n "$CAP_PEERS" ] || { echo "unknown no-peers-named"; return 0; }
  sp=""; sb=""
  for t in $CAP_PEERS; do
    ( probe_bound __CHECK_S__ ssh -n -o BatchMode=yes -o ConnectTimeout=__SSH_S__ -o ConnectionAttempts=1 "$t" true >/dev/null 2>&1 || echo "$t" ) > "$CAP_DIR/.ssh.$t" &
    sp="$sp $!"
  done
  for p in $sp; do wait "$p"; done
  for t in $CAP_PEERS; do [ -s "$CAP_DIR/.ssh.$t" ] && sb="$sb,$t"; rm -f "$CAP_DIR/.ssh.$t"; done
  if [ -n "$sb" ]; then echo "missing no-ssh-route-to:${sb#,}"; else echo "ok reached:$(echo $CAP_PEERS | tr ' ' ',')"; fi
}
# A check runs detached and is never waited on: the bus check alone took 10 s on one lab host (2026-10-19) against the remote probe's 20 s.
# The probe prints the last verdict the host wrote (``unknown not-read`` before the first), and starts a refresh once it is older than
# the fresh window; the lock keeps one refresh per check, and a lock left by a killed refresh is cleared after __LOCK_MIN__ minutes.
cap_cached() {
  cn=$1; shift
  [ -s "$CAP_DIR/$cn" ] && [ -n "$(find "$CAP_DIR/$cn" -mmin -__FRESH_MIN__ 2>/dev/null)" ] && return 0
  find "$CAP_DIR/$cn.lock" -maxdepth 0 -mmin +__LOCK_MIN__ -exec rmdir {} \; 2>/dev/null
  mkdir "$CAP_DIR/$cn.lock" 2>/dev/null || return 0
  ( trap '' HUP; "$@" > "$CAP_DIR/$cn.tmp" 2>/dev/null; mv -f "$CAP_DIR/$cn.tmp" "$CAP_DIR/$cn"; rmdir "$CAP_DIR/$cn.lock" ) </dev/null >/dev/null 2>&1 &
}
cap_cached __SCM__ cap_venv __SCM__ __SCM_LANE__ __SCM_S__ "${PARITY_H:-}"
cap_cached __KEY__ cap_venv __KEY__ __SECRET_REF__
cap_cached __GIT__ cap_git
cap_cached __SSH__-__PEERS_KEY__ cap_ssh
for cn in __SCM__ __GIT__ __KEY__; do cv=$(head -n 1 "$CAP_DIR/$cn" 2>/dev/null); echo "PARITY-CAP $cn ${cv:-unknown not-read}"; done
cv=$(head -n 1 "$CAP_DIR/__SSH__-__PEERS_KEY__" 2>/dev/null); echo "PARITY-CAP __SSH__ ${cv:-unknown not-read}"
cv=$( __FETCH_PATH_EXPORT__; command -v shellcheck 2>/dev/null ) || cv=""
if [ -n "$cv" ]; then echo "PARITY-CAP __SHELLCHECK__ ok $cv"; else echo "PARITY-CAP __SHELLCHECK__ missing shellcheck-is-not-on-the-lane-PATH"; fi
"""


def peer_codes(peers: Sequence[str]) -> str:
    """The peers as ``printf %b`` octal escapes, one word each."""
    return " ".join("".join(f"\\0{ord(ch):03o}" for ch in p) for p in peers)


REGISTRY_ROOT_RE = re.compile(r"^[A-Za-z0-9_$/.~-]+$")


def _validate_registry_roots(roots: Sequence[str], source: str) -> tuple[str, ...]:
    if (
        not roots
        or isinstance(roots, (str, bytes))
        or any(
            not isinstance(root, str) or REGISTRY_ROOT_RE.fullmatch(root) is None
            for root in roots
        )
    ):
        raise NodeOverlayError(
            f"{source}: lab_hosts.registry_root_candidates must be a non-empty "
            "list of non-empty shell-safe path strings; set ONEX_REGISTRY_ROOT "
            "or supply an overlay under ONEX_SKILL_OVERLAY_ROOTS"
        )
    return tuple(roots)


def registry_root_candidates(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Resolve registry roots from the environment or the lab hosts overlay."""
    env = os.environ if environ is None else environ
    pinned = env.get("ONEX_REGISTRY_ROOT", "")
    if pinned.strip():
        return _validate_registry_roots((pinned,), "ONEX_REGISTRY_ROOT")
    overlay = (
        load_structured_overlay("lab_hosts", ModelLabHostsRegistryRoots, env)
        if find_node_overlay("lab_hosts", env) is not None
        else None
    )
    if overlay is None or overlay.registry_root_candidates is None:
        where = find_node_overlay("lab_hosts", env)
        raise NodeOverlayError(
            (f"{where}: " if where else "")
            + not_configured_message(
                "lab_hosts",
                "lab_hosts.registry_root_candidates",
                ("ONEX_REGISTRY_ROOT",),
            )
        )
    return overlay.registry_root_candidates


def capability_probe_sh(
    peers: Sequence[str],
    *,
    bound: str,
    path_export: str,
    registry_roots: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """The probe block printing one PARITY-CAP line per capability. ``peers`` are the ssh targets of the other lab hosts (a target
    that is not a plain host word is dropped), ``bound`` the shell that defines ``probe_bound`` and ``path_export`` the PATH export
    lanes run with (``landing_placement.fetch_path_export``)."""
    roots = (
        registry_root_candidates(environ)
        if registry_roots is None
        else _validate_registry_roots(registry_roots, "explicit registry_roots")
    )
    kept = [p for p in dict.fromkeys(peers) if PEER_RE.match(p)]
    key = hashlib.sha256(",".join(kept).encode()).hexdigest()[:12] if kept else "none"
    swaps = {
        "__REGISTRY_ROOTS__": " ".join(f'"{root}"' for root in roots),
        "__BOUND__": bound,
        "__CAPABILITY_PY__": CAPABILITY_PY,
        "__FETCH_PATH_EXPORT__": path_export,
        "__PYTHON_ENV__": PYTHON_ENV,
        "__PEER_CODES__": peer_codes(kept),
        "__PEERS_KEY__": key,
        "__CHECK_S__": str(CAPABILITY_CHECK_S),
        "__SCM_S__": str(CAPABILITY_SCM_S),
        "__VENV_S__": str(CAPABILITY_VENV_S),
        "__LOCK_MIN__": str(CAPABILITY_LOCK_MIN),
        "__SSH_S__": str(SSH_CONNECT_S),
        "__FRESH_MIN__": str(CAPABILITY_FRESH_MIN),
        "__SCM_LANE__": SCM_BUS_LANE,
        "__SECRET_REF__": DELEGATION_SECRET_REF,
        "__SCM__": SCM_PR_CREATE,
        "__KEY__": DELEGATION_KEY,
        "__GIT__": GIT_WORKFLOW_PUSH,
        "__SSH__": LAB_SSH,
        "__SHELLCHECK__": SHELLCHECK,
    }
    text = CAPABILITY_SH
    for placeholder, value in swaps.items():
        text = text.replace(placeholder, value)
    return text


def parse_capability_lines(text: str) -> tuple[ModelHostCapability, ...]:
    """The last PARITY-CAP line of each known capability, in CAPABILITIES order; lines of another shape are skipped."""
    found: dict[str, ModelHostCapability] = {}
    for line in text.splitlines():
        parts = line.split(None, 3)
        if (
            len(parts) >= 3
            and parts[0] == "PARITY-CAP"
            and parts[1] in CAPABILITIES
            and parts[2] in VERDICTS
        ):
            found[parts[1]] = ModelHostCapability(
                name=parts[1],
                verdict=parts[2],
                detail=parts[3].strip() if len(parts) > 3 else "-",
            )
    return tuple(found[name] for name in CAPABILITIES if name in found)


def capability_failures(
    capabilities: Iterable[ModelHostCapability], needs: Iterable[str]
) -> list[tuple[str, str]]:
    """``(item, reason)`` for each capability in ``needs`` the host reads ``missing``; ``unknown`` and an unread one pass."""
    wanted = frozenset(needs)
    return [
        (
            f"capability:{c.name}",
            f"CAPABILITY-MISSING {c.name}: {OWNER_FIX[c.name]} ({c.detail})",
        )
        for c in capabilities
        if c.name in wanted and c.verdict == "missing"
    ]


def system_shellcheck_hook(config_text: str) -> bool:
    """Whether a ``.pre-commit-config.yaml`` holds a ``language: system`` hook that runs shellcheck (omnimarket's shell-hygiene),
    which needs the host's own shellcheck; a shellcheck-py hook installs its own and needs none."""
    blocks = re.split(r"\n\s*- id:", config_text)
    return any(
        "shellcheck" in b.lower() and re.search(r"^\s*language:\s*system\b", b, re.M)
        for b in blocks
    )


def unit_capabilities(
    repo: str | None, env: Mapping[str, str] | None = None
) -> frozenset[str]:
    """The capabilities a unit built from registry clone ``repo`` needs beyond its kind's: ``shellcheck`` when the launching host's
    clone of it runs a system shellcheck hook, and those ``NEEDS_ENV`` names."""
    env = os.environ if env is None else env
    named = {
        n.strip()
        for n in env.get(NEEDS_ENV, "").split(",")
        if n.strip() in CAPABILITIES
    }
    home = env.get("ONEX_REGISTRY_ROOT")
    if repo and home:
        try:
            config = (Path(home) / repo / ".pre-commit-config.yaml").read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeDecodeError):
            config = ""
        if system_shellcheck_hook(config):
            named.add(SHELLCHECK)
    return frozenset(named)


class HandlerHostCapability:
    def handle(self, request: ModelHostCapabilityRequest) -> ModelHostCapabilityResult:
        return ModelHostCapabilityResult(
            failures=tuple(capability_failures(request.capabilities, request.needs))
        )
