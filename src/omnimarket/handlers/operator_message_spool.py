# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the host-wide arrival outbox without changing it; G1 and ticks share this view."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from omnimarket.models.model_operator_message_spool import (
    ModelOperatorArrivalPolicy,
    ModelOperatorSpoolStatus,
)


def read_operator_spool_status(
    directory: Path, *, now: float, bound_seconds: float
) -> ModelOperatorSpoolStatus:
    ages: list[float] = []
    errors: list[str] = []
    paths: tuple[Path, ...] = ()
    depth: int | None = 0
    try:
        with os.scandir(directory) as entries:
            paths = tuple(
                Path(entry.path)
                for entry in entries
                if entry.name.endswith(".pending.json")
            )
        depth = len(paths)
    except FileNotFoundError:
        pass  # No arrival has ever been spooled on this host.
    except OSError as exc:
        depth = None
        errors.append(f"{type(exc).__name__}: {exc}")
    for path in paths:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            ages.append(max(0.0, now - float(entry["created_at"])))
        except FileNotFoundError:
            # A concurrent successful drain atomically renamed this row.
            if depth is not None:
                depth -= 1
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
    oldest = max(ages) if ages else None
    return ModelOperatorSpoolStatus(
        depth=depth,
        oldest_age_seconds=oldest,
        bound_seconds=bound_seconds,
        state="FINDING"
        if errors or (oldest is not None and oldest > bound_seconds)
        else "OK",
        errors=tuple(errors),
    )


OVERLAY = Path(__file__).parents[1] / "contracts" / "operator_message_arrivals.json"


def load_arrival_policy(environ: Mapping[str, str]) -> ModelOperatorArrivalPolicy:
    return ModelOperatorArrivalPolicy(
        **json.loads(
            Path(environ.get("ONEX_OPERATOR_MESSAGE_OVERLAY") or OVERLAY).read_text(
                encoding="utf-8"
            )
        )
    )


def spool_directory(
    environ: Mapping[str, str], policy: ModelOperatorArrivalPolicy
) -> Path:
    declared = environ.get(policy.spool_root_env)
    if declared:
        return Path(declared)
    root = environ.get(policy.state_root_env) or environ.get(
        policy.fallback_state_root_env
    )
    return (
        Path(root) if root else Path.home() / policy.state_subdir
    ) / policy.spool_subdir
