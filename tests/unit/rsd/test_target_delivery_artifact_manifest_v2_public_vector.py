"""Immutable, fail-closed public expected-acceptance coverage for B2 V2.

The fixture composes two immutable public sources: the existing B2 V1 vector
owns the full B1/V4 map, projection, profiles, and bindings, while the V5
vector owns the original four worker closures.  This vector carries only the
separately signed V2 manifest and expected acceptance; it derives upstream
evidence from the pinned snapshots and never copies the legacy B1 map payload
into a new fixture.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import inspect
import json
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from omnimarket.rsd.historical_target_delivery_map_v1 import TargetDeliveryMapV1
from pydantic import BaseModel
from yaml.events import AliasEvent
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

from omnimarket.rsd import b1_projection_binding as binding
from omnimarket.rsd import container_bootstrap_artifact_evidence_v5 as evidence_v5
from omnimarket.rsd import historical_target_delivery_map_v1 as map_signing
from omnimarket.rsd import (
    target_delivery_artifact_manifest_trust_anchor_v1 as manifest_v1,
)
from omnimarket.rsd import target_delivery_artifact_manifest_v2 as manifest_v2
from omnimarket.rsd import v4_static_profile as static_v4

_ROOT = Path(__file__).parents[3] / "src/omnimarket/rsd/vectors"
_VECTOR = _ROOT / "target_delivery_artifact_manifest_v2_public_vector.yaml"
_B2_V1_VECTOR = _ROOT / "target_delivery_artifact_manifest_public_vector.yaml"
_V5_VECTOR = _ROOT / "container_bootstrap_artifact_evidence_v5_public_vector.yaml"
_COMPONENTS = (
    "primary_infisical",
    "primary_valkey",
    "restore_infisical",
    "restore_valkey",
)
_MAX_VECTOR_BYTES = 2 * 1024 * 1024
_MAX_VECTOR_DEPTH = 32
_MAX_VECTOR_NODES = 32_768
_VECTOR_SHA256 = "5b91cfb95243403b3ddc234d154df355f46787f0c3ea49613d5bf404b1a72237"
_VECTOR_BYTE_COUNT = 52_693
_VECTOR_LINE_COUNT = 667
_CANONICAL_YAML_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_CANONICAL_YAML_BOOLEANS = frozenset({"true", "false"})
_YAML_11_BOOLEAN_LIKE = re.compile(
    r"(?:y|yes|n|no|true|false|on|off)\Z", re.ASCII | re.IGNORECASE
)
_YAML_NULL_LIKE = re.compile(r"(?:~|null)\Z", re.ASCII | re.IGNORECASE)
_YAML_SPECIAL_FLOAT_LIKE = re.compile(
    r"[+-]?(?:\.(?:inf(?:inity)?|nan)|(?:inf(?:inity)?|nan))\Z",
    re.ASCII | re.IGNORECASE,
)
_YAML_NUMERIC_LIKE = re.compile(
    r"""
    [+-]?(?:
        0[xX][0-9A-Fa-f]+ |
        0[oO][0-7]+ |
        0[bB][01]+ |
        [0-9][0-9_]*
        (?::[0-9][0-9_]*(?::[0-9][0-9_]*)*)?
        (?:\.[0-9_]*)?
        (?:[eE][+-]?[0-9_]+)? |
        \.[0-9_]+(?:[eE][+-]?[0-9_]+)?
    )
    \Z
    """,
    re.ASCII | re.VERBOSE,
)
_YAML_TIMESTAMP_LIKE = re.compile(
    r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt \t].*)?\Z", re.ASCII
)
_YAML_BOOL_TAG = "tag:yaml.org,2002:bool"
_YAML_FLOAT_TAG = "tag:yaml.org,2002:float"
_YAML_INT_TAG = "tag:yaml.org,2002:int"
_YAML_NULL_TAG = "tag:yaml.org,2002:null"
_YAML_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_PRIVATE_OR_INTERNAL_ENDPOINT = re.compile(
    r"""(?ix)
    (?:
        \b(?:10|127)[.]\d{1,3}[.]\d{1,3}[.]\d{1,3}\b |
        \b172[.](?:1[6-9]|2\d|3[01])[.]\d{1,3}[.]\d{1,3}\b |
        \b192[.]168[.]\d{1,3}[.]\d{1,3}\b |
        (?:[a-z0-9-]+[.])+(?:local|lan|internal|corp|home|intranet)\b
    )
    """
)
_CREDENTIAL_URI = re.compile(r"(?i)[a-z][a-z0-9+.-]*://[^/\s@]+@")


class _StrictVectorLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate and merge mapping keys."""


def _strict_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if type(key) is not str or key in result or key == "<<":
            raise ValueError("vector mapping is invalid")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictVectorLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_mapping
)


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


def _validate_plain_yaml_scalar_portability(value: str) -> None:
    """Require a scalar spelling stable across YAML 1.1 and 1.2."""

    if value in _CANONICAL_YAML_BOOLEANS or _CANONICAL_YAML_INTEGER.fullmatch(value):
        return
    if (
        not value
        or _YAML_11_BOOLEAN_LIKE.fullmatch(value) is not None
        or _YAML_NULL_LIKE.fullmatch(value) is not None
        or _YAML_SPECIAL_FLOAT_LIKE.fullmatch(value) is not None
        or _YAML_NUMERIC_LIKE.fullmatch(value) is not None
        or _YAML_TIMESTAMP_LIKE.fullmatch(value) is not None
    ):
        raise ValueError("vector scalar spelling is invalid")


