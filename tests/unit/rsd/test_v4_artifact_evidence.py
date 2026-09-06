"""Offline adversarial tests for the isolated V4 artifact-evidence closure."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from yaml.events import AliasEvent

from omnimarket.rsd import v4_artifact_evidence as evidence
from omnimarket.rsd import v4_static_profile as static_v4

_STATIC_VECTOR = (
    Path(__file__).parents[2] / "fixtures/rsd/container_attach_static_v4_vectors.yaml"
)
_VECTOR = (
    Path(__file__).parents[2]
    / "fixtures/rsd/container_bootstrap_artifact_evidence_v4_vectors.yaml"
)
_VECTOR_BASE64_ENCODING = "standard_base64_fixed_segments_v1"
_VECTOR_BASE64_SEGMENT_CHARS = 16


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _decoded(value: object) -> bytes:
    mapped = cast(dict[str, object], value)
    encoding = mapped.get("encoding")
    segments = mapped.get("segments")
    if (
        encoding != _VECTOR_BASE64_ENCODING
        or type(segments) is not list
        or not segments
        or any(type(segment) is not str for segment in segments)
        or any(
            len(segment) != _VECTOR_BASE64_SEGMENT_CHARS for segment in segments[:-1]
        )
        or not 1 <= len(segments[-1]) <= _VECTOR_BASE64_SEGMENT_CHARS
    ):
        raise ValueError("vector base64 is invalid")
    compact = "".join(cast(list[str], segments))
    decoded = base64.b64decode(compact, validate=True)
    if [
        compact[index : index + _VECTOR_BASE64_SEGMENT_CHARS]
        for index in range(0, len(compact), _VECTOR_BASE64_SEGMENT_CHARS)
    ] != segments:
        raise ValueError("vector base64 is noncanonical")
    return decoded


class _StrictVectorLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys; aliases are rejected before construction."""


def _strict_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError("duplicate vector key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictVectorLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_mapping
)


def _strict_vector() -> dict[str, object]:
    raw = _VECTOR.read_bytes()
    if any(isinstance(event, AliasEvent) for event in yaml.parse(raw)):
        raise ValueError("YAML aliases are invalid")
    parsed = yaml.load(raw, Loader=_StrictVectorLoader)
    if type(parsed) is not dict:
        raise ValueError("vector is invalid")
    return cast(dict[str, object], parsed)


def _profile() -> tuple[object, object]:
    values = cast(
        dict[str, object], yaml.safe_load(_STATIC_VECTOR.read_text(encoding="ascii"))
    )
    envelope = static_v4.parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json(
        _decoded(values["profile_envelope_canonical_json_utf8_base64"])
    )
    anchor = static_v4.parse_container_bootstrap_static_profile_trust_anchor_v4_canonical_json(
        _decoded(values["profile_root_canonical_json_utf8_base64"])
    )
    return envelope, anchor


def _resigned_profile_envelope_for_launch(
    *,
    wrapper_argv_prefix: tuple[str, ...],
    base_entrypoint: tuple[str, ...],
    base_command: tuple[str, ...],
) -> tuple[object, object]:
    """Create a test-only signed profile for one legal static V4 launch shape."""

    source_envelope, _ = _profile()
    source_profile = cast(Any, source_envelope).static_role_profile
    launch = static_v4.ContainerBootstrapStaticLaunchPlanV4(
        **{
            **source_profile.static_launch_plan.model_dump(mode="python"),
            "wrapper_argv_prefix": wrapper_argv_prefix,
            "base_entrypoint": base_entrypoint,
            "base_command": base_command,
            "merged_argv_sha256": static_v4._merged_argv_sha256(
                wrapper_argv_prefix=wrapper_argv_prefix,
                base_entrypoint=base_entrypoint,
                base_command=base_command,
            ),
        }
    )
    preimage = static_v4.ContainerBootstrapStaticPatchPreimageV4(
        **{
            **source_profile.static_patch_preimage.model_dump(mode="python"),
            "static_launch_plan_sha256": (
                static_v4.container_bootstrap_static_launch_plan_v4_sha256(launch)
            ),
        }
    )
    patch = static_v4.ContainerBootstrapStaticPatchPolicyV4(
        **{
            **source_profile.static_patch_policy.model_dump(mode="python"),
            "preimage": preimage,
            "static_patch_preimage_sha256": (
                static_v4.container_bootstrap_static_patch_preimage_v4_sha256(preimage)
            ),
        }
    )
    profile = static_v4.build_container_bootstrap_static_role_profile_v4(
        wrapper_source_tree_sha256=source_profile.wrapper_source_tree_sha256,
        component=source_profile.component,
        component_role=source_profile.component_role,
        compile_target=source_profile.compile_target,
        wrapper_executable_path=source_profile.wrapper_executable_path,
        wrapper_executable_mode=source_profile.wrapper_executable_mode,
        wrapper_executable_symlink_allowed=source_profile.wrapper_executable_symlink_allowed,
        ticket_trust_anchor=source_profile.ticket_trust_anchor,
        replay_receipt_trust_anchor=source_profile.replay_receipt_trust_anchor,
        attach_protocol=source_profile.attach_protocol,
        static_delivery_projection=source_profile.static_delivery_projection,
        selected_delivery_route=source_profile.selected_delivery_route,
        static_launch_plan=launch,
        static_patch_preimage=preimage,
        static_patch_policy=patch,
        static_environment=source_profile.static_environment,
        child_environment_policy=source_profile.child_environment_policy,
        fd_policy=source_profile.fd_policy,
        pid1_policy=source_profile.pid1_policy,
        memory_safety_policy=source_profile.memory_safety_policy,
        valkey_launch_policy=source_profile.valkey_launch_policy,
    )
    signing_key = Ed25519PrivateKey.generate()
    public = signing_key.public_key().public_bytes_raw()
    profile_root = static_v4.ContainerBootstrapStaticProfileTrustAnchorV4(
        schema_version="rsd.container-bootstrap-static-profile-trust-anchor.v4",
        key_id="phase-a-test-profile-root",
        public_key_base64=base64.b64encode(public).decode("ascii"),
        public_key_fingerprint_sha256=hashlib.sha256(public).hexdigest(),
        algorithm="ed25519",
    )
    unsigned = static_v4.ContainerBootstrapStaticRoleProfileEnvelopeV4(
        schema_version="rsd.container-bootstrap-static-role-profile-envelope.v4",
        static_role_profile=profile,
        static_role_profile_sha256=(
            static_v4.container_bootstrap_static_role_profile_v4_sha256(profile)
        ),
        signer_key_id=profile_root.key_id,
        signature_base64=base64.b64encode(b"\0" * 64).decode("ascii"),
    )
    envelope = unsigned.model_copy(
        update={
            "signature_base64": base64.b64encode(
                signing_key.sign(
                    static_v4.container_bootstrap_static_role_profile_envelope_v4_canonical_message(
                        unsigned
                    )
                )
            ).decode("ascii")
        }
    )
    return envelope, profile_root


