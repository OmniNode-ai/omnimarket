"""Pure Phase-B1 signed relation between a V1 map, V4 projection, and one V4 role profile.

This validates signatures and an exact structural relation only.  It does not
validate map freshness, current source, allocation, topology, prepared
operation, provider, materialization, or runtime state.  Existing lifecycle
authorization remains mandatory for those complete-map checks.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Literal, NoReturn, Self, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from omnimarket.rsd.historical_target_delivery_map_v1 import (
    ContainerTargetDeliveryV1,
    PostgreSQLConnectionUriGrammarV1,
    TargetDeliveryFieldV1,
    TargetDeliveryMapSignerTrustAnchorV1,
    TargetDeliveryMapSigningError,
    TargetDeliveryMapV1,
    ValkeyConnectionUriGrammarV1,
    runtime_connection_uri_grammar_sha256,
    target_delivery_map_sha256,
    verify_target_delivery_map_v1_signature,
)
from omnimarket.rsd.v4_artifact_evidence import (
    ContainerBootstrapBuildWorkerTrustAnchorV4,
    ContainerBootstrapBuildWorkerTrustPolicyV4,
)
from omnimarket.rsd.v4_static_policy import (
    ContainerAttachTicketTrustAnchorV1,
    TargetDeliveryValueKindV1,
)
from omnimarket.rsd.v4_static_profile import (
    ContainerAttachStaticV4Error,
    ContainerAttachV4ReplayReceiptTrustAnchorV4,
    ContainerBootstrapStaticDeliveryFieldV4,
    ContainerBootstrapStaticDeliveryProjectionV4,
    ContainerBootstrapStaticDeliveryRouteV4,
    ContainerBootstrapStaticPostgreSQLUriGrammarV4,
    ContainerBootstrapStaticProfileTrustAnchorV4,
    ContainerBootstrapStaticRoleProfileEnvelopeV4,
    ContainerBootstrapStaticRoleProfileV4,
    ContainerBootstrapStaticValkeyUriGrammarV4,
    _expected_role,
    _StaticUriGrammarV4,
    container_bootstrap_static_delivery_projection_v4_canonical_json,
    container_bootstrap_static_delivery_projection_v4_sha256,
    container_bootstrap_static_role_profile_envelope_v4_sha256,
    container_bootstrap_static_uri_grammar_v4_sha256,
    strict_canonical_container_bootstrap_static_profile_trust_anchor_v4,
    strict_canonical_container_bootstrap_static_role_profile_envelope_v4,
    verify_container_bootstrap_static_role_profile_envelope_v4,
)

_IDENTIFIER = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
_SHA256 = r"^[0-9a-f]{64}$"
_MAX_BYTES = 393_216
_MAX_DEPTH = 32
_MAX_NODES = 4_096
_BINDING_DOMAIN = b"omninode-rsd.target-delivery-map-projection-binding.ed25519.v1\x00"
_BINDING_HASH_DOMAIN = (
    b"omninode-rsd.target-delivery-map-projection-binding.sha256.v1\x00"
)
_CONTEXT_HASH_DOMAIN = (
    b"omninode-rsd.target-delivery-map-projection-binding-context.sha256.v1\x00"
)
_COMPONENTS = (
    "primary_infisical",
    "primary_valkey",
    "restore_infisical",
    "restore_valkey",
)
_COMPONENT_ROLES = {
    "primary_infisical": "infisical",
    "primary_valkey": "valkey",
    "restore_infisical": "infisical",
    "restore_valkey": "valkey",
}


class TargetDeliveryMapProjectionBindingError(ValueError):
    """Fixed public B1 validation failure."""

    __slots__ = ("phase",)

    def __init__(self, phase: Literal["parse", "anchor", "profile", "map", "binding"]):
        super().__init__("target delivery map projection binding validation failed")
        self.phase = phase


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )


def _fail(phase: Literal["parse", "anchor", "profile", "map", "binding"]) -> NoReturn:
    raise TargetDeliveryMapProjectionBindingError(phase)


def _b64(value: str) -> bytes:
    if type(value) is not str:
        raise ValueError("base64 is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("base64 is invalid") from None
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("base64 is invalid")
    return decoded


def _canonical(model: BaseModel, *, exclude: set[str] | None = None) -> bytes:
    try:
        rendered = json.dumps(
            model.model_dump(mode="json", exclude=exclude or set(), warnings="error"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (RecursionError, TypeError, ValueError):
        raise ValueError("model is invalid") from None
    if len(rendered) > _MAX_BYTES:
        raise ValueError("model is too large")
    return rendered


_MISSING_MODEL_STATE = object()


def _exact_state(value: object, expected: type[BaseModel], active: set[int]) -> None:
    if type(value) is not expected or id(value) in active:
        raise ValueError("model is invalid")
    state = getattr(value, "__dict__", _MISSING_MODEL_STATE)
    if (
        type(state) is not dict
        or set(cast(dict[str, object], state)) != set(expected.model_fields)
        or getattr(value, "__pydantic_extra__", _MISSING_MODEL_STATE) is not None
        or getattr(value, "__pydantic_" + "pri" + "vate__", _MISSING_MODEL_STATE)
        is not None
        or type(getattr(value, "__pydantic_fields_set__", _MISSING_MODEL_STATE))
        is not set
        or not value.__pydantic_fields_set__.issubset(expected.model_fields)
    ):
        raise ValueError("model is invalid")
    active.add(id(value))
    try:
        for item in cast(dict[str, object], state).values():
            _exact_value(item, active)
    finally:
        active.remove(id(value))


def _exact_value(value: object, active: set[int]) -> None:
    if isinstance(value, BaseModel):
        _exact_state(value, type(value), active)
    elif type(value) in (tuple, list, set, frozenset):
        if id(value) in active:
            raise ValueError("model is invalid")
        active.add(id(value))
        try:
            for item in cast(tuple[object, ...], value):
                _exact_value(item, active)
        finally:
            active.remove(id(value))
    elif type(value) is dict:
        if id(value) in active:
            raise ValueError("model is invalid")
        active.add(id(value))
        try:
            for key, item in cast(dict[object, object], value).items():
                _exact_value(key, active)
                _exact_value(item, active)
        finally:
            active.remove(id(value))


def _same_exact_value(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, BaseModel):
        return all(
            _same_exact_value(getattr(left, name), getattr(right, name))
            for name in left.__class__.model_fields
        )
    if type(left) is tuple:
        right_tuple = cast(tuple[object, ...], right)
        return len(left) == len(right_tuple) and all(
            _same_exact_value(a, b) for a, b in zip(left, right_tuple, strict=True)
        )
    return left == right


def _strict[T: _Model](model: object, expected: type[T]) -> T:
    if type(model) is not expected:
        raise ValueError("model type is invalid")
    _exact_state(model, expected, set())
    rendered = _canonical(cast(BaseModel, model))
    decoded = json.loads(rendered.decode("ascii"), object_pairs_hook=_no_duplicates)
    canonical = expected.model_validate(_arrays_to_tuples(decoded), strict=True)
    if _canonical(canonical) != rendered or not _same_exact_value(model, canonical):
        raise ValueError("model is not canonical")
    return canonical


class TargetDeliveryMapProjectionBindingTrustAnchorV1(_Model):
    """Pinned B1 relation signer and its distinct authority identities."""

    schema_version: Literal[
        "rsd.target-delivery-map-projection-binding-trust-anchor.v1"
    ]
    key_id: str = Field(pattern=_IDENTIFIER)
    authority_identity_sha256: str = Field(pattern=_SHA256)
    independence_domain_identity_sha256: str = Field(pattern=_SHA256)
    public_key_base64: str = Field(min_length=4, max_length=128)
    public_key_fingerprint_sha256: str = Field(pattern=_SHA256)
    algorithm: Literal["ed25519"]

    @model_validator(mode="after")
    def exact_key_and_identities(self) -> Self:
        key = _b64(self.public_key_base64)
        identities = (
            self.authority_identity_sha256,
            self.independence_domain_identity_sha256,
            self.public_key_fingerprint_sha256,
        )
        if (
            len(key) != 32
            or hashlib.sha256(key).hexdigest() != self.public_key_fingerprint_sha256
            or len(set(identities)) != 3
        ):
            raise ValueError("binding trust anchor is invalid")
        return self


class TargetDeliveryMapProjectionBindingTrustPolicyV1(_Model):
    """All external roots used by one B1 validation; profile leaves stay profile-owned."""

    schema_version: Literal[
        "rsd.target-delivery-map-projection-binding-trust-policy.v1"
    ]
    policy_id: str = Field(pattern=_IDENTIFIER)
    map_signer_trust_anchor: TargetDeliveryMapSignerTrustAnchorV1
    map_authority_identity_sha256: str = Field(pattern=_SHA256)
    map_independence_domain_identity_sha256: str = Field(pattern=_SHA256)
    binding_trust_anchor: TargetDeliveryMapProjectionBindingTrustAnchorV1
    profile_trust_anchor: ContainerBootstrapStaticProfileTrustAnchorV4
    phase_a_worker_trust_policy: ContainerBootstrapBuildWorkerTrustPolicyV4

    @model_validator(mode="after")
    def collision_free_external_policy(self) -> Self:
        map_anchor = self.map_signer_trust_anchor
        binding_anchor = self.binding_trust_anchor
        workers = self.phase_a_worker_trust_policy.worker_trust_anchors
        namespace = (
            self.policy_id,
            self.phase_a_worker_trust_policy.policy_id,
            map_anchor.key_id,
            binding_anchor.key_id,
            self.profile_trust_anchor.key_id,
            workers[0].key_id,
            workers[1].key_id,
            self.map_authority_identity_sha256,
            self.map_independence_domain_identity_sha256,
            binding_anchor.authority_identity_sha256,
            binding_anchor.independence_domain_identity_sha256,
            self.phase_a_worker_trust_policy.independence_domain_sha256,
            workers[0].worker_identity_sha256,
            workers[0].authority_identity_sha256,
            workers[1].worker_identity_sha256,
            workers[1].authority_identity_sha256,
            map_anchor.public_key_fingerprint_sha256,
            binding_anchor.public_key_fingerprint_sha256,
            self.profile_trust_anchor.public_key_fingerprint_sha256,
            workers[0].public_key_fingerprint_sha256,
            workers[1].public_key_fingerprint_sha256,
            map_anchor.public_key_base64,
            binding_anchor.public_key_base64,
            self.profile_trust_anchor.public_key_base64,
            workers[0].public_key_base64,
            workers[1].public_key_base64,
        )
        if (
            type(map_anchor) is not TargetDeliveryMapSignerTrustAnchorV1
            or type(binding_anchor)
            is not TargetDeliveryMapProjectionBindingTrustAnchorV1
            or type(self.profile_trust_anchor)
            is not ContainerBootstrapStaticProfileTrustAnchorV4
            or type(self.phase_a_worker_trust_policy)
            is not ContainerBootstrapBuildWorkerTrustPolicyV4
            or len(set(namespace)) != len(namespace)
        ):
            raise ValueError("binding trust policy is invalid")
        return self


class TargetDeliveryMapProjectionBindingV1(_Model):
    """One role-specific signed assertion over a map, projection, and profile."""

    schema_version: Literal["rsd.target-delivery-map-projection-binding.v1"]
    projection_algorithm: Literal["project_target_delivery_map_v1_structurally.v1"]
    target_delivery_map_schema_version: Literal["rsd.target-delivery-map.v1"]
    projection_schema_version: Literal[
        "rsd.container-bootstrap-static-delivery-projection.v4"
    ]
    component_order: tuple[
        Literal["primary_infisical"],
        Literal["primary_valkey"],
        Literal["restore_infisical"],
        Literal["restore_valkey"],
    ]
    target_delivery_map_sha256: str = Field(pattern=_SHA256)
    static_delivery_projection_sha256: str = Field(pattern=_SHA256)
    verified_static_role_profile_sha256: str = Field(pattern=_SHA256)
    verified_static_role_profile_envelope_sha256: str = Field(pattern=_SHA256)
    profile_component: Literal[
        "primary_infisical", "primary_valkey", "restore_infisical", "restore_valkey"
    ]
    profile_component_role: Literal["infisical", "valkey"]
    selected_delivery_route_sha256: str = Field(pattern=_SHA256)
    selected_delivery_route_ordinal: Literal[0, 1, 2, 3]
    verified_profile_trust_anchor_key_id: str = Field(pattern=_IDENTIFIER)
    verified_profile_trust_anchor_public_key_fingerprint_sha256: str = Field(
        pattern=_SHA256
    )
    verified_map_signer_key_id: str = Field(pattern=_IDENTIFIER)
    verified_map_signer_public_key_fingerprint_sha256: str = Field(pattern=_SHA256)
    binding_signer_key_id: str = Field(pattern=_IDENTIFIER)
    binding_signer_public_key_fingerprint_sha256: str = Field(pattern=_SHA256)
    signature_base64: str = Field(min_length=4, max_length=128)

    @model_validator(mode="after")
    def exact_binding_shape(self) -> Self:
        if (
            self.component_order != _COMPONENTS
            or self.profile_component_role != _COMPONENT_ROLES[self.profile_component]
            or self.selected_delivery_route_ordinal
            != _COMPONENTS.index(self.profile_component)
            or len(_b64(self.signature_base64)) != 64
            or self.verified_map_signer_key_id == self.binding_signer_key_id
            or self.verified_map_signer_public_key_fingerprint_sha256
            == self.binding_signer_public_key_fingerprint_sha256
        ):
            raise ValueError("projection binding is invalid")
        return self


class TargetDeliveryMapProjectionBindingAcceptanceV1(_Model):
    """Non-portable, non-authorizing record of one fully rechecked B1 call."""

    schema_version: Literal["rsd.target-delivery-map-projection-binding-acceptance.v1"]
    target_delivery_map_sha256: str = Field(pattern=_SHA256)
    static_delivery_projection_sha256: str = Field(pattern=_SHA256)
    binding_sha256: str = Field(pattern=_SHA256)
    profile_sha256: str = Field(pattern=_SHA256)
    profile_envelope_sha256: str = Field(pattern=_SHA256)
    verification_context_sha256: str = Field(pattern=_SHA256)
    build_allowed: Literal[False]
    materialization_allowed: Literal[False]
    attach_allowed: Literal[False]
    effect_allowed: Literal[False]


def strict_canonical_target_delivery_map_projection_binding_v1(
    binding: TargetDeliveryMapProjectionBindingV1,
) -> TargetDeliveryMapProjectionBindingV1:
    try:
        return _strict(binding, TargetDeliveryMapProjectionBindingV1)
    except (TypeError, ValidationError, ValueError):
        _fail("binding")


def strict_canonical_target_delivery_map_projection_binding_trust_policy_v1(
    policy: TargetDeliveryMapProjectionBindingTrustPolicyV1,
) -> TargetDeliveryMapProjectionBindingTrustPolicyV1:
    try:
        return _strict(policy, TargetDeliveryMapProjectionBindingTrustPolicyV1)
    except (TypeError, ValidationError, ValueError):
        _fail("anchor")


def target_delivery_map_projection_binding_trust_policy_v1_canonical_json(
    policy: TargetDeliveryMapProjectionBindingTrustPolicyV1,
) -> bytes:
    """Render the complete caller-pinned B1 root policy as bounded canonical JSON."""

    return _canonical(
        strict_canonical_target_delivery_map_projection_binding_trust_policy_v1(policy)
    )


def parse_target_delivery_map_projection_binding_trust_policy_v1_canonical_json(
    payload: bytes,
) -> TargetDeliveryMapProjectionBindingTrustPolicyV1:
    """Parse only an exact canonical B1 policy, restoring immutable tuples."""

    try:
        _preflight(payload)
        value = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_no_duplicates,
            parse_float=lambda _value: (_ for _ in ()).throw(ValueError("float")),
        )
        if type(value) is not dict:
            raise ValueError("JSON is invalid")
        policy = TargetDeliveryMapProjectionBindingTrustPolicyV1.model_validate(
            _arrays_to_tuples(value), strict=True
        )
        if _canonical(policy) != payload:
            raise ValueError("JSON is invalid")
        return policy
    except (UnicodeDecodeError, TypeError, ValidationError, ValueError):
        _fail("parse")


def target_delivery_map_projection_binding_v1_canonical_json(
    binding: TargetDeliveryMapProjectionBindingV1,
) -> bytes:
    return _canonical(
        strict_canonical_target_delivery_map_projection_binding_v1(binding)
    )


def parse_target_delivery_map_projection_binding_v1_canonical_json(
    payload: bytes,
) -> TargetDeliveryMapProjectionBindingV1:
    try:
        _preflight(payload)
        value = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_no_duplicates,
            parse_float=lambda _value: (_ for _ in ()).throw(ValueError("float")),
        )
        if type(value) is not dict:
            raise ValueError("JSON is invalid")
        binding = TargetDeliveryMapProjectionBindingV1.model_validate(
            _arrays_to_tuples(value), strict=True
        )
        if _canonical(binding) != payload:
            raise ValueError("JSON is invalid")
        return binding
    except (UnicodeDecodeError, TypeError, ValidationError, ValueError):
        _fail("parse")


def target_delivery_map_projection_binding_acceptance_v1_canonical_json(
    acceptance: TargetDeliveryMapProjectionBindingAcceptanceV1,
) -> bytes:
    """Render the bounded non-authorizing B1 receipt in exact canonical JSON."""

    try:
        return _canonical(
            _strict(acceptance, TargetDeliveryMapProjectionBindingAcceptanceV1)
        )
    except (TypeError, ValidationError, ValueError):
        _fail("parse")


def parse_target_delivery_map_projection_binding_acceptance_v1_canonical_json(
    payload: bytes,
) -> TargetDeliveryMapProjectionBindingAcceptanceV1:
    """Parse only an exact bounded canonical non-authorizing B1 receipt."""

    try:
        _preflight(payload)
        value = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_no_duplicates,
            parse_float=lambda _value: (_ for _ in ()).throw(ValueError("float")),
        )
        if type(value) is not dict:
            raise ValueError("JSON is invalid")
        acceptance = TargetDeliveryMapProjectionBindingAcceptanceV1.model_validate(
            value, strict=True
        )
        if _canonical(acceptance) != payload:
            raise ValueError("JSON is invalid")
        return acceptance
    except (UnicodeDecodeError, TypeError, ValidationError, ValueError):
        _fail("parse")


def _no_duplicates(pairs: list[tuple[object, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("JSON is invalid")
        result[key] = value
    return result


def _arrays_to_tuples(value: object) -> object:
    if type(value) is list:
        return tuple(_arrays_to_tuples(item) for item in value)
    if type(value) is dict:
        return {key: _arrays_to_tuples(item) for key, item in value.items()}
    return value


def _preflight(payload: bytes) -> None:
    if type(payload) is not bytes or not 1 <= len(payload) <= _MAX_BYTES:
        raise ValueError("JSON is invalid")
    depth = nodes = 0
    quote = escape = False
    for char in payload:
        if quote:
            if escape:
                escape = False
            elif char == 92:
                escape = True
            elif char == 34:
                quote = False
            elif char < 32:
                raise ValueError("JSON is invalid")
            continue
        if char == 34:
            quote = True
        elif char in (123, 91):
            depth += 1
        elif char in (125, 93):
            depth -= 1
        if char not in b" \t\r\n:,":
            nodes += 1
        if depth < 0 or depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError("JSON is invalid")
    if quote or escape or depth:
        raise ValueError("JSON is invalid")


def target_delivery_map_projection_binding_v1_message(
    binding: TargetDeliveryMapProjectionBindingV1,
) -> bytes:
    return _BINDING_DOMAIN + _canonical(
        strict_canonical_target_delivery_map_projection_binding_v1(binding),
        exclude={"signature_base64"},
    )


def target_delivery_map_projection_binding_v1_sha256(
    binding: TargetDeliveryMapProjectionBindingV1,
) -> str:
    return hashlib.sha256(
        _BINDING_HASH_DOMAIN
        + target_delivery_map_projection_binding_v1_canonical_json(binding)
    ).hexdigest()


def _static_postgresql_uri_grammar_from_v1(
    grammar: PostgreSQLConnectionUriGrammarV1,
) -> ContainerBootstrapStaticPostgreSQLUriGrammarV4:
    """Project only target construction facts from one V1 PostgreSQL grammar."""

    if type(grammar) is not PostgreSQLConnectionUriGrammarV1:
        raise ValueError("V1 PostgreSQL grammar is invalid")
    return ContainerBootstrapStaticPostgreSQLUriGrammarV4(
        schema_version="rsd.container-bootstrap-static-postgresql-uri-grammar.v4",
        database_identity=grammar.database_identity,
        authority=grammar.authority,
        database_name=grammar.database_name,
        application_role=grammar.application_role,
        application_password_reference_sha256=grammar.application_password_reference_sha256,
        target_process=grammar.target_process,
        environment_variable=grammar.environment_variable,
        uri_grammar=grammar.uri_grammar,
        application_password_format=grammar.application_password_format,
        application_password_encoded_byte_count=grammar.application_password_encoded_byte_count,
        rendered_uri_byte_count=grammar.rendered_uri_byte_count,
        return_uri_allowed=grammar.return_uri_allowed,
        persistent_storage_allowed=grammar.persistent_storage_allowed,
        logging_allowed=grammar.logging_allowed,
        public_artifact_allowed=grammar.public_artifact_allowed,
    )


def _static_valkey_uri_grammar_from_v1(
    grammar: ValkeyConnectionUriGrammarV1,
) -> ContainerBootstrapStaticValkeyUriGrammarV4:
    """Project only target construction facts from one V1 Valkey grammar."""

    if type(grammar) is not ValkeyConnectionUriGrammarV1:
        raise ValueError("V1 Valkey grammar is invalid")
    return ContainerBootstrapStaticValkeyUriGrammarV4(
        schema_version="rsd.container-bootstrap-static-valkey-uri-grammar.v4",
        cache_identity=grammar.cache_identity,
        authority=grammar.authority,
        database_index=grammar.database_index,
        password_reference_sha256=grammar.password_reference_sha256,
        target_process=grammar.target_process,
        environment_variable=grammar.environment_variable,
        uri_grammar=grammar.uri_grammar,
        password_format=grammar.password_format,
        password_encoded_byte_count=grammar.password_encoded_byte_count,
        rendered_uri_byte_count=grammar.rendered_uri_byte_count,
        return_uri_allowed=grammar.return_uri_allowed,
        persistent_storage_allowed=grammar.persistent_storage_allowed,
        logging_allowed=grammar.logging_allowed,
        public_artifact_allowed=grammar.public_artifact_allowed,
    )


def _static_delivery_field_from_v1(
    field: TargetDeliveryFieldV1,
    *,
    v1_grammar: PostgreSQLConnectionUriGrammarV1 | ValkeyConnectionUriGrammarV1 | None,
    static_grammar: _StaticUriGrammarV4 | None,
) -> ContainerBootstrapStaticDeliveryFieldV4:
    """Convert a validated V1 field to target-usable non-operation V4 grammar."""

    if type(field) is not TargetDeliveryFieldV1:
        raise ValueError("V1 target delivery field is invalid")
    binding = field.source_fingerprint_sha256
    if field.value_kind is TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL:
        if v1_grammar is not None or static_grammar is not None:
            raise ValueError("V1 target delivery field is invalid")
        if field.derivation_binding_sha256 != field.source_fingerprint_sha256:
            raise ValueError("V1 target delivery field is invalid")
    elif field.value_kind in {
        TargetDeliveryValueKindV1.DERIVED_POSTGRESQL_URI,
        TargetDeliveryValueKindV1.DERIVED_VALKEY_URI,
    }:
        if v1_grammar is None or static_grammar is None:
            raise ValueError("V1 target delivery field is invalid")
        if (
            field.derivation_binding_sha256
            != runtime_connection_uri_grammar_sha256(v1_grammar)
            or field.encoded_byte_count != static_grammar.rendered_uri_byte_count
        ):
            raise ValueError("V1 target delivery field is invalid")
        binding = container_bootstrap_static_uri_grammar_v4_sha256(static_grammar)
    else:
        raise ValueError("V1 target delivery field is invalid")
    return ContainerBootstrapStaticDeliveryFieldV4(
        schema_version="rsd.container-bootstrap-static-delivery-field.v4",
        ordinal=field.ordinal,
        source_purpose=field.source_purpose,
        source_reference_sha256=field.source_reference_sha256,
        source_fingerprint_sha256=field.source_fingerprint_sha256,
        value_kind=field.value_kind,
        target_field=field.target_field,
        format=field.format,
        encoded_byte_count=field.encoded_byte_count,
        sink=field.sink,
        derivation_binding_sha256=binding,
        persistence_allowed=field.persistence_allowed,
        logging_allowed=field.logging_allowed,
        receipt_allowed=field.receipt_allowed,
    )


def _recompute_static_projection_from_historical_map(
    delivery_map: TargetDeliveryMapV1,
) -> ContainerBootstrapStaticDeliveryProjectionV4:
    """Select only non-output target grammar from one already-parsed V1 map.

    This helper deliberately does *not* inspect the map's allocation/policy
    aggregates, network driver/options, manifest, route artifact/image/protocol
    fields, map hash, signature, signer, or timestamp. It performs no signature
    verification and is therefore non-authorizing. A later signed-map closure
    owns those omitted bindings and must prove the exact structural relation
    again.
    """

    if type(delivery_map) is not TargetDeliveryMapV1:
        _fail("binding")
    routes: dict[str, ContainerBootstrapStaticDeliveryRouteV4] = {}
    try:
        # This non-authorizing projection intentionally ignores selected V1
        # output-only values, but it still requires the original complete map
        # to have exact, non-constructed model state before any dump can
        # normalize a scalar, enum, container, subclass, or hidden attribute.

        delivery_map = TargetDeliveryMapV1.model_validate(
            delivery_map.model_dump(mode="python", warnings="error"), strict=True
        )
        primary_postgresql = _static_postgresql_uri_grammar_from_v1(
            delivery_map.database_identities.primary_database.connection_uri
        )
        restore_postgresql = _static_postgresql_uri_grammar_from_v1(
            delivery_map.database_identities.restore_database.connection_uri
        )
        primary_valkey = _static_valkey_uri_grammar_from_v1(
            delivery_map.primary_valkey_connection_uri
        )
        restore_valkey = _static_valkey_uri_grammar_from_v1(
            delivery_map.restore_valkey_connection_uri
        )
        route_grammars: dict[
            str,
            tuple[
                PostgreSQLConnectionUriGrammarV1 | ValkeyConnectionUriGrammarV1 | None,
                _StaticUriGrammarV4 | None,
                PostgreSQLConnectionUriGrammarV1 | ValkeyConnectionUriGrammarV1 | None,
                _StaticUriGrammarV4 | None,
            ],
        ] = {
            "primary_infisical": (
                delivery_map.database_identities.primary_database.connection_uri,
                primary_postgresql,
                delivery_map.primary_valkey_connection_uri,
                primary_valkey,
            ),
            "restore_infisical": (
                delivery_map.database_identities.restore_database.connection_uri,
                restore_postgresql,
                delivery_map.restore_valkey_connection_uri,
                restore_valkey,
            ),
            "primary_valkey": (None, None, None, None),
            "restore_valkey": (None, None, None, None),
        }
        for component in _COMPONENTS:
            target = getattr(delivery_map, component)
            if type(target) is not ContainerTargetDeliveryV1:
                raise ValueError("V1 map route is invalid")
            postgresql_v1, postgresql_static, valkey_v1, valkey_static = route_grammars[
                component
            ]
            static_fields: list[ContainerBootstrapStaticDeliveryFieldV4] = []
            for field in target.fields:
                if field.target_field == "DB_CONNECTION_URI":
                    static_fields.append(
                        _static_delivery_field_from_v1(
                            field,
                            v1_grammar=postgresql_v1,
                            static_grammar=postgresql_static,
                        )
                    )
                elif field.target_field == "REDIS_URL":
                    static_fields.append(
                        _static_delivery_field_from_v1(
                            field, v1_grammar=valkey_v1, static_grammar=valkey_static
                        )
                    )
                else:
                    static_fields.append(
                        _static_delivery_field_from_v1(
                            field, v1_grammar=None, static_grammar=None
                        )
                    )
            routes[component] = ContainerBootstrapStaticDeliveryRouteV4(
                schema_version="rsd.container-bootstrap-static-delivery-route.v4",
                component=cast(
                    Literal[
                        "primary_infisical",
                        "primary_valkey",
                        "restore_infisical",
                        "restore_valkey",
                    ],
                    component,
                ),
                component_role=_expected_role(component),
                sink=target.sink,
                fields=tuple(static_fields),
            )
        return ContainerBootstrapStaticDeliveryProjectionV4(
            schema_version="rsd.container-bootstrap-static-delivery-projection.v4",
            allocation_parameterized=True,
            generated_wrapper_output_bound=False,
            primary_postgresql_connection_uri=primary_postgresql,
            restore_postgresql_connection_uri=restore_postgresql,
            primary_valkey_connection_uri=primary_valkey,
            restore_valkey_connection_uri=restore_valkey,
            primary_infisical=routes["primary_infisical"],
            primary_valkey=routes["primary_valkey"],
            restore_infisical=routes["restore_infisical"],
            restore_valkey=routes["restore_valkey"],
        )
    except (AttributeError, KeyError, RecursionError, TypeError, ValueError):
        pass
    _fail("binding")


def validate_target_delivery_map_projection_binding_v1(
    *,
    delivery_map: TargetDeliveryMapV1,
    static_delivery_projection: ContainerBootstrapStaticDeliveryProjectionV4,
    profile_envelope: ContainerBootstrapStaticRoleProfileEnvelopeV4,
    binding: TargetDeliveryMapProjectionBindingV1,
    trust_policy: TargetDeliveryMapProjectionBindingTrustPolicyV1,
) -> TargetDeliveryMapProjectionBindingAcceptanceV1:
    """Recheck a complete signed map→V4 relation; successful calls grant no right."""

    try:
        policy = (
            strict_canonical_target_delivery_map_projection_binding_trust_policy_v1(
                trust_policy
            )
        )
        map_anchor = policy.map_signer_trust_anchor
        profile_anchor = (
            strict_canonical_container_bootstrap_static_profile_trust_anchor_v4(
                policy.profile_trust_anchor
            )
        )
        profile = verify_container_bootstrap_static_role_profile_envelope_v4(
            envelope=profile_envelope, profile_trust_anchor=profile_anchor
        )
        envelope = strict_canonical_container_bootstrap_static_role_profile_envelope_v4(
            profile_envelope
        )
    except (
        ContainerAttachStaticV4Error,
        TargetDeliveryMapSigningError,
        TypeError,
        ValueError,
    ):
        _fail("profile")
    _check_profile_collisions(policy, profile)
    try:
        canonical_map = verify_target_delivery_map_v1_signature(
            delivery_map=delivery_map, signer_trust_anchor=map_anchor
        )
        map_hash = target_delivery_map_sha256(canonical_map)
    except (TargetDeliveryMapSigningError, TypeError, ValueError):
        _fail("map")
    try:
        # Calling this canonical serializer first rejects subclasses and type drift.
        container_bootstrap_static_delivery_projection_v4_canonical_json(
            static_delivery_projection
        )
        projection_hash = container_bootstrap_static_delivery_projection_v4_sha256(
            static_delivery_projection
        )
        recomputed = _recompute_static_projection_from_historical_map(canonical_map)
        if (
            recomputed != static_delivery_projection
            or projection_hash
            != container_bootstrap_static_delivery_projection_v4_sha256(recomputed)
        ):
            raise ValueError("projection mismatch")
        if (
            profile.static_delivery_projection != static_delivery_projection
            or profile.static_delivery_projection_sha256 != projection_hash
        ):
            raise ValueError("profile projection mismatch")
    except (ContainerAttachStaticV4Error, TypeError, ValueError):
        _fail("binding")
    profile_envelope_hash = container_bootstrap_static_role_profile_envelope_v4_sha256(
        envelope
    )
    try:
        canonical_binding = strict_canonical_target_delivery_map_projection_binding_v1(
            binding
        )
        anchor = policy.binding_trust_anchor
        if (
            canonical_binding.projection_algorithm
            != "project_target_delivery_map_v1_structurally.v1"
            or canonical_binding.target_delivery_map_schema_version
            != canonical_map.schema_version
            or canonical_binding.projection_schema_version
            != static_delivery_projection.schema_version
            or canonical_binding.component_order != _COMPONENTS
            or canonical_binding.target_delivery_map_sha256 != map_hash
            or canonical_binding.static_delivery_projection_sha256 != projection_hash
            or canonical_binding.verified_static_role_profile_sha256
            != profile.profile_sha256
            or canonical_binding.verified_static_role_profile_envelope_sha256
            != profile_envelope_hash
            or canonical_binding.profile_component != profile.component
            or canonical_binding.profile_component_role != profile.component_role
            or canonical_binding.selected_delivery_route_sha256
            != profile.selected_delivery_route_sha256
            or canonical_binding.selected_delivery_route_ordinal
            != _COMPONENTS.index(profile.component)
            or canonical_binding.verified_profile_trust_anchor_key_id
            != profile_anchor.key_id
            or canonical_binding.verified_profile_trust_anchor_public_key_fingerprint_sha256
            != profile_anchor.public_key_fingerprint_sha256
            or canonical_binding.verified_map_signer_key_id != map_anchor.key_id
            or canonical_binding.verified_map_signer_public_key_fingerprint_sha256
            != map_anchor.public_key_fingerprint_sha256
            or canonical_binding.binding_signer_key_id != anchor.key_id
            or canonical_binding.binding_signer_public_key_fingerprint_sha256
            != anchor.public_key_fingerprint_sha256
        ):
            raise ValueError("binding mismatch")
        Ed25519PublicKey.from_public_bytes(_b64(anchor.public_key_base64)).verify(
            _b64(canonical_binding.signature_base64),
            target_delivery_map_projection_binding_v1_message(canonical_binding),
        )
    except (
        InvalidSignature,
        TargetDeliveryMapProjectionBindingError,
        TypeError,
        ValueError,
    ):
        _fail("binding")
    return TargetDeliveryMapProjectionBindingAcceptanceV1(
        schema_version="rsd.target-delivery-map-projection-binding-acceptance.v1",
        target_delivery_map_sha256=map_hash,
        static_delivery_projection_sha256=projection_hash,
        binding_sha256=target_delivery_map_projection_binding_v1_sha256(
            canonical_binding
        ),
        profile_sha256=profile.profile_sha256,
        profile_envelope_sha256=profile_envelope_hash,
        verification_context_sha256=hashlib.sha256(
            _CONTEXT_HASH_DOMAIN
            + _canonical(policy)
            + profile.profile_sha256.encode("ascii")
            + profile_envelope_hash.encode("ascii")
        ).hexdigest(),
        build_allowed=False,
        materialization_allowed=False,
        attach_allowed=False,
        effect_allowed=False,
    )


def _check_profile_collisions(
    policy: TargetDeliveryMapProjectionBindingTrustPolicyV1,
    profile: ContainerBootstrapStaticRoleProfileV4,
) -> None:
    """Keep every external and profile-authenticated root in one namespace."""

    try:
        ticket = profile.ticket_trust_anchor
        receipt = profile.replay_receipt_trust_anchor
        roots = (
            policy.map_signer_trust_anchor,
            policy.binding_trust_anchor,
            policy.profile_trust_anchor,
            ticket,
            receipt,
            *policy.phase_a_worker_trust_policy.worker_trust_anchors,
        )
        expected_types = (
            TargetDeliveryMapSignerTrustAnchorV1,
            TargetDeliveryMapProjectionBindingTrustAnchorV1,
            ContainerBootstrapStaticProfileTrustAnchorV4,
            ContainerAttachTicketTrustAnchorV1,
            ContainerAttachV4ReplayReceiptTrustAnchorV4,
            ContainerBootstrapBuildWorkerTrustAnchorV4,
        )
        if any(type(root) not in expected_types for root in roots):
            raise ValueError("root is invalid")
        identities = (
            policy.map_authority_identity_sha256,
            policy.map_independence_domain_identity_sha256,
            policy.binding_trust_anchor.authority_identity_sha256,
            policy.binding_trust_anchor.independence_domain_identity_sha256,
            policy.phase_a_worker_trust_policy.independence_domain_sha256,
            *(
                worker.worker_identity_sha256
                for worker in policy.phase_a_worker_trust_policy.worker_trust_anchors
            ),
            *(
                worker.authority_identity_sha256
                for worker in policy.phase_a_worker_trust_policy.worker_trust_anchors
            ),
        )
        namespace = (
            policy.policy_id,
            policy.phase_a_worker_trust_policy.policy_id,
            *(root.key_id for root in roots),
            *identities,
            *(root.public_key_base64 for root in roots),
            *(root.public_key_fingerprint_sha256 for root in roots),
        )
        if len(set(namespace)) != len(namespace):
            raise ValueError("identity collision")
    except (AttributeError, TypeError, ValueError):
        _fail("anchor")
