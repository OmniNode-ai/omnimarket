# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-config-read provenance for the delegation/routing path (OMN-12967).

The config-authority end-state (operator directive, 2026-06-11) is:

* config **keys/structure/refs** are declared in contract overlays (routing
  provability),
* config **values** resolve from Infisical at the runtime/effect boundary,
* on-disk overlay files are a **bootstrap fallback** for standalone installs —
  used only when no higher authority resolves the value, and only with logged
  provenance.

This module is the provenance surface for the delegation path. It does not
re-implement secret resolution (that is :mod:`secret_store_resolver`, OMN-12824)
nor the file-level deployed-vs-packaged drift gate (that is omnibase_infra
``config_provenance``, OMN-12958). It answers a different, complementary
question: for each delegation-path config read, *which authority produced the
value* — a contract-overlay env override, or the packaged bootstrap default —
and emits a single structured provenance line so a cold runtime's resolution
order is auditable from the logs and proof packets.

The directive forbids silent defaults that mask cross-machine breakage
(CLAUDE.md rule 8). A bootstrap fallback is allowed *only when logged*; an
unlogged fallback is exactly the drift this module exists to surface.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class EnumDelegationConfigSource(StrEnum):
    """Authority that produced a resolved delegation-path config value.

    Ordered from highest to lowest authority. ``BOOTSTRAP_DEFAULT`` is the
    packaged on-disk fallback that is only legitimate for standalone installs
    and must always be logged.
    """

    CONTRACT_OVERLAY_ENV = "contract_overlay_env"
    BOOTSTRAP_DEFAULT = "bootstrap_default"


class ModelDelegationConfigProvenance(BaseModel):
    """Provenance record for a single delegation-path config read.

    Carries the env-var key consulted, the source that produced the value, and
    the resolved on-disk path. Never carries a secret value — path config and
    secret refs only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_key: str = Field(description="The env-var key consulted for an override.")
    source: EnumDelegationConfigSource = Field(
        description="Authority that produced the resolved value."
    )
    resolved_path: Path = Field(description="The resolved on-disk path.")
    override_present: bool = Field(
        description="Whether a non-empty env override was present for config_key."
    )

    def log_line(self) -> str:
        """Return the single-line provenance summary for startup logs."""
        return (
            "config_provenance surface=delegation "
            f"config_key={self.config_key} "
            f"source={self.source.value} "
            f"override_present={str(self.override_present).lower()} "
            f"resolved_path={self.resolved_path}"
        )


def resolve_path_config(
    config_key: str,
    bootstrap_default: Path,
) -> tuple[Path, ModelDelegationConfigProvenance]:
    """Resolve a delegation-path config *path* with logged provenance.

    The contract/overlay authority supplies the path via ``config_key`` (an
    env-driven render input declared in the contract overlay). When the override
    is absent or empty, the packaged ``bootstrap_default`` is used — the
    standalone-install fallback — and the fallback is logged.

    This resolves a config *path*, never a secret value. Secret values resolve
    through :mod:`secret_store_resolver` at the effect boundary.

    Args:
        config_key: The env-var key the contract overlay binds to this path.
        bootstrap_default: The packaged on-disk path used when no override is
            present.

    Returns:
        A tuple of the resolved :class:`Path` and the
        :class:`ModelDelegationConfigProvenance` record describing how it was
        resolved. The provenance is emitted to the logger at INFO before return.
    """
    raw_override = _read_override(config_key)
    if raw_override:
        resolved = Path(raw_override)
        source = EnumDelegationConfigSource.CONTRACT_OVERLAY_ENV
        override_present = True
    else:
        resolved = bootstrap_default
        source = EnumDelegationConfigSource.BOOTSTRAP_DEFAULT
        override_present = False

    provenance = ModelDelegationConfigProvenance(
        config_key=config_key,
        source=source,
        resolved_path=resolved,
        override_present=override_present,
    )
    logger.info(provenance.log_line())
    return resolved, provenance


# Sentinel resolved-path recorded when a loader supplies its own packaged default
# (the override is absent and this surface does not compute the default path).
LOADER_PACKAGED_DEFAULT = Path("<loader-packaged-default>")


def resolve_optional_path_config(
    config_key: str,
) -> tuple[Path | None, ModelDelegationConfigProvenance]:
    """Resolve a delegation-path config *path* whose loader owns the default.

    Identical authority semantics to :func:`resolve_path_config`, but for reads
    where the downstream loader (e.g. the bifrost delegation config loader)
    applies its own packaged default when no override is supplied. Returns
    ``None`` for the absent case — the caller's None-passthrough contract — while
    still emitting a bootstrap-default provenance line so a cold runtime's
    resolution order is auditable.

    Args:
        config_key: The env-var key the contract overlay binds to this path.

    Returns:
        A tuple of the resolved :class:`Path` (or ``None`` when the loader's own
        packaged default applies) and the provenance record. The provenance is
        emitted to the logger at INFO before return.
    """
    raw_override = _read_override(config_key)
    if raw_override:
        resolved: Path | None = Path(raw_override)
        source = EnumDelegationConfigSource.CONTRACT_OVERLAY_ENV
        override_present = True
        recorded_path = resolved
    else:
        resolved = None
        source = EnumDelegationConfigSource.BOOTSTRAP_DEFAULT
        override_present = False
        recorded_path = LOADER_PACKAGED_DEFAULT

    provenance = ModelDelegationConfigProvenance(
        config_key=config_key,
        source=source,
        resolved_path=recorded_path,
        override_present=override_present,
    )
    logger.info(provenance.log_line())
    return resolved, provenance


def _read_override(config_key: str) -> str:
    """Read a contract-overlay-bound env override, stripped, empty when absent.

    This is the single env-read surface for delegation-path *path* config. The
    delegation-env scanner's provenance ratchet requires the canonical
    delegation-path config keys to flow through this module rather than raw
    ``os.environ`` reads scattered across the routing path.
    """
    return os.environ.get(config_key, "").strip()  # ONEX_EXCLUDE: provenance surface


# Canonical delegation/routing-path config *path* keys re-homed onto the
# provenance resolver (OMN-12967). The delegation-env scanner enforces that raw
# reads of these keys outside this module are violations even with a skip token,
# closing the silent-volume-selection hole proven on 2026-06-11.
DELEGATION_PATH_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "BIFROST_CONTRACT_PATH",
        "BIFROST_OVERLAY_PATH",
        "DELEGATION_ROUTING_TIERS_PATH",
        "TASK_CLASS_CONTRACT_PATH",
        "INFERENCE_PROTOCOL_CONFIG_PATH",
    }
)


__all__: list[str] = [
    "DELEGATION_PATH_CONFIG_KEYS",
    "LOADER_PACKAGED_DEFAULT",
    "EnumDelegationConfigSource",
    "ModelDelegationConfigProvenance",
    "resolve_optional_path_config",
    "resolve_path_config",
]
