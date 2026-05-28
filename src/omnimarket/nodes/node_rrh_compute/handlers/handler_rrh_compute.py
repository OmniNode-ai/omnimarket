# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRrhCompute — Release Readiness Handshake validation.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from omnimarket.nodes.node_rrh_compute.models.model_rrh_compute_request import (
    ModelRrhComputeRequest,
)
from omnimarket.nodes.node_rrh_compute.models.model_rrh_compute_result import (
    ModelRrhCheckResult,
    ModelRrhComputeResult,
)

_SEMVER_OR_RELEASE_BRANCH = re.compile(
    r"^(v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?|release/[0-9A-Za-z._-]+)$"
)

_REGISTERED_CHECKS: dict[str, Callable[[ModelRrhComputeRequest], ModelRrhCheckResult]]


class HandlerRrhCompute:
    """Deterministic release readiness validation over declared request fields."""

    def handle(self, request: ModelRrhComputeRequest) -> ModelRrhComputeResult:
        selected_checks = request.checks or list(_REGISTERED_CHECKS)
        unknown_checks = [
            check_name
            for check_name in selected_checks
            if check_name not in _REGISTERED_CHECKS
        ]

        if unknown_checks:
            results = [
                ModelRrhCheckResult(
                    name=check_name,
                    passed=False,
                    detail="unknown readiness check",
                )
                for check_name in unknown_checks
            ]
            return ModelRrhComputeResult(
                status="error",
                ready=False,
                results=results,
                blocking_checks=unknown_checks,
                error="unknown readiness checks: " + ", ".join(unknown_checks),
            )

        results = [
            _REGISTERED_CHECKS[check_name](request) for check_name in selected_checks
        ]
        blocking_checks = [result.name for result in results if not result.passed]

        return ModelRrhComputeResult(
            status="ok",
            ready=not blocking_checks,
            results=results,
            blocking_checks=blocking_checks,
        )


def _check_release_id_present(
    request: ModelRrhComputeRequest,
) -> ModelRrhCheckResult:
    release_id = request.release_id.strip()
    return ModelRrhCheckResult(
        name="release_id_present",
        passed=bool(release_id),
        detail="" if release_id else "release_id must be non-empty",
    )


def _check_release_id_format(request: ModelRrhComputeRequest) -> ModelRrhCheckResult:
    release_id = request.release_id.strip()
    passed = bool(_SEMVER_OR_RELEASE_BRANCH.fullmatch(release_id))
    return ModelRrhCheckResult(
        name="release_id_format",
        passed=passed,
        detail=("" if passed else "release_id must be semver-like or release/<name>"),
    )


_REGISTERED_CHECKS = {
    "release_id_present": _check_release_id_present,
    "release_id_format": _check_release_id_format,
}