def _closure_for_profile_envelope(
    envelope: object, profile_root: object
) -> tuple[object, object, object, tuple[Ed25519PrivateKey, Ed25519PrivateKey]]:
    """Build a two-worker closure for one independently signed test profile."""

    one, two = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    first_anchor, second_anchor = _anchor(one, "max-one"), _anchor(two, "max-two")
    policy = evidence.ContainerBootstrapBuildWorkerTrustPolicyV4(
        schema_version="rsd.container-bootstrap-build-worker-trust-policy.v4",
        policy_id="maximum-static-launch-worker-policy",
        independence_domain_sha256=_hash("maximum-static-launch-authorities"),
        worker_trust_anchors=(first_anchor, second_anchor),
    )
    first = _attestation(
        signing_key=one,
        anchor=first_anchor,
        run_id="maximum-launch-run-one",
        envelope=envelope,
    )
    second = _attestation(
        signing_key=two,
        anchor=second_anchor,
        run_id="maximum-launch-run-two",
        envelope=envelope,
    )
    closure = evidence.ContainerBootstrapArtifactEvidenceClosureV4(
        schema_version="rsd.container-bootstrap-artifact-evidence-closure.v4",
        worker_attestations=(first, second),
        evidence_effect_allowed=False,
        build_allowed=False,
        materialization_allowed=False,
        attach_allowed=False,
    )
    return closure, policy, profile_root, (one, two)


def _anchor(
    signing_key: Ed25519PrivateKey, name: str
) -> evidence.ContainerBootstrapBuildWorkerTrustAnchorV4:
    public = signing_key.public_key().public_bytes_raw()
    return evidence.ContainerBootstrapBuildWorkerTrustAnchorV4(
        schema_version="rsd.container-bootstrap-build-worker-trust-anchor.v4",
        key_id=f"worker-{name}",
        worker_identity_sha256=_hash(f"worker-identity-{name}"),
        authority_identity_sha256=_hash(f"worker-authority-{name}"),
        public_key_base64=base64.b64encode(public).decode("ascii"),
        public_key_fingerprint_sha256=hashlib.sha256(public).hexdigest(),
        algorithm="ed25519",
    )


