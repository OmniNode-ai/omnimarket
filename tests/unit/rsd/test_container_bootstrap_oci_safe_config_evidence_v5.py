"""Focused contract coverage for isolated, non-authorizing Phase-A V5 OCI evidence."""

# Upstream vector coverage intentionally retains its tuple parameter spelling.
# ruff: noqa: PT006, PT007

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from copy import deepcopy
from typing import Literal

import pytest
from pydantic import ValidationError

from omnimarket.rsd import (
    container_bootstrap_oci_safe_config_evidence_v5 as evidence_v5,
)
from omnimarket.rsd import oci_config_commitment

_REPOSITORY = "registry.example.invalid/omninode/rsd"
_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
_LAYER_MEDIA_TYPE: Literal["application/vnd.oci.image.layer.v1.tar+gzip"] = (
    "application/vnd.oci.image.layer.v1.tar+gzip"
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _config(*, suffix: str = "") -> dict[str, object]:
    return {
        "architecture": "amd64",
        "config": {
            "Cmd": ["serve", "--foreground" + suffix],
            "Entrypoint": ["/usr/local/bin/rsd-bootstrap"],
            "Env": ["APP_MODE=production" + suffix, "PATH=/usr/bin:/bin"],
            "User": "12345:23456",
            "WorkingDir": "/opt/rsd" + suffix,
        },
        "os": "linux",
        "rootfs": {
            "diff_ids": ["sha256:" + "b" * 64, "sha256:" + "d" * 64],
            "type": "layers",
        },
    }


def _archive() -> evidence_v5.ContainerBootstrapOciSafeConfigWrapperArchiveInspectionV5:
    return evidence_v5.ContainerBootstrapOciSafeConfigWrapperArchiveInspectionV5(
        schema_version="rsd.container-bootstrap-oci-safe-config-wrapper-archive-inspection.v5",
        archive_entry_count=1,
        inspected_layer_digest_sha256="c" * 64,
        wrapper_path="/usr/local/bin/rsd-bootstrap",
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


def _evidence(
    *,
    repository: str = _REPOSITORY,
    config: dict[str, object] | None = None,
) -> tuple[evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5, bytes]:
    raw_config = _canonical(_config() if config is None else config)
    claim = oci_config_commitment.derive_phase_a_v5_expanded_oci_config_claim_v1(
        raw_config,
        _CONFIG_MEDIA_TYPE,
        "sha256:" + _digest(raw_config),
        len(raw_config),
    )
    layers = (
        evidence_v5.ContainerBootstrapOciSafeConfigLayerDescriptorV5(
            schema_version="rsd.container-bootstrap-oci-safe-config-layer-descriptor.v5",
            media_type=_LAYER_MEDIA_TYPE,
            digest_sha256="a" * 64,
            byte_count=11,
            diff_id_sha256="b" * 64,
        ),
        evidence_v5.ContainerBootstrapOciSafeConfigLayerDescriptorV5(
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
    wrapper = evidence_v5.ContainerBootstrapOciSafeConfigWrapperEntryV5(
        schema_version="rsd.container-bootstrap-oci-safe-config-wrapper-entry.v5",
        path="/usr/local/bin/rsd-bootstrap",
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
    value = evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5(
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
        wrapper_archive_inspection=_archive(),
        entrypoint=("/usr/local/bin/rsd-bootstrap",),
        cmd=(
            "serve",
            "--foreground" if config is None else str(config["config"]["Cmd"][1]),
        ),  # type: ignore[index]
        expanded_oci_config_claim=claim,
        non_authorizing=True,
        evidence_effect_allowed=False,
        build_allowed=False,
        materialization_allowed=False,
        attach_allowed=False,
    )
    return value, raw_config


def _verify(
    value: evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5, raw_config: bytes
) -> evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5:
    return evidence_v5.verify_container_bootstrap_oci_safe_config_evidence_v5_internal_consistency(
        value, raw_config
    )


def _with_index_document(
    value: evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5,
    document: dict[str, object],
) -> evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5:
    raw = _canonical(document)
    digest = _digest(raw)
    return value.model_copy(
        update={
            "index_digest_sha256": digest,
            "index_canonical_json_sha256": digest,
            "index_canonical_json_byte_count": len(raw),
            "index_canonical_json_utf8_base64": base64.b64encode(raw).decode("ascii"),
        }
    )


def _with_manifest_document(
    value: evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5,
    document: dict[str, object],
) -> evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5:
    manifest = _canonical(document)
    manifest_digest = _digest(manifest)
    index = json.loads(base64.b64decode(value.index_canonical_json_utf8_base64))
    assert isinstance(index, dict)
    descriptors = index["manifests"]
    assert isinstance(descriptors, list)
    descriptor = descriptors[0]
    assert isinstance(descriptor, dict)
    descriptor["digest"] = "sha256:" + manifest_digest
    descriptor["size"] = len(manifest)
    indexed = _with_index_document(value, index)
    return indexed.model_copy(
        update={
            "derived_reference": value.derived_repository
            + "@sha256:"
            + manifest_digest,
            "selected_manifest_descriptor_digest_sha256": manifest_digest,
            "linux_amd64_manifest_digest_sha256": manifest_digest,
            "manifest_canonical_json_sha256": manifest_digest,
            "manifest_canonical_json_byte_count": len(manifest),
            "manifest_canonical_json_utf8_base64": base64.b64encode(manifest).decode(
                "ascii"
            ),
        }
    )


def _with_first_layer_size_one(
    value: evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5,
) -> evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5:
    first, second = value.ordered_layers
    first_size_one = first.model_copy(update={"byte_count": 1})
    document = json.loads(base64.b64decode(value.manifest_canonical_json_utf8_base64))
    assert isinstance(document, dict)
    layers = document["layers"]
    assert isinstance(layers, list)
    first_descriptor = layers[0]
    assert isinstance(first_descriptor, dict)
    first_descriptor["size"] = 1
    return _with_manifest_document(value, document).model_copy(
        update={"ordered_layers": (first_size_one, second)}
    )


def _invalid(
    value: evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5,
    raw_config: bytes,
) -> None:
    with pytest.raises(evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5Error):
        _verify(value, raw_config)


def test_complete_graph_is_verified_canonical_and_hashed() -> None:
    value, raw_config = _evidence()

    _verify(value, raw_config)
    payload = (
        evidence_v5.container_bootstrap_oci_safe_config_evidence_v5_canonical_json(
            value
        )
    )
    parsed = evidence_v5.parse_container_bootstrap_oci_safe_config_evidence_v5_canonical_json(
        payload
    )

    assert parsed == value
    assert (
        evidence_v5.strict_canonical_container_bootstrap_oci_safe_config_evidence_v5(
            value
        )
        == value
    )
    assert (
        evidence_v5.container_bootstrap_oci_safe_config_evidence_v5_sha256(value)
        == hashlib.sha256(
            b"omninode-rsd.container-bootstrap-oci-safe-config-evidence.sha256.v5\x00"
            + payload
        ).hexdigest()
    )
    serialized = payload.decode("ascii")
    for raw in ("12345:23456", "/opt/rsd", "APP_MODE=production", "PATH=/usr/bin:/bin"):
        assert raw not in serialized


@pytest.mark.parametrize(
    "mutation", ("zero", "duplicate", "extra", "platform", "media", "digest", "size")
)
def test_rejects_exact_index_duplicate_zero_extra_platform_media_digest_and_size_shapes(
    mutation: str,
) -> None:
    value, raw_config = _evidence()
    document = json.loads(base64.b64decode(value.index_canonical_json_utf8_base64))
    assert isinstance(document, dict)
    manifests = document["manifests"]
    assert isinstance(manifests, list)
    descriptor = manifests[0]
    assert isinstance(descriptor, dict)
    if mutation == "zero":
        document["manifests"] = []
    elif mutation == "duplicate":
        manifests.append(deepcopy(descriptor))
    elif mutation == "extra":
        document["annotations"] = {"x": "y"}
    elif mutation == "platform":
        platform = descriptor["platform"]
        assert isinstance(platform, dict)
        platform["architecture"] = "arm64"
    elif mutation == "media":
        descriptor["mediaType"] = _INDEX_MEDIA_TYPE
    elif mutation == "digest":
        descriptor["digest"] = "sha256:" + "f" * 64
    else:
        descriptor["size"] = 1

    _invalid(_with_index_document(value, document), raw_config)


@pytest.mark.parametrize(
    "mutation", ("placement", "media", "digest", "size", "manifest-media", "layers")
)
def test_rejects_exact_manifest_config_tuple_placement_media_digest_size_and_layers(
    mutation: str,
) -> None:
    value, raw_config = _evidence()
    document = json.loads(base64.b64decode(value.manifest_canonical_json_utf8_base64))
    assert isinstance(document, dict)
    config = document["config"]
    assert isinstance(config, dict)
    if mutation == "placement":
        document["replacement"] = document.pop("config")
    elif mutation == "media":
        config["mediaType"] = _MANIFEST_MEDIA_TYPE
    elif mutation == "digest":
        config["digest"] = "sha256:" + "f" * 64
    elif mutation == "size":
        config["size"] = 2
    elif mutation == "manifest-media":
        document["mediaType"] = _INDEX_MEDIA_TYPE
    else:
        document["layers"] = []

    _invalid(_with_manifest_document(value, document), raw_config)


@pytest.mark.parametrize(
    "document_kind, field",
    (
        ("index", "schemaVersion"),
        ("index", "size"),
        ("manifest", "schemaVersion"),
        ("manifest", "config-size"),
        ("manifest", "layer-size"),
    ),
)
def test_rejects_bool_for_every_index_and_manifest_integer_slot(
    document_kind: str, field: str
) -> None:
    value, raw_config = _evidence()
    if document_kind == "index":
        document = json.loads(base64.b64decode(value.index_canonical_json_utf8_base64))
        assert isinstance(document, dict)
        if field == "schemaVersion":
            document[field] = True
        else:
            manifests = document["manifests"]
            assert isinstance(manifests, list)
            descriptor = manifests[0]
            assert isinstance(descriptor, dict)
            descriptor["size"] = True
        _invalid(_with_index_document(value, document), raw_config)
        return
    document = json.loads(base64.b64decode(value.manifest_canonical_json_utf8_base64))
    assert isinstance(document, dict)
    if field == "schemaVersion":
        document[field] = True
    elif field == "config-size":
        config = document["config"]
        assert isinstance(config, dict)
        config["size"] = True
    else:
        layers = document["layers"]
        assert isinstance(layers, list)
        descriptor = layers[0]
        assert isinstance(descriptor, dict)
        descriptor["size"] = True
    _invalid(_with_manifest_document(value, document), raw_config)


def test_rejects_bool_for_one_byte_layer_size_without_python_integer_aliasing() -> None:
    value, raw_config = _evidence()
    one_byte_layer = _with_first_layer_size_one(value)
    _verify(one_byte_layer, raw_config)
    document = json.loads(
        base64.b64decode(one_byte_layer.manifest_canonical_json_utf8_base64)
    )
    assert isinstance(document, dict)
    layers = document["layers"]
    assert isinstance(layers, list)
    descriptor = layers[0]
    assert isinstance(descriptor, dict)
    descriptor["size"] = True
    bool_document = _with_manifest_document(one_byte_layer, document)
    bool_layer_model = one_byte_layer.model_copy(
        update={
            "ordered_layers": (
                one_byte_layer.ordered_layers[0].model_copy(
                    update={"byte_count": True}
                ),
                one_byte_layer.ordered_layers[1],
            )
        }
    )

    _invalid(bool_document, raw_config)
    _invalid(bool_layer_model, raw_config)


def test_rejects_bool_for_all_model_and_claim_integer_slots() -> None:
    value, raw_config = _evidence()
    cases = (
        value.model_copy(update={"index_canonical_json_byte_count": True}),
        value.model_copy(update={"manifest_canonical_json_byte_count": True}),
        value.model_copy(update={"wrapper_layer_ordinal": True}),
        value.model_copy(
            update={
                "wrapper_tar_entry": value.wrapper_tar_entry.model_copy(
                    update={"byte_count": True}
                )
            }
        ),
        value.model_copy(
            update={
                "wrapper_archive_inspection": value.wrapper_archive_inspection.model_copy(
                    update={"archive_entry_count": True}
                )
            }
        ),
        *(
            value.model_copy(
                update={
                    "expanded_oci_config_claim": value.expanded_oci_config_claim.model_copy(
                        update={field: True}
                    )
                }
            )
            for field in (
                "oci_config_descriptor_size",
                "runtime_uid",
                "runtime_gid",
                "user_byte_count",
                "working_dir_byte_count",
                "environment_entry_count",
                "environment_rendered_byte_count",
            )
        ),
    )

    for candidate in cases:
        _invalid(candidate, raw_config)


@pytest.mark.parametrize(
    "field, replacement",
    (
        ("config_rootfs_diff_ids_sha256", ("d" * 64, "b" * 64)),
        ("wrapper_layer_ordinal", 0),
        ("wrapper_layer_digest_sha256", "a" * 64),
        ("entrypoint", ("/bin/other",)),
        ("cmd", ("serve", "--mutated")),
        ("non_authorizing", False),
        ("evidence_effect_allowed", True),
        ("build_allowed", True),
        ("materialization_allowed", True),
        ("attach_allowed", True),
    ),
)
def test_rejects_layer_wrapper_argv_and_effect_mutations(
    field: str, replacement: object
) -> None:
    value, raw_config = _evidence()

    _invalid(value.model_copy(update={field: replacement}), raw_config)


def test_rejects_layer_reordering_duplicates_and_diff_id_aliases() -> None:
    value, raw_config = _evidence()
    first = value.ordered_layers[0]
    duplicate = value.model_copy(
        update={
            "ordered_layers": (first, first),
            "config_rootfs_diff_ids_sha256": (
                first.diff_id_sha256,
                first.diff_id_sha256,
            ),
            "wrapper_layer_digest_sha256": first.digest_sha256,
            "wrapper_layer_ordinal": 1,
            "wrapper_archive_inspection": value.wrapper_archive_inspection.model_copy(
                update={"inspected_layer_digest_sha256": first.digest_sha256}
            ),
        }
    )
    reordered = value.model_copy(
        update={
            "ordered_layers": tuple(reversed(value.ordered_layers)),
            "config_rootfs_diff_ids_sha256": tuple(
                reversed(value.config_rootfs_diff_ids_sha256)
            ),
            "wrapper_layer_digest_sha256": value.ordered_layers[0].digest_sha256,
            "wrapper_archive_inspection": value.wrapper_archive_inspection.model_copy(
                update={
                    "inspected_layer_digest_sha256": value.ordered_layers[
                        0
                    ].digest_sha256
                }
            ),
        }
    )

    _invalid(duplicate, raw_config)
    _invalid(reordered, raw_config)


@pytest.mark.parametrize(
    "field, replacement",
    (
        ("uid", 1),
        ("uid", True),
        ("gid", 1),
        ("gid", True),
        ("mode", "0755"),
        ("entry_type", "symlink"),
        ("link_count", 2),
        ("link_count", True),
        ("symlink", True),
        ("hardlink", True),
        ("setuid", True),
        ("setgid", True),
        ("sticky", True),
    ),
)
def test_rejects_wrapper_archive_and_permission_safety_mutations(
    field: str, replacement: object
) -> None:
    value, raw_config = _evidence()
    if field in evidence_v5.ContainerBootstrapOciSafeConfigWrapperEntryV5.model_fields:
        mutated = value.model_copy(
            update={
                "wrapper_tar_entry": value.wrapper_tar_entry.model_copy(
                    update={field: replacement}
                )
            }
        )
    else:
        mutated = value.model_copy(
            update={
                "wrapper_archive_inspection": value.wrapper_archive_inspection.model_copy(
                    update={field: replacement}
                )
            }
        )

    _invalid(mutated, raw_config)


@pytest.mark.parametrize(
    "field",
    tuple(
        evidence_v5.ContainerBootstrapOciSafeConfigWrapperArchiveInspectionV5.model_fields
    )[4:],
)
def test_rejects_every_archive_safety_flag(field: str) -> None:
    value, raw_config = _evidence()
    mutated = value.model_copy(
        update={
            "wrapper_archive_inspection": value.wrapper_archive_inspection.model_copy(
                update={field: False}
            )
        }
    )

    _invalid(mutated, raw_config)


@pytest.mark.parametrize(
    "field, replacement",
    (("wrapper_matching_entry_count", True), ("wrapper_path", "/usr/local/bin/other")),
)
def test_rejects_archive_exact_count_type_and_wrapper_path(
    field: str, replacement: object
) -> None:
    value, raw_config = _evidence()
    mutated = value.model_copy(
        update={
            "wrapper_archive_inspection": value.wrapper_archive_inspection.model_copy(
                update={field: replacement}
            )
        }
    )

    _invalid(mutated, raw_config)


def test_transplantation_is_rejected_for_another_image_claim_manifest_or_repository() -> (
    None
):
    first, first_raw = _evidence()
    second_config = _config(suffix="-b")
    second, _second_raw = _evidence(
        repository="registry.example.invalid/omninode/other",
        config=second_config,
    )
    claim_transplant = first.model_copy(
        update={"expanded_oci_config_claim": second.expanded_oci_config_claim}
    )

    _invalid(claim_transplant, first_raw)
    # A self-consistent raw image remains deliberately unauthenticated even
    # when callers can copy its repository/reference fields from itself.
    assert _verify(first, first_raw) == first


@pytest.mark.parametrize(
    "mutation", ("Entrypoint", "Cmd", "Env", "User", "WorkingDir", "rootfs")
)
def test_raw_config_mutations_are_rejected_without_value_reflection(
    mutation: str,
) -> None:
    value, _raw_config = _evidence()
    config = deepcopy(_config())
    image_config = config["config"]
    assert isinstance(image_config, dict)
    if mutation == "rootfs":
        rootfs = config["rootfs"]
        assert isinstance(rootfs, dict)
        rootfs["diff_ids"] = ["sha256:" + "f" * 64]
    elif mutation == "Entrypoint":
        image_config[mutation] = ["/bin/other"]
    elif mutation == "Cmd":
        image_config[mutation] = ["serve", "--other"]
    elif mutation == "Env":
        image_config[mutation] = ["APP_MODE=staging", "PATH=/usr/bin:/bin"]
    elif mutation == "User":
        image_config[mutation] = "34567:45678"
    else:
        image_config[mutation] = "/srv/rsd"
    raw_config = _canonical(config)

    with pytest.raises(
        evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5Error
    ) as error:
        _verify(value, raw_config)
    assert "34567:45678" not in str(error.value)
    assert "/srv/rsd" not in str(error.value)
    assert "APP_MODE=staging" not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    (
        b"{",
        b'{"schema_version":1.0}',
        b'{"schema_version":"x","schema_version":"x"}',
        b'{ "schema_version":"x"}',
        b'{"schema_\\u0076ersion":"x"}',
        b"\xff",
        b'{"x":' + b"[" * 25 + b"]" * 25 + b"}",
        b"{" + b'"x":0,' * 4097 + b'"z":0}',
        b"{" + b" " * 131_072 + b"}",
    ),
)
def test_canonical_parser_rejects_invalid_utf8_float_duplicate_escaped_deep_and_large_json(
    payload: bytes,
) -> None:
    with pytest.raises(evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5Error):
        evidence_v5.parse_container_bootstrap_oci_safe_config_evidence_v5_canonical_json(
            payload
        )


def test_static_surface_has_no_runtime_or_external_effect_api() -> None:
    source = inspect.getsource(evidence_v5)

    for forbidden in (
        "import socket",
        "import subprocess",
        "import requests",
        "import docker",
    ):
        assert forbidden not in source
    assert "os.environ" not in source
    assert "open(" not in source


def test_aggregate_bound_and_strict_bool_rejection() -> None:
    value, raw_config = _evidence()
    payload = (
        evidence_v5.container_bootstrap_oci_safe_config_evidence_v5_canonical_json(
            value
        )
    )

    with pytest.raises(evidence_v5.ContainerBootstrapOciSafeConfigEvidenceV5Error):
        evidence_v5.parse_container_bootstrap_oci_safe_config_evidence_v5_canonical_json(
            payload + b" "
        )
    with pytest.raises(ValidationError):
        evidence_v5.ContainerBootstrapOciSafeConfigLayerDescriptorV5(
            schema_version="rsd.container-bootstrap-oci-safe-config-layer-descriptor.v5",
            media_type=_LAYER_MEDIA_TYPE,
            digest_sha256="a" * 64,
            byte_count=True,
            diff_id_sha256="b" * 64,
        )
    _verify(value, raw_config)
