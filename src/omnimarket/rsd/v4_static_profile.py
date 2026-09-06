"""Inert canonical V4 static-profile verification primitives.

This module is AST-extracted from immutable RSD public commit e85340063227.
It verifies only static profile identity; it contains no route-map authority,
ticket issuance, replay redemption, runtime attach, or secret delivery path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import math
import re
import time
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Literal, NoReturn, Self, cast
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from omnimarket.rsd.v4_static_policy import (
    ContainerAttachTicketTrustAnchorV1,
    ContainerBootstrapEnvironmentConstructionPolicyV2,
    ContainerBootstrapFdPolicyV2,
    ContainerBootstrapMemorySafetyPolicyV2,
    ContainerBootstrapPid1PolicyV2,
    ContainerBootstrapStaticEnvironmentV2,
    ContainerBootstrapValkeyLaunchPolicyV2,
    ContainerSecretSinkV1,
    TargetDeliveryValueKindV1,
    container_bootstrap_environment_construction_policy_sha256,
    container_bootstrap_valkey_static_configuration_sha256,
    valkey_static_authority,
)

_SHA256 = r"^[0-9a-f]{64}$"

_IDENTIFIER = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"

_UUID = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_CONTAINER_ID = r"^[0-9a-f]{64}$"

_HOSTNAME = r"^[a-z0-9][a-z0-9-]{14,61}[a-z0-9]$"

_STATIC_PATH = r"^/[A-Za-z0-9._/-]{1,240}$"

_STATIC_ARG = r"^[A-Za-z0-9._/:=@+,%=-]{1,256}$"

_MAX_STATIC_ARG_ITEMS = 64
_MAX_STATIC_ARG_BYTES = 256
_MAX_STATIC_ARG_VECTOR_BYTES = _MAX_STATIC_ARG_ITEMS * _MAX_STATIC_ARG_BYTES

_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

_MAX_STATIC_CANONICAL_BYTES = 262_144

_MAX_PROFILE_ENVELOPE_CANONICAL_BYTES = 270_336

_MAX_CLAIM_INTENT_BYTES = 524_288

_MAX_ATTACH_METADATA_BYTES = 16_384

_MAX_CANONICAL_JSON_DEPTH = 32

_MAX_CANONICAL_JSON_NODES = 4_096

_STATIC_PROJECTION_DOMAIN = (
    b"omninode-rsd.container-bootstrap-static-delivery-projection.sha256.v4\x00"
)

_STATIC_ROUTE_DOMAIN = (
    b"omninode-rsd.container-bootstrap-static-delivery-route.sha256.v4\x00"
)

_STATIC_FIELD_DOMAIN = (
    b"omninode-rsd.container-bootstrap-static-delivery-field.sha256.v4\x00"
)

_STATIC_URI_GRAMMAR_DOMAIN = (
    b"omninode-rsd.container-bootstrap-static-uri-grammar.sha256.v4\x00"
)

_STATIC_LAUNCH_PLAN_DOMAIN = (
    b"omninode-rsd.container-bootstrap-static-launch-plan.sha256.v4\x00"
)

_STATIC_PATCH_PREIMAGE_DOMAIN = (
    b"omninode-rsd.container-bootstrap-static-patch-preimage.sha256.v4\x00"
)

_STATIC_PATCH_POLICY_DOMAIN = (
    b"omninode-rsd.container-bootstrap-static-patch-policy.sha256.v4\x00"
)

_STATIC_VALKEY_LAUNCH_POLICY_DOMAIN = (
    b"omninode-rsd.container-bootstrap-static-valkey-launch-policy.sha256.v4\x00"
)

_STATIC_MERGED_ARGV_DOMAIN = (
    b"omninode-rsd.container-bootstrap-static-merged-argv.sha256.v4\x00"
)

_STATIC_PROFILE_DOMAIN = (
    b"omninode-rsd.container-bootstrap-static-role-profile.sha256.v4\x00"
)

_STATIC_PROFILE_ENVELOPE_DOMAIN = (
    b"omninode-rsd.container-bootstrap-static-role-profile-envelope.ed25519.v4\x00"
)

_STATIC_PROFILE_ENVELOPE_HASH_DOMAIN = (
    b"omninode-rsd.container-bootstrap-static-role-profile-envelope.sha256.v4\x00"
)

_ATTACH_PROTOCOL_DOMAIN = (
    b"omninode-rsd.container-bootstrap-attach-protocol.sha256.v4\x00"
)

_ATTACH_REQUEST_DOMAIN = b"omninode-rsd.container-attach-request.sha256.v4\x00"

_ATTACH_TICKET_DOMAIN = b"omninode-rsd.container-attach-ticket.ed25519.v4\x00"

_ATTACH_TICKET_HASH_DOMAIN = b"omninode-rsd.container-attach-ticket.sha256.v4\x00"

_RUNTIME_BINDING_DOMAIN = b"omninode-rsd.container-attach-runtime-binding.sha256.v4\x00"

_TICKET_REPLAY_CLAIM_DOMAIN = (
    b"omninode-rsd.container-attach-ticket-replay-claim.sha256.v4\x00"
)

_CONTAINER_LIFETIME_CLAIM_DOMAIN = (
    b"omninode-rsd.container-attach-container-lifetime-claim.sha256.v4\x00"
)

_REPLAY_CLAIM_DOMAIN = b"omninode-rsd.container-attach-replay-claim.sha256.v4\x00"

_REPLAY_PREPARATION_DOMAIN = (
    b"omninode-rsd.container-attach-claim-preparation.sha256.v4\x00"
)

_REPLAY_RECEIPT_DOMAIN = b"omninode-rsd.container-attach-replay-receipt.ed25519.v4\x00"

_ComponentV4 = Literal[
    "primary_infisical",
    "primary_valkey",
    "restore_infisical",
    "restore_valkey",
]

_OperationScopeV4 = Literal["materialize_and_start_runtime_v1", "start_runtime_v2"]

_COMPONENTS: tuple[_ComponentV4, _ComponentV4, _ComponentV4, _ComponentV4] = (
    "primary_infisical",
    "primary_valkey",
    "restore_infisical",
    "restore_valkey",
)


class ContainerAttachStaticV4Error(ValueError):
    """A fixed, value-free V4 static-contract verification failure."""

    __slots__ = ("phase",)

    def __init__(
        self,
        phase: Literal[
            "projection",
            "profile",
            "ticket",
            "signature",
            "freshness",
            "binding",
            "replay",
        ],
    ) -> None:
        super().__init__("container attach V4 verification failed")
        self.phase = phase


class _Model(BaseModel):
    """Strict immutable V4 public metadata."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True
    )


_MISSING_MODEL_STATE = object()


@lru_cache(maxsize=512)
def _exact_field_validator(
    expected: type[BaseModel], name: str
) -> tuple[object, TypeAdapter[Any]]:
    annotation = expected.model_fields[name].rebuild_annotation()
    return annotation, TypeAdapter(annotation)


def _same_exact_runtime_value(
    original: object,
    normalized: object,
    *,
    active_pairs: set[tuple[int, int]] | None = None,
) -> bool:
    if type(original) is not type(normalized):
        return False
    if original is normalized:
        return True
    if type(original) not in (tuple, list, dict, set, frozenset) and not isinstance(
        original, BaseModel
    ):
        return original == normalized
    pairs = set() if active_pairs is None else active_pairs
    pair = (id(original), id(normalized))
    if pair in pairs:
        return False
    pairs.add(pair)
    try:
        if isinstance(original, BaseModel):
            if type(normalized) is not type(original):
                return False
            original_state = getattr(original, "__dict__", _MISSING_MODEL_STATE)
            normalized_state = getattr(normalized, "__dict__", _MISSING_MODEL_STATE)
            if type(original_state) is not dict or type(normalized_state) is not dict:
                return False
            if set(original_state) != set(normalized_state):
                return False
            return all(
                _same_exact_runtime_value(
                    cast(dict[str, object], original_state)[name],
                    cast(dict[str, object], normalized_state)[name],
                    active_pairs=pairs,
                )
                for name in original_state
            )
        if type(original) is tuple:
            original_tuple_items = cast(tuple[object, ...], original)
            normalized_tuple_items = cast(tuple[object, ...], normalized)
            return len(original_tuple_items) == len(normalized_tuple_items) and all(
                _same_exact_runtime_value(a, b, active_pairs=pairs)
                for a, b in zip(
                    original_tuple_items, normalized_tuple_items, strict=True
                )
            )
        if type(original) is list:
            original_list_items = cast(list[object], original)
            normalized_list_items = cast(list[object], normalized)
            return len(original_list_items) == len(normalized_list_items) and all(
                _same_exact_runtime_value(a, b, active_pairs=pairs)
                for a, b in zip(original_list_items, normalized_list_items, strict=True)
            )
        if type(original) is dict:
            left_items = tuple(cast(dict[object, object], original).items())
            right_items = tuple(cast(dict[object, object], normalized).items())
            return len(left_items) == len(right_items) and all(
                _same_exact_runtime_value(a, b, active_pairs=pairs)
                and _same_exact_runtime_value(c, d, active_pairs=pairs)
                for (a, c), (b, d) in zip(left_items, right_items, strict=True)
            )
        original_set_items: list[object] = list(
            cast(set[object] | frozenset[object], original)
        )
        normalized_set_items: list[object] = list(
            cast(set[object] | frozenset[object], normalized)
        )
        if len(original_set_items) != len(normalized_set_items):
            return False
        unmatched = list(normalized_set_items)
        for item in original_set_items:
            for index, candidate in enumerate(unmatched):
                if _same_exact_runtime_value(item, candidate, active_pairs=pairs):
                    unmatched.pop(index)
                    break
            else:
                return False
        return not unmatched
    finally:
        pairs.remove(pair)


def _assert_exact_field_annotation(
    expected: type[BaseModel], name: str, value: object
) -> None:
    try:
        annotation, adapter = _exact_field_validator(expected, name)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if type(value) is not annotation:
                raise ValueError("canonical model is invalid")
            return
        normalized = adapter.validate_python(value, strict=True)
        if not _same_exact_runtime_value(value, normalized):
            raise ValueError("canonical model is invalid")
    except (KeyError, RecursionError, TypeError, ValidationError, ValueError):
        raise ValueError("canonical model is invalid") from None


def _assert_exact_value(
    value: object,
    *,
    active_path: set[int],
    completed_ids: set[int],
) -> None:
    try:
        if isinstance(value, BaseModel):
            _assert_exact_model_state(
                value, type(value), active_path=active_path, completed_ids=completed_ids
            )
            return
        if type(value) not in (tuple, list, dict, set, frozenset):
            return
        value_id = id(value)
        if value_id in active_path:
            raise ValueError("canonical model is invalid")
        if value_id in completed_ids:
            return
        active_path.add(value_id)
        try:
            values = (
                cast(dict[object, object], value).items()
                if type(value) is dict
                else ((item, None) for item in cast(tuple[object, ...], value))
            )
            for item, maybe_value in values:
                _assert_exact_value(
                    item, active_path=active_path, completed_ids=completed_ids
                )
                if maybe_value is not None:
                    _assert_exact_value(
                        maybe_value,
                        active_path=active_path,
                        completed_ids=completed_ids,
                    )
        finally:
            active_path.remove(value_id)
        completed_ids.add(value_id)
    except RecursionError:
        raise ValueError("canonical model is invalid") from None


