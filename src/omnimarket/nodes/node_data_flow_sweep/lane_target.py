# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file-internal-ip OMN-13552 reason="lane registry resolves the canonical .201 runtime-lane host; overridable via --runtime-host; not a shipping connection string"
"""Lane/target resolution for node_data_flow_sweep collection probes.

OMN-13552: the collector previously probed *whatever host the CLI process is on*
(``docker exec omnibase-infra-redpanda ...`` / ``psql -h localhost ...``) rather
than the lane it was nominally verifying. For the dev/stability/prod lanes that
run on ``192.168.86.201`` this produced false-broken (no local container =>
"Topic does not exist" => PRODUCER_DOWN) or false-clean (a stale local stack)
verdicts for a lane that was never actually probed.

This module resolves a lane name to the concrete broker + Postgres endpoints for
that lane (container names + the host they live on), mirroring the runtime
address-registry / lane-manifest source of truth (OMN-10345) and the SSH +
``docker exec`` remote-probe transport already proven in
``node_integration_sweep_orchestrator`` (OMN-7238 class fix). The collector then
probes the *targeted* lane regardless of which host the CLI runs on.

A ``ModelLaneTarget`` carries either:

* an explicit ``runtime_host`` (SSH host) + container names — remote lanes reach
  the broker/DB via ``ssh <host> docker exec <container> ...``; or
* ``runtime_host=""`` — the local lane, probed via local ``docker exec`` /
  ``psql`` exactly as before (no transport change for in-stack callers).
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Lane registry — the .201 runtime lanes and their broker/DB container names.
#
# Container names are derived from the canonical lane manifest
# (omnibase_infra/deploy/lane-census/lane-manifest.yaml): every non-dev lane
# prefixes its containers with the compose project (omnibase-infra-<lane>-*),
# while the dev lane (compose project ``omnibase-infra``) uses the unprefixed
# ``omnibase-infra-redpanda`` / ``omnibase-infra-postgres`` names.
#
# host="" means "the host this process runs on" (local docker / local psql).
# A non-empty host is reached over SSH; the probes run ``docker exec`` inside the
# lane's containers on that remote host.
# ---------------------------------------------------------------------------

# onex-allow-internal-ip OMN-13552 reason="default runtime host for the .201 lanes; overridden by --runtime-host / ONEX_DATA_FLOW_RUNTIME_HOST; not a shipping connection string"
_DEFAULT_RUNTIME_HOST: Final[str] = "192.168.86.201"
_SSH_USER: Final[str] = "jonah"


class ModelLaneTarget(BaseModel):
    """Resolved probe endpoints for one runtime lane.

    ``runtime_host`` empty => local probes (``docker exec`` / ``psql`` on this
    host). Non-empty => remote probes via ``ssh <ssh_user>@<runtime_host>
    docker exec <container> ...``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    lane: str = Field(..., min_length=1, description="Lane name (e.g. dev, prod).")
    runtime_host: str = Field(
        default="",
        description="SSH host the lane runs on; empty => local docker/psql.",
    )
    ssh_user: str = Field(
        default=_SSH_USER,
        min_length=1,
        description="SSH user for remote docker-exec probes.",
    )
    redpanda_container: str = Field(
        ...,
        min_length=1,
        description="Redpanda container name on the lane for rpk probes.",
    )
    postgres_container: str = Field(
        ...,
        min_length=1,
        description="Postgres container name on the lane for psql probes.",
    )
    postgres_user: str = Field(
        default="postgres",
        min_length=1,
        description="Postgres role used by psql row-count probes.",
    )
    postgres_db: str = Field(
        default="omnidash_analytics",
        min_length=1,
        description="Postgres database the row-count probes inspect.",
    )
    consumer_group: str = Field(
        default="omnidash-read-model-v2",
        min_length=1,
        description="Consumer group whose lag is probed via rpk group describe.",
    )

    @property
    def is_remote(self) -> bool:
        """True when probes must cross the network to a remote host."""
        return bool(self.runtime_host.strip())


# Per-lane container layout on .201, keyed by lane name. dev uses the unprefixed
# compose-project container names; every other lane prefixes by compose project.
_LANE_CONTAINERS: Final[dict[str, tuple[str, str]]] = {
    "dev": ("omnibase-infra-redpanda", "omnibase-infra-postgres"),
    "stability-test": (
        "omnibase-infra-stability-test-redpanda",
        "omnibase-infra-stability-test-postgres",
    ),
    "prod": ("omnibase-infra-prod-redpanda", "omnibase-infra-prod-postgres"),
    "judge": ("omnibase-infra-judge-redpanda", "omnibase-infra-judge-postgres"),
}

# Lane aliases so operators can pass the common short names.
_LANE_ALIASES: Final[dict[str, str]] = {
    "stability": "stability-test",
}

KNOWN_LANES: Final[tuple[str, ...]] = tuple(_LANE_CONTAINERS)


class LaneResolutionError(ValueError):
    """Raised when a lane name cannot be resolved to probe endpoints.

    Fail-loud on an unknown lane: never silently fall back to a local stack that
    would mislabel a remote lane as PRODUCER_DOWN / clean (OMN-13552 DoD).
    """


def resolve_lane_target(
    lane: str,
    *,
    runtime_host: str | None = None,
    ssh_user: str | None = None,
    postgres_db: str | None = None,
    postgres_user: str | None = None,
    consumer_group: str | None = None,
) -> ModelLaneTarget:
    """Resolve ``lane`` to a :class:`ModelLaneTarget`.

    ``lane="local"`` keeps the legacy in-stack behavior (local docker/psql). Any
    of the .201 lanes (``dev`` / ``stability-test`` / ``prod`` / ``judge``)
    resolve to that lane's container names on the runtime host (default
    ``192.168.86.201``, overridable). An unknown lane raises
    :class:`LaneResolutionError` so the caller can fail loud instead of probing
    the wrong host.
    """
    normalized = lane.strip().lower()
    normalized = _LANE_ALIASES.get(normalized, normalized)

    if normalized == "local":
        # Local lane: probe this host's stack. Container/host overrides still
        # honored so a caller can target a non-default local container.
        return ModelLaneTarget(
            lane="local",
            runtime_host=(runtime_host or "").strip(),
            ssh_user=(ssh_user or _SSH_USER),
            redpanda_container="omnibase-infra-redpanda",
            postgres_container="omnibase-infra-postgres",
            postgres_user=(postgres_user or "postgres"),
            postgres_db=(postgres_db or "omnidash_analytics"),
            consumer_group=(consumer_group or "omnidash-read-model-v2"),
        )

    containers = _LANE_CONTAINERS.get(normalized)
    if containers is None:
        raise LaneResolutionError(
            f"unknown lane {lane!r}: known lanes are "
            f"{', '.join(('local', *KNOWN_LANES))}"
        )
    redpanda_container, postgres_container = containers

    host = (runtime_host or _DEFAULT_RUNTIME_HOST).strip()
    return ModelLaneTarget(
        lane=normalized,
        runtime_host=host,
        ssh_user=(ssh_user or _SSH_USER),
        redpanda_container=redpanda_container,
        postgres_container=postgres_container,
        postgres_user=(postgres_user or "postgres"),
        postgres_db=(postgres_db or "omnidash_analytics"),
        consumer_group=(consumer_group or "omnidash-read-model-v2"),
    )


__all__ = [
    "KNOWN_LANES",
    "LaneResolutionError",
    "ModelLaneTarget",
    "resolve_lane_target",
]
