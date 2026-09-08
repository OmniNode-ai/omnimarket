# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain boundary checks for the V5 OCI safe-config node."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from omnimarket.nodes.node_rsd_v5_oci_safe_config_validate_compute.handlers.handler_rsd_v5_oci_safe_config import (
    validate_rsd_v5_oci_safe_config,
)
from omnimarket.nodes.node_rsd_v5_oci_safe_config_validate_compute.models.model_rsd_v5_oci_safe_config import (
    ModelRsdV5OciSafeConfigInput,
)
from tests.unit.rsd.test_container_bootstrap_oci_safe_config_evidence_v5 import (
    _evidence,
)

_VECTOR = (
    Path(__file__).parents[3]
    / "src/omnimarket/rsd/vectors/container_bootstrap_artifact_evidence_v5_public_vector.yaml"
)


@pytest.mark.unit
def test_node_revalidates_raw_config_and_returns_non_authorizing_commitments() -> None:
    evidence, raw_config = _evidence()
    result = validate_rsd_v5_oci_safe_config(
        ModelRsdV5OciSafeConfigInput(evidence=evidence, raw_config_json=raw_config)
    )
    assert (
        result.schema_version == "rsd.container-bootstrap-oci-safe-config-evidence.v5"
    )
    assert result.non_authorizing is True
    assert result.evidence_effect_allowed is False
    assert result.build_allowed is False
    assert result.materialization_allowed is False
    assert result.attach_allowed is False


@pytest.mark.unit
def test_node_rejects_raw_config_drift() -> None:
    evidence, raw_config = _evidence()
    with pytest.raises(ValueError, match="OCI safe-config evidence validation failed"):
        validate_rsd_v5_oci_safe_config(
            ModelRsdV5OciSafeConfigInput(
                evidence=evidence,
                raw_config_json=raw_config.replace(b"production", b"tampered__"),
            )
        )


@pytest.mark.unit
def test_immutable_public_vector_matches_pinned_source_hash() -> None:
    payload = _VECTOR.read_bytes()
    assert len(payload) == 238_294
    assert payload.count(b"\n") == 2_948
    assert (
        hashlib.sha256(payload).hexdigest()
        == "6c66df411fd080f1d20e2cfe8f8004f600a1cfe1fe5942259b148af15166ca91"
    )
