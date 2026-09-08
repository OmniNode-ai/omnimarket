"""Node-boundary proof for the inert V4 artifact-evidence verifier."""

import base64
import hashlib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_rsd_v4_artifact_evidence_validate_compute.handlers.handler_v4_artifact_evidence import (
    validate_v4_artifact_evidence,
)
from omnimarket.nodes.node_rsd_v4_artifact_evidence_validate_compute.models.model_v4_artifact_evidence import (
    ModelV4ArtifactEvidenceValidationInput,
)
from omnimarket.rsd.v4_artifact_evidence import (
    ContainerBootstrapArtifactEvidenceV4Error,
)

_VECTOR = (
    Path(__file__).parents[2]
    / "fixtures/rsd/container_bootstrap_artifact_evidence_v4_vectors.yaml"
)


def _vector_bytes(name: str) -> bytes:
    vector = yaml.safe_load(_VECTOR.read_text(encoding="ascii"))
    encoded = "".join(vector[name]["segments"])
    return base64.b64decode(encoded, validate=True)


def _request(*, closure: bytes | None = None) -> ModelV4ArtifactEvidenceValidationInput:
    return ModelV4ArtifactEvidenceValidationInput(
        closure_canonical_json=closure
        or _vector_bytes("closure_canonical_json_utf8_base64"),
        worker_trust_policy_canonical_json=_vector_bytes(
            "worker_trust_policy_canonical_json_utf8_base64"
        ),
        profile_envelope_canonical_json=_vector_bytes(
            "profile_envelope_canonical_json_utf8_base64"
        ),
        profile_trust_anchor_canonical_json=_vector_bytes(
            "profile_root_canonical_json_utf8_base64"
        ),
    )


@pytest.mark.unit
def test_node_accepts_the_immutable_fcd5553_vector_without_authority() -> None:
    assert hashlib.sha256(_VECTOR.read_bytes()).hexdigest() == (
        "248bdc9d3aedb18f7b84feb93e28faaa6d6082602d1f0a03e4d4095da75b1a68"
    )
    result = validate_v4_artifact_evidence(_request())
    assert (
        result.closure_sha256
        == "a4d4b6f2a9ec85a9b80d4c7c899bcfc261448bdfb5de965412da9d6ea5e144b3"
    )
    assert result.non_authorizing is True
    assert result.evidence_effect_allowed is False
    assert result.build_allowed is False
    assert result.materialization_allowed is False
    assert result.attach_allowed is False


@pytest.mark.unit
def test_node_refuses_tampered_closure_before_any_effect() -> None:
    closure = bytearray(_vector_bytes("closure_canonical_json_utf8_base64"))
    closure[-2] ^= 1
    with pytest.raises(ContainerBootstrapArtifactEvidenceV4Error):
        validate_v4_artifact_evidence(_request(closure=bytes(closure)))