def _assert_exact_model_state(
    value: object,
    expected: type[BaseModel],
    *,
    active_path: set[int] | None = None,
    completed_ids: set[int] | None = None,
) -> BaseModel:
    """Reject constructed, hidden, deleted, cyclic, or type-drifted state."""
    active = set() if active_path is None else active_path
    completed = set() if completed_ids is None else completed_ids
    try:
        if type(value) is not expected:
            raise ValueError("canonical model is invalid")
        model_id = id(value)
        if model_id in active:
            raise ValueError("canonical model is invalid")
        if model_id in completed:
            return value
        active.add(model_id)
        try:
            fields = set(expected.model_fields)
            state = getattr(value, "__dict__", _MISSING_MODEL_STATE)
            extra = getattr(value, "__pydantic_extra__", _MISSING_MODEL_STATE)
            hidden = getattr(
                value, "__pydantic_" + "pri" + "vate__", _MISSING_MODEL_STATE
            )
            fields_set = getattr(value, "__pydantic_fields_set__", _MISSING_MODEL_STATE)
            if (
                type(state) is not dict
                or set(cast(dict[str, object], state)) != fields
                or extra is not None
                or hidden is not None
                or type(fields_set) is not set
                or not cast(set[str], fields_set).issubset(fields)
            ):
                raise ValueError("canonical model is invalid")
            for name in fields:
                field_value = cast(dict[str, object], state)[name]
                _assert_exact_value(
                    field_value, active_path=active, completed_ids=completed
                )
                _assert_exact_field_annotation(expected, name, field_value)
        finally:
            active.remove(model_id)
        completed.add(model_id)
        return value
    except (KeyError, RecursionError):
        raise ValueError("canonical model is invalid") from None


def _fail(
    phase: Literal[
        "projection", "profile", "ticket", "signature", "freshness", "binding", "replay"
    ],
) -> NoReturn:
    """Raise after a collaborator scope without preserving its exception chain."""

    raise ContainerAttachStaticV4Error(phase)


def _items(value: object, *, field: str) -> tuple[object, ...]:
    """Accept only an exact immutable tuple at a V4 static boundary."""

    if type(value) is not tuple:
        raise ValueError(f"{field} must be a tuple")
    return value


def _canonical_base64_bytes(value: str) -> bytes:
    """Decode one exact standard-base64 spelling without aliases."""

    if type(value) is not str:
        raise ValueError("base64 value is invalid")
    raw: bytes | None = None
    try:
        encoded = value.encode("ascii")
        raw = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error):
        raw = None
    if raw is None or base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("base64 value is invalid")
    return raw


def _canonical_timestamp(value: str) -> datetime:
    """Parse the one UTC timestamp spelling used by V4 tickets."""

    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError("ticket timestamp is invalid")
    parsed: datetime | None = None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        parsed = None
    if parsed is None:
        raise ValueError("ticket timestamp is invalid")
    return parsed


def _canonical_timestamp_text(value: datetime) -> str:
    """Render the exact second-granularity UTC timestamp spelling."""

    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.microsecond != 0
    ):
        raise ValueError("ticket timestamp is invalid")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_model_bytes(
    model: BaseModel, *, exclude: set[str] | None = None
) -> bytes:
    """Render a strict model as canonical ASCII-safe JSON."""

    payload: object | None = None
    try:
        _assert_exact_model_state(model, type(model))
        payload = model.model_dump(
            mode="json", exclude=exclude or set(), warnings="error"
        )
    except (RecursionError, TypeError, ValueError):
        payload = None
    if payload is None:
        raise ValueError("canonical model is invalid")
    rendered: str | None = None
    try:
        rendered = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError):
        rendered = None
    if rendered is None:
        raise ValueError("canonical model is invalid")
    return rendered.encode("ascii")


def _no_duplicate_json_object(pairs: list[tuple[object, object]]) -> dict[str, object]:
    """Build one JSON object while rejecting duplicate or non-string keys."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("canonical JSON object is invalid")
        result[key] = value
    return result


def _json_arrays_as_tuples(
    value: object,
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
) -> object:
    """Bound and translate JSON's mutable sequence shape into immutable tuples."""

    if depth > _MAX_CANONICAL_JSON_DEPTH:
        raise ValueError("canonical JSON is invalid")
    node_count = nodes if nodes is not None else [0]
    node_count[0] += 1
    if node_count[0] > _MAX_CANONICAL_JSON_NODES:
        raise ValueError("canonical JSON is invalid")
    if type(value) is list:
        return tuple(
            _json_arrays_as_tuples(item, depth=depth + 1, nodes=node_count)
            for item in value
        )
    if type(value) is dict:
        return {
            key: _json_arrays_as_tuples(item, depth=depth + 1, nodes=node_count)
            for key, item in value.items()
        }
    return value


def _hash(domain: bytes, model: BaseModel, *, exclude: set[str] | None = None) -> str:
    """Hash one canonical model under an explicit V4 domain."""

    return hashlib.sha256(
        domain + _canonical_model_bytes(model, exclude=exclude)
    ).hexdigest()


def _expected_role(component: str) -> Literal["infisical", "valkey"]:
    """Return the fixed role for one of the four V4 components."""

    return "valkey" if component.endswith("valkey") else "infisical"


def _trusted_utc_now() -> datetime:
    """Read production UTC at a V4 ticket verification boundary."""

    return datetime.now(UTC).replace(microsecond=0)


def _trusted_monotonic_now() -> float:
    """Read production monotonic time for one bounded replay-authority call."""

    return time.monotonic()


def _valid_monotonic(value: object) -> bool:
    """Accept only exact finite nonnegative monotonic-clock observations."""

    return type(value) is float and math.isfinite(value) and value >= 0.0


def _trusted_now() -> datetime:
    """Read exact trusted UTC or fail closed without a clock cause chain."""

    now: datetime | None = None
    try:
        candidate = _trusted_utc_now()
        if (
            type(candidate) is datetime
            and candidate.tzinfo is not None
            and candidate.utcoffset() == timedelta(0)
        ):
            now = candidate.replace(microsecond=0)
    except Exception:
        now = None
    if now is None:
        _fail("freshness")
    return now


def _read_trusted_monotonic_now() -> float:
    """Read one finite monotonic value through the module-local boundary."""

    now: float | None = None
    try:
        candidate = _trusted_monotonic_now()
        if _valid_monotonic(candidate):
            now = candidate
    except Exception:
        now = None
    if now is None:
        _fail("freshness")
    return now


def _strict_canonical_model[T: _Model](value: T, expected: type[T]) -> T:
    """Reparse one exact immutable model without accepting constructed trees."""

    if type(value) is not expected:
        raise ValueError("canonical model is invalid")
    _assert_exact_model_state(value, expected)
    canonical: T | None = None
    try:
        payload = json.loads(
            _canonical_model_bytes(value), object_pairs_hook=_no_duplicate_json_object
        )
        canonical = expected.model_validate(_json_arrays_as_tuples(payload))
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
        canonical = None
    if canonical is None or type(canonical) is not expected or canonical != value:
        raise ValueError("canonical model is invalid")
    return canonical


def _canonical_uri_authority_suffix_utf8_byte_count(
    *, authority: str, scheme: str
) -> int:
    prefix = scheme + "://"
    canonical = _canonical_uri_authority_v4(
        authority, scheme=cast(Literal["postgresql", "redis"], scheme)
    )
    if not canonical.startswith(prefix):
        raise ValueError("URI grammar authority is invalid")
    return len(canonical.removeprefix(prefix).encode("utf-8"))


def postgresql_connection_uri_rendered_byte_count(
    *, authority: str, application_role: str, database_name: str
) -> int:
    authority_bytes = _canonical_uri_authority_suffix_utf8_byte_count(
        authority=authority, scheme="postgresql"
    )
    return (
        len(b"postgresql:")
        + len(b"//")
        + len(application_role.encode("utf-8"))
        + 1
        + 43
        + 1
        + authority_bytes
        + 1
        + len(database_name.encode("utf-8"))
    )


def valkey_connection_uri_rendered_byte_count(
    *, authority: str, database_index: int
) -> int:
    authority_bytes = _canonical_uri_authority_suffix_utf8_byte_count(
        authority=authority, scheme="redis"
    )
    return (
        len(b"redis:")
        + len(b"//")
        + 1
        + 43
        + 1
        + authority_bytes
        + 1
        + len(str(database_index).encode("ascii"))
    )


def container_bootstrap_static_valkey_launch_policy_v4_sha256(
    policy: ContainerBootstrapValkeyLaunchPolicyV2,
) -> str:
    """Commit the exact inert V2 Valkey policy under the V4 profile domain."""

    if type(policy) is not ContainerBootstrapValkeyLaunchPolicyV2:
        _fail("profile")
    try:
        payload = json.loads(
            _canonical_model_bytes(policy), object_pairs_hook=_no_duplicate_json_object
        )
        canonical = ContainerBootstrapValkeyLaunchPolicyV2.model_validate(
            _json_arrays_as_tuples(payload)
        )
        return _hash(_STATIC_VALKEY_LAUNCH_POLICY_DOMAIN, canonical)
    except (RecursionError, TypeError, ValueError):
        _fail("profile")