def _attestation(
    *,
    signing_key: Ed25519PrivateKey,
    anchor: evidence.ContainerBootstrapBuildWorkerTrustAnchorV4,
    run_id: str,
    envelope: Any,
    source_snapshot: str | None = None,
    derived_repository: str = "example.invalid/rsd/v4-wrapper",
) -> evidence.ContainerBootstrapArtifactWorkerAttestationV4:
    profile = envelope.static_role_profile
    layer = evidence.ContainerBootstrapOciLayerDescriptorV4(
        schema_version="rsd.container-bootstrap-oci-layer-descriptor.v4",
        media_type="application/vnd.oci.image.layer.v1.tar+gzip",
        digest_sha256=_hash("layer"),
        byte_count=512,
        diff_id_sha256=_hash("diff-id"),
    )
    entry = evidence.ContainerBootstrapOciWrapperEntryV4(
        schema_version="rsd.container-bootstrap-oci-wrapper-entry.v4",
        path=profile.wrapper_executable_path,
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
        content_sha256=_hash("artifact"),
        byte_count=12345,
    )
    archive = evidence.ContainerBootstrapWrapperArchiveInspectionV4(
        schema_version="rsd.container-bootstrap-wrapper-archive-inspection.v4",
        archive_entry_count=1,
        inspected_layer_digest_sha256=layer.digest_sha256,
        wrapper_path=profile.wrapper_executable_path,
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
    entrypoint = (
        *profile.static_launch_plan.wrapper_argv_prefix,
        *profile.static_launch_plan.base_entrypoint,
    )
    config_bytes = _canonical_bytes(
        {
            "architecture": "amd64",
            "config": {
                "Cmd": list(profile.static_launch_plan.base_command),
                "Entrypoint": list(entrypoint),
            },
            "os": "linux",
            "rootfs": {
                "diff_ids": [f"sha256:{layer.diff_id_sha256}"],
                "type": "layers",
            },
        }
    )
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    manifest_bytes = _canonical_bytes(
        {
            "config": {
                "digest": f"sha256:{config_digest}",
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": len(config_bytes),
            },
            "layers": [
                {
                    "digest": f"sha256:{layer.digest_sha256}",
                    "mediaType": layer.media_type,
                    "size": layer.byte_count,
                }
            ],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        }
    )
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    index_bytes = _canonical_bytes(
        {
            "manifests": [
                {
                    "digest": f"sha256:{manifest_digest}",
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "platform": {"architecture": "amd64", "os": "linux"},
                    "size": len(manifest_bytes),
                }
            ],
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        }
    )
    index_digest = hashlib.sha256(index_bytes).hexdigest()
    oci = evidence.ContainerBootstrapOciEvidenceV4(
        schema_version="rsd.container-bootstrap-oci-evidence.v4",
        derived_repository=derived_repository,
        derived_reference=f"{derived_repository}@sha256:{manifest_digest}",
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
        index_digest_sha256=index_digest,
        linux_amd64_manifest_digest_sha256=manifest_digest,
        config_digest_sha256=config_digest,
        index_canonical_json_sha256=index_digest,
        index_canonical_json_byte_count=len(index_bytes),
        index_canonical_json_utf8_base64=base64.b64encode(index_bytes).decode("ascii"),
        selected_manifest_descriptor_digest_sha256=manifest_digest,
        manifest_canonical_json_sha256=manifest_digest,
        manifest_canonical_json_byte_count=len(manifest_bytes),
        manifest_canonical_json_utf8_base64=base64.b64encode(manifest_bytes).decode(
            "ascii"
        ),
        manifest_config_descriptor_digest_sha256=config_digest,
        config_canonical_json_sha256=config_digest,
        config_canonical_json_byte_count=len(config_bytes),
        config_canonical_json_utf8_base64=base64.b64encode(config_bytes).decode(
            "ascii"
        ),
        platform_os="linux",
        platform_architecture="amd64",
        ordered_layers=(layer,),
        config_rootfs_diff_ids_sha256=(layer.diff_id_sha256,),
        wrapper_layer_digest_sha256=layer.digest_sha256,
        wrapper_layer_ordinal=0,
        wrapper_tar_entry=entry,
        wrapper_archive_inspection=archive,
        entrypoint=entrypoint,
        cmd=profile.static_launch_plan.base_command,
    )
    envelope_hash = (
        static_v4.container_bootstrap_static_role_profile_envelope_v4_sha256(envelope)
    )
    fields: dict[str, object] = {
        "schema_version": "rsd.container-bootstrap-artifact-worker-attestation.v4",
        "signer_key_id": anchor.key_id,
        "worker_identity_sha256": anchor.worker_identity_sha256,
        "run_id": run_id,
        "canonical_repository_identity_sha256": _hash("repo"),
        "git_object_format": "sha1",
        "commit_oid": "1" * 40,
        "tree_oid": "2" * 40,
        "canonical_source_snapshot_sha256": (
            source_snapshot or profile.static_patch_preimage.wrapper_source_tree_sha256
        ),
        "wrapper_subtree_path": "wrapper",
        "wrapper_tree_entries": (
            evidence.ContainerBootstrapWrapperTreeEntryV4(
                schema_version="rsd.container-bootstrap-wrapper-tree-entry.v4",
                path="Cargo.lock",
                object_sha256=_hash("cargo-lock"),
                mode="0644",
                entry_type="regular",
            ),
        ),
        "source_clean": True,
        "untracked_files_absent": True,
        "submodules_absent": True,
        "recipe_sha256": _hash("recipe"),
        "toolchain_sha256": _hash("toolchain"),
        "lock_sha256": _hash("lock"),
        "vendor_sha256": _hash("vendor"),
        "builder_recipe_identity_sha256": _hash("builder-recipe"),
        "physical_builder_identity_sha256": _hash(f"physical-builder-{run_id}"),
        "component": profile.component,
        "component_role": profile.component_role,
        "static_role_profile_sha256": profile.profile_sha256,
        "profile_envelope_sha256": envelope_hash,
        "static_delivery_projection_sha256": profile.static_delivery_projection_sha256,
        "static_launch_plan_sha256": profile.static_launch_plan_sha256,
        "static_patch_preimage_sha256": profile.static_patch_preimage_sha256,
        "static_patch_policy_sha256": profile.static_patch_policy_sha256,
        "wrapper_artifact_sha256": _hash("artifact"),
        "wrapper_artifact_byte_count": 12345,
        "wrapper_executable_path": profile.wrapper_executable_path,
        "wrapper_uid": 0,
        "wrapper_gid": 0,
        "wrapper_mode": "0555",
        "wrapper_regular_file": True,
        "wrapper_link_count": 1,
        "wrapper_symlink": False,
        "wrapper_hardlink": False,
        "wrapper_setuid": False,
        "wrapper_setgid": False,
        "wrapper_sticky": False,
        "oci": oci,
        "signature_base64": base64.b64encode(b"\0" * 64).decode("ascii"),
    }
    unsigned = evidence.ContainerBootstrapArtifactWorkerAttestationV4(**fields)
    fields["signature_base64"] = base64.b64encode(
        signing_key.sign(
            evidence.container_bootstrap_artifact_worker_attestation_v4_message(
                unsigned
            )
        )
    ).decode("ascii")
    return evidence.ContainerBootstrapArtifactWorkerAttestationV4(**fields)


def _closure() -> tuple[object, object, object, object, object]:
    envelope, profile_anchor = _profile()
    one, two = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    first_anchor, second_anchor = _anchor(one, "one"), _anchor(two, "two")
    policy = evidence.ContainerBootstrapBuildWorkerTrustPolicyV4(
        schema_version="rsd.container-bootstrap-build-worker-trust-policy.v4",
        policy_id="public-test-worker-policy",
        independence_domain_sha256=_hash("independent-worker-authorities"),
        worker_trust_anchors=(first_anchor, second_anchor),
    )
    first = _attestation(
        signing_key=one, anchor=first_anchor, run_id="build-run-one", envelope=envelope
    )
    second = _attestation(
        signing_key=two, anchor=second_anchor, run_id="build-run-two", envelope=envelope
    )
    closure = evidence.ContainerBootstrapArtifactEvidenceClosureV4(
        schema_version="rsd.container-bootstrap-artifact-evidence-closure.v4",
        worker_attestations=(first, second),
        evidence_effect_allowed=False,
        build_allowed=False,
        materialization_allowed=False,
        attach_allowed=False,
    )
    return closure, policy, envelope, profile_anchor, (one, two)


def _recomputed_duplicate_layer_oci(
    original: evidence.ContainerBootstrapOciEvidenceV4,
    second_layer: evidence.ContainerBootstrapOciLayerDescriptorV4 | None = None,
) -> evidence.ContainerBootstrapOciEvidenceV4:
    """Forge a syntactically complete duplicate-layer graph without invoking validation."""

    layer = original.ordered_layers[0]
    second_layer = second_layer or layer
    layers = (layer, second_layer)
    config_bytes = _canonical_bytes(
        {
            "architecture": "amd64",
            "config": {
                "Cmd": list(original.cmd),
                "Entrypoint": list(original.entrypoint),
            },
            "os": "linux",
            "rootfs": {
                "diff_ids": [
                    f"sha256:{layer.diff_id_sha256}",
                    f"sha256:{second_layer.diff_id_sha256}",
                ],
                "type": "layers",
            },
        }
    )
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    manifest_bytes = _canonical_bytes(
        {
            "config": {
                "digest": f"sha256:{config_digest}",
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": len(config_bytes),
            },
            "layers": [
                {
                    "digest": f"sha256:{layer.digest_sha256}",
                    "mediaType": layer.media_type,
                    "size": layer.byte_count,
                },
                {
                    "digest": f"sha256:{second_layer.digest_sha256}",
                    "mediaType": second_layer.media_type,
                    "size": second_layer.byte_count,
                },
            ],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        }
    )
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    index_bytes = _canonical_bytes(
        {
            "manifests": [
                {
                    "digest": f"sha256:{manifest_digest}",
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "platform": {"architecture": "amd64", "os": "linux"},
                    "size": len(manifest_bytes),
                }
            ],
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        }
    )
    index_digest = hashlib.sha256(index_bytes).hexdigest()
    return evidence.ContainerBootstrapOciEvidenceV4.model_construct(
        **{
            **original.model_dump(mode="python"),
            "derived_reference": f"{original.derived_repository}@sha256:{manifest_digest}",
            "index_digest_sha256": index_digest,
            "linux_amd64_manifest_digest_sha256": manifest_digest,
            "config_digest_sha256": config_digest,
            "index_canonical_json_sha256": index_digest,
            "index_canonical_json_byte_count": len(index_bytes),
            "index_canonical_json_utf8_base64": base64.b64encode(index_bytes).decode(
                "ascii"
            ),
            "selected_manifest_descriptor_digest_sha256": manifest_digest,
            "manifest_canonical_json_sha256": manifest_digest,
            "manifest_canonical_json_byte_count": len(manifest_bytes),
            "manifest_canonical_json_utf8_base64": base64.b64encode(
                manifest_bytes
            ).decode("ascii"),
            "manifest_config_descriptor_digest_sha256": config_digest,
            "config_canonical_json_sha256": config_digest,
            "config_canonical_json_byte_count": len(config_bytes),
            "config_canonical_json_utf8_base64": base64.b64encode(config_bytes).decode(
                "ascii"
            ),
            "ordered_layers": layers,
            "config_rootfs_diff_ids_sha256": (
                layer.diff_id_sha256,
                second_layer.diff_id_sha256,
            ),
            "wrapper_layer_ordinal": 1,
        }
    )


def _recomputed_oci_with_cmd(
    original: evidence.ContainerBootstrapOciEvidenceV4,
    cmd: tuple[str, ...],
) -> evidence.ContainerBootstrapOciEvidenceV4:
    """Recompute the complete canonical OCI document chain for a bounded argv test."""

    layer = original.ordered_layers[0]
    config_bytes = _canonical_bytes(
        {
            "architecture": "amd64",
            "config": {"Cmd": list(cmd), "Entrypoint": list(original.entrypoint)},
            "os": "linux",
            "rootfs": {
                "diff_ids": [f"sha256:{layer.diff_id_sha256}"],
                "type": "layers",
            },
        }
    )
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    manifest_bytes = _canonical_bytes(
        {
            "config": {
                "digest": f"sha256:{config_digest}",
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": len(config_bytes),
            },
            "layers": [
                {
                    "digest": f"sha256:{layer.digest_sha256}",
                    "mediaType": layer.media_type,
                    "size": layer.byte_count,
                }
            ],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        }
    )
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    index_bytes = _canonical_bytes(
        {
            "manifests": [
                {
                    "digest": f"sha256:{manifest_digest}",
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "platform": {"architecture": "amd64", "os": "linux"},
                    "size": len(manifest_bytes),
                }
            ],
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        }
    )
    index_digest = hashlib.sha256(index_bytes).hexdigest()
    return evidence.ContainerBootstrapOciEvidenceV4(
        **{
            **original.model_dump(mode="python"),
            "derived_reference": f"{original.derived_repository}@sha256:{manifest_digest}",
            "index_digest_sha256": index_digest,
            "linux_amd64_manifest_digest_sha256": manifest_digest,
            "config_digest_sha256": config_digest,
            "index_canonical_json_sha256": index_digest,
            "index_canonical_json_byte_count": len(index_bytes),
            "index_canonical_json_utf8_base64": base64.b64encode(index_bytes).decode(
                "ascii"
            ),
            "selected_manifest_descriptor_digest_sha256": manifest_digest,
            "manifest_canonical_json_sha256": manifest_digest,
            "manifest_canonical_json_byte_count": len(manifest_bytes),
            "manifest_canonical_json_utf8_base64": base64.b64encode(
                manifest_bytes
            ).decode("ascii"),
            "manifest_config_descriptor_digest_sha256": config_digest,
            "config_canonical_json_sha256": config_digest,
            "config_canonical_json_byte_count": len(config_bytes),
            "config_canonical_json_utf8_base64": base64.b64encode(config_bytes).decode(
                "ascii"
            ),
            "cmd": cmd,
        }
    )


def _maximum_shape_oci(
    original: evidence.ContainerBootstrapOciEvidenceV4,
    *,
    entrypoint: tuple[str, ...],
    cmd: tuple[str, ...],
) -> evidence.ContainerBootstrapOciEvidenceV4:
    """Build one valid 32-layer OCI claim at every Phase-A public bound."""

    layers = tuple(
        evidence.ContainerBootstrapOciLayerDescriptorV4(
            schema_version="rsd.container-bootstrap-oci-layer-descriptor.v4",
            media_type="application/vnd.oci.image.layer.v1.tar+gzip",
            digest_sha256=_hash(f"maximum-layer-digest-{ordinal}"),
            byte_count=1_073_741_824,
            diff_id_sha256=_hash(f"maximum-layer-diff-{ordinal}"),
        )
        for ordinal in range(32)
    )
    wrapper_entry = original.wrapper_tar_entry
    archive = evidence.ContainerBootstrapWrapperArchiveInspectionV4(
        **{
            **original.wrapper_archive_inspection.model_dump(mode="python"),
            "archive_entry_count": 65_536,
            "inspected_layer_digest_sha256": layers[-1].digest_sha256,
            "wrapper_path": wrapper_entry.path,
        }
    )
    config_bytes = _canonical_bytes(
        {
            "architecture": "amd64",
            "config": {"Cmd": list(cmd), "Entrypoint": list(entrypoint)},
            "os": "linux",
            "rootfs": {
                "diff_ids": [f"sha256:{layer.diff_id_sha256}" for layer in layers],
                "type": "layers",
            },
        }
    )
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    manifest_bytes = _canonical_bytes(
        {
            "config": {
                "digest": f"sha256:{config_digest}",
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": len(config_bytes),
            },
            "layers": [
                {
                    "digest": f"sha256:{layer.digest_sha256}",
                    "mediaType": layer.media_type,
                    "size": layer.byte_count,
                }
                for layer in layers
            ],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        }
    )
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    index_bytes = _canonical_bytes(
        {
            "manifests": [
                {
                    "digest": f"sha256:{manifest_digest}",
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "platform": {"architecture": "amd64", "os": "linux"},
                    "size": len(manifest_bytes),
                }
            ],
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        }
    )
    index_digest = hashlib.sha256(index_bytes).hexdigest()
    return evidence.ContainerBootstrapOciEvidenceV4(
        **{
            **original.model_dump(mode="python"),
            "derived_reference": f"{original.derived_repository}@sha256:{manifest_digest}",
            "index_digest_sha256": index_digest,
            "linux_amd64_manifest_digest_sha256": manifest_digest,
            "config_digest_sha256": config_digest,
            "index_canonical_json_sha256": index_digest,
            "index_canonical_json_byte_count": len(index_bytes),
            "index_canonical_json_utf8_base64": base64.b64encode(index_bytes).decode(
                "ascii"
            ),
            "selected_manifest_descriptor_digest_sha256": manifest_digest,
            "manifest_canonical_json_sha256": manifest_digest,
            "manifest_canonical_json_byte_count": len(manifest_bytes),
            "manifest_canonical_json_utf8_base64": base64.b64encode(
                manifest_bytes
            ).decode("ascii"),
            "manifest_config_descriptor_digest_sha256": config_digest,
            "config_canonical_json_sha256": config_digest,
            "config_canonical_json_byte_count": len(config_bytes),
            "config_canonical_json_utf8_base64": base64.b64encode(config_bytes).decode(
                "ascii"
            ),
            "ordered_layers": layers,
            "config_rootfs_diff_ids_sha256": tuple(
                layer.diff_id_sha256 for layer in layers
            ),
            "wrapper_layer_digest_sha256": layers[-1].digest_sha256,
            "wrapper_layer_ordinal": len(layers) - 1,
            "wrapper_tar_entry": wrapper_entry,
            "wrapper_archive_inspection": archive,
            "entrypoint": entrypoint,
            "cmd": cmd,
        }
    )


def _maximum_tree_path(ordinal: int) -> str:
    """Return one sorted, maximum-length safe relative source-tree path."""

    prefix = "wrapper/" + str(ordinal).zfill(2) + "-"
    return prefix + "s" * (240 - len(prefix))


def _signed_constructed_attestation(
    original: evidence.ContainerBootstrapArtifactWorkerAttestationV4,
    oci: evidence.ContainerBootstrapOciEvidenceV4,
    signing_key: Ed25519PrivateKey,
    tree_entries: tuple[evidence.ContainerBootstrapWrapperTreeEntryV4, ...]
    | None = None,
    additional_fields: dict[str, object] | None = None,
) -> evidence.ContainerBootstrapArtifactWorkerAttestationV4:
    fields: dict[str, object] = {**original.model_dump(mode="python"), "oci": oci}
    if tree_entries is not None:
        fields["wrapper_tree_entries"] = tree_entries
    if additional_fields is not None:
        fields.update(additional_fields)
    unsigned = evidence.ContainerBootstrapArtifactWorkerAttestationV4.model_construct(
        **fields
    )
    unsigned_bytes = json.dumps(
        unsigned.model_dump(mode="json", exclude={"signature_base64"}, warnings=False),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    fields["signature_base64"] = base64.b64encode(
        signing_key.sign(evidence._DOMAIN_ATTESTATION + unsigned_bytes)
    ).decode("ascii")
    return evidence.ContainerBootstrapArtifactWorkerAttestationV4.model_construct(
        **fields
    )


def test_validates_two_distinct_signed_workers_and_is_repeatably_non_authorizing() -> (
    None
):
    closure, policy, envelope, profile_anchor, _ = _closure()
    first = evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
        closure=cast(Any, closure),
        worker_trust_policy=cast(Any, policy),
        profile_envelope=cast(Any, envelope),
        profile_trust_anchor=cast(Any, profile_anchor),
    )
    second = evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
        closure=cast(Any, closure),
        worker_trust_policy=cast(Any, policy),
        profile_envelope=cast(Any, envelope),
        profile_trust_anchor=cast(Any, profile_anchor),
    )
    assert first == second
    assert not any(
        (
            first.evidence_effect_allowed,
            first.build_allowed,
            first.materialization_allowed,
            first.attach_allowed,
        )
    )


