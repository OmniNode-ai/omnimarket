# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pytest failure digest compute node.

Pure COMPUTE: one focused pytest run's junit XML and exit code in, one bounded
failure digest out (outcome, first failing node, exception type, capped
message and frames, and a fingerprint that ignores timing). Used by the
delegated test loop to feed a failure back to a local model and to stop when
the same failure repeats.
"""

from omnimarket.nodes.node_pytest_failure_digest_compute.handlers.handler_pytest_failure_digest import (
    HandlerPytestFailureDigest,
    digest_pytest_run,
)
from omnimarket.nodes.node_pytest_failure_digest_compute.models.model_pytest_failure_digest import (
    EnumPytestRunOutcome,
    ModelPytestFailureDigest,
    ModelPytestRunReport,
)


class NodePytestFailureDigestCompute(HandlerPytestFailureDigest):
    """ONEX entry-point wrapper for HandlerPytestFailureDigest."""


__all__ = [
    "EnumPytestRunOutcome",
    "HandlerPytestFailureDigest",
    "ModelPytestFailureDigest",
    "ModelPytestRunReport",
    "NodePytestFailureDigestCompute",
    "digest_pytest_run",
]