def _preflight_canonical_json_structure(payload: bytes) -> None:
    """Bound JSON nesting and lexical nodes before allocating a parsed tree.

    This deliberately conservative scanner counts every container, string,
    and scalar token (including object keys), so its count is an upper bound
    on the nodes later traversed by :func:`_json_arrays_as_tuples`.  It is not
    a JSON parser: the standard parser still validates grammar and escapes.
    Keeping the structural limit before ``json.loads`` prevents a valid-size
    but deeply nested or token-dense payload from reaching recursive model
    validation first.
    """

    depth = 0
    nodes = 0
    index = 0
    in_string = False
    escaped = False
    length = len(payload)
    while index < length:
        character = payload[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == 0x5C:  # ``\\``
                escaped = True
            elif character == 0x22:  # ``\"``
                in_string = False
            elif character < 0x20:
                raise ValueError("canonical JSON is invalid")
            index += 1
            continue
        if character in b" \t\r\n:,":
            index += 1
            continue
        if character == 0x22:  # ``\"``
            nodes += 1
            in_string = True
        elif character in b"{[":
            nodes += 1
            depth += 1
            if depth > _MAX_CANONICAL_JSON_DEPTH:
                raise ValueError("canonical JSON is invalid")
        elif character in b"}]":
            depth -= 1
            if depth < 0:
                raise ValueError("canonical JSON is invalid")
        else:
            # One unquoted lexical unit can only become one JSON scalar if
            # grammar validation accepts it.  Skip it here so digits in a
            # number or letters in a literal cannot inflate the node count.
            nodes += 1
            while index < length and payload[index] not in b" \t\r\n,]}":
                index += 1
            index -= 1
        if nodes > _MAX_CANONICAL_JSON_NODES:
            raise ValueError("canonical JSON is invalid")
        index += 1
    if in_string or escaped or depth != 0:
        raise ValueError("canonical JSON is invalid")


def _parse_canonical_json[T: _Model](
    payload: bytes,
    expected: type[T],
    *,
    max_bytes: int = _MAX_STATIC_CANONICAL_BYTES,
) -> T:
    """Parse exact canonical JSON while rejecting duplicate keys and aliases."""

    if (
        type(payload) is not bytes
        or type(max_bytes) is not int
        or not 1 <= max_bytes <= _MAX_CLAIM_INTENT_BYTES
        or not 1 <= len(payload) <= max_bytes
    ):
        raise ValueError("canonical JSON is invalid")
    _preflight_canonical_json_structure(payload)
    decoded: object | None = None
    try:
        decoded = json.loads(
            payload.decode("ascii"), object_pairs_hook=_no_duplicate_json_object
        )
    except (RecursionError, UnicodeDecodeError, TypeError, ValueError):
        decoded = None
    if type(decoded) is not dict:
        raise ValueError("canonical JSON is invalid")
    model: T | None = None
    try:
        model = expected.model_validate(_json_arrays_as_tuples(decoded))
        model = _strict_canonical_model(model, expected)
    except (RecursionError, TypeError, ValueError):
        model = None
    canonical: bytes | None = None
    try:
        if model is not None:
            canonical = _canonical_model_bytes(model)
    except (RecursionError, TypeError, ValueError):
        canonical = None
    if model is None or canonical != payload:
        raise ValueError("canonical JSON is invalid")
    return model


def _canonical_uri_authority_v4(
    value: str, *, scheme: Literal["postgresql", "redis"]
) -> str:
    """Validate one canonical literal-IP authority without rendering a URI value."""

    if type(value) is not str or any(character.isspace() for character in value):
        raise ValueError("container bootstrap V4 URI authority is invalid")
    parsed = None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed is None
        or parsed.scheme != scheme
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port is None
    ):
        raise ValueError("container bootstrap V4 URI authority is invalid")
    host = None
    try:
        host = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        host = None
    if host is None:
        raise ValueError("container bootstrap V4 URI authority is invalid")
    rendered_host = f"[{host.compressed}]" if host.version == 6 else host.compressed
    if value != f"{scheme}://{rendered_host}:{port}":
        raise ValueError("container bootstrap V4 URI authority is invalid")
    return value


class ContainerBootstrapStaticPostgreSQLUriGrammarV4(_Model):
    """Output-independent target URI grammar with no observed operation identity.

    This is a fresh V4 projection of a V1 grammar.  In particular it excludes
    V1's ``prepared_operation_id``: that observed-operation binding belongs to
    the later signed V1-map-to-V4-projection closure, not to compiled static
    target input.  It still commits every value-free fact needed to construct
    the target-only PostgreSQL URI.
    """

    schema_version: Literal["rsd.container-bootstrap-static-postgresql-uri-grammar.v4"]
    database_identity: Literal["primary_database", "restore_database"]
    authority: str
    database_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    application_role: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    application_password_reference_sha256: str = Field(pattern=_SHA256)
    target_process: Literal["primary_infisical", "restore_infisical"]
    environment_variable: Literal["DB_CONNECTION_URI"]
    uri_grammar: Literal["postgresql_user_password_authority_database_v1"]
    application_password_format: Literal[
        "postgres_application_password_base64url_32_v1"
    ]
    application_password_encoded_byte_count: Literal[43]
    rendered_uri_byte_count: int = Field(ge=1, le=1024)
    return_uri_allowed: Literal[False]
    persistent_storage_allowed: Literal[False]
    logging_allowed: Literal[False]
    public_artifact_allowed: Literal[False]

    @field_validator("authority")
    @classmethod
    def canonical_authority(cls, value: str) -> str:
        return _canonical_uri_authority_v4(value, scheme="postgresql")

    @model_validator(mode="after")
    def exact_target_grammar(self) -> Self:
        expected_target = (
            "primary_infisical"
            if self.database_identity == "primary_database"
            else "restore_infisical"
        )
        if (
            self.target_process != expected_target
            or self.rendered_uri_byte_count
            != postgresql_connection_uri_rendered_byte_count(
                authority=self.authority,
                application_role=self.application_role,
                database_name=self.database_name,
            )
        ):
            raise ValueError("container bootstrap V4 PostgreSQL URI grammar is invalid")
        return self


class ContainerBootstrapStaticValkeyUriGrammarV4(_Model):
    """Output-independent target Valkey URI grammar for one Infisical role."""

    schema_version: Literal["rsd.container-bootstrap-static-valkey-uri-grammar.v4"]
    cache_identity: Literal["primary_valkey", "restore_valkey"]
    authority: str
    database_index: int = Field(ge=0, le=15)
    password_reference_sha256: str = Field(pattern=_SHA256)
    target_process: Literal["primary_infisical", "restore_infisical"]
    environment_variable: Literal["REDIS_URL"]
    uri_grammar: Literal["redis_password_authority_database_v1"]
    password_format: Literal["valkey_password_base64url_32_v1"]
    password_encoded_byte_count: Literal[43]
    rendered_uri_byte_count: int = Field(ge=1, le=1024)
    return_uri_allowed: Literal[False]
    persistent_storage_allowed: Literal[False]
    logging_allowed: Literal[False]
    public_artifact_allowed: Literal[False]

    @field_validator("authority")
    @classmethod
    def canonical_authority(cls, value: str) -> str:
        return _canonical_uri_authority_v4(value, scheme="redis")

    @model_validator(mode="after")
    def exact_target_grammar(self) -> Self:
        expected_target = (
            "primary_infisical"
            if self.cache_identity == "primary_valkey"
            else "restore_infisical"
        )
        if (
            self.target_process != expected_target
            or self.rendered_uri_byte_count
            != valkey_connection_uri_rendered_byte_count(
                authority=self.authority, database_index=self.database_index
            )
        ):
            raise ValueError("container bootstrap V4 Valkey URI grammar is invalid")
        return self


_StaticUriGrammarV4 = (
    ContainerBootstrapStaticPostgreSQLUriGrammarV4
    | ContainerBootstrapStaticValkeyUriGrammarV4
)


def container_bootstrap_static_uri_grammar_v4_sha256(
    grammar: _StaticUriGrammarV4,
) -> str:
    """Hash one exact output-independent V4 target URI grammar."""

    expected: type[_Model] | None = None
    if type(grammar) is ContainerBootstrapStaticPostgreSQLUriGrammarV4:
        expected = ContainerBootstrapStaticPostgreSQLUriGrammarV4
    elif type(grammar) is ContainerBootstrapStaticValkeyUriGrammarV4:
        expected = ContainerBootstrapStaticValkeyUriGrammarV4
    if expected is None:
        _fail("projection")
    try:
        canonical = _strict_canonical_model(grammar, expected)
        return _hash(_STATIC_URI_GRAMMAR_DOMAIN, canonical)
    except (RecursionError, TypeError, ValueError):
        pass
    _fail("projection")


class ContainerBootstrapStaticDeliveryFieldV4(_Model):
    """One exact target-usable V4 field descriptor without V1 operation binding.

    V1's descriptor binds derived fields to a full observed-operation grammar.
    V4 instead binds derived fields to the fresh static grammar hash below;
    future evidence must prove the signed V1 descriptor maps to this field.
    """

    schema_version: Literal["rsd.container-bootstrap-static-delivery-field.v4"]
    ordinal: int = Field(ge=1, le=4)
    source_purpose: Literal[
        "encryption_key",
        "auth_secret",
        "primary_valkey_password",
        "restore_valkey_password",
        "postgres_application_password",
    ]
    source_reference_sha256: str = Field(pattern=_SHA256)
    source_fingerprint_sha256: str = Field(pattern=_SHA256)
    value_kind: TargetDeliveryValueKindV1
    target_field: Literal[
        "ENCRYPTION_KEY", "AUTH_SECRET", "DB_CONNECTION_URI", "REDIS_URL", "requirepass"
    ]
    format: Literal[
        "infisical_hex_16_v1",
        "infisical_auth_secret_base64_32_v1",
        "valkey_password_base64url_32_v1",
        "derived_postgresql_uri_v1",
        "derived_valkey_uri_v1",
    ]
    encoded_byte_count: int = Field(ge=1, le=1024)
    sink: ContainerSecretSinkV1
    derivation_binding_sha256: str = Field(pattern=_SHA256)
    persistence_allowed: Literal[False]
    logging_allowed: Literal[False]
    receipt_allowed: Literal[False]

    @field_validator("value_kind", mode="before")
    @classmethod
    def canonical_value_kind(cls, value: object) -> TargetDeliveryValueKindV1:
        if type(value) is TargetDeliveryValueKindV1:
            return value
        if type(value) is str:
            try:
                return TargetDeliveryValueKindV1(value)
            except ValueError:
                pass
        raise ValueError("container bootstrap V4 delivery field is invalid")

    @field_validator("sink", mode="before")
    @classmethod
    def canonical_sink(cls, value: object) -> ContainerSecretSinkV1:
        if type(value) is ContainerSecretSinkV1:
            return value
        if type(value) is str:
            try:
                return ContainerSecretSinkV1(value)
            except ValueError:
                pass
        raise ValueError("container bootstrap V4 delivery field is invalid")