def _validate_yaml_scalar_spellings(node: Node) -> None:
    if isinstance(node, ScalarNode):
        if node.style is None:
            _validate_plain_yaml_scalar_portability(node.value)
        if node.tag == _YAML_BOOL_TAG:
            if node.style is not None or node.value not in _CANONICAL_YAML_BOOLEANS:
                raise ValueError("vector scalar spelling is invalid")
        elif node.tag == _YAML_INT_TAG:
            if (
                node.style is not None
                or _CANONICAL_YAML_INTEGER.fullmatch(node.value) is None
            ):
                raise ValueError("vector scalar spelling is invalid")
        elif node.tag in {_YAML_FLOAT_TAG, _YAML_NULL_TAG, _YAML_TIMESTAMP_TAG}:
            raise ValueError("vector scalar spelling is invalid")
        return
    if isinstance(node, SequenceNode):
        for child in node.value:
            _validate_yaml_scalar_spellings(child)
        return
    if isinstance(node, MappingNode):
        for key_node, value_node in node.value:
            _validate_yaml_scalar_spellings(key_node)
            _validate_yaml_scalar_spellings(value_node)
        return
    raise ValueError("vector YAML node is invalid")


def _safe_tree(
    value: object, *, depth: int = 0, nodes: list[int] | None = None
) -> None:
    if depth > _MAX_VECTOR_DEPTH:
        raise ValueError("vector exceeds depth")
    count = [0] if nodes is None else nodes
    count[0] += 1
    if count[0] > _MAX_VECTOR_NODES:
        raise ValueError("vector exceeds node limit")
    if type(value) in (str, int, bool):
        if type(value) is str and not value.isascii():
            raise ValueError("vector contains non-ASCII text")
        return
    if type(value) is list:
        for item in cast(list[object], value):
            _safe_tree(item, depth=depth + 1, nodes=count)
        return
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str or not key.isascii():
                raise ValueError("vector key is invalid")
            _safe_tree(item, depth=depth + 1, nodes=count)
        return
    raise ValueError("vector scalar is invalid")


def _load_yaml(raw: bytes) -> dict[str, object]:
    if not 1 <= len(raw) <= _MAX_VECTOR_BYTES or not raw.isascii():
        raise ValueError("vector bytes are invalid")
    text = raw.decode("ascii")
    if not text.endswith("\n") or any(
        line.endswith((" ", "\t")) for line in text.splitlines()
    ):
        raise ValueError("vector whitespace is invalid")
    for token in yaml.scan(text):
        if isinstance(token, (AliasToken, AnchorToken, TagToken)):
            raise ValueError("vector YAML feature is invalid")
    if any(isinstance(event, AliasEvent) for event in yaml.parse(text)):
        raise ValueError("vector YAML feature is invalid")
    nodes = list(yaml.compose_all(text, Loader=_StrictVectorLoader))
    if len(nodes) != 1 or nodes[0] is None:
        raise ValueError("vector document is invalid")
    _validate_yaml_scalar_spellings(nodes[0])
    parsed = yaml.load(text, Loader=_StrictVectorLoader)
    if type(parsed) is not dict:
        raise ValueError("vector document is invalid")
    result = cast(dict[str, object], parsed)
    _safe_tree(result)
    return result


def _mapping(value: object, expected: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != expected:
        raise ValueError("vector mapping shape is invalid")
    return cast(dict[str, object], value)


def _string(value: object) -> str:
    if type(value) is not str or not value.isascii():
        raise ValueError("vector string is invalid")
    return cast(str, value)


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("vector integer is invalid")
    return cast(int, value)


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("vector boolean is invalid")
    return cast(bool, value)


def _bytes(value: object) -> bytes:
    item = _mapping(value, {"encoding", "segments"})
    if (
        item["encoding"] != "standard_base64_fixed_segments_v1"
        or type(item["segments"]) is not list
    ):
        raise ValueError("vector base64 envelope is invalid")
    segments = cast(list[object], item["segments"])
    if not segments or any(
        type(segment) is not str or not segment.isascii() for segment in segments
    ):
        raise ValueError("vector base64 segments are invalid")
    rendered = cast(list[str], segments)
    if (
        any(len(segment) != 76 for segment in rendered[:-1])
        or not 1 <= len(rendered[-1]) <= 76
    ):
        raise ValueError("vector base64 segmentation is invalid")
    encoded = "".join(rendered)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("vector base64 is invalid") from None
    if base64.b64encode(decoded).decode("ascii") != encoded:
        raise ValueError("vector base64 is invalid")
    return decoded


def _vector_bytes(payload: bytes) -> dict[str, object]:
    encoded = base64.b64encode(payload).decode("ascii")
    return {
        "encoding": "standard_base64_fixed_segments_v1",
        "segments": [
            encoded[index : index + 76] for index in range(0, len(encoded), 76)
        ],
    }


def _tuples(value: object) -> object:
    if type(value) is list:
        return tuple(_tuples(item) for item in cast(list[object], value))
    if type(value) is dict:
        return {
            key: _tuples(item) for key, item in cast(dict[str, object], value).items()
        }
    return value


def _canonical_json(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json", warnings="error"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _dependency(value: object, *, filename: str, sha256: str) -> bytes:
    item = _mapping(value, {"filename", "sha256", "byte_count", "line_count"})
    if (
        item["filename"] != filename
        or item["sha256"] != sha256
        or _integer(item["byte_count"]) < 1
        or _integer(item["line_count"]) < 1
    ):
        raise ValueError("immutable dependency pin is invalid")
    path = _ROOT / filename
    try:
        file_mode = path.lstat().st_mode
    except OSError:
        raise ValueError("immutable dependency is unavailable") from None
    if not stat.S_ISREG(file_mode):
        raise ValueError("immutable dependency is not a regular file")
    raw = path.read_bytes()
    if (
        hashlib.sha256(raw).hexdigest() != sha256
        or len(raw) != item["byte_count"]
        or raw.count(b"\n") != item["line_count"]
    ):
        raise ValueError("immutable dependency bytes are invalid")
    return raw


def _immutable_vector_snapshot() -> bytes:
    try:
        file_mode = _VECTOR.lstat().st_mode
    except OSError:
        raise ValueError("immutable B2 V2 public vector is unavailable") from None
    if not stat.S_ISREG(file_mode):
        raise ValueError("immutable B2 V2 public vector is not a regular file")
    raw = _VECTOR.read_bytes()
    if (
        hashlib.sha256(raw).hexdigest() != _VECTOR_SHA256
        or len(raw) != _VECTOR_BYTE_COUNT
        or raw.count(b"\n") != _VECTOR_LINE_COUNT
    ):
        raise ValueError("immutable B2 V2 public vector hash is invalid")
    return raw


def _verify_ed25519(
    *, public_key_base64: str, signature_base64: str, message: bytes
) -> None:
    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key_base64, validate=True)
        )
        public_key.verify(base64.b64decode(signature_base64, validate=True), message)
    except (InvalidSignature, ValueError, binascii.Error):
        raise ValueError("public signature is invalid") from None


