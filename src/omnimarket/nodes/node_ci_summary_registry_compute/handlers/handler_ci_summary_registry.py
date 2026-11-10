# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_ci_summary_registry_compute (OMN-23505).

Pure COMPUTE. The CI Summary poller's sweep-exclusion registry and per-check
advisory/blocking registry were loaded inside ``scripts/ci/ci_summary_gate.py``, a
baselined file that may only shrink. The loading and validation live here; the gate
reads the resolved result. A bad registry never admits anything: it yields no
entries and a named refusal, and the gate fails closed on a refusal.
"""

from __future__ import annotations

from omnimarket.nodes.node_ci_summary_registry_compute.models.model_ci_summary_registry import (
    ModelCiSummaryExclusion,
    ModelCiSummaryRegistry,
    ModelCiSummaryRegistryRequest,
)

EXCLUSIONS_PREFIX = "sweep-exclusions: "
CHECK_MODES_PREFIX = "check-modes: "


def _resolve_exclusions(
    raw: object, error: str | None
) -> tuple[dict[str, ModelCiSummaryExclusion], list[str]]:
    if error is not None:
        return {}, [f"{EXCLUSIONS_PREFIX}unreadable or malformed: {error}"]
    try:
        if not isinstance(raw, dict):
            raise TypeError("the registry must be a JSON object")
        return {
            str(name): ModelCiSummaryExclusion(**fields) for name, fields in raw.items()
        }, []
    except (TypeError, ValueError) as exc:
        return {}, [f"{EXCLUSIONS_PREFIX}unreadable or malformed: {exc!r}"]


def _resolve_check_modes(
    spec: object, decls: object, error: str | None
) -> tuple[dict[str, list[str]], list[str]]:
    if error is not None:
        return {}, [f"{CHECK_MODES_PREFIX}unreadable or malformed: {error}"]
    try:
        if not isinstance(spec, dict) or not isinstance(decls, dict):
            raise TypeError("the contract and the overlay must be JSON objects")
        modes = {m: frozenset(c) for m, c in spec["modes"].items()}
        need = sorted({"mode", *spec["entry_fields"]})
        bad = {
            n
            for n, e in decls.items()
            if not isinstance(e, dict)
            or not all(isinstance(e.get(f), str) and e[f].strip() for f in need)
            or e["mode"] not in modes
        }
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return {}, [f"{CHECK_MODES_PREFIX}unreadable or malformed: {exc!r}"]
    admitted = {n: sorted(modes[e["mode"]]) for n, e in decls.items() if n not in bad}
    return admitted, [
        f"{CHECK_MODES_PREFIX}{n}: needs non-empty text {', '.join(need)} and a declared mode"
        for n in sorted(bad)
    ]


class HandlerCiSummaryRegistry:
    """Resolve the sweep exclusions and the per-check modes from their parsed files."""

    def handle(self, request: ModelCiSummaryRegistryRequest) -> ModelCiSummaryRegistry:
        exclusions, ex_refusals = _resolve_exclusions(
            request.sweep_exclusions, request.sweep_exclusions_error
        )
        check_modes, mode_refusals = _resolve_check_modes(
            request.check_modes_contract,
            request.check_modes_overlay,
            request.check_modes_error,
        )
        return ModelCiSummaryRegistry(
            exclusions=exclusions,
            check_modes=check_modes,
            refusals=[*ex_refusals, *mode_refusals],
        )