def test_profile_bytes_stay_unchanged_while_downstream_drift_fails() -> None:
    closure, policy, envelope, profile_anchor, signing_keys = _closure()
    before = (
        static_v4.container_bootstrap_static_role_profile_envelope_v4_canonical_json(
            envelope
        )
    )
    before_hash = static_v4.container_bootstrap_static_role_profile_envelope_v4_sha256(
        envelope
    )
    changed_second = _attestation(
        signing_key=signing_keys[1],
        anchor=policy.worker_trust_anchors[1],
        run_id="build-run-two",
        envelope=envelope,
        source_snapshot=_hash("different-snapshot"),
    )
    drifted = evidence.ContainerBootstrapArtifactEvidenceClosureV4(
        **{
            **closure.model_dump(mode="python"),
            "worker_attestations": (closure.worker_attestations[0], changed_second),
        }
    )
    assert (
        static_v4.container_bootstrap_static_role_profile_envelope_v4_canonical_json(
            envelope
        )
        == before
    )
    assert (
        static_v4.container_bootstrap_static_role_profile_envelope_v4_sha256(envelope)
        == before_hash
    )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV4Error):
        evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
            closure=drifted,
            worker_trust_policy=policy,
            profile_envelope=envelope,
            profile_trust_anchor=profile_anchor,
        )


