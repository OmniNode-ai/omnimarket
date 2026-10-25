# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render shell discovery of configured host inputs and the published snapshot."""

from __future__ import annotations

import re

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, resolve_setting
from omnimarket.nodes.node_lane_host_discovery_compute.models.model_lane_host_discovery import (
    ModelLaneHostDiscoveryRequest,
    ModelLaneHostDiscoveryResult,
)


def _profile(name: str) -> str:
    """The login profile's value of ``name``: the lane's own shell is not a login shell."""
    return f'"$(bash -lc "printf %s \\"\\${{{name}:-}}\\"" 2>/dev/null </dev/null)"'


def _validate(setting: str, value: str, pattern: str) -> None:
    if re.fullmatch(pattern, value) is None:
        raise NodeOverlayError(f"node_lane_host_discovery_compute: invalid {setting}")


def kb_lines(
    knowledge_base: str, home_env: str, registry_root_var: str = "ONEX_REGISTRY_ROOT"
) -> tuple[str, ...]:
    """Find a configured clone through the host environment, profile or registry root."""
    _validate("knowledge_base", knowledge_base, r"[A-Za-z0-9._-]+")
    _validate("knowledge_base_home_env", home_env, r"[A-Za-z_][A-Za-z0-9_]*")
    _validate("registry_root_var", registry_root_var, r"[A-Za-z_][A-Za-z0-9_]*")
    return (
        (
            'KB=""; for d in '
            f'"${{{home_env}:-}}" '
            f"{_profile(home_env)} "
            f'"${registry_root_var}/{knowledge_base}"; '
            'do [ -n "$d" ] && [ -e "$d/.git" ] && { KB="$d"; break; }; done'
        ),
        (
            f'if [ -n "$KB" ]; then export {home_env}="$KB"; echo "REMOTE-LANE-KB-PATH $KB"; '
            f'else unset {home_env}; echo "REMOTE-LANE-KB-ABSENT no knowledge-base clone on '
            f"$(hostname) ({home_env} in ~/.omnibase/.env or the login profile, "
            f'${registry_root_var}/{knowledge_base})"; fi'
        ),
    )


def prrp_snapshot_lines(destination: str) -> tuple[str, ...]:
    """The runner-placement hook's dated snapshot, for every engine (OMN-20141). A lane cannot read repository
    visibility or Actions variables live (raw GitHub read budget 0, HTTP 403), so the private-repo-runner-placement
    hook needs ``PRRP_VARIABLES_SNAPSHOT``; without it every workflow commit in a private repository exits 2. The
    file's age is the hook's to judge (it refuses one older than 24 hours), so these lines never read its content."""
    return (
        (
            'PS=""; for f in "${PRRP_VARIABLES_SNAPSHOT:-}" '
            f"{_profile('PRRP_VARIABLES_SNAPSHOT')} "
            f'"$HOME/{destination}"; do [ -n "$f" ] && [ -r "$f" ] && {{ PS="$f"; break; }}; done'
        ),
        (
            'if [ -n "$PS" ]; then export PRRP_VARIABLES_SNAPSHOT="$PS"; echo "REMOTE-LANE-PRRP-SNAPSHOT $PS"; '
            'else unset PRRP_VARIABLES_SNAPSHOT; echo "REMOTE-LANE-PRRP-SNAPSHOT-ABSENT no runner-placement snapshot on '
            f"$(hostname) (PRRP_VARIABLES_SNAPSHOT in ~/.omnibase/.env or the login profile, $HOME/{destination}; "
            'publish it with onex-prrp-snapshot-publish)"; fi'
        ),
    )


class HandlerLaneHostDiscovery:
    def handle(
        self, request: ModelLaneHostDiscoveryRequest
    ) -> ModelLaneHostDiscoveryResult:
        fields = frozenset({"knowledge_base", "knowledge_base_home_env"})

        def setting(name: str, explicit: str | None, env_var: str) -> str:
            return (
                explicit
                if explicit is not None
                else resolve_setting(
                    "node_lane_host_discovery_compute",
                    name,
                    fields=fields,
                    env_vars=(env_var,),
                    environ=request.environ,
                )
            )

        knowledge_base = setting(
            "knowledge_base", request.knowledge_base, "ONEX_KNOWLEDGE_BASE_REPO"
        )
        home_env = setting(
            "knowledge_base_home_env",
            request.knowledge_base_home_env,
            "ONEX_KNOWLEDGE_BASE_HOME_ENV",
        )
        return ModelLaneHostDiscoveryResult(
            kb_lines=kb_lines(knowledge_base, home_env, request.registry_root_var),
            prrp_snapshot_lines=prrp_snapshot_lines(request.prrp_snapshot_destination),
        )
