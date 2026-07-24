# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelThresholdConfigLoader — resolves the real A6 thresholds (OMN-14948, B10->B11).

Reads ``thresholds.yaml`` from this node's package root and resolves it to
typed, fully-resolved :class:`ModelThresholdSpec` instances -- one per
canary-monitoring signal domain (auth/TLS/broker/lag/RDS).

This is the actual wiring step named in OMN-14732 ("Jonah's agents wire =
B10") and tracked to completion in OMN-14948: before this module existed,
every threshold spec built from real inputs was unresolved by construction
(``ModelThresholdSpec.is_resolved is False``) and the gate could only ever
report ``BLOCKED_PENDING_A6``. ``default_threshold_specs()`` now returns
real, resolved specs citing the delivered A6 artifact, so the same pure
evaluation logic in ``handler_canary_monitoring_gate.py`` starts producing
real ``PASS``/``WARN``/``ABORT`` verdicts with no change to that logic.

Follows the same local-static-config-file loading pattern as
``omnimarket.nodes.node_build_loop_orchestrator.handlers.model_policy_loader.ModelPolicyLoader``:
reads a package-relative YAML file, fails fast (no silent fallback) if it is
missing or malformed. No network/AWS/bus I/O.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_signal_reading import (
    SignalName,
)
from omnimarket.nodes.node_canary_monitoring_gate_compute.models.model_threshold_spec import (
    ModelThresholdSpec,
)

_THRESHOLDS_FILE = Path(__file__).parents[1] / "thresholds.yaml"

# The signal domains OMN-14732 (A6) delivered numeric thresholds for. If a
# future A6 revision adds/removes a domain, this set (and the config file)
# must be updated together -- a contract change, not a threshold-value
# change.
_EXPECTED_SIGNALS: frozenset[SignalName] = frozenset(
    {"auth", "tls", "broker", "lag", "rds"}
)


@lru_cache(maxsize=1)
def _load_config_file() -> dict[str, Any]:
    if not _THRESHOLDS_FILE.exists():
        raise FileNotFoundError(
            f"thresholds.yaml not found at {_THRESHOLDS_FILE}. Create it at "
            "src/omnimarket/nodes/node_canary_monitoring_gate_compute/thresholds.yaml "
            "citing the A6 artifact (OMN-14732)."
        )
    data: dict[str, Any] = yaml.safe_load(_THRESHOLDS_FILE.read_text())
    return data


def default_threshold_specs() -> tuple[ModelThresholdSpec, ...]:
    """Return the real, resolved A6 threshold specs for all five signal domains.

    Raises:
        FileNotFoundError: ``thresholds.yaml`` is missing.
        KeyError: a signal declared in :data:`_EXPECTED_SIGNALS` has no
            entry in the config file (fail fast -- never silently skip a
            gated signal).
        ValidationError: a config entry fails :class:`ModelThresholdSpec`
            validation (e.g. a half-resolved threshold).
    """
    config = _load_config_file()
    source = config["source"]
    signals: dict[str, Any] = config["signals"]

    specs: list[ModelThresholdSpec] = []
    for signal_name in sorted(_EXPECTED_SIGNALS):
        if signal_name not in signals:
            raise KeyError(
                f"thresholds.yaml is missing a config entry for signal "
                f"{signal_name!r}. Every domain in _EXPECTED_SIGNALS must "
                "be declared."
            )
        entry = signals[signal_name]
        specs.append(
            ModelThresholdSpec(
                signal_name=signal_name,
                comparison=entry["comparison"],
                warn_threshold=float(entry["warn_threshold"]),
                abort_threshold=float(entry["abort_threshold"]),
                source=source,
            )
        )
    return tuple(specs)


__all__ = ["default_threshold_specs"]