class ContainerBootstrapStaticDeliveryRouteV4(_Model):
    """One exact V4 target route without output or allocation topology identity."""

    schema_version: Literal["rsd.container-bootstrap-static-delivery-route.v4"]
    component: _ComponentV4
    component_role: Literal["infisical", "valkey"]
    sink: ContainerSecretSinkV1
    fields: tuple[ContainerBootstrapStaticDeliveryFieldV4, ...] = Field(
        min_length=1, max_length=4
    )

    @field_validator("fields", mode="before")
    @classmethod
    def declared_fields(cls, value: object) -> tuple[object, ...]:
        return _items(value, field="container bootstrap V4 delivery fields")

    @field_validator("sink", mode="before")
    @classmethod
    def canonical_sink(cls, value: object) -> ContainerSecretSinkV1:
        if type(value) is ContainerSecretSinkV1:
            return value
        if type(value) is str:
            try:
                return ContainerSecretSinkV1(value)
            except ValueError:
                pass
        raise ValueError("container bootstrap V4 delivery sink is invalid")

    @model_validator(mode="after")
    def exact_route_layout(self) -> Self:
        expected: dict[
            str,
            tuple[
                ContainerSecretSinkV1,
                tuple[tuple[str, TargetDeliveryValueKindV1, str, int | None, str], ...],
            ],
        ] = {
            "primary_infisical": (
                ContainerSecretSinkV1.INFISICAL_TARGET_PROCESS_ENVIRONMENT,
                (
                    (
                        "encryption_key",
                        TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL,
                        "infisical_hex_16_v1",
                        32,
                        "ENCRYPTION_KEY",
                    ),
                    (
                        "auth_secret",
                        TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL,
                        "infisical_auth_secret_base64_32_v1",
                        44,
                        "AUTH_SECRET",
                    ),
                    (
                        "postgres_application_password",
                        TargetDeliveryValueKindV1.DERIVED_POSTGRESQL_URI,
                        "derived_postgresql_uri_v1",
                        None,
                        "DB_CONNECTION_URI",
                    ),
                    (
                        "primary_valkey_password",
                        TargetDeliveryValueKindV1.DERIVED_VALKEY_URI,
                        "derived_valkey_uri_v1",
                        None,
                        "REDIS_URL",
                    ),
                ),
            ),
            "restore_infisical": (
                ContainerSecretSinkV1.INFISICAL_TARGET_PROCESS_ENVIRONMENT,
                (
                    (
                        "encryption_key",
                        TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL,
                        "infisical_hex_16_v1",
                        32,
                        "ENCRYPTION_KEY",
                    ),
                    (
                        "auth_secret",
                        TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL,
                        "infisical_auth_secret_base64_32_v1",
                        44,
                        "AUTH_SECRET",
                    ),
                    (
                        "postgres_application_password",
                        TargetDeliveryValueKindV1.DERIVED_POSTGRESQL_URI,
                        "derived_postgresql_uri_v1",
                        None,
                        "DB_CONNECTION_URI",
                    ),
                    (
                        "restore_valkey_password",
                        TargetDeliveryValueKindV1.DERIVED_VALKEY_URI,
                        "derived_valkey_uri_v1",
                        None,
                        "REDIS_URL",
                    ),
                ),
            ),
            "primary_valkey": (
                ContainerSecretSinkV1.VALKEY_STDIN_CONFIGURATION,
                (
                    (
                        "primary_valkey_password",
                        TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL,
                        "valkey_password_base64url_32_v1",
                        43,
                        "requirepass",
                    ),
                ),
            ),
            "restore_valkey": (
                ContainerSecretSinkV1.VALKEY_STDIN_CONFIGURATION,
                (
                    (
                        "restore_valkey_password",
                        TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL,
                        "valkey_password_base64url_32_v1",
                        43,
                        "requirepass",
                    ),
                ),
            ),
        }
        expected_sink, expected_fields = expected[self.component]
        if (
            self.component_role != _expected_role(self.component)
            or type(self.sink) is not ContainerSecretSinkV1
            or self.sink is not expected_sink
            or any(
                type(field) is not ContainerBootstrapStaticDeliveryFieldV4
                for field in self.fields
            )
            or tuple(field.ordinal for field in self.fields)
            != tuple(range(1, len(self.fields) + 1))
            or len(self.fields) != len(expected_fields)
            or len({field.target_field for field in self.fields}) != len(self.fields)
            or len({field.source_purpose for field in self.fields}) != len(self.fields)
            or len({field.source_reference_sha256 for field in self.fields})
            != len(self.fields)
            or len({field.source_fingerprint_sha256 for field in self.fields})
            != len(self.fields)
        ):
            raise ValueError("container bootstrap V4 delivery route is invalid")
        for field, (purpose, kind, field_format, byte_count, target) in zip(
            self.fields, expected_fields, strict=True
        ):
            if (
                type(field.value_kind) is not TargetDeliveryValueKindV1
                or type(field.sink) is not ContainerSecretSinkV1
                or field.source_purpose != purpose
                or field.value_kind is not kind
                or field.format != field_format
                or (byte_count is not None and field.encoded_byte_count != byte_count)
                or field.target_field != target
                or field.sink is not self.sink
                or field.persistence_allowed is not False
                or field.logging_allowed is not False
                or field.receipt_allowed is not False
                or (
                    kind is TargetDeliveryValueKindV1.DIRECT_PROVIDER_MATERIAL
                    and field.derivation_binding_sha256
                    != field.source_fingerprint_sha256
                )
            ):
                raise ValueError("container bootstrap V4 delivery route is invalid")
        return self


class ContainerBootstrapStaticDeliveryProjectionV4(_Model):
    """Four allocation-parameterized, wrapper-output-independent V4 routes.

    The projection is only a structural, value-free selection from a V1 map.
    It intentionally excludes V1 allocation topology and intent, secret
    handling, policy aggregates, map signature/hash, wrapper manifest,
    artifact, image, OCI, receipt, and effect fields.  Those are downstream
    allocation aggregate/topology, authorization, or wrapper-output facts.
    Exact URI authorities and grammar remain because target construction needs
    them before an output exists.  This projection is intentionally
    many-to-one, is not signed,
    does not verify a V1 map, and grants no delivery right.  A successor
    closure must validate the complete signed V1 map independently, bind every
    omitted field there, and prove this exact structural relation.
    """

    schema_version: Literal["rsd.container-bootstrap-static-delivery-projection.v4"]
    allocation_parameterized: Literal[True]
    generated_wrapper_output_bound: Literal[False]
    primary_postgresql_connection_uri: ContainerBootstrapStaticPostgreSQLUriGrammarV4
    restore_postgresql_connection_uri: ContainerBootstrapStaticPostgreSQLUriGrammarV4
    primary_valkey_connection_uri: ContainerBootstrapStaticValkeyUriGrammarV4
    restore_valkey_connection_uri: ContainerBootstrapStaticValkeyUriGrammarV4
    primary_infisical: ContainerBootstrapStaticDeliveryRouteV4
    primary_valkey: ContainerBootstrapStaticDeliveryRouteV4
    restore_infisical: ContainerBootstrapStaticDeliveryRouteV4
    restore_valkey: ContainerBootstrapStaticDeliveryRouteV4

    @model_validator(mode="after")
    def exact_complete_projection(self) -> Self:
        routes = (
            self.primary_infisical,
            self.primary_valkey,
            self.restore_infisical,
            self.restore_valkey,
        )
        if (
            self.allocation_parameterized is not True
            or self.generated_wrapper_output_bound is not False
            or any(
                type(route) is not ContainerBootstrapStaticDeliveryRouteV4
                for route in routes
            )
            or tuple(route.component for route in routes) != _COMPONENTS
            or type(self.primary_postgresql_connection_uri)
            is not ContainerBootstrapStaticPostgreSQLUriGrammarV4
            or type(self.restore_postgresql_connection_uri)
            is not ContainerBootstrapStaticPostgreSQLUriGrammarV4
            or type(self.primary_valkey_connection_uri)
            is not ContainerBootstrapStaticValkeyUriGrammarV4
            or type(self.restore_valkey_connection_uri)
            is not ContainerBootstrapStaticValkeyUriGrammarV4
            or self.primary_postgresql_connection_uri.database_identity
            != "primary_database"
            or self.restore_postgresql_connection_uri.database_identity
            != "restore_database"
            or self.primary_postgresql_connection_uri.target_process
            != "primary_infisical"
            or self.restore_postgresql_connection_uri.target_process
            != "restore_infisical"
            or self.primary_valkey_connection_uri.cache_identity != "primary_valkey"
            or self.restore_valkey_connection_uri.cache_identity != "restore_valkey"
            or self.primary_valkey_connection_uri.target_process != "primary_infisical"
            or self.restore_valkey_connection_uri.target_process != "restore_infisical"
            or self.primary_postgresql_connection_uri.database_name
            == self.restore_postgresql_connection_uri.database_name
            or self.primary_valkey_connection_uri.authority
            == self.restore_valkey_connection_uri.authority
            or self.primary_valkey_connection_uri.password_reference_sha256
            == self.restore_valkey_connection_uri.password_reference_sha256
        ):
            raise ValueError("container bootstrap V4 delivery projection is invalid")
        self._require_derived_field(
            route=self.primary_infisical,
            target_field="DB_CONNECTION_URI",
            grammar=self.primary_postgresql_connection_uri,
        )
        self._require_derived_field(
            route=self.restore_infisical,
            target_field="DB_CONNECTION_URI",
            grammar=self.restore_postgresql_connection_uri,
        )
        self._require_derived_field(
            route=self.primary_infisical,
            target_field="REDIS_URL",
            grammar=self.primary_valkey_connection_uri,
        )
        self._require_derived_field(
            route=self.restore_infisical,
            target_field="REDIS_URL",
            grammar=self.restore_valkey_connection_uri,
        )
        return self

    @staticmethod
    def _require_derived_field(
        *,
        route: ContainerBootstrapStaticDeliveryRouteV4,
        target_field: Literal["DB_CONNECTION_URI", "REDIS_URL"],
        grammar: _StaticUriGrammarV4,
    ) -> None:
        field = next(
            (item for item in route.fields if item.target_field == target_field), None
        )
        expected_kind = (
            TargetDeliveryValueKindV1.DERIVED_POSTGRESQL_URI
            if target_field == "DB_CONNECTION_URI"
            else TargetDeliveryValueKindV1.DERIVED_VALKEY_URI
        )
        if type(grammar) is ContainerBootstrapStaticPostgreSQLUriGrammarV4:
            expected_reference = grammar.application_password_reference_sha256
        elif type(grammar) is ContainerBootstrapStaticValkeyUriGrammarV4:
            expected_reference = grammar.password_reference_sha256
        else:
            raise ValueError("container bootstrap V4 delivery projection is invalid")
        if (
            type(field) is not ContainerBootstrapStaticDeliveryFieldV4
            or field.value_kind is not expected_kind
            or field.encoded_byte_count != grammar.rendered_uri_byte_count
            or field.source_reference_sha256 != expected_reference
            or field.derivation_binding_sha256
            != container_bootstrap_static_uri_grammar_v4_sha256(grammar)
        ):
            raise ValueError("container bootstrap V4 delivery projection is invalid")


def _strict_canonical_projection(
    projection: ContainerBootstrapStaticDeliveryProjectionV4,
) -> ContainerBootstrapStaticDeliveryProjectionV4:
    try:
        return _strict_canonical_model(
            projection, ContainerBootstrapStaticDeliveryProjectionV4
        )
    except (TypeError, ValueError):
        pass
    _fail("projection")


def container_bootstrap_static_delivery_projection_v4_canonical_json(
    projection: ContainerBootstrapStaticDeliveryProjectionV4,
) -> bytes:
    """Render canonical V4 static-delivery projection JSON."""

    projection = _strict_canonical_projection(projection)
    try:
        return _canonical_model_bytes(projection)
    except ValueError:
        pass
    _fail("projection")


def parse_container_bootstrap_static_delivery_projection_v4_canonical_json(
    payload: bytes,
) -> ContainerBootstrapStaticDeliveryProjectionV4:
    """Parse only exact canonical V4 static-delivery projection JSON."""

    try:
        return _parse_canonical_json(
            payload, ContainerBootstrapStaticDeliveryProjectionV4
        )
    except (TypeError, ValueError):
        pass
    _fail("projection")