@pytest.mark.parametrize("payload", [b'{"x":1,"x":1}', b'{"x":1.0}', b"{\xff}", b"[1]"])
def test_parser_rejects_noncanonical_and_wrong_shapes(payload: bytes) -> None:
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV4Error):
        evidence.parse_container_bootstrap_artifact_evidence_closure_v4_canonical_json(
            payload
        )


def test_rejects_same_worker_same_run_and_mutable_oci_reference() -> None:
    closure, policy, envelope, profile_anchor, signing_keys = _closure()
    same = _attestation(
        signing_key=signing_keys[0],
        anchor=policy.worker_trust_anchors[0],
        run_id="build-run-one",
        envelope=envelope,
    )
    invalid = evidence.ContainerBootstrapArtifactEvidenceClosureV4(
        **{
            **closure.model_dump(mode="python"),
            "worker_attestations": (closure.worker_attestations[0], same),
        }
    )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV4Error):
        evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
            closure=invalid,
            worker_trust_policy=policy,
            profile_envelope=envelope,
            profile_trust_anchor=profile_anchor,
        )
    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapOciEvidenceV4(
            **{
                **closure.worker_attestations[0].oci.model_dump(mode="python"),
                "derived_reference": "example.invalid/rsd/v4-wrapper:latest",
            }
        )


def test_oci_evidence_uses_canonical_repository_and_digest_reference_grammar() -> None:
    _, policy, envelope, _, signing_keys = _closure()
    repository = "registry.example:443/rsd/generic-wrapper"
    attestation = _attestation(
        signing_key=signing_keys[0],
        anchor=policy.worker_trust_anchors[0],
        run_id="generic-repository-run",
        envelope=envelope,
        derived_repository=repository,
    )
    assert attestation.oci.derived_repository == repository
    assert attestation.oci.derived_reference == (
        f"{repository}@sha256:{attestation.oci.linux_amd64_manifest_digest_sha256}"
    )
    for field, value in (
        ("derived_repository", "Registry.example/rsd/generic-wrapper"),
        ("derived_repository", "registry.example:0443/rsd/generic-wrapper"),
        ("derived_reference", f"{repository}:latest"),
        ("derived_reference", f"{repository}@sha256:{'0' * 64}"),
    ):
        with pytest.raises(ValueError, match=r"."):
            evidence.ContainerBootstrapOciEvidenceV4(
                **{**attestation.oci.model_dump(mode="python"), field: value}
            )


def test_rejects_nonfinal_wrapper_layer_and_reused_authority_root() -> None:
    closure, policy, envelope, profile_anchor, _ = _closure()
    oci_fields = closure.worker_attestations[0].oci.model_dump(mode="python")
    layer = closure.worker_attestations[0].oci.ordered_layers[0]
    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapOciEvidenceV4(
            **{
                **oci_fields,
                "ordered_layers": (layer, layer),
                "config_rootfs_diff_ids_sha256": (
                    layer.diff_id_sha256,
                    layer.diff_id_sha256,
                ),
                "wrapper_layer_ordinal": 0,
            }
        )
    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapBuildWorkerTrustPolicyV4(
            **{
                **policy.model_dump(mode="python"),
                "worker_trust_anchors": (
                    policy.worker_trust_anchors[0],
                    policy.worker_trust_anchors[0],
                ),
            }
        )
    acceptance = evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
        closure=closure,
        worker_trust_policy=policy,
        profile_envelope=envelope,
        profile_trust_anchor=profile_anchor,
    )
    assert acceptance.verification_context_sha256 != acceptance.closure_sha256


def test_rejects_cross_role_identity_aliases_even_when_fully_resigned() -> None:
    """Worker, authority, execution, root, and domain identities never alias."""

    closure, policy, envelope, profile_anchor, signing_keys = _closure()
    first_anchor, second_anchor = policy.worker_trust_anchors

    # The ordinary, independent two-worker policy remains constructible and accepted.
    acceptance = evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
        closure=closure,
        worker_trust_policy=policy,
        profile_envelope=envelope,
        profile_trust_anchor=profile_anchor,
    )
    assert not acceptance.attach_allowed

    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapBuildWorkerTrustAnchorV4(
            **{
                **first_anchor.model_dump(mode="python"),
                "authority_identity_sha256": first_anchor.worker_identity_sha256,
            }
        )
    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapBuildWorkerTrustAnchorV4(
            **{
                **first_anchor.model_dump(mode="python"),
                "authority_identity_sha256": first_anchor.public_key_fingerprint_sha256,
            }
        )

    # This is the exact two-key cross-role swap: A.worker=X/A.authority=Y,
    # B.worker=Y/B.authority=X.  Each anchor is locally coherent, but the
    # externally pinned policy must reject the global identity collision.
    swapped_second_anchor = evidence.ContainerBootstrapBuildWorkerTrustAnchorV4(
        **{
            **second_anchor.model_dump(mode="python"),
            "worker_identity_sha256": first_anchor.authority_identity_sha256,
            "authority_identity_sha256": first_anchor.worker_identity_sha256,
        }
    )
    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapBuildWorkerTrustPolicyV4(
            **{
                **policy.model_dump(mode="python"),
                "worker_trust_anchors": (first_anchor, swapped_second_anchor),
            }
        )
    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapBuildWorkerTrustPolicyV4(
            **{
                **policy.model_dump(mode="python"),
                "independence_domain_sha256": first_anchor.public_key_fingerprint_sha256,
            }
        )

    # Bypass model construction solely to demonstrate that both counterfeit
    # attestations are correctly re-signed; strict validation must still reject.
    swapped_policy = (
        evidence.ContainerBootstrapBuildWorkerTrustPolicyV4.model_construct(
            **{
                **policy.model_dump(mode="python"),
                "worker_trust_anchors": (first_anchor, swapped_second_anchor),
            }
        )
    )
    swapped_second = _signed_constructed_attestation(
        closure.worker_attestations[1],
        closure.worker_attestations[1].oci,
        signing_keys[1],
        additional_fields={
            "worker_identity_sha256": swapped_second_anchor.worker_identity_sha256,
        },
    )
    swapped_closure = (
        evidence.ContainerBootstrapArtifactEvidenceClosureV4.model_construct(
            **{
                **closure.model_dump(mode="python"),
                "worker_attestations": (closure.worker_attestations[0], swapped_second),
            }
        )
    )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV4Error):
        evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
            closure=swapped_closure,
            worker_trust_policy=swapped_policy,
            profile_envelope=envelope,
            profile_trust_anchor=profile_anchor,
        )

    for aliased_execution_identity in (
        first_anchor.authority_identity_sha256,
        first_anchor.public_key_fingerprint_sha256,
    ):
        aliased_second = _signed_constructed_attestation(
            closure.worker_attestations[1],
            closure.worker_attestations[1].oci,
            signing_keys[1],
            additional_fields={
                "physical_builder_identity_sha256": aliased_execution_identity,
            },
        )
        aliased_closure = (
            evidence.ContainerBootstrapArtifactEvidenceClosureV4.model_construct(
                **{
                    **closure.model_dump(mode="python"),
                    "worker_attestations": (
                        closure.worker_attestations[0],
                        aliased_second,
                    ),
                }
            )
        )
        with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV4Error):
            evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
                closure=aliased_closure,
                worker_trust_policy=policy,
                profile_envelope=envelope,
                profile_trust_anchor=profile_anchor,
            )

    root_aliased_policy = evidence.ContainerBootstrapBuildWorkerTrustPolicyV4(
        **{
            **policy.model_dump(mode="python"),
            "independence_domain_sha256": profile_anchor.public_key_fingerprint_sha256,
        }
    )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV4Error):
        evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
            closure=closure,
            worker_trust_policy=root_aliased_policy,
            profile_envelope=envelope,
            profile_trust_anchor=profile_anchor,
        )