@dataclass(frozen=True)
class _Evidence:
    delivery_map: TargetDeliveryMapV1
    projection: static_v4.ContainerBootstrapStaticDeliveryProjectionV4
    b1_policy: binding.TargetDeliveryMapProjectionBindingTrustPolicyV1
    manifest_anchor: manifest_v1.TargetDeliveryArtifactManifestTrustAnchorV1
    role_inputs: tuple[
        manifest_v2.TargetDeliveryArtifactManifestRoleInputV2,
        manifest_v2.TargetDeliveryArtifactManifestRoleInputV2,
        manifest_v2.TargetDeliveryArtifactManifestRoleInputV2,
        manifest_v2.TargetDeliveryArtifactManifestRoleInputV2,
    ]
    policy_inputs: tuple[
        manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2,
    ]
    manifest: manifest_v2.TargetDeliveryArtifactManifestV2
    acceptance: manifest_v2.TargetDeliveryArtifactManifestAcceptanceV2
    manifest_message: bytes


def _b2_v1_inputs(
    document: dict[str, object],
) -> tuple[
    TargetDeliveryMapV1,
    static_v4.ContainerBootstrapStaticDeliveryProjectionV4,
    binding.TargetDeliveryMapProjectionBindingTrustPolicyV1,
    tuple[
        static_v4.ContainerBootstrapStaticRoleProfileEnvelopeV4,
        static_v4.ContainerBootstrapStaticRoleProfileEnvelopeV4,
        static_v4.ContainerBootstrapStaticRoleProfileEnvelopeV4,
        static_v4.ContainerBootstrapStaticRoleProfileEnvelopeV4,
    ],
    tuple[
        binding.TargetDeliveryMapProjectionBindingV1,
        binding.TargetDeliveryMapProjectionBindingV1,
        binding.TargetDeliveryMapProjectionBindingV1,
        binding.TargetDeliveryMapProjectionBindingV1,
    ],
]:
    expected = {
        "schema_version",
        "component_order",
        "target_delivery_map",
        "static_delivery_projection",
        "b1_policy",
        "manifest_anchor",
        "roles",
        "manifest",
        "manifest_message",
        "manifest_sha256",
        "acceptance",
        "acceptance_sha256",
    }
    values = _mapping(document, expected)
    if (
        values["schema_version"]
        != "rsd.target-delivery-artifact-manifest-public-vector.v1"
    ):
        raise ValueError("B2 V1 vector schema is invalid")
    if values["component_order"] != list(_COMPONENTS):
        raise ValueError("B2 V1 component order is invalid")
    map_bytes = _bytes(values["target_delivery_map"])
    try:
        delivery_map = TargetDeliveryMapV1.model_validate(
            _tuples(json.loads(map_bytes.decode("ascii"))), strict=True
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("B2 V1 delivery map is invalid") from None
    if _canonical_json(delivery_map) != map_bytes:
        raise ValueError("B2 V1 delivery map is not canonical")
    projection = static_v4.parse_container_bootstrap_static_delivery_projection_v4_canonical_json(
        _bytes(values["static_delivery_projection"])
    )
    policy = binding.parse_target_delivery_map_projection_binding_trust_policy_v1_canonical_json(
        _bytes(values["b1_policy"])
    )
    roles = values["roles"]
    if type(roles) is not list or len(roles) != 4:
        raise ValueError("B2 V1 roles are invalid")
    envelopes: list[static_v4.ContainerBootstrapStaticRoleProfileEnvelopeV4] = []
    bindings: list[binding.TargetDeliveryMapProjectionBindingV1] = []
    for ordinal, component in enumerate(_COMPONENTS):
        role = _mapping(
            cast(list[object], roles)[ordinal],
            {"profile_envelope", "projection_binding", "phase_a_closure"},
        )
        envelope = static_v4.parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json(
            _bytes(role["profile_envelope"])
        )
        if envelope.static_role_profile.component != component:
            raise ValueError("B2 V1 profile order is invalid")
        envelopes.append(envelope)
        bindings.append(
            binding.parse_target_delivery_map_projection_binding_v1_canonical_json(
                _bytes(role["projection_binding"])
            )
        )
    return (
        delivery_map,
        projection,
        policy,
        cast(tuple[Any, Any, Any, Any], tuple(envelopes)),
        cast(tuple[Any, Any, Any, Any], tuple(bindings)),
    )


def _assert_non_authorizing(value: object) -> None:
    if type(value) is dict:
        mapping = cast(dict[str, object], value)
        expected = {
            "non_authorizing": True,
            "evidence_effect_allowed": False,
            "build_allowed": False,
            "materialization_allowed": False,
            "attach_allowed": False,
            "effect_allowed": False,
        }
        for key, expected_value in expected.items():
            if key in mapping and mapping[key] is not expected_value:
                raise ValueError("vector effect flag is invalid")
        for nested in mapping.values():
            _assert_non_authorizing(nested)
    elif type(value) is list:
        for nested in cast(list[object], value):
            _assert_non_authorizing(nested)


def _assert_redacted(value: object) -> None:
    forbidden_keys = {
        "User",
        "WorkingDir",
        "Env",
        "private_key",
        "private_key_base64",
        "seed",
        "seed_base64",
        "password",
        "token",
        "credential",
        "layers",
        "archive",
    }
    forbidden_fragments = (
        "-----BEGIN",
        "PRIV" + "ATE KEY",
        "/" + "Users/",
        "/" + "Volumes/",
        "localhost",
        "127.0.0.1",
        "192.168.",
        "token=",
        "password=",
        "APP_BUILD_VARIANT",
        "PATH=/usr/bin:/bin",
        "RSD_PHASE=offline-public-vector",
    )
    if type(value) is dict:
        mapping = cast(dict[str, object], value)
        if forbidden_keys & set(mapping):
            raise ValueError("forbidden public-vector key is present")
        for nested in mapping.values():
            _assert_redacted(nested)
    elif type(value) is list:
        for nested in cast(list[object], value):
            _assert_redacted(nested)
    elif type(value) is str:
        if (
            any(fragment in value for fragment in forbidden_fragments)
            or _PRIVATE_OR_INTERNAL_ENDPOINT.search(value) is not None
            or _CREDENTIAL_URI.search(value) is not None
        ):
            raise ValueError("forbidden public-vector value is present")


def _scan_vector_envelopes(value: object, *, manifest_message: bytes) -> None:
    """Decode every fixed-segment carrier and reject unsafe canonical JSON."""

    if type(value) is list:
        for item in cast(list[object], value):
            _scan_vector_envelopes(item, manifest_message=manifest_message)
        return
    if type(value) is not dict:
        return
    mapping = cast(dict[str, object], value)
    if set(mapping) == {"encoding", "segments"}:
        payload = _bytes(mapping)
        if payload == manifest_message:
            return
        try:
            decoded = json.loads(
                payload.decode("ascii"),
                object_pairs_hook=_no_duplicate_json_mapping,
                parse_float=lambda _value: (_ for _ in ()).throw(ValueError("float")),
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("constant")
                ),
            )
        except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise ValueError("vector envelope JSON is invalid") from None
        canonical = json.dumps(
            decoded,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        if canonical != payload:
            raise ValueError("vector envelope JSON is not canonical")
        _assert_redacted(decoded)
        return
    for nested in mapping.values():
        _scan_vector_envelopes(nested, manifest_message=manifest_message)


def _no_duplicate_json_mapping(pairs: list[tuple[object, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _assert_identity_rules(evidence: _Evidence) -> None:
    policies = tuple(item.worker_trust_policy for item in evidence.policy_inputs)
    workers = tuple(
        worker for policy in policies for worker in policy.worker_trust_anchors
    )
    runs = tuple(
        attestation.run_id
        for role in evidence.role_inputs
        for attestation in role.phase_a_v5_closure.worker_attestations
    )
    if not (
        len({policy.policy_id for policy in policies}) == 4
        and all(type(policy.epoch) is int and policy.epoch >= 1 for policy in policies)
        and len({policy.independence_domain_sha256 for policy in policies}) == 4
        and len({worker.key_id for worker in workers}) == 8
        and len({worker.public_key_base64 for worker in workers}) == 8
        and len({worker.public_key_fingerprint_sha256 for worker in workers}) == 8
        and len({worker.worker_identity_sha256 for worker in workers}) == 8
        and len({worker.authority_identity_sha256 for worker in workers}) == 8
        and len(set(runs)) == 8
    ):
        raise ValueError("V5 identity namespace is invalid")
    if any(
        len(set(role.physical_builder_identity_sha256s)) != 2
        for role in evidence.manifest.roles
    ):
        raise ValueError("role physical builder pair is invalid")
    protected = {
        evidence.b1_policy.policy_id,
        evidence.b1_policy.profile_trust_anchor.key_id,
        evidence.manifest_anchor.key_id,
        *[worker.key_id for worker in workers],
        *[worker.worker_identity_sha256 for worker in workers],
        *[worker.authority_identity_sha256 for worker in workers],
        *runs,
    }
    builders = {
        builder
        for role in evidence.manifest.roles
        for builder in role.physical_builder_identity_sha256s
    }
    if builders & protected:
        raise ValueError("physical builders alias protected identities")


def _validate(document: dict[str, object]) -> _Evidence:
    top = _mapping(
        document,
        {
            "schema_version",
            "purpose",
            "base64_encoding",
            "immutable_dependencies",
            "manifest_trust_anchor",
            "manifest",
            "manifest_message",
            "manifest_sha256",
            "expected_acceptance",
            "acceptance_sha256",
        },
    )
    if (
        top["schema_version"]
        != "rsd.target-delivery-artifact-manifest-v2-public-vector.v1"
        or top["purpose"] != "synthetic_offline_b2_v2_expected_acceptance_public_vector"
        or top["base64_encoding"] != "standard_base64_fixed_segments_v1"
    ):
        raise ValueError("B2 V2 vector metadata is invalid")
    dependencies = _mapping(
        top["immutable_dependencies"], {"b1_v1_four_role", "phase_a_v5"}
    )
    b2_raw = _dependency(
        dependencies["b1_v1_four_role"],
        filename="target_delivery_artifact_manifest_public_vector.yaml",
        sha256="4b10ec2b37f0768d0a8fa283d5a26cc6020e26a968ebb3928b78d4b8f73c65ed",
    )
    v5_raw = _dependency(
        dependencies["phase_a_v5"],
        filename="container_bootstrap_artifact_evidence_v5_public_vector.yaml",
        sha256="6c66df411fd080f1d20e2cfe8f8004f600a1cfe1fe5942259b148af15166ca91",
    )
    # Every carrier below is parsed from the same byte snapshot that was just
    # SHA/size/line pinned; no loader rereads either dependency.
    b2_document = _load_yaml(b2_raw)
    v5_document = _load_yaml(v5_raw)
    _scan_vector_envelopes(
        b2_document,
        manifest_message=_bytes(b2_document.get("manifest_message")),
    )
    _scan_vector_envelopes(v5_document, manifest_message=b"")
    _scan_vector_envelopes(
        document,
        manifest_message=_bytes(top["manifest_message"]),
    )
    delivery_map, projection, b1_policy, envelopes, b1_bindings = _b2_v1_inputs(
        b2_document
    )
    b1_bytes = (
        binding.target_delivery_map_projection_binding_trust_policy_v1_canonical_json(
            b1_policy
        )
    )
    expected_b1_policy_sha256 = hashlib.sha256(
        manifest_v2._B1_POLICY_HASH_DOMAIN + b1_bytes
    ).hexdigest()
    root = b1_policy.profile_trust_anchor
    root_bytes = (
        static_v4.container_bootstrap_static_profile_trust_anchor_v4_canonical_json(
            root
        )
    )
    v5_values = _mapping(
        v5_document,
        {
            "schema_version",
            "purpose",
            "base64_encoding",
            "component_order",
            "common_profile_root",
            "common_source",
            "common_oci",
            "roles",
        },
    )
    if _bytes(v5_values["common_profile_root"]) != root_bytes:
        raise ValueError("V5 profile root is not the immutable B1 root")
    v5_roles = v5_values["roles"]
    if type(v5_roles) is not list or len(v5_roles) != 4:
        raise ValueError("B2 V2 role count is invalid")
    role_inputs: list[manifest_v2.TargetDeliveryArtifactManifestRoleInputV2] = []
    policy_inputs: list[
        manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2
    ] = []
    for ordinal, component in enumerate(_COMPONENTS):
        v5_item = _mapping(
            cast(list[object], v5_roles)[ordinal],
            {
                "component",
                "ordinal",
                "profile_envelope",
                "worker_policy",
                "closure",
                "acceptance",
                "expected",
            },
        )
        if v5_item["component"] != component or _integer(v5_item["ordinal"]) != ordinal:
            raise ValueError("immutable V5 role order is invalid")
        if _bytes(v5_item["profile_envelope"]) != (
            static_v4.container_bootstrap_static_role_profile_envelope_v4_canonical_json(
                envelopes[ordinal]
            )
        ):
            raise ValueError("B2 V1 and V5 V4 profile envelopes differ")
        _verify_ed25519(
            public_key_base64=root.public_key_base64,
            signature_base64=envelopes[ordinal].signature_base64,
            message=static_v4.container_bootstrap_static_role_profile_envelope_v4_canonical_message(
                envelopes[ordinal]
            ),
        )
        _verify_ed25519(
            public_key_base64=b1_policy.binding_trust_anchor.public_key_base64,
            signature_base64=b1_bindings[ordinal].signature_base64,
            message=binding.target_delivery_map_projection_binding_v1_message(
                b1_bindings[ordinal]
            ),
        )
        policy_bytes = _bytes(v5_item["worker_policy"])
        closure_bytes = _bytes(v5_item["closure"])
        policy = evidence_v5.parse_container_bootstrap_build_worker_trust_policy_v5_canonical_json(
            policy_bytes
        )
        closure = evidence_v5.parse_container_bootstrap_artifact_evidence_closure_v5_canonical_json(
            closure_bytes
        )
        if (
            policy_bytes
            != evidence_v5.container_bootstrap_build_worker_trust_policy_v5_canonical_json(
                policy
            )
            or closure_bytes
            != evidence_v5.container_bootstrap_artifact_evidence_closure_v5_canonical_json(
                closure
            )
        ):
            raise ValueError("B2 V2 V5 evidence is not canonical")
        if closure.worker_attestations[0].component != component:
            raise ValueError("B2 V2 V5 closure component is invalid")
        for attestation, worker_anchor in zip(
            closure.worker_attestations, policy.worker_trust_anchors, strict=True
        ):
            _verify_ed25519(
                public_key_base64=worker_anchor.public_key_base64,
                signature_base64=attestation.signature_base64,
                message=evidence_v5.container_bootstrap_artifact_worker_attestation_v5_message(
                    attestation
                ),
            )
        role_inputs.append(
            manifest_v2.TargetDeliveryArtifactManifestRoleInputV2(
                schema_version="rsd.target-delivery-artifact-manifest-role-input.v2",
                component=cast(Any, component),
                profile_envelope=envelopes[ordinal],
                projection_binding=b1_bindings[ordinal],
                phase_a_v5_closure=closure,
            )
        )
        policy_inputs.append(
            manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2(
                schema_version="rsd.target-delivery-artifact-manifest-v5-role-policy-input.v2",
                component=cast(Any, component),
                worker_trust_policy=policy,
            )
        )
    anchor = manifest_v1.parse_target_delivery_artifact_manifest_trust_anchor_v1_canonical_json(
        _bytes(top["manifest_trust_anchor"])
    )
    manifest = manifest_v2.parse_target_delivery_artifact_manifest_v2_canonical_json(
        _bytes(top["manifest"])
    )
    acceptance = manifest_v2.parse_target_delivery_artifact_manifest_acceptance_v2_canonical_json(
        _bytes(top["expected_acceptance"])
    )
    message = _bytes(top["manifest_message"])
    if message != manifest_v2.target_delivery_artifact_manifest_v2_message(manifest):
        raise ValueError("V2 manifest message is invalid")
    _verify_ed25519(
        public_key_base64=anchor.public_key_base64,
        signature_base64=manifest.signature_base64,
        message=message,
    )
    _verify_ed25519(
        public_key_base64=b1_policy.map_signer_trust_anchor.public_key_base64,
        signature_base64=delivery_map.signature_base64,
        message=map_signing.target_delivery_map_v1_canonical_message(delivery_map),
    )
    if _string(
        top["manifest_sha256"]
    ) != manifest_v2.target_delivery_artifact_manifest_v2_sha256(manifest):
        raise ValueError("V2 manifest hash is invalid")
    if acceptance.manifest_sha256 != top["manifest_sha256"]:
        raise ValueError("expected acceptance manifest hash is invalid")
    if manifest.b1_policy_sha256 != expected_b1_policy_sha256:
        raise ValueError("V2 B1 policy hash is invalid")
    if (
        _string(top["acceptance_sha256"])
        != hashlib.sha256(
            manifest_v2.target_delivery_artifact_manifest_acceptance_v2_canonical_json(
                acceptance
            )
        ).hexdigest()
    ):
        raise ValueError("V2 acceptance hash is invalid")
    evidence = _Evidence(
        delivery_map=delivery_map,
        projection=projection,
        b1_policy=b1_policy,
        manifest_anchor=anchor,
        role_inputs=cast(tuple[Any, Any, Any, Any], tuple(role_inputs)),
        policy_inputs=cast(tuple[Any, Any, Any, Any], tuple(policy_inputs)),
        manifest=manifest,
        acceptance=acceptance,
        manifest_message=message,
    )
    if (
        manifest_v2.validate_target_delivery_artifact_manifest_v2(
            delivery_map=evidence.delivery_map,
            static_delivery_projection=evidence.projection,
            b1_trust_policy=evidence.b1_policy,
            manifest_trust_anchor=evidence.manifest_anchor,
            role_inputs=evidence.role_inputs,
            v5_role_policy_inputs=evidence.policy_inputs,
            manifest=evidence.manifest,
        )
        != evidence.acceptance
    ):
        raise ValueError("V2 expected acceptance is invalid")
    _assert_identity_rules(evidence)
    _assert_non_authorizing(json.loads(_bytes(top["manifest"]).decode("ascii")))
    _assert_non_authorizing(
        json.loads(_bytes(top["expected_acceptance"]).decode("ascii"))
    )
    for role in evidence.role_inputs:
        _assert_redacted(
            role.phase_a_v5_closure.model_dump(mode="json", warnings="error")
        )
    _assert_redacted(evidence.manifest.model_dump(mode="json", warnings="error"))
    return evidence


@cache
def _cached_immutable_document(raw: bytes) -> dict[str, object]:
    """Cache only already hash-pinned bytes; callers still revalidate dependencies."""

    return _load_yaml(raw)


def _validated_evidence(raw: bytes | None = None) -> _Evidence:
    if raw is None:
        # The top-level hash is checked before cache lookup.  Deep-copying the
        # parsed YAML ensures neither cached state nor caller state can affect
        # a later composition; _validate rereads and pins each dependency.
        return _validate(
            copy.deepcopy(_cached_immutable_document(_immutable_vector_snapshot()))
        )
    return _validate(_load_yaml(raw))


def _render(document: dict[str, object]) -> bytes:
    return yaml.dump(
        document,
        Dumper=_NoAliasDumper,
        allow_unicode=False,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    ).encode("ascii")


def test_fixed_public_vector_revalidates_exact_expected_acceptance(
    tmp_path: Path,
) -> None:
    raw = _VECTOR.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _VECTOR_SHA256
    assert len(raw) == _VECTOR_BYTE_COUNT
    assert raw.count(b"\n") == _VECTOR_LINE_COUNT
    assert raw.count(b"# gitleaks:allow") == 0
    evidence = _validated_evidence()
    public_key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(evidence.manifest_anchor.public_key_base64, validate=True)
    )
    public_key.verify(
        base64.b64decode(evidence.manifest.signature_base64, validate=True),
        evidence.manifest_message,
    )
    assert (
        evidence.acceptance.manifest_sha256
        == manifest_v2.target_delivery_artifact_manifest_v2_sha256(evidence.manifest)
    )
    assert tuple(role.component for role in evidence.manifest.roles) == _COMPONENTS
    assert tuple(role.component for role in evidence.acceptance.roles) == _COMPONENTS
    assert (
        len({role.v5_worker_trust_policy_sha256 for role in evidence.manifest.roles})
        == 4
    )
    assert (
        len({role.v5_worker_trust_policy_id for role in evidence.manifest.roles}) == 4
    )
    assert (
        len(
            {
                role.v5_worker_independence_domain_sha256
                for role in evidence.manifest.roles
            }
        )
        == 4
    )
    assert (
        len(
            {
                run_id
                for role in evidence.manifest.roles
                for run_id in role.worker_run_ids
            }
        )
        == 8
    )
    assert (
        evidence.manifest.non_authorizing,
        evidence.manifest.evidence_effect_allowed,
        evidence.manifest.build_allowed,
        evidence.manifest.materialization_allowed,
        evidence.manifest.attach_allowed,
        evidence.manifest.effect_allowed,
    ) == (True, False, False, False, False, False)
    assert (
        evidence.acceptance.non_authorizing,
        evidence.acceptance.evidence_effect_allowed,
        evidence.acceptance.build_allowed,
        evidence.acceptance.materialization_allowed,
        evidence.acceptance.attach_allowed,
        evidence.acceptance.effect_allowed,
    ) == (True, False, False, False, False, False)

    openssl = shutil.which("openssl")
    if openssl is not None:
        key_path = tmp_path / "public.pem"
        message_path = tmp_path / "manifest-message.bin"
        signature_path = tmp_path / "manifest-signature.bin"
        key_path.write_bytes(
            public_key.public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        message_path.write_bytes(evidence.manifest_message)
        signature_path.write_bytes(
            base64.b64decode(evidence.manifest.signature_base64, validate=True)
        )
        result = subprocess.run(
            [
                openssl,
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(key_path),
                "-rawin",
                "-in",
                str(message_path),
                "-sigfile",
                str(signature_path),
            ],
            check=False,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr.decode("ascii", errors="replace")


def test_immutable_hash_is_checked_before_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stale = tmp_path / "stale-v2-vector.yaml"
    stale.write_bytes(_VECTOR.read_bytes() + b"\n")
    _cached_immutable_document.cache_clear()
    monkeypatch.setitem(globals(), "_VECTOR", stale)
    try:
        with pytest.raises(ValueError, match="immutable B2 V2 public vector hash"):
            _validated_evidence()
    finally:
        _cached_immutable_document.cache_clear()


def test_cached_top_level_document_revalidates_dependency_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cached_immutable_document.cache_clear()
    _validated_evidence()
    for filename in (
        "target_delivery_artifact_manifest_public_vector.yaml",
        "container_bootstrap_artifact_evidence_v5_public_vector.yaml",
    ):
        shutil.copyfile(_ROOT / filename, tmp_path / filename)
    monkeypatch.setitem(globals(), "_ROOT", tmp_path)
    dependency = (
        tmp_path / "container_bootstrap_artifact_evidence_v5_public_vector.yaml"
    )
    dependency.write_bytes(dependency.read_bytes() + b"\n")
    try:
        with pytest.raises(ValueError, match="immutable dependency bytes"):
            _validated_evidence()
    finally:
        _cached_immutable_document.cache_clear()


def test_baseline_keeps_exact_v5_builder_uniqueness_without_claiming_it_as_a_contract_rule() -> (
    None
):
    evidence = _validated_evidence()
    builders = {
        builder
        for role in evidence.manifest.roles
        for builder in role.physical_builder_identity_sha256s
    }
    # This exact V5 vector intentionally has eight distinct builders.  The
    # V2 contract separately permits cross-role pair reuse, exercised by
    # test_v2_revalidates_original_b1_and_v5_with_reused_physical_builders.
    assert len(builders) == 8
    # Epochs 1..4 are exact baseline data, not an identity namespace rule.
    assert tuple(item.worker_trust_policy.epoch for item in evidence.policy_inputs) == (
        1,
        2,
        3,
        4,
    )


def test_identity_rules_allow_coincident_positive_v5_policy_epochs() -> None:
    evidence = _validated_evidence()
    common_epoch = evidence.policy_inputs[0].worker_trust_policy.epoch
    coincident_policies = cast(
        tuple[
            manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2,
            manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2,
            manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2,
            manifest_v2.TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        ],
        tuple(
            item.model_copy(
                update={
                    "worker_trust_policy": item.worker_trust_policy.model_copy(
                        update={"epoch": common_epoch}
                    )
                }
            )
            for item in evidence.policy_inputs
        ),
    )
    assert {item.worker_trust_policy.epoch for item in coincident_policies} == {
        common_epoch
    }
    # This exercises identity separation only.  The signed manifest still
    # pins each role's actual policy epoch and is revalidated independently.
    _assert_identity_rules(replace(evidence, policy_inputs=coincident_policies))


def test_manifest_is_bound_to_the_externally_pinned_public_root() -> None:
    evidence = _validated_evidence()
    alternate_key = Ed25519PrivateKey.generate()
    alternate_public = alternate_key.public_key().public_bytes_raw()
    alternate_root = manifest_v1.TargetDeliveryArtifactManifestTrustAnchorV1(
        schema_version="rsd.target-delivery-artifact-manifest-trust-anchor.v1",
        key_id="b2-v2-alternate-public-root",
        public_key_base64=base64.b64encode(alternate_public).decode("ascii"),
        public_key_fingerprint_sha256=hashlib.sha256(alternate_public).hexdigest(),
        authority_identity_sha256=hashlib.sha256(b"alternate-authority").hexdigest(),
        independence_domain_identity_sha256=hashlib.sha256(
            b"alternate-domain"
        ).hexdigest(),
        algorithm="ed25519",
    )
    with pytest.raises(manifest_v2.TargetDeliveryArtifactManifestV2Error):
        manifest_v2.validate_target_delivery_artifact_manifest_v2(
            delivery_map=evidence.delivery_map,
            static_delivery_projection=evidence.projection,
            b1_trust_policy=evidence.b1_policy,
            manifest_trust_anchor=alternate_root,
            role_inputs=evidence.role_inputs,
            v5_role_policy_inputs=evidence.policy_inputs,
            manifest=evidence.manifest,
        )


def test_manifest_and_acceptance_are_non_authorizing_diagnostics_only() -> None:
    assert (
        "acceptance"
        not in inspect.signature(
            manifest_v2.validate_target_delivery_artifact_manifest_v2
        ).parameters
    )
    action_functions = {
        name
        for name, value in vars(manifest_v2).items()
        if inspect.isfunction(value)
        and value.__module__ == manifest_v2.__name__
        and any(
            term in name.lower()
            for term in ("build", "attach", "materialize", "deploy", "pull")
        )
    }
    assert action_functions == set()


@pytest.mark.parametrize(
    "field",
    [
        "deployment",
        "pull_authority",
        "freshness",
        "expiry",
        "timestamp",
        "nonce",
        "runtime",
        "effect",
        "authorization_back_edge",
    ],
)
def test_vector_schema_rejects_authority_and_replay_fields(field: str) -> None:
    mutated = copy.deepcopy(_load_yaml(_VECTOR.read_bytes()))
    mutated[field] = False
    with pytest.raises(ValueError, match="vector mapping shape"):
        _validated_evidence(_render(mutated))


@pytest.mark.parametrize(
    "path",
    [
        ("roles", 0, "v5_worker_trust_policy_id"),
        ("source", "commit_oid"),
        ("derived_oci_repository",),
        ("roles", 0, "oci", "config", "runtime_uid"),
    ],
)
def test_vector_rejects_role_policy_source_repository_and_config_substitutions(
    path: tuple[str | int, ...],
) -> None:
    mutated = copy.deepcopy(_load_yaml(_VECTOR.read_bytes()))
    manifest_value = cast(
        dict[str, object], json.loads(_bytes(mutated["manifest"]).decode("ascii"))
    )
    target: object = manifest_value
    for item in path[:-1]:
        target = cast(dict[str, object] | list[object], target)[item]
    replacement: object = "different-value"
    if path == ("source", "commit_oid"):
        replacement = "0" * 40
    elif path == ("derived_oci_repository",):
        replacement = "registry.example.invalid/other"
    elif path[-1] == "runtime_uid":
        replacement = cast(int, cast(dict[str, object], target)[path[-1]]) + 1
    cast(dict[str, object] | list[object], target)[path[-1]] = replacement
    mutated["manifest"] = _vector_bytes(
        json.dumps(
            manifest_value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    )
    with pytest.raises((ValueError, manifest_v2.TargetDeliveryArtifactManifestV2Error)):
        _validated_evidence(_render(mutated))


def test_carrier_scanner_rejects_duplicate_json_keys_before_model_parsing() -> None:
    mutated = copy.deepcopy(_load_yaml(_VECTOR.read_bytes()))
    manifest_bytes = _bytes(mutated["manifest"])
    duplicate = manifest_bytes.replace(
        b'{"attach_allowed":',
        b'{"attach_allowed":false,"attach_allowed":',
        1,
    )
    mutated["manifest"] = _vector_bytes(duplicate)
    with pytest.raises(ValueError, match="vector envelope JSON is invalid"):
        _validated_evidence(_render(mutated))


def test_corrupted_v2_signature_is_normalized_to_a_redacted_failure() -> None:
    mutated = copy.deepcopy(_load_yaml(_VECTOR.read_bytes()))
    manifest_value = cast(
        dict[str, object], json.loads(_bytes(mutated["manifest"]).decode("ascii"))
    )
    manifest_value["signature_base64"] = base64.b64encode(bytes(64)).decode("ascii")
    mutated["manifest"] = _vector_bytes(
        json.dumps(
            manifest_value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    )
    with pytest.raises(ValueError, match="public signature is invalid"):
        _validated_evidence(_render(mutated))


@pytest.mark.parametrize(
    "raw",
    [
        b"schema_version: one\nschema_version: two\n",
        b"first: &same value\nsecond: *same\n",
        b"value: !!str literal\n",
        b"<<: {value: literal}\n",
        b"value: literal\n---\nvalue: second\n",
        b"value: literal \n",
        b"\xef\xbb\xbfvalue: literal\n",
        b"x" * (_MAX_VECTOR_BYTES + 1),
    ],
)
def test_loader_rejects_yaml_features_and_size(raw: bytes) -> None:
    with pytest.raises((UnicodeDecodeError, ValueError, yaml.YAMLError)):
        _validated_evidence(raw)


@pytest.mark.parametrize(
    "scalar",
    [
        b"yes",
        b"ON",
        b"FALSE",
        b"null",
        b"~",
        b".Inf",
        b"1e3",
        b"0o10",
        b"0x10",
        b"0b101",
        b"00",
        b"1_000",
        b"1:20",
        b"2026-08-30",
        b"+1",
        b"-1",
        b"1.0",
    ],
)
def test_loader_rejects_nonportable_plain_scalars(scalar: bytes) -> None:
    with pytest.raises(ValueError, match="vector scalar spelling"):
        _load_yaml(b"value: " + scalar + b"\n")


@pytest.mark.parametrize(
    "scalar",
    [b"0b" + b"a" * 62, b"0xhello", b"0octopus", b"0bcat", b"0x", b"0O"],
)
def test_loader_accepts_legitimate_zero_prefix_strings(scalar: bytes) -> None:
    assert _load_yaml(b"value: " + scalar + b"\n") == {"value": scalar.decode("ascii")}
    assert _load_yaml(b'value: "' + scalar + b'"\n') == {
        "value": scalar.decode("ascii")
    }


def test_loader_rejects_malformed_child_base64_and_json() -> None:
    malformed = copy.deepcopy(_load_yaml(_VECTOR.read_bytes()))
    segment = cast(
        list[object],
        cast(dict[str, object], malformed["manifest"])["segments"],
    )
    segment[0] = "!" + _string(segment[0])[1:]
    with pytest.raises(ValueError, match=r".+"):
        _validated_evidence(_render(malformed))

    malformed = copy.deepcopy(_load_yaml(_VECTOR.read_bytes()))
    malformed["manifest"] = _vector_bytes(b"{")
    with pytest.raises(ValueError, match="vector envelope JSON is invalid"):
        _validated_evidence(_render(malformed))


def test_loader_rejects_too_deep_and_too_many_nodes() -> None:
    nested = (
        "value: "
        + "[" * (_MAX_VECTOR_DEPTH + 2)
        + "0"
        + "]" * (_MAX_VECTOR_DEPTH + 2)
        + "\n"
    )
    with pytest.raises(ValueError, match="vector exceeds depth"):
        _load_yaml(nested.encode("ascii"))
    nodes = "items:\n" + "".join("- value\n" for _ in range(_MAX_VECTOR_NODES + 1))
    with pytest.raises(ValueError, match="vector exceeds node limit"):
        _load_yaml(nodes.encode("ascii"))


def test_vector_models_reject_wrong_type_constructed_hidden_deleted_and_cycles() -> (
    None
):
    evidence = _validated_evidence()
    with pytest.raises(manifest_v2.TargetDeliveryArtifactManifestV2Error):
        manifest_v2.validate_target_delivery_artifact_manifest_v2(
            delivery_map=evidence.delivery_map,
            static_delivery_projection=evidence.projection,
            b1_trust_policy=evidence.b1_policy,
            manifest_trust_anchor=evidence.manifest_anchor,
            role_inputs=evidence.role_inputs,
            v5_role_policy_inputs=evidence.policy_inputs,
            manifest=cast(Any, object()),
        )
    constructed = manifest_v2.TargetDeliveryArtifactManifestV2.model_construct(
        **{
            name: value
            for name, value in evidence.manifest.model_dump(mode="python").items()
            if name != "effect_allowed"
        }
    )
    with pytest.raises(manifest_v2.TargetDeliveryArtifactManifestV2Error):
        manifest_v2.target_delivery_artifact_manifest_v2_canonical_json(constructed)
    hidden = evidence.manifest.model_copy()
    object.__setattr__(hidden, "unexpected_state", "value")
    with pytest.raises(manifest_v2.TargetDeliveryArtifactManifestV2Error):
        manifest_v2.target_delivery_artifact_manifest_v2_canonical_json(hidden)
    deleted = evidence.acceptance.model_copy()
    object.__delattr__(deleted, "__pydantic_fields_set__")
    with pytest.raises(manifest_v2.TargetDeliveryArtifactManifestV2Error):
        manifest_v2.target_delivery_artifact_manifest_acceptance_v2_canonical_json(
            deleted
        )
    cyclic = evidence.manifest.model_copy()
    object.__setattr__(cyclic, "source", cyclic)
    with pytest.raises(manifest_v2.TargetDeliveryArtifactManifestV2Error):
        manifest_v2.target_delivery_artifact_manifest_v2_canonical_json(cyclic)