def container_bootstrap_static_delivery_projection_v4_sha256(
    projection: ContainerBootstrapStaticDeliveryProjectionV4,
) -> str:
    """Hash only output-independent V4 static-delivery inputs."""

    projection = _strict_canonical_projection(projection)
    try:
        return _hash(_STATIC_PROJECTION_DOMAIN, projection)
    except ValueError:
        pass
    _fail("projection")


def container_bootstrap_static_delivery_route_v4_sha256(
    route: ContainerBootstrapStaticDeliveryRouteV4,
) -> str:
    """Hash one exact V4 role route under its own domain."""

    try:
        canonical = _strict_canonical_model(
            route, ContainerBootstrapStaticDeliveryRouteV4
        )
        return _hash(_STATIC_ROUTE_DOMAIN, canonical)
    except (TypeError, ValueError):
        pass
    _fail("projection")


def container_bootstrap_static_delivery_route_v4_canonical_json(
    route: ContainerBootstrapStaticDeliveryRouteV4,
) -> bytes:
    """Render one canonical V4 route for a future target implementation."""

    try:
        canonical = _strict_canonical_model(
            route, ContainerBootstrapStaticDeliveryRouteV4
        )
        return _canonical_model_bytes(canonical)
    except (TypeError, ValueError):
        pass
    _fail("projection")


def parse_container_bootstrap_static_delivery_route_v4_canonical_json(
    payload: bytes,
) -> ContainerBootstrapStaticDeliveryRouteV4:
    """Parse only exact canonical JSON for one V4 static delivery route."""

    try:
        return _parse_canonical_json(payload, ContainerBootstrapStaticDeliveryRouteV4)
    except (TypeError, ValueError):
        pass
    _fail("projection")


def _static_argument_items(value: object, *, field: str) -> tuple[str, ...]:
    items = _items(value, field=field)
    if (
        not 1 <= len(items) <= _MAX_STATIC_ARG_ITEMS
        or any(
            type(item) is not str or re.fullmatch(_STATIC_ARG, item) is None
            for item in items
        )
        or sum(len(cast(str, item).encode("ascii")) for item in items)
        > _MAX_STATIC_ARG_VECTOR_BYTES
    ):
        raise ValueError("container bootstrap V4 static argv is invalid")
    return tuple(cast(str, item) for item in items)


def _canonical_static_absolute_path(value: str) -> str:
    """Return one exact V4 path spelling accepted by the static profile."""
    if (
        type(value) is not str
        or not value.isascii()
        or re.fullmatch(_STATIC_PATH, value) is None
        or not value.startswith("/usr/local/libexec/")
        or "\\" in value
        or "%" in value
        or "//" in value
        or value.endswith("/")
    ):
        raise ValueError("container bootstrap V4 static launch path is invalid")
    if any(part in ("", ".", "..") for part in value.split("/")[1:]):
        raise ValueError("container bootstrap V4 static launch path is invalid")
    return value