def test_rejects_fully_recomputed_and_resigned_duplicate_layer_closure() -> None:
    closure, policy, envelope, profile_anchor, signing_keys = _closure()
    duplicate_oci = _recomputed_duplicate_layer_oci(closure.worker_attestations[1].oci)
    duplicate_attestation = _signed_constructed_attestation(
        closure.worker_attestations[1], duplicate_oci, signing_keys[1]
    )
    duplicate_closure = (
        evidence.ContainerBootstrapArtifactEvidenceClosureV4.model_construct(
            **{
                **closure.model_dump(mode="python"),
                "worker_attestations": (
                    closure.worker_attestations[0],
                    duplicate_attestation,
                ),
            }
        )
    )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV4Error):
        evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
            closure=duplicate_closure,
            worker_trust_policy=policy,
            profile_envelope=envelope,
            profile_trust_anchor=profile_anchor,
        )
    same_descriptor_new_diff = evidence.ContainerBootstrapOciLayerDescriptorV4(
        **{
            **closure.worker_attestations[0]
            .oci.ordered_layers[0]
            .model_dump(mode="python"),
            "diff_id_sha256": _hash("different-diff-id"),
        }
    )
    duplicate_descriptor = _recomputed_duplicate_layer_oci(
        closure.worker_attestations[0].oci, same_descriptor_new_diff
    )
    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapOciEvidenceV4(
            **duplicate_descriptor.model_dump(mode="python", warnings=False)
        )


def test_rejects_fully_resigned_nul_source_path_closure() -> None:
    closure, policy, envelope, profile_anchor, signing_keys = _closure()
    original_entry = closure.worker_attestations[1].wrapper_tree_entries[0]
    nul_entry = evidence.ContainerBootstrapWrapperTreeEntryV4.model_construct(
        **{**original_entry.model_dump(mode="python"), "path": "wrapper\x00ghost"}
    )
    malformed = _signed_constructed_attestation(
        closure.worker_attestations[1],
        closure.worker_attestations[1].oci,
        signing_keys[1],
        tree_entries=(nul_entry,),
    )
    malformed_closure = (
        evidence.ContainerBootstrapArtifactEvidenceClosureV4.model_construct(
            **{
                **closure.model_dump(mode="python"),
                "worker_attestations": (closure.worker_attestations[0], malformed),
            }
        )
    )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV4Error):
        evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
            closure=malformed_closure,
            worker_trust_policy=policy,
            profile_envelope=envelope,
            profile_trust_anchor=profile_anchor,
        )


@pytest.mark.parametrize(
    "assertion", ["whiteout_entries_absent", "privilege_bits_absent"]
)
def test_rejects_missing_or_fully_signed_archive_hazard_claim(assertion: str) -> None:
    """A signed archive inspection must explicitly rule out each all-member hazard."""

    closure, policy, envelope, profile_anchor, signing_keys = _closure()
    original = closure.worker_attestations[1]
    archive_fields = original.oci.wrapper_archive_inspection.model_dump(mode="python")
    missing = dict(archive_fields)
    missing.pop(assertion)
    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapWrapperArchiveInspectionV4(**missing)
    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapWrapperArchiveInspectionV4(
            **{**archive_fields, assertion: False}
        )

    false_archive = (
        evidence.ContainerBootstrapWrapperArchiveInspectionV4.model_construct(
            **{**archive_fields, assertion: False}
        )
    )
    false_oci = evidence.ContainerBootstrapOciEvidenceV4.model_construct(
        **{
            **original.oci.model_dump(mode="python"),
            "wrapper_archive_inspection": false_archive,
        }
    )
    false_attestation = _signed_constructed_attestation(
        original, false_oci, signing_keys[1]
    )
    false_closure = (
        evidence.ContainerBootstrapArtifactEvidenceClosureV4.model_construct(
            **{
                **closure.model_dump(mode="python"),
                "worker_attestations": (
                    closure.worker_attestations[0],
                    false_attestation,
                ),
            }
        )
    )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV4Error):
        evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
            closure=false_closure,
            worker_trust_policy=policy,
            profile_envelope=envelope,
            profile_trust_anchor=profile_anchor,
        )


