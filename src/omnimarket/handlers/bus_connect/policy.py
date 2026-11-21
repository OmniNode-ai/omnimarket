# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the connect policy and a lane's declared second addresses (OMN-26027).

The deadline is declared in ``bus_connect_policy.yaml`` beside this module; a lane declaration
overrides it with ``connect_deadline_seconds``. A lane declares second addresses of its broker as
``broker_fallbacks``: each names a ``source`` (today ``hook_edge_lane``, the lane's address in
``hook_edge_lane.yaml`` beside the declaration, so an address is declared in one place) and the
``advertised_names`` the broker puts in its metadata.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.handlers.bus_connect.errors import LabRunBusError
from omnimarket.models.bus_connect import (
    ModelBrokerFallback,
    ModelBrokerFallbackDeclaration,
    ModelBusConnectPolicy,
)

HOOK_EDGE_LANE_FILE = "hook_edge_lane.yaml"


def _lane_mapping(declared_in: Path | None, lane: str | None) -> dict[str, Any]:
    if declared_in is None or lane is None:
        return {}
    try:
        raw = yaml.safe_load(declared_in.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LabRunBusError(
            f"lane declaration {declared_in} is unreadable: {exc}"
        ) from exc
    lanes = raw.get("lanes") if isinstance(raw, dict) else None
    declared = lanes.get(lane) if isinstance(lanes, dict) else None
    return declared if isinstance(declared, dict) else {}


def load_connect_policy(
    declared_in: Path | None = None, lane: str | None = None
) -> ModelBusConnectPolicy:
    """The packaged policy, with the lane's ``connect_deadline_seconds`` when it declares one."""
    text = (
        resources.files("omnimarket.handlers.bus_connect")
        .joinpath("bus_connect_policy.yaml")
        .read_text(encoding="utf-8")
    )
    values: dict[str, Any] = dict(yaml.safe_load(text))
    override = _lane_mapping(declared_in, lane).get("connect_deadline_seconds")
    if override is not None:
        values["connect_deadline_seconds"] = override
    try:
        return ModelBusConnectPolicy.model_validate(values)
    except ValidationError as exc:
        raise LabRunBusError(f"bus connect policy is invalid: {exc}") from exc


def _hook_edge_address(declared_in: Path, lane: str) -> str:
    path = declared_in.parent / HOOK_EDGE_LANE_FILE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LabRunBusError(
            f"lane {lane!r} declares a broker fallback from {path}, which cannot be read: {exc}"
        ) from exc
    known = raw.get("known_lanes") if isinstance(raw, dict) else None
    entry = known.get(lane) if isinstance(known, dict) else None
    address = entry.get("bootstrap_servers") if isinstance(entry, dict) else None
    if not isinstance(address, str) or not address.strip():
        raise LabRunBusError(
            f"lane {lane!r} declares a broker fallback from {path}, which has no known_lanes.{lane}.bootstrap_servers"
        )
    return address.strip()


def load_broker_fallbacks(
    declared_in: Path | None,
    lane: str | None,
    defaults: tuple[ModelBrokerFallbackDeclaration, ...] = (),
) -> tuple[ModelBrokerFallback, ...]:
    """The second addresses of the lane, in order.

    The lane's own ``broker_fallbacks`` when it declares them; otherwise the policy's
    ``defaults`` (skipped, not an error, when ``hook_edge_lane.yaml`` has no address for the lane).
    None when neither applies.
    """
    if declared_in is None or lane is None:
        return ()
    declared = _lane_mapping(declared_in, lane).get("broker_fallbacks")
    if declared is None:
        return _resolve_defaults(declared_in, lane, defaults)
    if not isinstance(declared, list):
        raise LabRunBusError(f"lane {lane!r} broker_fallbacks must be a list")
    fallbacks: list[ModelBrokerFallback] = []
    for entry in declared:
        if not isinstance(entry, dict) or entry.get("source") != "hook_edge_lane":
            raise LabRunBusError(
                f"lane {lane!r} broker_fallbacks entry {entry!r} must name source: hook_edge_lane"
            )
        names = entry.get("advertised_names") or []
        try:
            fallbacks.append(
                ModelBrokerFallback(
                    address=_hook_edge_address(declared_in, lane),
                    advertised_names=tuple(names),
                )
            )
        except ValidationError as exc:
            raise LabRunBusError(f"lane {lane!r} broker_fallbacks: {exc}") from exc
    return tuple(fallbacks)


def _resolve_defaults(
    declared_in: Path,
    lane: str,
    defaults: tuple[ModelBrokerFallbackDeclaration, ...],
) -> tuple[ModelBrokerFallback, ...]:
    resolved: list[ModelBrokerFallback] = []
    for default in defaults:
        try:
            address = _hook_edge_address(declared_in, lane)
        except LabRunBusError:
            continue
        resolved.append(
            ModelBrokerFallback(
                address=address,
                advertised_names=default.advertised_names,
                advertised_names_from_bootstrap=default.advertised_names_from_bootstrap,
            )
        )
    return tuple(resolved)