def _merged_argv_sha256(
    *,
    wrapper_argv_prefix: tuple[str, ...],
    base_entrypoint: tuple[str, ...],
    base_command: tuple[str, ...],
) -> str:
    rendered = json.dumps(
        wrapper_argv_prefix + base_entrypoint + base_command,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(_STATIC_MERGED_ARGV_DOMAIN + rendered).hexdigest()


class ContainerBootstrapStaticLaunchPlanV4(_Model):
    """Exact source input for a future wrapper's base launch, never output evidence."""

    schema_version: Literal["rsd.container-bootstrap-static-launch-plan.v4"]
    component: _ComponentV4
    component_role: Literal["infisical", "valkey"]
    base_image_policy_sha256: str = Field(pattern=_SHA256)
    base_resolution_attestation_sha256: str = Field(pattern=_SHA256)
    base_registry_index_digest_sha256: str = Field(pattern=_SHA256)
    base_linux_amd64_manifest_digest_sha256: str = Field(pattern=_SHA256)
    base_config_digest_sha256: str = Field(pattern=_SHA256)
    wrapper_executable_path: str = Field(pattern=_STATIC_PATH)
    wrapper_argv_prefix: tuple[str, ...] = Field(max_length=_MAX_STATIC_ARG_ITEMS)
    base_entrypoint: tuple[str, ...] = Field(max_length=_MAX_STATIC_ARG_ITEMS)
    base_command: tuple[str, ...] = Field(max_length=_MAX_STATIC_ARG_ITEMS)
    entrypoint_command_merge: Literal["exec_wrapper_then_base_entrypoint_and_cmd_v4"]
    merged_argv_sha256: str = Field(pattern=_SHA256)

    @field_validator(
        "wrapper_argv_prefix", "base_entrypoint", "base_command", mode="before"
    )
    @classmethod
    def declared_argv(cls, value: object, info: object) -> tuple[str, ...]:
        field = getattr(info, "field_name", "static argv")
        return _static_argument_items(value, field=field)

    @field_validator("wrapper_executable_path")
    @classmethod
    def canonical_wrapper_path(cls, value: str) -> str:
        return _canonical_static_absolute_path(value)

    @model_validator(mode="after")
    def exact_static_launch(self) -> Self:
        digests = (
            self.base_image_policy_sha256,
            self.base_resolution_attestation_sha256,
            self.base_registry_index_digest_sha256,
            self.base_linux_amd64_manifest_digest_sha256,
            self.base_config_digest_sha256,
            self.merged_argv_sha256,
        )
        if (
            self.component_role != _expected_role(self.component)
            or len(set(digests)) != len(digests)
            or self.wrapper_argv_prefix[0] != self.wrapper_executable_path
            or self.merged_argv_sha256
            != _merged_argv_sha256(
                wrapper_argv_prefix=self.wrapper_argv_prefix,
                base_entrypoint=self.base_entrypoint,
                base_command=self.base_command,
            )
        ):
            raise ValueError("container bootstrap V4 static launch plan is invalid")
        return self


def container_bootstrap_static_launch_plan_v4_sha256(
    plan: ContainerBootstrapStaticLaunchPlanV4,
) -> str:
    """Hash one V4 source-only launch plan."""

    try:
        canonical = _strict_canonical_model(plan, ContainerBootstrapStaticLaunchPlanV4)
        return _hash(_STATIC_LAUNCH_PLAN_DOMAIN, canonical)
    except (TypeError, ValueError):
        pass
    _fail("profile")


def container_bootstrap_static_launch_plan_v4_canonical_json(
    plan: ContainerBootstrapStaticLaunchPlanV4,
) -> bytes:
    """Render canonical V4 source-only launch-plan JSON."""

    try:
        canonical = _strict_canonical_model(plan, ContainerBootstrapStaticLaunchPlanV4)
        return _canonical_model_bytes(canonical)
    except (TypeError, ValueError):
        pass
    _fail("profile")


def parse_container_bootstrap_static_launch_plan_v4_canonical_json(
    payload: bytes,
) -> ContainerBootstrapStaticLaunchPlanV4:
    """Parse only exact canonical V4 source-only launch-plan JSON."""

    try:
        return _parse_canonical_json(payload, ContainerBootstrapStaticLaunchPlanV4)
    except (TypeError, ValueError):
        pass
    _fail("profile")


class ContainerBootstrapStaticPatchPreimageV4(_Model):
    """Source-only static patch input; it cannot refer to generated output."""

    schema_version: Literal["rsd.container-bootstrap-static-patch-preimage.v4"]
    wrapper_source_tree_sha256: str = Field(pattern=_SHA256)
    component: _ComponentV4
    component_role: Literal["infisical", "valkey"]
    patch_kind: Literal[
        "infisical_no_write_launcher_and_envp_v4",
        "valkey_stdin_launcher_and_acl_v4",
    ]
    patch_content_sha256: str = Field(pattern=_SHA256)
    static_launch_plan_sha256: str = Field(pattern=_SHA256)
    child_environment_policy_sha256: str = Field(pattern=_SHA256)
    static_environment_sha256: str = Field(pattern=_SHA256)
    valkey_static_configuration_sha256: str | None = Field(
        default=None, pattern=_SHA256
    )
    valkey_launch_policy_sha256: str | None = Field(default=None, pattern=_SHA256)
    infisical_ca_updater_allowed: Literal[False]
    infisical_explicit_target_envp_required: Literal[False, True]
    mutable_configuration_carrier_allowed: Literal[False]
    secret_carrier_allowed: Literal[False]
    provider_access_allowed: Literal[False]
    network_access_allowed: Literal[False]
    filesystem_output_binding_allowed: Literal[False]
    artifact_output_binding_allowed: Literal[False]
    derived_image_output_binding_allowed: Literal[False]
    provenance_sbom_repro_output_binding_allowed: Literal[False]

    @model_validator(mode="after")
    def exact_source_only_patch(self) -> Self:
        is_valkey = self.component.endswith("valkey")
        if (
            self.component_role != _expected_role(self.component)
            or (
                not is_valkey
                and (
                    self.patch_kind != "infisical_no_write_launcher_and_envp_v4"
                    or self.infisical_ca_updater_allowed is not False
                    or self.infisical_explicit_target_envp_required is not True
                    or self.valkey_static_configuration_sha256 is not None
                    or self.valkey_launch_policy_sha256 is not None
                )
            )
            or (
                is_valkey
                and (
                    self.patch_kind != "valkey_stdin_launcher_and_acl_v4"
                    or self.infisical_ca_updater_allowed is not False
                    or self.infisical_explicit_target_envp_required is not False
                    or self.valkey_static_configuration_sha256 is None
                    or self.valkey_launch_policy_sha256 is None
                )
            )
        ):
            raise ValueError("container bootstrap V4 static patch preimage is invalid")
        return self


def container_bootstrap_static_patch_preimage_v4_sha256(
    preimage: ContainerBootstrapStaticPatchPreimageV4,
) -> str:
    """Hash one V4 source-only static patch preimage."""

    try:
        canonical = _strict_canonical_model(
            preimage, ContainerBootstrapStaticPatchPreimageV4
        )
        return _hash(_STATIC_PATCH_PREIMAGE_DOMAIN, canonical)
    except (TypeError, ValueError):
        pass
    _fail("profile")


def container_bootstrap_static_patch_preimage_v4_canonical_json(
    preimage: ContainerBootstrapStaticPatchPreimageV4,
) -> bytes:
    """Render canonical V4 source-only patch-preimage JSON."""

    try:
        canonical = _strict_canonical_model(
            preimage, ContainerBootstrapStaticPatchPreimageV4
        )
        return _canonical_model_bytes(canonical)
    except (TypeError, ValueError):
        pass
    _fail("profile")


def parse_container_bootstrap_static_patch_preimage_v4_canonical_json(
    payload: bytes,
) -> ContainerBootstrapStaticPatchPreimageV4:
    """Parse only exact canonical V4 source-only patch-preimage JSON."""

    try:
        return _parse_canonical_json(payload, ContainerBootstrapStaticPatchPreimageV4)
    except (TypeError, ValueError):
        pass
    _fail("profile")


class ContainerBootstrapStaticPatchPolicyV4(_Model):
    """A sealed source-only patch policy that carries no build result."""

    schema_version: Literal["rsd.container-bootstrap-static-patch-policy.v4"]
    preimage: ContainerBootstrapStaticPatchPreimageV4
    static_patch_preimage_sha256: str = Field(pattern=_SHA256)
    patch_policy_intent: Literal["compile_static_target_inputs_only_v4"]
    wrapper_bytes_claimed: Literal[False]
    generated_artifact_claimed: Literal[False]
    generated_image_claimed: Literal[False]
    generated_provenance_claimed: Literal[False]
    generated_sbom_claimed: Literal[False]
    generated_reproducibility_claimed: Literal[False]

    @model_validator(mode="after")
    def exact_patch_policy(self) -> Self:
        if (
            type(self.preimage) is not ContainerBootstrapStaticPatchPreimageV4
            or self.static_patch_preimage_sha256
            != container_bootstrap_static_patch_preimage_v4_sha256(self.preimage)
        ):
            raise ValueError("container bootstrap V4 static patch policy is invalid")
        return self


def container_bootstrap_static_patch_policy_v4_sha256(
    policy: ContainerBootstrapStaticPatchPolicyV4,
) -> str:
    """Hash one V4 static patch policy without a wrapper-output dependency."""

    try:
        canonical = _strict_canonical_model(
            policy, ContainerBootstrapStaticPatchPolicyV4
        )
        return _hash(_STATIC_PATCH_POLICY_DOMAIN, canonical)
    except (TypeError, ValueError):
        pass
    _fail("profile")


def container_bootstrap_static_patch_policy_v4_canonical_json(
    policy: ContainerBootstrapStaticPatchPolicyV4,
) -> bytes:
    """Render canonical V4 source-only patch-policy JSON."""

    try:
        canonical = _strict_canonical_model(
            policy, ContainerBootstrapStaticPatchPolicyV4
        )
        return _canonical_model_bytes(canonical)
    except (TypeError, ValueError):
        pass
    _fail("profile")


def parse_container_bootstrap_static_patch_policy_v4_canonical_json(
    payload: bytes,
) -> ContainerBootstrapStaticPatchPolicyV4:
    """Parse only exact canonical V4 source-only patch-policy JSON."""

    try:
        return _parse_canonical_json(payload, ContainerBootstrapStaticPatchPolicyV4)
    except (TypeError, ValueError):
        pass
    _fail("profile")


class ContainerBootstrapAttachProtocolV4(_Model):
    """Fresh V4 frame grammar and limits, without a runtime codec."""

    schema_version: Literal["rsd.container-bootstrap-attach-protocol.v4"]
    protocol_name: Literal["rsd_container_bootstrap_attach_v4"]
    frame_magic: Literal["ONC4"]
    frame_version: Literal[4]
    metadata_encoding: Literal["canonical_json_utf8_v1"]
    frame_header_layout: Literal["magic_4_version_u8_type_u8_length_u32be_v1"]
    secret_chunk_ordinal_layout: Literal["u16be_v1"]
    allowed_operation_scopes: tuple[
        Literal["materialize_and_start_runtime_v1"], Literal["start_runtime_v2"]
    ]
    first_frame: Literal["ticket_envelope_v4"]
    ready_state: Literal["ready_v4"]
    claim_state: Literal["claimed_v4"]
    write_closed_state: Literal["write_closed_v4"]
    terminal_ack_state: Literal["terminal_ack_v4"]
    ambiguous_state: Literal["attach_ambiguous_v4"]
    max_metadata_bytes: int = Field(ge=512, le=16_384)
    max_chunk_bytes: int = Field(ge=1, le=65_536)
    max_chunks_per_target: int = Field(ge=1, le=4)
    max_total_secret_bytes: int = Field(ge=1, le=262_144)
    max_stdout_bytes: int = Field(ge=512, le=1_048_576)
    max_stdout_frames: int = Field(ge=1, le=1024)
    ready_timeout_seconds: int = Field(ge=1, le=60)
    claim_timeout_seconds: int = Field(ge=1, le=60)
    terminal_ack_timeout_seconds: int = Field(ge=1, le=60)
    absolute_timeout_seconds: int = Field(ge=1, le=300)
    max_ticket_lifetime_seconds: int = Field(ge=1, le=300)
    docker_non_tty_required: Literal[True]
    docker_stdout_only_required: Literal[True]
    docker_stderr_rejected: Literal[True]
    actual_write_half_close_required: Literal[True]
    eof_required_before_terminal_ack: Literal[True]
    protocol_output_eof_required_after_terminal_ack: Literal[True]
    one_attach_per_container_lifetime: Literal[True]
    replay_allowed: Literal[False]
    auto_retry_after_secret_delivery_allowed: Literal[False]
    secret_persistence_allowed: Literal[False]
    secret_logging_allowed: Literal[False]
    secret_receipt_allowed: Literal[False]

    @field_validator("allowed_operation_scopes", mode="before")
    @classmethod
    def declared_scopes(cls, value: object) -> tuple[object, ...]:
        return _items(value, field="container attach V4 operation scopes")

    @model_validator(mode="after")
    def exact_bounded_protocol(self) -> Self:
        if (
            self.allowed_operation_scopes
            != ("materialize_and_start_runtime_v1", "start_runtime_v2")
            or self.max_total_secret_bytes < self.max_chunk_bytes
            or self.max_stdout_bytes < self.max_metadata_bytes
            or self.absolute_timeout_seconds
            < max(
                self.ready_timeout_seconds,
                self.claim_timeout_seconds,
                self.terminal_ack_timeout_seconds,
            )
            or self.max_ticket_lifetime_seconds < self.absolute_timeout_seconds
        ):
            raise ValueError("container bootstrap V4 attach protocol is invalid")
        return self


def container_bootstrap_attach_v4_protocol_canonical_json(
    protocol: ContainerBootstrapAttachProtocolV4,
) -> bytes:
    """Return canonical V4 protocol JSON for future target implementations."""

    try:
        canonical = _strict_canonical_model(
            protocol, ContainerBootstrapAttachProtocolV4
        )
        return _canonical_model_bytes(canonical)
    except (TypeError, ValueError):
        pass
    _fail("profile")


def parse_container_bootstrap_attach_v4_protocol_canonical_json(
    payload: bytes,
) -> ContainerBootstrapAttachProtocolV4:
    """Parse only exact canonical V4 protocol JSON spelling."""

    try:
        return _parse_canonical_json(payload, ContainerBootstrapAttachProtocolV4)
    except (TypeError, ValueError):
        pass
    _fail("profile")


def container_bootstrap_attach_v4_protocol_sha256(
    protocol: ContainerBootstrapAttachProtocolV4,
) -> str:
    """Return the V4 protocol commitment used by profiles and tickets."""

    try:
        canonical = _strict_canonical_model(
            protocol, ContainerBootstrapAttachProtocolV4
        )
        return _hash(_ATTACH_PROTOCOL_DOMAIN, canonical)
    except (TypeError, ValueError):
        pass
    _fail("profile")


class ContainerAttachV4ReplayReceiptTrustAnchorV4(_Model):
    """Pinned public key for signed replay receipts in one verified profile.

    The profile envelope authenticates this distinct receipt-verification root.
    It is deliberately separate from both the external profile root and the
    profile-owned ticket signer.  No caller may provide a replacement receipt
    root at receipt-validation time.
    """

    schema_version: Literal["rsd.container-attach-replay-receipt-trust-anchor.v4"]
    key_id: str = Field(pattern=_IDENTIFIER)
    public_key_base64: str = Field(min_length=4, max_length=128)
    public_key_fingerprint_sha256: str = Field(pattern=_SHA256)
    algorithm: Literal["ed25519"]

    @model_validator(mode="after")
    def exact_public_key(self) -> Self:
        key = _canonical_base64_bytes(self.public_key_base64)
        if (
            len(key) != 32
            or hashlib.sha256(key).hexdigest() != self.public_key_fingerprint_sha256
        ):
            raise ValueError("container attach V4 replay receipt anchor is invalid")
        return self


class ContainerBootstrapStaticRoleProfileV4(_Model):
    """One output-independent V4 target profile for a fixed role.

    ``wrapper_source_tree_sha256`` is a caller-provided source-input
    commitment, not a Git commit or an observation of generated output.  This
    module neither discovers source control metadata nor proves a source tree
    mapping.  A future signed provenance closure must attest the source
    tree/commit relation and any artifact result independently.  The profile
    hash excludes only its own self-binding field; it excludes every artifact,
    manifest, derived image, evidence, inspection, authorization, and ticket.
    """

    schema_version: Literal["rsd.container-bootstrap-static-role-profile.v4"]
    wrapper_source_tree_sha256: str = Field(pattern=_SHA256)
    component: _ComponentV4
    component_role: Literal["infisical", "valkey"]
    compile_target: Literal["x86_64-unknown-linux-musl"]
    wrapper_executable_path: str = Field(pattern=_STATIC_PATH)
    wrapper_executable_mode: Literal["0555"]
    wrapper_executable_symlink_allowed: Literal[False]
    ticket_trust_anchor: ContainerAttachTicketTrustAnchorV1
    replay_receipt_trust_anchor: ContainerAttachV4ReplayReceiptTrustAnchorV4
    attach_protocol: ContainerBootstrapAttachProtocolV4
    static_delivery_projection: ContainerBootstrapStaticDeliveryProjectionV4
    static_delivery_projection_sha256: str = Field(pattern=_SHA256)
    selected_delivery_route: ContainerBootstrapStaticDeliveryRouteV4
    selected_delivery_route_sha256: str = Field(pattern=_SHA256)
    static_launch_plan: ContainerBootstrapStaticLaunchPlanV4
    static_launch_plan_sha256: str = Field(pattern=_SHA256)
    static_patch_preimage: ContainerBootstrapStaticPatchPreimageV4
    static_patch_preimage_sha256: str = Field(pattern=_SHA256)
    static_patch_policy: ContainerBootstrapStaticPatchPolicyV4
    static_patch_policy_sha256: str = Field(pattern=_SHA256)
    static_environment: ContainerBootstrapStaticEnvironmentV2
    child_environment_policy: ContainerBootstrapEnvironmentConstructionPolicyV2
    fd_policy: ContainerBootstrapFdPolicyV2
    pid1_policy: ContainerBootstrapPid1PolicyV2
    memory_safety_policy: ContainerBootstrapMemorySafetyPolicyV2
    valkey_launch_policy: ContainerBootstrapValkeyLaunchPolicyV2 | None = None
    profile_sha256: str = Field(pattern=_SHA256)

    @field_validator("wrapper_executable_path")
    @classmethod
    def canonical_wrapper_path(cls, value: str) -> str:
        return _canonical_static_absolute_path(value)

    @model_validator(mode="after")
    def exact_static_role_profile(self) -> Self:
        is_valkey = self.component.endswith("valkey")
        route = self.selected_delivery_route
        expected_route = getattr(self.static_delivery_projection, self.component)
        nested_exact = (
            type(self.ticket_trust_anchor) is ContainerAttachTicketTrustAnchorV1
            and type(self.replay_receipt_trust_anchor)
            is ContainerAttachV4ReplayReceiptTrustAnchorV4
            and type(self.attach_protocol) is ContainerBootstrapAttachProtocolV4
            and type(self.static_delivery_projection)
            is ContainerBootstrapStaticDeliveryProjectionV4
            and type(route) is ContainerBootstrapStaticDeliveryRouteV4
            and type(self.static_launch_plan) is ContainerBootstrapStaticLaunchPlanV4
            and type(self.static_patch_preimage)
            is ContainerBootstrapStaticPatchPreimageV4
            and type(self.static_patch_policy) is ContainerBootstrapStaticPatchPolicyV4
            and type(self.static_environment) is ContainerBootstrapStaticEnvironmentV2
            and type(self.child_environment_policy)
            is ContainerBootstrapEnvironmentConstructionPolicyV2
            and type(self.fd_policy) is ContainerBootstrapFdPolicyV2
            and type(self.pid1_policy) is ContainerBootstrapPid1PolicyV2
            and type(self.memory_safety_policy)
            is ContainerBootstrapMemorySafetyPolicyV2
            and (
                type(self.valkey_launch_policy)
                is ContainerBootstrapValkeyLaunchPolicyV2
                if is_valkey
                else self.valkey_launch_policy is None
            )
        )
        try:
            expected_profile_hash = _hash(
                _STATIC_PROFILE_DOMAIN, self, exclude={"profile_sha256"}
            )
        except ValueError:
            expected_profile_hash = ""
        canonical_size_valid = False
        try:
            canonical_size_valid = (
                len(_canonical_model_bytes(self)) <= _MAX_STATIC_CANONICAL_BYTES
            )
        except (RecursionError, TypeError, ValueError):
            canonical_size_valid = False
        valid = (
            nested_exact
            and canonical_size_valid
            and self.component_role == _expected_role(self.component)
            and self.ticket_trust_anchor.key_id
            != self.replay_receipt_trust_anchor.key_id
            and self.ticket_trust_anchor.public_key_fingerprint_sha256
            != self.replay_receipt_trust_anchor.public_key_fingerprint_sha256
            and route == expected_route
            and route.component == self.component
            and route.component_role == self.component_role
            and self.static_delivery_projection_sha256
            == container_bootstrap_static_delivery_projection_v4_sha256(
                self.static_delivery_projection
            )
            and self.selected_delivery_route_sha256
            == container_bootstrap_static_delivery_route_v4_sha256(route)
            and self.static_launch_plan.component == self.component
            and self.static_launch_plan.component_role == self.component_role
            and self.static_launch_plan.wrapper_executable_path
            == self.wrapper_executable_path
            and self.static_launch_plan_sha256
            == container_bootstrap_static_launch_plan_v4_sha256(self.static_launch_plan)
            and self.static_patch_preimage.component == self.component
            and self.static_patch_preimage.component_role == self.component_role
            and self.static_patch_preimage.wrapper_source_tree_sha256
            == self.wrapper_source_tree_sha256
            and self.static_patch_preimage.static_launch_plan_sha256
            == self.static_launch_plan_sha256
            and self.static_patch_preimage.child_environment_policy_sha256
            == container_bootstrap_environment_construction_policy_sha256(
                self.child_environment_policy
            )
            and self.static_patch_preimage.static_environment_sha256
            == self.static_environment.environment_sha256
            and self.static_patch_preimage_sha256
            == container_bootstrap_static_patch_preimage_v4_sha256(
                self.static_patch_preimage
            )
            and self.static_patch_policy.preimage == self.static_patch_preimage
            and self.static_patch_policy.static_patch_preimage_sha256
            == self.static_patch_preimage_sha256
            and self.static_patch_policy_sha256
            == container_bootstrap_static_patch_policy_v4_sha256(
                self.static_patch_policy
            )
            and self.child_environment_policy.component == self.component
            and self.child_environment_policy.image_static_environment_sha256
            == self.static_environment.environment_sha256
            and self.attach_protocol.secret_persistence_allowed is False
            and self.attach_protocol.secret_logging_allowed is False
            and self.attach_protocol.secret_receipt_allowed is False
            and (
                not is_valkey
                or (
                    self.valkey_launch_policy is not None
                    and (
                        self.static_delivery_projection.primary_valkey_connection_uri.authority
                        if self.component == "primary_valkey"
                        else self.static_delivery_projection.restore_valkey_connection_uri.authority
                    )
                    == valkey_static_authority(
                        self.valkey_launch_policy.isolated_bind_address
                    )
                    and self.static_patch_preimage.valkey_static_configuration_sha256
                    == container_bootstrap_valkey_static_configuration_sha256(
                        self.valkey_launch_policy
                    )
                    and self.static_patch_preimage.valkey_launch_policy_sha256
                    == container_bootstrap_static_valkey_launch_policy_v4_sha256(
                        self.valkey_launch_policy
                    )
                )
            )
            and (
                is_valkey
                or (
                    self.static_patch_preimage.valkey_static_configuration_sha256
                    is None
                    and self.static_patch_preimage.valkey_launch_policy_sha256 is None
                )
            )
            and self.profile_sha256 == expected_profile_hash
        )
        if not valid:
            raise ValueError("container bootstrap V4 static role profile is invalid")
        return self


def build_container_bootstrap_static_role_profile_v4(
    *,
    wrapper_source_tree_sha256: str,
    component: _ComponentV4,
    component_role: Literal["infisical", "valkey"],
    compile_target: Literal["x86_64-unknown-linux-musl"],
    wrapper_executable_path: str,
    wrapper_executable_mode: Literal["0555"],
    wrapper_executable_symlink_allowed: Literal[False],
    ticket_trust_anchor: ContainerAttachTicketTrustAnchorV1,
    replay_receipt_trust_anchor: ContainerAttachV4ReplayReceiptTrustAnchorV4,
    attach_protocol: ContainerBootstrapAttachProtocolV4,
    static_delivery_projection: ContainerBootstrapStaticDeliveryProjectionV4,
    selected_delivery_route: ContainerBootstrapStaticDeliveryRouteV4,
    static_launch_plan: ContainerBootstrapStaticLaunchPlanV4,
    static_patch_preimage: ContainerBootstrapStaticPatchPreimageV4,
    static_patch_policy: ContainerBootstrapStaticPatchPolicyV4,
    static_environment: ContainerBootstrapStaticEnvironmentV2,
    child_environment_policy: ContainerBootstrapEnvironmentConstructionPolicyV2,
    fd_policy: ContainerBootstrapFdPolicyV2,
    pid1_policy: ContainerBootstrapPid1PolicyV2,
    memory_safety_policy: ContainerBootstrapMemorySafetyPolicyV2,
    valkey_launch_policy: ContainerBootstrapValkeyLaunchPolicyV2 | None,
) -> ContainerBootstrapStaticRoleProfileV4:
    """Build a self-bound V4 profile without deriving any current metadata.

    The local ``model_construct`` is used only to calculate the one permitted
    self-excluding hash. The returned model immediately undergoes normal V4
    validation, and all public verification paths reject constructed callers.
    """

    projection_sha256 = container_bootstrap_static_delivery_projection_v4_sha256(
        static_delivery_projection
    )
    route_sha256 = container_bootstrap_static_delivery_route_v4_sha256(
        selected_delivery_route
    )
    launch_sha256 = container_bootstrap_static_launch_plan_v4_sha256(static_launch_plan)
    preimage_sha256 = container_bootstrap_static_patch_preimage_v4_sha256(
        static_patch_preimage
    )
    patch_sha256 = container_bootstrap_static_patch_policy_v4_sha256(
        static_patch_policy
    )
    draft = ContainerBootstrapStaticRoleProfileV4.model_construct(
        schema_version="rsd.container-bootstrap-static-role-profile.v4",
        wrapper_source_tree_sha256=wrapper_source_tree_sha256,
        component=component,
        component_role=component_role,
        compile_target=compile_target,
        wrapper_executable_path=wrapper_executable_path,
        wrapper_executable_mode=wrapper_executable_mode,
        wrapper_executable_symlink_allowed=wrapper_executable_symlink_allowed,
        ticket_trust_anchor=ticket_trust_anchor,
        replay_receipt_trust_anchor=replay_receipt_trust_anchor,
        attach_protocol=attach_protocol,
        static_delivery_projection=static_delivery_projection,
        static_delivery_projection_sha256=projection_sha256,
        selected_delivery_route=selected_delivery_route,
        selected_delivery_route_sha256=route_sha256,
        static_launch_plan=static_launch_plan,
        static_launch_plan_sha256=launch_sha256,
        static_patch_preimage=static_patch_preimage,
        static_patch_preimage_sha256=preimage_sha256,
        static_patch_policy=static_patch_policy,
        static_patch_policy_sha256=patch_sha256,
        static_environment=static_environment,
        child_environment_policy=child_environment_policy,
        fd_policy=fd_policy,
        pid1_policy=pid1_policy,
        memory_safety_policy=memory_safety_policy,
        valkey_launch_policy=valkey_launch_policy,
        profile_sha256="0" * 64,
    )
    profile_sha256 = _hash(_STATIC_PROFILE_DOMAIN, draft, exclude={"profile_sha256"})
    try:
        return ContainerBootstrapStaticRoleProfileV4(
            schema_version="rsd.container-bootstrap-static-role-profile.v4",
            wrapper_source_tree_sha256=wrapper_source_tree_sha256,
            component=component,
            component_role=component_role,
            compile_target=compile_target,
            wrapper_executable_path=wrapper_executable_path,
            wrapper_executable_mode=wrapper_executable_mode,
            wrapper_executable_symlink_allowed=wrapper_executable_symlink_allowed,
            ticket_trust_anchor=ticket_trust_anchor,
            replay_receipt_trust_anchor=replay_receipt_trust_anchor,
            attach_protocol=attach_protocol,
            static_delivery_projection=static_delivery_projection,
            static_delivery_projection_sha256=projection_sha256,
            selected_delivery_route=selected_delivery_route,
            selected_delivery_route_sha256=route_sha256,
            static_launch_plan=static_launch_plan,
            static_launch_plan_sha256=launch_sha256,
            static_patch_preimage=static_patch_preimage,
            static_patch_preimage_sha256=preimage_sha256,
            static_patch_policy=static_patch_policy,
            static_patch_policy_sha256=patch_sha256,
            static_environment=static_environment,
            child_environment_policy=child_environment_policy,
            fd_policy=fd_policy,
            pid1_policy=pid1_policy,
            memory_safety_policy=memory_safety_policy,
            valkey_launch_policy=valkey_launch_policy,
            profile_sha256=profile_sha256,
        )
    except (TypeError, ValueError):
        pass
    _fail("profile")


def strict_canonical_container_bootstrap_static_role_profile_v4(
    profile: ContainerBootstrapStaticRoleProfileV4,
) -> ContainerBootstrapStaticRoleProfileV4:
    """Return an exact V4 static profile or a fixed public failure."""

    try:
        return _strict_canonical_model(profile, ContainerBootstrapStaticRoleProfileV4)
    except (TypeError, ValueError):
        pass
    _fail("profile")


def container_bootstrap_static_role_profile_v4_canonical_json(
    profile: ContainerBootstrapStaticRoleProfileV4,
) -> bytes:
    """Return canonical, self-bound V4 profile JSON."""

    profile = strict_canonical_container_bootstrap_static_role_profile_v4(profile)
    try:
        return _canonical_model_bytes(profile)
    except ValueError:
        pass
    _fail("profile")


def parse_container_bootstrap_static_role_profile_v4_canonical_json(
    payload: bytes,
) -> ContainerBootstrapStaticRoleProfileV4:
    """Parse only exact canonical V4 static-profile JSON spelling."""

    try:
        return _parse_canonical_json(payload, ContainerBootstrapStaticRoleProfileV4)
    except (TypeError, ValueError):
        pass
    _fail("profile")


def container_bootstrap_static_role_profile_v4_sha256(
    profile: ContainerBootstrapStaticRoleProfileV4,
) -> str:
    """Return the V4 profile's single permitted self-excluding hash."""

    profile = strict_canonical_container_bootstrap_static_role_profile_v4(profile)
    try:
        expected = _hash(_STATIC_PROFILE_DOMAIN, profile, exclude={"profile_sha256"})
    except ValueError:
        expected = ""
    if profile.profile_sha256 != expected:
        _fail("profile")
    return expected


class ContainerBootstrapStaticProfileTrustAnchorV4(_Model):
    """An externally pinned public root for a compiled V4 profile envelope.

    This distinct root authenticates the supplied profile before the target
    accepts the profile-owned ticket key.  It is not an owner authorization,
    artifact proof, manifest proof, or effect permission; those closures
    remain deliberately deferred.  Ticket signature verification itself still
    uses only the key embedded in the verified static profile.
    """

    schema_version: Literal["rsd.container-bootstrap-static-profile-trust-anchor.v4"]
    key_id: str = Field(pattern=_IDENTIFIER)
    public_key_base64: str = Field(min_length=4, max_length=128)
    public_key_fingerprint_sha256: str = Field(pattern=_SHA256)
    algorithm: Literal["ed25519"]

    @model_validator(mode="after")
    def exact_public_key(self) -> Self:
        key = _canonical_base64_bytes(self.public_key_base64)
        if (
            len(key) != 32
            or hashlib.sha256(key).hexdigest() != self.public_key_fingerprint_sha256
        ):
            raise ValueError("container bootstrap V4 profile trust anchor is invalid")
        return self


class ContainerBootstrapStaticRoleProfileEnvelopeV4(_Model):
    """A separately signed immutable identity for one static V4 profile.

    The envelope is intentionally outside the profile hash, avoiding a fixed
    point.  It authenticates only the supplied profile identity under an
    externally pinned root; it does not claim generated wrapper bytes,
    artifact provenance, OCI inspection, authorization, or runtime evidence.
    """

    schema_version: Literal["rsd.container-bootstrap-static-role-profile-envelope.v4"]
    static_role_profile: ContainerBootstrapStaticRoleProfileV4
    static_role_profile_sha256: str = Field(pattern=_SHA256)
    signer_key_id: str = Field(pattern=_IDENTIFIER)
    signature_base64: str = Field(min_length=4, max_length=256)

    @model_validator(mode="after")
    def exact_profile_identity(self) -> Self:
        canonical_size_valid = False
        try:
            canonical_size_valid = (
                len(_canonical_model_bytes(self))
                <= _MAX_PROFILE_ENVELOPE_CANONICAL_BYTES
            )
        except (RecursionError, TypeError, ValueError):
            canonical_size_valid = False
        if (
            type(self.static_role_profile) is not ContainerBootstrapStaticRoleProfileV4
            or self.static_role_profile_sha256
            != container_bootstrap_static_role_profile_v4_sha256(
                self.static_role_profile
            )
            or len(_canonical_base64_bytes(self.signature_base64)) != 64
            or not canonical_size_valid
        ):
            raise ValueError(
                "container bootstrap V4 static profile envelope is invalid"
            )
        return self


def strict_canonical_container_bootstrap_static_profile_trust_anchor_v4(
    anchor: ContainerBootstrapStaticProfileTrustAnchorV4,
) -> ContainerBootstrapStaticProfileTrustAnchorV4:
    """Return one exact external V4 profile root or a fixed public failure."""

    try:
        return _strict_canonical_model(
            anchor, ContainerBootstrapStaticProfileTrustAnchorV4
        )
    except (RecursionError, TypeError, ValueError):
        pass
    _fail("profile")


def container_bootstrap_static_profile_trust_anchor_v4_canonical_json(
    anchor: ContainerBootstrapStaticProfileTrustAnchorV4,
) -> bytes:
    """Render exact canonical V4 external profile-root JSON."""

    anchor = strict_canonical_container_bootstrap_static_profile_trust_anchor_v4(anchor)
    try:
        return _canonical_model_bytes(anchor)
    except (RecursionError, TypeError, ValueError):
        pass
    _fail("profile")


def parse_container_bootstrap_static_profile_trust_anchor_v4_canonical_json(
    payload: bytes,
) -> ContainerBootstrapStaticProfileTrustAnchorV4:
    """Parse only exact bounded V4 external profile-root JSON."""

    try:
        return _parse_canonical_json(
            payload, ContainerBootstrapStaticProfileTrustAnchorV4
        )
    except (RecursionError, TypeError, ValueError):
        pass
    _fail("profile")


def strict_canonical_container_bootstrap_static_role_profile_envelope_v4(
    envelope: ContainerBootstrapStaticRoleProfileEnvelopeV4,
) -> ContainerBootstrapStaticRoleProfileEnvelopeV4:
    """Return one exact V4 profile envelope or a fixed public failure."""

    try:
        return _strict_canonical_model(
            envelope, ContainerBootstrapStaticRoleProfileEnvelopeV4
        )
    except (RecursionError, TypeError, ValueError):
        pass
    _fail("profile")


def container_bootstrap_static_role_profile_envelope_v4_canonical_json(
    envelope: ContainerBootstrapStaticRoleProfileEnvelopeV4,
) -> bytes:
    """Render canonical V4 profile-envelope JSON without authenticating it."""

    envelope = strict_canonical_container_bootstrap_static_role_profile_envelope_v4(
        envelope
    )
    try:
        return _canonical_model_bytes(envelope)
    except (RecursionError, TypeError, ValueError):
        pass
    _fail("profile")


def parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json(
    payload: bytes,
) -> ContainerBootstrapStaticRoleProfileEnvelopeV4:
    """Parse only exact bounded canonical V4 profile-envelope JSON."""

    try:
        return _parse_canonical_json(
            payload,
            ContainerBootstrapStaticRoleProfileEnvelopeV4,
            max_bytes=_MAX_PROFILE_ENVELOPE_CANONICAL_BYTES,
        )
    except (RecursionError, TypeError, ValueError):
        pass
    _fail("profile")


def container_bootstrap_static_role_profile_envelope_v4_sha256(
    envelope: ContainerBootstrapStaticRoleProfileEnvelopeV4,
) -> str:
    """Hash one exact signed-profile identity under a fresh V4 domain."""

    try:
        canonical = (
            strict_canonical_container_bootstrap_static_role_profile_envelope_v4(
                envelope
            )
        )
        return _hash(_STATIC_PROFILE_ENVELOPE_HASH_DOMAIN, canonical)
    except ContainerAttachStaticV4Error:
        pass
    _fail("profile")


def container_bootstrap_static_role_profile_envelope_v4_canonical_message(
    envelope: ContainerBootstrapStaticRoleProfileEnvelopeV4,
) -> bytes:
    """Return the exact domain-separated Ed25519 message for profile integrity."""

    envelope = strict_canonical_container_bootstrap_static_role_profile_envelope_v4(
        envelope
    )
    try:
        return _STATIC_PROFILE_ENVELOPE_DOMAIN + _canonical_model_bytes(
            envelope, exclude={"signature_base64"}
        )
    except (RecursionError, TypeError, ValueError):
        pass
    _fail("profile")


def verify_container_bootstrap_static_role_profile_envelope_v4(
    *,
    envelope: ContainerBootstrapStaticRoleProfileEnvelopeV4,
    profile_trust_anchor: ContainerBootstrapStaticProfileTrustAnchorV4,
) -> ContainerBootstrapStaticRoleProfileV4:
    """Verify external profile integrity before using its embedded ticket anchor.

    This deliberately verifies *only* the profile identity.  Callers must not
    construe the returned static metadata as authorization or as a generated
    wrapper/artifact/inspection/evidence assertion.
    """

    canonical_envelope: ContainerBootstrapStaticRoleProfileEnvelopeV4 | None = None
    canonical_anchor: ContainerBootstrapStaticProfileTrustAnchorV4 | None = None
    try:
        canonical_envelope = (
            strict_canonical_container_bootstrap_static_role_profile_envelope_v4(
                envelope
            )
        )
        canonical_anchor = (
            strict_canonical_container_bootstrap_static_profile_trust_anchor_v4(
                profile_trust_anchor
            )
        )
    except ContainerAttachStaticV4Error:
        pass
    if canonical_envelope is None or canonical_anchor is None:
        _fail("profile")
    profile = canonical_envelope.static_role_profile
    ticket_anchor = profile.ticket_trust_anchor
    receipt_anchor = profile.replay_receipt_trust_anchor
    if (
        canonical_envelope.signer_key_id != canonical_anchor.key_id
        or ticket_anchor.key_id == canonical_anchor.key_id
        or receipt_anchor.key_id == canonical_anchor.key_id
        or ticket_anchor.key_id == receipt_anchor.key_id
        or ticket_anchor.public_key_fingerprint_sha256
        == canonical_anchor.public_key_fingerprint_sha256
        or receipt_anchor.public_key_fingerprint_sha256
        == canonical_anchor.public_key_fingerprint_sha256
        or ticket_anchor.public_key_fingerprint_sha256
        == receipt_anchor.public_key_fingerprint_sha256
    ):
        _fail("profile")
    signature_valid = False
    try:
        key = Ed25519PublicKey.from_public_bytes(
            _canonical_base64_bytes(canonical_anchor.public_key_base64)
        )
        key.verify(
            _canonical_base64_bytes(canonical_envelope.signature_base64),
            container_bootstrap_static_role_profile_envelope_v4_canonical_message(
                canonical_envelope
            ),
        )
        signature_valid = True
    except (ContainerAttachStaticV4Error, InvalidSignature, ValueError):
        signature_valid = False
    if not signature_valid:
        _fail("signature")
    return profile