@pytest.mark.parametrize(
    "path",
    [
        "a/./b",
        "a//b",
        "a/b/",
        "a/../b",
        "a\\b",
        "a%2fb",
        "café",
        "wrapper\x00ghost",
        "a/\x01b",
        "a/\x1fb",
        "a/\x7fb",
        "a/\nb",
        "a/\tb",
    ],
)
def test_rejects_source_and_archive_path_normalization_aliases(path: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapWrapperTreeEntryV4(
            schema_version="rsd.container-bootstrap-wrapper-tree-entry.v4",
            path=path,
            object_sha256=_hash("path-entry"),
            mode="0644",
            entry_type="regular",
        )
    closure, _, _, _, _ = _closure()
    entry_fields = closure.worker_attestations[0].oci.wrapper_tar_entry.model_dump(
        mode="python"
    )
    archive_fields = closure.worker_attestations[
        0
    ].oci.wrapper_archive_inspection.model_dump(mode="python")
    for absolute_alias in (
        "/usr/local/libexec/./wrapper",
        "/usr/local/libexec/wrapper/.",
        "/usr/local/libexec/wrapper/",
    ):
        with pytest.raises(ValueError, match=r"."):
            evidence.ContainerBootstrapOciWrapperEntryV4(
                **{**entry_fields, "path": absolute_alias}
            )
        with pytest.raises(ValueError, match=r"."):
            evidence.ContainerBootstrapWrapperArchiveInspectionV4(
                **{**archive_fields, "wrapper_path": absolute_alias}
            )
    for valid_relative in ("a", ".cargo/config", "src/A-Z_@+-/file.rs"):
        assert (
            evidence.ContainerBootstrapWrapperTreeEntryV4(
                schema_version="rsd.container-bootstrap-wrapper-tree-entry.v4",
                path=valid_relative,
                object_sha256=_hash(f"valid-{valid_relative}"),
                mode="0644",
                entry_type="regular",
            ).path
            == valid_relative
        )
    assert (
        evidence.ContainerBootstrapOciWrapperEntryV4(**entry_fields).path
        == entry_fields["path"]
    )


def test_full_static_launch_domain_round_trips_through_two_worker_evidence() -> None:
    """Phase A accepts the complete upstream 64+64 launch domain."""

    wrapper_path = "/usr/local/libexec/omninode-rsd-bootstrap-v4"
    wrapper_prefix = (wrapper_path, *("p" * 256 for _ in range(63)))
    base_entrypoint = tuple("e" * 256 for _ in range(64))
    base_command = tuple("c" * 256 for _ in range(64))
    envelope, profile_root = _resigned_profile_envelope_for_launch(
        wrapper_argv_prefix=wrapper_prefix,
        base_entrypoint=base_entrypoint,
        base_command=base_command,
    )
    closure, policy, profile_root, _ = _closure_for_profile_envelope(
        envelope, profile_root
    )
    profile = cast(Any, envelope).static_role_profile
    first = cast(Any, closure).worker_attestations[0]

    assert len(profile.static_launch_plan.wrapper_argv_prefix) == 64
    assert len(profile.static_launch_plan.base_entrypoint) == 64
    assert len(profile.static_launch_plan.base_command) == 64
    assert len(first.oci.entrypoint) == 128
    assert len(first.oci.cmd) == 64
    assert sum(len(item.encode("ascii")) for item in first.oci.entrypoint) <= 32_768
    assert sum(len(item.encode("ascii")) for item in first.oci.cmd) <= 16_384
    assert first.oci.config_canonical_json_byte_count <= 65_536

    attestation_payload = (
        evidence.container_bootstrap_artifact_worker_attestation_v4_canonical_json(
            first
        )
    )
    closure_payload = (
        evidence.container_bootstrap_artifact_evidence_closure_v4_canonical_json(
            closure
        )
    )
    assert len(attestation_payload) <= evidence._MAX_ATTESTATION_CANONICAL_BYTES
    assert len(closure_payload) <= evidence._MAX_CLOSURE_CANONICAL_BYTES
    assert (
        evidence.parse_container_bootstrap_artifact_worker_attestation_v4_canonical_json(
            attestation_payload
        )
        == first
    )
    assert (
        evidence.parse_container_bootstrap_artifact_evidence_closure_v4_canonical_json(
            closure_payload
        )
        == closure
    )
    assert (
        evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
            closure=closure,
            worker_trust_policy=policy,
            profile_envelope=envelope,
            profile_trust_anchor=profile_root,
        ).attach_allowed
        is False
    )


def test_full_shape_evidence_is_serializer_parser_closed() -> None:
    """The full 32-layer/source-entry shape remains below every finite cap."""

    wrapper_path = "/usr/local/libexec/omninode-rsd-bootstrap-v4"
    wrapper_prefix = (wrapper_path, *("p" * 256 for _ in range(63)))
    base_entrypoint = tuple("e" * 256 for _ in range(64))
    base_command = tuple("c" * 256 for _ in range(64))
    envelope, profile_root = _resigned_profile_envelope_for_launch(
        wrapper_argv_prefix=wrapper_prefix,
        base_entrypoint=base_entrypoint,
        base_command=base_command,
    )
    closure, policy, profile_root, signing_keys = _closure_for_profile_envelope(
        envelope, profile_root
    )
    original_first, original_second = cast(Any, closure).worker_attestations
    maximum_oci = _maximum_shape_oci(
        original_first.oci,
        entrypoint=wrapper_prefix + base_entrypoint,
        cmd=base_command,
    )
    tree_entries = tuple(
        evidence.ContainerBootstrapWrapperTreeEntryV4(
            schema_version="rsd.container-bootstrap-wrapper-tree-entry.v4",
            path=_maximum_tree_path(ordinal),
            object_sha256=_hash(f"maximum-tree-entry-{ordinal}"),
            mode="0644",
            entry_type="regular",
        )
        for ordinal in range(32)
    )
    first = _signed_constructed_attestation(
        original_first, maximum_oci, signing_keys[0], tree_entries=tree_entries
    )
    second = _signed_constructed_attestation(
        original_second, maximum_oci, signing_keys[1], tree_entries=tree_entries
    )
    maximum_closure = evidence.ContainerBootstrapArtifactEvidenceClosureV4(
        schema_version="rsd.container-bootstrap-artifact-evidence-closure.v4",
        worker_attestations=(first, second),
        evidence_effect_allowed=False,
        build_allowed=False,
        materialization_allowed=False,
        attach_allowed=False,
    )

    oci_payload = evidence._canonical(maximum_oci)
    attestation_payload = (
        evidence.container_bootstrap_artifact_worker_attestation_v4_canonical_json(
            first
        )
    )
    closure_payload = (
        evidence.container_bootstrap_artifact_evidence_closure_v4_canonical_json(
            maximum_closure
        )
    )
    assert len(maximum_oci.ordered_layers) == 32
    assert len(first.wrapper_tree_entries) == 32
    assert len(maximum_oci.entrypoint) == 128
    assert len(maximum_oci.cmd) == 64
    assert len(oci_payload) <= evidence._MAX_OCI_EVIDENCE_CANONICAL_BYTES
    assert len(attestation_payload) <= evidence._MAX_ATTESTATION_CANONICAL_BYTES
    assert len(closure_payload) <= evidence._MAX_CLOSURE_CANONICAL_BYTES
    assert (
        evidence._strict(maximum_oci, evidence.ContainerBootstrapOciEvidenceV4)
        == maximum_oci
    )
    assert (
        evidence.parse_container_bootstrap_artifact_worker_attestation_v4_canonical_json(
            attestation_payload
        )
        == first
    )
    assert (
        evidence.parse_container_bootstrap_artifact_evidence_closure_v4_canonical_json(
            closure_payload
        )
        == maximum_closure
    )
    assert (
        evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
            closure=maximum_closure,
            worker_trust_policy=policy,
            profile_envelope=envelope,
            profile_trust_anchor=profile_root,
        ).materialization_allowed
        is False
    )


def test_static_64_plus_one_and_one_over_launch_boundaries() -> None:
    """A 65-item merged entrypoint is legal; 65-item source vectors are not."""

    wrapper_path = "/usr/local/libexec/omninode-rsd-bootstrap-v4"
    wrapper_prefix = (wrapper_path, *("p" * 256 for _ in range(63)))
    envelope, profile_root = _resigned_profile_envelope_for_launch(
        wrapper_argv_prefix=wrapper_prefix,
        base_entrypoint=("e",),
        base_command=("c",),
    )
    closure, policy, profile_root, _ = _closure_for_profile_envelope(
        envelope, profile_root
    )
    first = cast(Any, closure).worker_attestations[0]
    assert len(first.oci.entrypoint) == 65
    assert (
        evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
            closure=closure,
            worker_trust_policy=policy,
            profile_envelope=envelope,
            profile_trust_anchor=profile_root,
        ).build_allowed
        is False
    )

    source_envelope, _ = _profile()
    source_launch = cast(Any, source_envelope).static_role_profile.static_launch_plan
    overlong_source = (
        source_launch.wrapper_executable_path,
        *("x" * 256 for _ in range(64)),
    )
    with pytest.raises(ValueError, match=r"."):
        static_v4.ContainerBootstrapStaticLaunchPlanV4(
            **{
                **source_launch.model_dump(mode="python"),
                "wrapper_argv_prefix": overlong_source,
                "merged_argv_sha256": static_v4._merged_argv_sha256(
                    wrapper_argv_prefix=overlong_source,
                    base_entrypoint=source_launch.base_entrypoint,
                    base_command=source_launch.base_command,
                ),
            }
        )
    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapOciEvidenceV4(
            **{
                **first.oci.model_dump(mode="python"),
                "entrypoint": (wrapper_path, *("x" * 256 for _ in range(128))),
            }
        )


