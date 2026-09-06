"""Focused adversarial coverage for the Phase-A V5 worker-evidence closure."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omnimarket.nodes.node_rsd_v5_artifact_evidence_validate_compute.handlers.handler_rsd_v5_artifact_evidence import (
    validate_rsd_v5_artifact_evidence,
)
from omnimarket.nodes.node_rsd_v5_artifact_evidence_validate_compute.models.model_rsd_v5_artifact_evidence import (
    ModelRsdV5ArtifactEvidenceInput,
)
from omnimarket.rsd import container_bootstrap_artifact_evidence_v5 as evidence
from omnimarket.rsd import (
    container_bootstrap_oci_safe_config_evidence_v5 as oci_evidence,
)
from omnimarket.rsd import oci_config_commitment
from omnimarket.rsd import v4_static_profile as static_v4

_STATIC_VECTOR = (
    Path(__file__).parents[2] / "fixtures/rsd/container_attach_static_v4_vectors.yaml"
)
_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
_LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _decoded(value: object) -> bytes:
    mapped = value
    return base64.b64decode("".join(mapped["segments"]), validate=True)


def _profile() -> tuple[object, object]:
    values = yaml.safe_load(_STATIC_VECTOR.read_text(encoding="ascii"))
    return (
        static_v4.parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json(
            _decoded(values["profile_envelope_canonical_json_utf8_base64"])
        ),
        static_v4.parse_container_bootstrap_static_profile_trust_anchor_v4_canonical_json(
            _decoded(values["profile_root_canonical_json_utf8_base64"])
        ),
    )


def _shared_oci(profile: object) -> object:
    launch = profile.static_role_profile.static_launch_plan
    raw_config = _canonical(
        {
            "architecture": "amd64",
            "config": {
                "Cmd": list(launch.base_command),
                "Entrypoint": list(launch.wrapper_argv_prefix + launch.base_entrypoint),
                "Env": ["APP_MODE=production", "PATH=/usr/bin:/bin"],
                "User": "12345:23456",
                "WorkingDir": "/opt/rsd",
            },
            "os": "linux",
            "rootfs": {
                "diff_ids": ["sha256:" + "b" * 64, "sha256:" + "d" * 64],
                "type": "layers",
            },
        }
    )
    claim = oci_config_commitment.derive_phase_a_v5_expanded_oci_config_claim_v1(
        raw_config,
        _CONFIG_MEDIA_TYPE,
        "sha256:" + _digest(raw_config),
        len(raw_config),
    )
    layers = (
        oci_evidence.ContainerBootstrapOciSafeConfigLayerDescriptorV5(
            schema_version="rsd.container-bootstrap-oci-safe-config-layer-descriptor.v5",
            media_type=_LAYER_MEDIA_TYPE,
            digest_sha256="a" * 64,
            byte_count=11,
            diff_id_sha256="b" * 64,
        ),
        oci_evidence.ContainerBootstrapOciSafeConfigLayerDescriptorV5(
            schema_version="rsd.container-bootstrap-oci-safe-config-layer-descriptor.v5",
            media_type=_LAYER_MEDIA_TYPE,
            digest_sha256="c" * 64,
            byte_count=12,
            diff_id_sha256="d" * 64,
        ),
    )
    manifest = _canonical(
        {
            "config": {
                "digest": claim.oci_config_descriptor_digest,
                "mediaType": claim.oci_config_descriptor_media_type,
                "size": claim.oci_config_descriptor_size,
            },
            "layers": [
                {
                    "digest": "sha256:" + layer.digest_sha256,
                    "mediaType": layer.media_type,
                    "size": layer.byte_count,
                }
                for layer in layers
            ],
            "mediaType": _MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
        }
    )
    manifest_digest = _digest(manifest)
    index = _canonical(
        {
            "manifests": [
                {
                    "digest": "sha256:" + manifest_digest,
                    "mediaType": _MANIFEST_MEDIA_TYPE,
                    "platform": {"architecture": "amd64", "os": "linux"},
                    "size": len(manifest),
                }
            ],
            "mediaType": _INDEX_MEDIA_TYPE,
            "schemaVersion": 2,
        }
    )
    wrapper = oci_evidence.ContainerBootstrapOciSafeConfigWrapperEntryV5(
        schema_version="rsd.container-bootstrap-oci-safe-config-wrapper-entry.v5",
        path=profile.static_role_profile.wrapper_executable_path,
        uid=0,
        gid=0,
        mode="0555",
        entry_type="regular",
        link_count=1,
        symlink=False,
        hardlink=False,
        setuid=False,
        setgid=False,
        sticky=False,
        content_sha256="e" * 64,
        byte_count=42,
    )
    archive = oci_evidence.ContainerBootstrapOciSafeConfigWrapperArchiveInspectionV5(
        schema_version="rsd.container-bootstrap-oci-safe-config-wrapper-archive-inspection.v5",
        archive_entry_count=1,
        inspected_layer_digest_sha256="c" * 64,
        wrapper_path=wrapper.path,
        wrapper_matching_entry_count=1,
        duplicate_names_absent=True,
        pax_headers_absent=True,
        gnu_longname_or_link_absent=True,
        traversal_paths_absent=True,
        absolute_paths_absent=True,
        whiteout_entries_absent=True,
        privilege_bits_absent=True,
        symlink_entries_absent=True,
        hardlink_entries_absent=True,
        device_fifo_socket_entries_absent=True,
        sparse_entries_absent=True,
        nonregular_entries_absent=True,
        trailing_conflicting_wrapper_entries_absent=True,
    )
    repository = "registry.example.invalid/omninode/rsd"
    return oci_evidence.ContainerBootstrapOciSafeConfigEvidenceV5(
        schema_version="rsd.container-bootstrap-oci-safe-config-evidence.v5",
        derived_repository=repository,
        derived_reference=repository + "@sha256:" + manifest_digest,
        index_digest_sha256=_digest(index),
        index_canonical_json_sha256=_digest(index),
        index_canonical_json_byte_count=len(index),
        index_canonical_json_utf8_base64=base64.b64encode(index).decode("ascii"),
        selected_manifest_descriptor_digest_sha256=manifest_digest,
        linux_amd64_manifest_digest_sha256=manifest_digest,
        manifest_canonical_json_sha256=manifest_digest,
        manifest_canonical_json_byte_count=len(manifest),
        manifest_canonical_json_utf8_base64=base64.b64encode(manifest).decode("ascii"),
        platform_os="linux",
        platform_architecture="amd64",
        ordered_layers=layers,
        config_rootfs_diff_ids_sha256=("b" * 64, "d" * 64),
        wrapper_layer_digest_sha256="c" * 64,
        wrapper_layer_ordinal=1,
        wrapper_tar_entry=wrapper,
        wrapper_archive_inspection=archive,
        entrypoint=launch.wrapper_argv_prefix + launch.base_entrypoint,
        cmd=launch.base_command,
        expanded_oci_config_claim=claim,
        non_authorizing=True,
        evidence_effect_allowed=False,
        build_allowed=False,
        materialization_allowed=False,
        attach_allowed=False,
    )


def _anchor(
    signing_key: Ed25519PrivateKey, name: str
) -> evidence.ContainerBootstrapBuildWorkerTrustAnchorV5:
    public_key = signing_key.public_key().public_bytes_raw()
    return evidence.ContainerBootstrapBuildWorkerTrustAnchorV5(
        schema_version="rsd.container-bootstrap-build-worker-trust-anchor.v5",
        key_id=f"worker-{name}",
        worker_identity_sha256=_hash(f"worker-{name}"),
        authority_identity_sha256=_hash(f"authority-{name}"),
        physical_builder_identity_sha256=_hash(f"physical-builder-{name}"),
        public_key_base64=base64.b64encode(public_key).decode("ascii"),
        public_key_fingerprint_sha256=hashlib.sha256(public_key).hexdigest(),
        algorithm="ed25519",
    )


def _policy(
    anchors: tuple[
        evidence.ContainerBootstrapBuildWorkerTrustAnchorV5,
        evidence.ContainerBootstrapBuildWorkerTrustAnchorV5,
    ],
) -> evidence.ContainerBootstrapBuildWorkerTrustPolicyV5:
    return evidence.ContainerBootstrapBuildWorkerTrustPolicyV5(
        schema_version="rsd.container-bootstrap-build-worker-trust-policy.v5",
        policy_id="phase-a-v5-worker-policy",
        epoch=1,
        independence_domain_sha256=_hash("phase-a-v5-independent-authorities"),
        scope="phase_a_v5_worker_evidence_closure",
        worker_trust_anchors=anchors,
    )


def _attestation(
    *,
    signing_key: Ed25519PrivateKey,
    anchor: evidence.ContainerBootstrapBuildWorkerTrustAnchorV5,
    policy: evidence.ContainerBootstrapBuildWorkerTrustPolicyV5,
    profile_envelope: object,
    shared_oci: object,
    run_id: str,
) -> evidence.ContainerBootstrapArtifactWorkerAttestationV5:
    profile = profile_envelope.static_role_profile
    unsigned = evidence.ContainerBootstrapArtifactWorkerAttestationV5(
        schema_version="rsd.container-bootstrap-artifact-worker-attestation.v5",
        policy_id=policy.policy_id,
        policy_epoch=policy.epoch,
        worker_trust_policy_sha256=(
            evidence.container_bootstrap_build_worker_trust_policy_v5_sha256(policy)
        ),
        independence_domain_sha256=policy.independence_domain_sha256,
        signer_key_id=anchor.key_id,
        worker_identity_sha256=anchor.worker_identity_sha256,
        authority_identity_sha256=anchor.authority_identity_sha256,
        physical_builder_identity_sha256=anchor.physical_builder_identity_sha256,
        run_id=run_id,
        canonical_repository_identity_sha256=_hash("source-repository"),
        git_object_format="sha1",
        commit_oid="a" * 40,
        tree_oid="b" * 40,
        canonical_source_snapshot_sha256=profile.static_patch_preimage.wrapper_source_tree_sha256,
        wrapper_subtree_path="wrapper",
        wrapper_tree_entries=(
            evidence.ContainerBootstrapWrapperTreeEntryV5(
                schema_version="rsd.container-bootstrap-wrapper-tree-entry.v5",
                path="main.py",
                object_sha256=_hash("wrapper-entry"),
                mode="0755",
                entry_type="regular",
            ),
        ),
        source_clean=True,
        untracked_files_absent=True,
        submodules_absent=True,
        recipe_sha256=_hash("recipe"),
        toolchain_sha256=_hash("toolchain"),
        lock_sha256=_hash("lock"),
        vendor_sha256=_hash("vendor"),
        builder_recipe_identity_sha256=_hash("builder-recipe"),
        component=profile.component,
        component_role=profile.component_role,
        static_role_profile_sha256=profile.profile_sha256,
        profile_envelope_sha256=(
            static_v4.container_bootstrap_static_role_profile_envelope_v4_sha256(
                profile_envelope
            )
        ),
        static_delivery_projection_sha256=profile.static_delivery_projection_sha256,
        selected_delivery_route_sha256=profile.selected_delivery_route_sha256,
        static_launch_plan_sha256=profile.static_launch_plan_sha256,
        static_patch_preimage_sha256=profile.static_patch_preimage_sha256,
        static_patch_policy_sha256=profile.static_patch_policy_sha256,
        wrapper_artifact_sha256=shared_oci.wrapper_tar_entry.content_sha256,
        wrapper_artifact_byte_count=shared_oci.wrapper_tar_entry.byte_count,
        wrapper_executable_path=profile.wrapper_executable_path,
        wrapper_uid=0,
        wrapper_gid=0,
        wrapper_mode="0555",
        wrapper_regular_file=True,
        wrapper_link_count=1,
        wrapper_symlink=False,
        wrapper_hardlink=False,
        wrapper_setuid=False,
        wrapper_setgid=False,
        wrapper_sticky=False,
        base_image_policy_sha256=profile.static_launch_plan.base_image_policy_sha256,
        base_resolution_attestation_sha256=(
            profile.static_launch_plan.base_resolution_attestation_sha256
        ),
        base_registry_index_digest_sha256=(
            profile.static_launch_plan.base_registry_index_digest_sha256
        ),
        base_linux_amd64_manifest_digest_sha256=(
            profile.static_launch_plan.base_linux_amd64_manifest_digest_sha256
        ),
        base_config_digest_sha256=profile.static_launch_plan.base_config_digest_sha256,
        oci_safe_config_evidence_sha256=(
            evidence.container_bootstrap_oci_safe_config_evidence_v5_sha256(shared_oci)
        ),
        raw_config_internal_consistency_attested_by_worker=True,
        archive_and_layer_inspection_attested_by_worker=True,
        non_authorizing=True,
        evidence_effect_allowed=False,
        build_allowed=False,
        materialization_allowed=False,
        attach_allowed=False,
        signature_base64=base64.b64encode(bytes(64)).decode("ascii"),
    )
    return unsigned.model_copy(
        update={
            "signature_base64": base64.b64encode(
                signing_key.sign(
                    evidence.container_bootstrap_artifact_worker_attestation_v5_message(
                        unsigned
                    )
                )
            ).decode("ascii")
        }
    )


def _closure() -> tuple[
    object, object, object, object, tuple[Ed25519PrivateKey, Ed25519PrivateKey]
]:
    profile_envelope, profile_anchor = _profile()
    shared_oci = _shared_oci(profile_envelope)
    keys = (Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate())
    anchors = (_anchor(keys[0], "one"), _anchor(keys[1], "two"))
    policy = _policy(anchors)
    closure = evidence.ContainerBootstrapArtifactEvidenceClosureV5(
        schema_version="rsd.container-bootstrap-artifact-evidence-closure.v5",
        worker_attestations=(
            _attestation(
                signing_key=keys[0],
                anchor=anchors[0],
                policy=policy,
                profile_envelope=profile_envelope,
                shared_oci=shared_oci,
                run_id="worker-run-one",
            ),
            _attestation(
                signing_key=keys[1],
                anchor=anchors[1],
                policy=policy,
                profile_envelope=profile_envelope,
                shared_oci=shared_oci,
                run_id="worker-run-two",
            ),
        ),
        oci_safe_config_evidence=shared_oci,
        oci_safe_config_evidence_sha256=(
            evidence.container_bootstrap_oci_safe_config_evidence_v5_sha256(shared_oci)
        ),
        non_authorizing=True,
        evidence_effect_allowed=False,
        build_allowed=False,
        materialization_allowed=False,
        attach_allowed=False,
    )
    return closure, policy, profile_envelope, profile_anchor, keys


def _validate(
    closure: object, policy: object, profile_envelope: object, profile_anchor: object
) -> object:
    return evidence.validate_container_bootstrap_artifact_evidence_closure_v5(
        closure=closure,
        worker_trust_policy=policy,
        profile_envelope=profile_envelope,
        profile_trust_anchor=profile_anchor,
    )


def _resigned_copy(
    attestation: evidence.ContainerBootstrapArtifactWorkerAttestationV5,
    signing_key: Ed25519PrivateKey,
    **update: object,
) -> evidence.ContainerBootstrapArtifactWorkerAttestationV5:
    changed = attestation.model_copy(update=update)
    return changed.model_copy(
        update={
            "signature_base64": base64.b64encode(
                signing_key.sign(
                    evidence.container_bootstrap_artifact_worker_attestation_v5_message(
                        changed
                    )
                )
            ).decode("ascii")
        }
    )


def test_v5_worker_closure_is_non_authorizing_and_records_attested_only_checks() -> (
    None
):
    closure, policy, profile_envelope, profile_anchor, _ = _closure()

    accepted = _validate(closure, policy, profile_envelope, profile_anchor)

    assert accepted.raw_config_internal_consistency_attested_by_both_workers is True
    assert accepted.archive_and_layer_inspection_attested_by_both_workers is True
    assert accepted.raw_config_internal_consistency_independently_reverified is False
    assert accepted.archive_and_layer_inspection_independently_reverified is False
    assert accepted.non_authorizing is True
    assert accepted.evidence_effect_allowed is False
    assert accepted.build_allowed is False
    assert accepted.materialization_allowed is False
    assert accepted.attach_allowed is False


def test_v5_worker_node_returns_only_non_authorizing_acceptance() -> None:
    closure, policy, profile_envelope, profile_anchor, _ = _closure()

    output = validate_rsd_v5_artifact_evidence(
        ModelRsdV5ArtifactEvidenceInput(
            closure=closure,
            worker_trust_policy=policy,
            profile_envelope=profile_envelope,
            profile_trust_anchor=profile_anchor,
        )
    )

    assert output.acceptance.closure_sha256 == output.closure_sha256
    assert output.acceptance.verification_context_sha256 == (
        output.verification_context_sha256
    )
    assert output.non_authorizing
    assert not output.evidence_effect_allowed
    assert not output.build_allowed
    assert not output.materialization_allowed
    assert not output.attach_allowed


@pytest.mark.parametrize(
    "update",
    [
        {"policy_id": "same-keys-policy-substitution"},
        {"epoch": 2},
        {"independence_domain_sha256": "a" * 64},
    ],
)
def test_v5_worker_closure_rejects_external_policy_substitution_with_same_keys(
    update: dict[str, object],
) -> None:
    closure, policy, profile_envelope, profile_anchor, _ = _closure()
    substituted = policy.model_copy(update=update)

    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        _validate(closure, substituted, profile_envelope, profile_anchor)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("policy_epoch", 2),
        ("worker_trust_policy_sha256", "f" * 64),
        ("independence_domain_sha256", "e" * 64),
        ("selected_delivery_route_sha256", "d" * 64),
        ("base_config_digest_sha256", "c" * 64),
        ("oci_safe_config_evidence_sha256", "b" * 64),
    ],
)
def test_v5_worker_closure_rejects_signed_context_substitution(
    field: str, replacement: object
) -> None:
    closure, policy, profile_envelope, profile_anchor, keys = _closure()
    first, second = closure.worker_attestations
    changed_first = _attestation(
        signing_key=keys[0],
        anchor=policy.worker_trust_anchors[0],
        policy=policy,
        profile_envelope=profile_envelope,
        shared_oci=closure.oci_safe_config_evidence,
        run_id=first.run_id,
    ).model_copy(update={field: replacement})
    changed_first = changed_first.model_copy(
        update={
            "signature_base64": base64.b64encode(
                keys[0].sign(
                    evidence.container_bootstrap_artifact_worker_attestation_v5_message(
                        changed_first
                    )
                )
            ).decode("ascii")
        }
    )
    changed_second = second
    changed = closure.model_copy(
        update={"worker_attestations": (changed_first, changed_second)}
    )

    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        _validate(changed, policy, profile_envelope, profile_anchor)


@pytest.mark.parametrize(
    ("field", "replacement", "accepted"),
    [
        ("canonical_repository_identity_sha256", "1" * 64, True),
        ("commit_oid", "c" * 40, True),
        ("tree_oid", "d" * 40, True),
        ("canonical_source_snapshot_sha256", "e" * 64, False),
        ("wrapper_subtree_path", "other-wrapper", True),
        ("recipe_sha256", "2" * 64, True),
        ("toolchain_sha256", "3" * 64, True),
        ("lock_sha256", "4" * 64, True),
        ("vendor_sha256", "5" * 64, True),
        ("builder_recipe_identity_sha256", "6" * 64, True),
        ("component", "restore_valkey", False),
        ("static_role_profile_sha256", "7" * 64, False),
        ("profile_envelope_sha256", "8" * 64, False),
        ("static_delivery_projection_sha256", "9" * 64, False),
        ("selected_delivery_route_sha256", "a" * 64, False),
        ("static_launch_plan_sha256", "b" * 64, False),
        ("static_patch_preimage_sha256", "c" * 64, False),
        ("static_patch_policy_sha256", "d" * 64, False),
        ("wrapper_artifact_sha256", "f" * 64, False),
        ("wrapper_artifact_byte_count", 43, False),
        ("wrapper_executable_path", "/usr/local/libexec/other-wrapper", False),
        ("base_image_policy_sha256", "0" * 64, False),
        ("base_resolution_attestation_sha256", "1" * 64, False),
        ("base_registry_index_digest_sha256", "2" * 64, False),
        ("base_linux_amd64_manifest_digest_sha256", "3" * 64, False),
        ("base_config_digest_sha256", "4" * 64, False),
        ("oci_safe_config_evidence_sha256", "5" * 64, False),
    ],
)
def test_v5_worker_closure_signed_subject_fields_are_exactly_bound(
    field: str, replacement: object, accepted: bool
) -> None:
    closure, policy, profile_envelope, profile_anchor, keys = _closure()
    first, second = closure.worker_attestations
    changed = closure.model_copy(
        update={
            "worker_attestations": (
                _resigned_copy(first, keys[0], **{field: replacement}),
                _resigned_copy(second, keys[1], **{field: replacement}),
            )
        }
    )

    if accepted:
        assert (
            _validate(changed, policy, profile_envelope, profile_anchor).non_authorizing
            is True
        )
    else:
        with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
            _validate(changed, policy, profile_envelope, profile_anchor)


def test_v5_worker_closure_signed_tree_and_cross_worker_exception_rules() -> None:
    closure, policy, profile_envelope, profile_anchor, keys = _closure()
    first, second = closure.worker_attestations
    entry = evidence.ContainerBootstrapWrapperTreeEntryV5(
        schema_version="rsd.container-bootstrap-wrapper-tree-entry.v5",
        path="main.py",
        object_sha256="a" * 64,
        mode="0755",
        entry_type="regular",
    )
    both_changed = closure.model_copy(
        update={
            "worker_attestations": (
                _resigned_copy(first, keys[0], wrapper_tree_entries=(entry,)),
                _resigned_copy(second, keys[1], wrapper_tree_entries=(entry,)),
            )
        }
    )
    only_one_changed = closure.model_copy(
        update={
            "worker_attestations": (
                _resigned_copy(first, keys[0], wrapper_tree_entries=(entry,)),
                second,
            )
        }
    )

    assert (
        _validate(
            both_changed, policy, profile_envelope, profile_anchor
        ).non_authorizing
        is True
    )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        _validate(only_one_changed, policy, profile_envelope, profile_anchor)


def test_v5_worker_closure_rejects_duplicate_run_and_wrong_policy_order() -> None:
    closure, policy, profile_envelope, profile_anchor, keys = _closure()
    first, _ = closure.worker_attestations
    duplicate_run_second = _attestation(
        signing_key=keys[1],
        anchor=policy.worker_trust_anchors[1],
        policy=policy,
        profile_envelope=profile_envelope,
        shared_oci=closure.oci_safe_config_evidence,
        run_id=first.run_id,
    )
    duplicate = closure.model_copy(
        update={"worker_attestations": (first, duplicate_run_second)}
    )
    reversed_order = closure.model_copy(
        update={"worker_attestations": tuple(reversed(closure.worker_attestations))}
    )

    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        _validate(duplicate, policy, profile_envelope, profile_anchor)
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        _validate(reversed_order, policy, profile_envelope, profile_anchor)


def test_v5_worker_closure_rejects_wrong_signature_domain_and_pinned_builder_mismatch() -> (
    None
):
    closure, policy, profile_envelope, profile_anchor, keys = _closure()
    first, second = closure.worker_attestations
    wrong_domain = closure.model_copy(
        update={
            "worker_attestations": (
                first.model_copy(
                    update={
                        "signature_base64": base64.b64encode(
                            keys[0].sign(
                                b"omninode-rsd.container-bootstrap-artifact-worker-attestation.ed25519.v4\0"
                                + b"not-a-v5-attestation"
                            )
                        ).decode("ascii")
                    }
                ),
                second,
            )
        }
    )
    anchors = list(policy.worker_trust_anchors)
    anchors[0] = anchors[0].model_copy(
        update={"physical_builder_identity_sha256": _hash("other-physical-builder")}
    )
    wrong_builder_policy = policy.model_copy(
        update={"worker_trust_anchors": tuple(anchors)}
    )

    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        _validate(wrong_domain, policy, profile_envelope, profile_anchor)
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        _validate(closure, wrong_builder_policy, profile_envelope, profile_anchor)


@pytest.mark.parametrize(
    "field",
    [
        "worker_identity_sha256",
        "authority_identity_sha256",
        "physical_builder_identity_sha256",
    ],
)
def test_v5_worker_policy_rejects_cross_anchor_identity_collisions(field: str) -> None:
    closure, policy, profile_envelope, profile_anchor, _ = _closure()
    first_anchor, second_anchor = policy.worker_trust_anchors
    colliding_second = second_anchor.model_copy(
        update={field: first_anchor.physical_builder_identity_sha256}
    )
    colliding_policy = policy.model_copy(
        update={"worker_trust_anchors": (first_anchor, colliding_second)}
    )

    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        _validate(closure, colliding_policy, profile_envelope, profile_anchor)


@pytest.mark.parametrize(
    ("anchor_index", "field", "replacement_kind"),
    [
        (0, "physical_builder_identity_sha256", "profile-root"),
        (0, "worker_identity_sha256", "ticket-root"),
        (1, "authority_identity_sha256", "replay-root"),
        (1, "key_id", "profile-root-key"),
    ],
)
def test_v5_worker_closure_rejects_profile_root_namespace_collisions(
    anchor_index: int, field: str, replacement_kind: str
) -> None:
    closure, policy, profile_envelope, profile_anchor, _ = _closure()
    profile = profile_envelope.static_role_profile
    replacement = {
        "profile-root": profile_anchor.public_key_fingerprint_sha256,
        "ticket-root": profile.ticket_trust_anchor.public_key_fingerprint_sha256,
        "replay-root": profile.replay_receipt_trust_anchor.public_key_fingerprint_sha256,
        "profile-root-key": profile_anchor.key_id,
    }[replacement_kind]
    anchors = list(policy.worker_trust_anchors)
    anchors[anchor_index] = anchors[anchor_index].model_copy(
        update={field: replacement}
    )
    colliding_policy = policy.model_copy(
        update={"worker_trust_anchors": tuple(anchors)}
    )

    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        _validate(closure, colliding_policy, profile_envelope, profile_anchor)


def test_v5_worker_closure_rejects_model_construct_bad_boolean_and_signature_encoding() -> (
    None
):
    closure, policy, profile_envelope, profile_anchor, _ = _closure()
    first, second = closure.worker_attestations
    constructed = evidence.ContainerBootstrapArtifactEvidenceClosureV5.model_construct(
        **{
            **closure.model_dump(mode="python"),
            "non_authorizing": 1,
        }
    )
    bad_boolean = closure.model_copy(
        update={
            "worker_attestations": (
                first.model_copy(update={"source_clean": 1}),
                second,
            )
        }
    )
    bad_base64 = closure.model_copy(
        update={
            "worker_attestations": (
                first.model_copy(update={"signature_base64": "%%%"}),
                second,
            )
        }
    )

    for candidate in (constructed, bad_boolean, bad_base64):
        with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
            _validate(candidate, policy, profile_envelope, profile_anchor)


def test_v5_worker_closure_canonical_parser_rejects_noncanonical_and_oversize() -> None:
    closure, _, _, _, _ = _closure()
    payload = evidence.container_bootstrap_artifact_evidence_closure_v5_canonical_json(
        closure
    )

    assert (
        evidence.parse_container_bootstrap_artifact_evidence_closure_v5_canonical_json(
            payload
        )
        == closure
    )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        evidence.parse_container_bootstrap_artifact_evidence_closure_v5_canonical_json(
            payload + b" "
        )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        evidence.parse_container_bootstrap_artifact_evidence_closure_v5_canonical_json(
            b"{" + b'"x":0,' * 100_000 + b'"y":0}'
        )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        evidence.parse_container_bootstrap_artifact_evidence_closure_v5_canonical_json(
            b'{"x":1,"x":1}'
        )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
        evidence.parse_container_bootstrap_artifact_evidence_closure_v5_canonical_json(
            b'{"x":1.0}'
        )


def test_v5_acceptance_canonical_serialization_is_strict_and_domain_separated() -> None:
    closure, policy, profile_envelope, profile_anchor, _ = _closure()
    acceptance = _validate(closure, policy, profile_envelope, profile_anchor)
    payload = (
        evidence.container_bootstrap_artifact_evidence_acceptance_v5_canonical_json(
            acceptance
        )
    )

    assert (
        evidence.parse_container_bootstrap_artifact_evidence_acceptance_v5_canonical_json(
            payload
        )
        == acceptance
    )
    assert (
        evidence.container_bootstrap_artifact_evidence_acceptance_v5_canonical_json(
            _validate(closure, policy, profile_envelope, profile_anchor)
        )
        == payload
    )
    assert evidence.container_bootstrap_artifact_evidence_acceptance_v5_sha256(
        acceptance
    ) == (
        hashlib.sha256(
            b"omninode-rsd.container-bootstrap-artifact-evidence-acceptance.sha256.v5\0"
            + payload
        ).hexdigest()
    )
    assert evidence.container_bootstrap_artifact_evidence_acceptance_v5_sha256(
        acceptance
    ) != (hashlib.sha256(payload).hexdigest())

    valid_constructed = (
        evidence.ContainerBootstrapArtifactEvidenceAcceptanceV5.model_construct(
            **acceptance.model_dump(mode="python")
        )
    )
    assert (
        evidence.container_bootstrap_artifact_evidence_acceptance_v5_canonical_json(
            valid_constructed
        )
        == payload
    )
    with pytest.raises(ValueError, match=r".+"):
        evidence.ContainerBootstrapArtifactEvidenceAcceptanceV5.model_construct(
            **{**acceptance.model_dump(mode="python"), "unexpected": True}
        )

    constructed = (
        evidence.ContainerBootstrapArtifactEvidenceAcceptanceV5.model_construct(
            **{**acceptance.model_dump(mode="python"), "non_authorizing": 1}
        )
    )
    copied_with_extra = acceptance.model_copy(update={"unexpected": True})
    manual_extra = acceptance.model_copy()
    manual_extra.__dict__["unexpected"] = True
    constructed_with_extra = (
        evidence.ContainerBootstrapArtifactEvidenceAcceptanceV5.model_construct(
            **acceptance.model_dump(mode="python")
        )
    )
    constructed_with_extra.__dict__["unexpected"] = True
    pydantic_extra = acceptance.model_copy()
    object.__setattr__(pydantic_extra, "__pydantic_extra__", {"unexpected": True})
    empty_pydantic_extra = acceptance.model_copy()
    object.__setattr__(empty_pydantic_extra, "__pydantic_extra__", {})
    private_metadata = acceptance.model_copy()
    object.__setattr__(private_metadata, "__pydantic_private__", {"unexpected": True})
    empty_private_metadata = acceptance.model_copy()
    object.__setattr__(empty_private_metadata, "__pydantic_private__", {})
    missing_field_set = acceptance.model_copy()
    missing_field_set.__pydantic_fields_set__.remove("attach_allowed")
    unexpected_field_set = acceptance.model_copy()
    unexpected_field_set.__pydantic_fields_set__.add("unexpected")
    for candidate in (
        closure,
        constructed,
        copied_with_extra,
        manual_extra,
        constructed_with_extra,
        pydantic_extra,
        empty_pydantic_extra,
        private_metadata,
        empty_private_metadata,
        missing_field_set,
        unexpected_field_set,
    ):
        for operation in (
            evidence.container_bootstrap_artifact_evidence_acceptance_v5_canonical_json,
            evidence.container_bootstrap_artifact_evidence_acceptance_v5_sha256,
        ):
            with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
                operation(candidate)  # type: ignore[arg-type]

    for malformed in (
        payload + b" ",
        payload.replace(b'"schema_version"', b'"schema\\u005fversion"', 1),
        payload[:-1] + b',"extra":false}',
        b'{"closure_sha256":"'
        + b"a" * 64
        + b'","closure_sha256":"'
        + b"a" * 64
        + b'"}',
        b'{"x":1.0}',
        b"{" + b'"x":0,' * 100_000 + b'"y":0}',
    ):
        with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
            evidence.parse_container_bootstrap_artifact_evidence_acceptance_v5_canonical_json(
                malformed
            )


@pytest.mark.parametrize(
    "internal_attribute",
    [
        "__pydantic_extra__",
        "__pydantic_private__",
        "__pydantic_fields_set__",
        "__dict__",
    ],
)
def test_v5_acceptance_deleted_internal_state_fails_closed(
    internal_attribute: str,
) -> None:
    closure, policy, profile_envelope, profile_anchor, _ = _closure()
    candidate = _validate(
        closure, policy, profile_envelope, profile_anchor
    ).model_copy()
    object.__delattr__(candidate, internal_attribute)

    for operation in (
        evidence.container_bootstrap_artifact_evidence_acceptance_v5_canonical_json,
        evidence.container_bootstrap_artifact_evidence_acceptance_v5_sha256,
    ):
        with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
            operation(candidate)


def test_v5_acceptance_wrong_type_rejects_before_attribute_access() -> None:
    class HostileWrongType:
        def __getattribute__(self, _name: str) -> object:
            raise AssertionError("wrong-type attribute access must not occur")

    hostile = HostileWrongType()
    for operation in (
        evidence.container_bootstrap_artifact_evidence_acceptance_v5_canonical_json,
        evidence.container_bootstrap_artifact_evidence_acceptance_v5_sha256,
    ):
        with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV5Error):
            operation(hostile)  # type: ignore[arg-type]