def test_type_specific_parser_limits_are_closed_before_unparsable_documents() -> None:
    """All constructible public documents fit their paired parser caps."""

    closure, _, _, _, _ = _closure()
    original = cast(Any, closure).worker_attestations[0]
    maximum_cmd = tuple("x" * 256 for _ in range(64))
    maximum_oci = _recomputed_oci_with_cmd(original.oci, maximum_cmd)
    bounded = evidence.ContainerBootstrapArtifactWorkerAttestationV4(
        **{**original.model_dump(mode="python"), "oci": maximum_oci}
    )
    bounded_payload = (
        evidence.container_bootstrap_artifact_worker_attestation_v4_canonical_json(
            bounded
        )
    )
    closure_payload = (
        evidence.container_bootstrap_artifact_evidence_closure_v4_canonical_json(
            closure
        )
    )
    assert len(bounded_payload) <= evidence._MAX_ATTESTATION_CANONICAL_BYTES
    assert len(closure_payload) <= evidence._MAX_CLOSURE_CANONICAL_BYTES
    assert (
        evidence.parse_container_bootstrap_artifact_worker_attestation_v4_canonical_json(
            bounded_payload
        )
        == bounded
    )
    assert (
        evidence.parse_container_bootstrap_artifact_evidence_closure_v4_canonical_json(
            closure_payload
        )
        == closure
    )

    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapOciEvidenceV4(
            **{
                **original.oci.model_dump(mode="python"),
                "cmd": ("x" * 250_261,),
            }
        )
    with pytest.raises(ValueError, match=r"."):
        evidence.ContainerBootstrapOciEvidenceV4(
            **{
                **original.oci.model_dump(mode="python"),
                "config_canonical_json_byte_count": evidence._MAX_OCI_CONFIG_JSON_BYTES
                + 1,
            }
        )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV4Error):
        evidence.parse_container_bootstrap_artifact_worker_attestation_v4_canonical_json(
            b"{" + b" " * evidence._MAX_ATTESTATION_CANONICAL_BYTES
        )
    nested = (
        b'{"x":' * (evidence._MAX_DEPTH + 1) + b"0" + b"}" * (evidence._MAX_DEPTH + 1)
    )
    with pytest.raises(ValueError, match=r"."):
        evidence._preflight(nested, max_bytes=evidence._MAX_ATTESTATION_CANONICAL_BYTES)
    excessive_nodes = (
        b"{"
        + b",".join(
            f'"key{index}":0'.encode("ascii") for index in range(evidence._MAX_NODES)
        )
        + b"}"
    )
    with pytest.raises(ValueError, match=r"."):
        evidence._preflight(
            excessive_nodes, max_bytes=evidence._MAX_ATTESTATION_CANONICAL_BYTES
        )


def test_fixed_public_vector_exercises_complete_graph_without_regeneration() -> None:
    assert hashlib.sha256(_VECTOR.read_bytes()).hexdigest() == (
        "248bdc9d3aedb18f7b84feb93e28faaa6d6082602d1f0a03e4d4095da75b1a68"
    )
    vector = _strict_vector()
    envelope = static_v4.parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json(
        _decoded(vector["profile_envelope_canonical_json_utf8_base64"])
    )
    profile_root = static_v4.parse_container_bootstrap_static_profile_trust_anchor_v4_canonical_json(
        _decoded(vector["profile_root_canonical_json_utf8_base64"])
    )
    policy = evidence._parse(
        _decoded(vector["worker_trust_policy_canonical_json_utf8_base64"]),
        evidence.ContainerBootstrapBuildWorkerTrustPolicyV4,
    )
    first = evidence.parse_container_bootstrap_artifact_worker_attestation_v4_canonical_json(
        _decoded(vector["worker_attestation_a_canonical_json_utf8_base64"])
    )
    second = evidence.parse_container_bootstrap_artifact_worker_attestation_v4_canonical_json(
        _decoded(vector["worker_attestation_b_canonical_json_utf8_base64"])
    )
    closure = (
        evidence.parse_container_bootstrap_artifact_evidence_closure_v4_canonical_json(
            _decoded(vector["closure_canonical_json_utf8_base64"])
        )
    )
    assert closure.worker_attestations == (first, second)
    assert _decoded(vector["attestation_signature_domain_message_a_base64"]) == (
        evidence.container_bootstrap_artifact_worker_attestation_v4_message(first)
    )
    assert _decoded(vector["attestation_signature_domain_message_b_base64"]) == (
        evidence.container_bootstrap_artifact_worker_attestation_v4_message(second)
    )
    for attestation, anchor in zip(
        closure.worker_attestations, policy.worker_trust_anchors, strict=True
    ):
        Ed25519PublicKey.from_public_bytes(
            base64.b64decode(anchor.public_key_base64, validate=True)
        ).verify(
            base64.b64decode(attestation.signature_base64, validate=True),
            evidence.container_bootstrap_artifact_worker_attestation_v4_message(
                attestation
            ),
        )
    acceptance = evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
        closure=closure,
        worker_trust_policy=policy,
        profile_envelope=envelope,
        profile_trust_anchor=profile_root,
    )
    expected = (
        evidence.ContainerBootstrapArtifactEvidenceAcceptanceV4.model_validate_json(
            _decoded(vector["acceptance_canonical_json_utf8_base64"]), strict=True
        )
    )
    assert acceptance == expected
    assert acceptance.closure_sha256 == vector["closure_sha256"]
    assert (
        acceptance.verification_context_sha256 == vector["verification_context_sha256"]
    )
    with pytest.raises(evidence.ContainerBootstrapArtifactEvidenceV4Error):
        evidence.validate_container_bootstrap_artifact_evidence_closure_v4(
            closure=closure,
            worker_trust_policy=evidence.ContainerBootstrapBuildWorkerTrustPolicyV4(
                **{
                    **policy.model_dump(mode="python"),
                    "worker_trust_anchors": tuple(
                        reversed(policy.worker_trust_anchors)
                    ),
                }
            ),
            profile_envelope=envelope,
            profile_trust_anchor=profile_root,
        )


def _walk(value: object) -> list[object]:
    found = [value]
    if isinstance(value, dict):
        for nested in value.values():
            found.extend(_walk(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_walk(nested))
    return found


def test_public_vector_has_no_private_or_live_material() -> None:
    raw = _VECTOR.read_bytes()
    forbidden_environment = b"." + b"env"
    forbidden_signing_material = b"PRI" + b"VATE"
    assert forbidden_environment not in raw
    assert b"BEGIN" not in raw
    assert forbidden_signing_material not in raw
    parsed = _strict_vector()
    decoded_segments = [
        _decoded(value)
        for value in parsed.values()
        if type(value) is dict and set(value) == {"encoding", "segments"}
    ]
    assert any(b"example.invalid" in value for value in decoded_segments)
    for node in _walk(parsed):
        if isinstance(node, str):
            assert not node.startswith(("/" + "Users/", "/" + "Volumes/"))
            assert not any(
                token in node.lower() for token in ("password", "credential", "receipt")
            )
    for decoded in decoded_segments:
        assert b"/" + b"Users/" not in decoded
        assert forbidden_environment not in decoded
        assert forbidden_signing_material not in decoded
