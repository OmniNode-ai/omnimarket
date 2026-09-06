# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure C0 validation against build-pinned route-contract snapshots."""

from __future__ import annotations

import hashlib
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, NoReturn, cast

import yaml
from omnibase_core.models.delegation.wire import (
    EnumTierCostType,
    ModelDelegationConfig,
    ModelRoutingTier,
    ModelTierCost,
    ModelTierModel,
)
from pydantic import ValidationError

from omnimarket.models.delegation.llm_cost_routing.model_llm_model_registry import (
    ModelLlmModelRegistry,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelBifrostDelegationConfig,
)
from omnimarket.models.rsd.model_endpoint_contract import ModelRsdEndpointContract
from omnimarket.nodes.node_rsd_offline_delivery_matrix_compute.models.model_rsd_offline_delivery_matrix import (
    ModelRsdOfflineDeliveryMatrixInput,
    ModelRsdOfflineDeliveryMatrixOutput,
)
from omnimarket.routing.generated_llm_routing_constants import (
    BIFROST_DELEGATION_SHA256,
    ENDPOINT_REGISTRY_SHA256,
    MODEL_REGISTRY_RAW_SHA256,
    MODEL_REGISTRY_SHA256,
    ROUTE_CONTRACT_BUNDLE_SHA256,
    ROUTING_TIERS_SHA256,
)
from omnimarket.rsd.route_contract_bundle import route_contract_bundle_sha256
from omnimarket.rsd.route_contract_view import ModelRsdSelectedRoute

__all__ = [
    "HandlerRsdOfflineDeliveryMatrix",
    "RsdOfflineDeliveryMatrixValidationError",
    "routing_tiers_sha256",
    "validate_rsd_offline_delivery_matrix",
]

_MAX_DEPTH = 16
_MAX_NODES = 4096
# Routing counts are carried by bounded runtime/API integer fields. Monetary
# values share the delegation budget store's NUMERIC(18, 6) business bound.
_MAX_ROUTING_INTEGER = 2_147_483_647
_MAX_USD_AMOUNT = Decimal("999999999999.999999")
_CANONICAL_INT = re.compile(r"^(?:0|[1-9][0-9]*)$")
_CANONICAL_NUMBER = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_CANONICAL_PRICING_DECIMAL = re.compile(r"^(?:0|[1-9][0-9]*)\.[0-9]{2}$")
_ROUTING_TOP = frozenset({"tiers"})
_TIER_FIELDS = frozenset(
    {
        "name",
        "cost_per_1k_tokens",
        "cost",
        "models",
        "eval_before_accept",
        "eval_model",
        "max_retries",
    }
)
_TIER_REQUIRED = _TIER_FIELDS - {"eval_model"}
_MODEL_FIELDS = frozenset(
    {"id", "backend_id", "max_context_tokens", "use_for", "fast_path_threshold_tokens"}
)
_MODEL_REQUIRED = _MODEL_FIELDS - {"fast_path_threshold_tokens"}
_COST_FIELDS = frozenset(
    {"cost_type", "rate_per_1k_usd", "monthly_cap_usd", "overage_rate_per_1k_usd"}
)
_BIFROST_TOP = frozenset(
    {
        "config_version",
        "schema_version",
        "backends",
        "routing_rules",
        "default_backends",
        "provider_quota_policy",
        "saturation_policy",
        "circuit_breaker",
        "failover",
        "shadow_mode",
    }
)
_BACKEND_REQUIRED = frozenset(
    {
        "backend_id",
        "model_name",
        "tier",
        "endpoint_url",
        "max_tokens",
        "timeout_ms",
        "capabilities",
    }
)
_BACKEND_FIELDS = _BACKEND_REQUIRED | frozenset(
    {"endpoint_url_env", "api_key_env", "api_key_ref", "secret_ref", "extra_headers"}
)
_ENDPOINT_FIELDS = frozenset(
    {
        "id",
        "base_url",
        "model_id",
        "provider",
        "capabilities",
        "context_window",
        "context_window_source",
        "cost_basis",
        "health_check_path",
        "declared_by",
        "declared_at",
        "endpoint_ref",
    }
)
_ENDPOINT_REQUIRED = _ENDPOINT_FIELDS - {"declared_at"}
_REGISTRY_TOP = frozenset(
    {
        "schema_version",
        "model_registry_version",
        "pricing_manifest_version",
        "observed_at",
        "models",
    }
)
_REGISTRY_REQUIRED = frozenset(
    {
        "model_id",
        "provider",
        "endpoint_env",
        "context_window",
        "pricing_per_1m_input",
        "pricing_per_1m_output",
        "cost_basis",
        "observed_at",
        "source",
    }
)
_REGISTRY_FIELDS = _REGISTRY_REQUIRED | frozenset(
    {
        "model_name",
        "served_model_names",
        "requires_api_key_env",
        "requires_secret_ref",
        "notes",
    }
)


class RsdOfflineDeliveryMatrixValidationError(ValueError):
    """Single fail-closed error surface for malformed or mismatched C0 input."""

    def __init__(self) -> None:
        super().__init__("offline delivery matrix validation failed")


class _NoDuplicateSafeLoader(yaml.SafeLoader):
    """Safe loader which refuses duplicate mapping keys."""


def _construct_no_duplicate_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = cast(Any, loader).construct_object(key_node, deep=deep)
        if type(key) is not str or key in result:
            raise ValueError("duplicate or non-string YAML key")
        result[key] = cast(Any, loader).construct_object(value_node, deep=deep)
    return result


_NoDuplicateSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_no_duplicate_mapping
)


def _fail() -> NoReturn:
    raise RsdOfflineDeliveryMatrixValidationError()


def routing_tiers_sha256(payload: bytes) -> str:
    """SHA-256 over exact caller-supplied bytes; this performs no resolution."""

    if type(payload) is not bytes or not 1 <= len(payload) <= 524288:
        _fail()
    return hashlib.sha256(payload).hexdigest()


def _nonblank(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _positive_int(value: object) -> bool:
    return type(value) is int and 0 < value <= _MAX_ROUTING_INTEGER


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_ROUTING_INTEGER


def _finite_number(value: object) -> bool:
    """Accept only finite, business-bounded numeric primitives.

    YAML yields ``int``/``float`` only, but this guard also safely rejects
    direct Decimal or oversized integer probes before any float conversion.
    """

    if type(value) is int:
        return -_MAX_USD_AMOUNT <= Decimal(value) <= _MAX_USD_AMOUNT
    if type(value) is not float or not math.isfinite(value):
        return False
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    return decimal.is_finite() and -_MAX_USD_AMOUNT <= decimal <= _MAX_USD_AMOUNT


def _nonnegative_number(value: object) -> bool:
    return _finite_number(value) and cast(int | float, value) >= 0


def _canonical_pricing_decimal(value: object) -> bool:
    if type(value) is not str or _CANONICAL_PRICING_DECIMAL.fullmatch(value) is None:
        return False
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, OverflowError, ValueError):
        return False
    return decimal.is_finite() and Decimal("0") <= decimal <= _MAX_USD_AMOUNT


def _string_list(value: object, *, unique: bool = False) -> bool:
    if type(value) is not list or not all(_nonblank(item) for item in value):
        return False
    items = cast(list[str], value)
    return not unique or len(items) == len(set(items))


def _exact_mapping(
    value: object, required: frozenset[str], allowed: frozenset[str] | None = None
) -> dict[str, object]:
    if type(value) is not dict:
        _fail()
    mapping = cast(dict[str, object], value)
    fields = set(mapping)
    if not required <= fields or not fields <= (
        allowed if allowed is not None else required
    ):
        _fail()
    return mapping


def _strict_use_for(value: object) -> tuple[str, ...]:
    """Accept only the canonical scalar-or-list task-class representation."""

    if _nonblank(value):
        return (cast(str, value),)
    if type(value) is not list or not all(_nonblank(item) for item in value):
        _fail()
    # The canonical DTO intentionally permits repeated task-class entries, so
    # preserve that representation rather than imposing a C0-only policy.
    return tuple(cast(list[str], value))


def _strict_tier_cost(value: object) -> ModelTierCost:
    """Prevalidate a cost row, then apply the canonical delegation DTO rules."""

    cost = _exact_mapping(value, frozenset({"cost_type"}), _COST_FIELDS)
    cost_type = cost["cost_type"]
    if type(cost_type) is not str:
        _fail()
    try:
        enum_cost_type = EnumTierCostType(cost_type)
    except ValueError:
        _fail()

    raw_rate = cost.get("rate_per_1k_usd", 0.0)
    raw_cap = cost.get("monthly_cap_usd")
    raw_overage = cost.get("overage_rate_per_1k_usd", 0.0)
    if (
        not _nonnegative_number(raw_rate)
        or (raw_cap is not None and not _nonnegative_number(raw_cap))
        or not _nonnegative_number(raw_overage)
    ):
        _fail()
    try:
        typed = ModelTierCost.model_validate(
            {
                "cost_type": enum_cost_type,
                "rate_per_1k_usd": float(cast(int | float, raw_rate)),
                "monthly_cap_usd": (
                    None if raw_cap is None else float(cast(int | float, raw_cap))
                ),
                "overage_rate_per_1k_usd": float(cast(int | float, raw_overage)),
            },
            strict=True,
        )
        return ModelTierCost.model_validate(
            typed.model_dump(mode="python"), strict=True
        )
    except (OverflowError, TypeError, ValidationError, ValueError):
        _fail()


def _validate_tree(
    value: object, *, depth: int = 1, count: list[int] | None = None
) -> None:
    nodes = count if count is not None else [0]
    nodes[0] += 1
    if depth > _MAX_DEPTH or nodes[0] > _MAX_NODES:
        raise ValueError("YAML bounds exceeded")
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ValueError("non-string YAML key")
            _validate_tree(item, depth=depth + 1, count=nodes)
    elif type(value) is list:
        for item in cast(list[object], value):
            _validate_tree(item, depth=depth + 1, count=nodes)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise ValueError("unsupported YAML scalar")


def _validate_scalar_nodes(node: yaml.Node) -> None:
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if (
                isinstance(key, yaml.ScalarNode)
                and key.value in {"pricing_per_1m_input", "pricing_per_1m_output"}
                and (
                    not isinstance(value, yaml.ScalarNode)
                    or value.style != '"'
                    or _CANONICAL_PRICING_DECIMAL.fullmatch(value.value) is None
                )
            ):
                raise ValueError("pricing must be a canonical quoted decimal")
            _validate_scalar_nodes(key)
            _validate_scalar_nodes(value)
        return
    if isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _validate_scalar_nodes(item)
        return
    if not isinstance(node, yaml.ScalarNode):
        raise ValueError("unsupported YAML node")
    if (
        node.tag == "tag:yaml.org,2002:int"
        and _CANONICAL_INT.fullmatch(node.value) is None
    ):
        raise ValueError("noncanonical YAML integer")
    if (
        node.tag == "tag:yaml.org,2002:float"
        and _CANONICAL_NUMBER.fullmatch(node.value) is None
    ):
        raise ValueError("noncanonical YAML number")
    if node.tag == "tag:yaml.org,2002:bool" and node.value not in {"true", "false"}:
        raise ValueError("noncanonical YAML boolean")
    if node.tag not in {
        "tag:yaml.org,2002:str",
        "tag:yaml.org,2002:int",
        "tag:yaml.org,2002:float",
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:null",
    }:
        raise ValueError("unsupported YAML scalar tag")


def _parse_yaml(payload: bytes) -> dict[str, object]:
    try:
        text = payload.decode("utf-8")
        for event in yaml.parse(text, Loader=_NoDuplicateSafeLoader):
            if (
                isinstance(event, yaml.events.AliasEvent)
                or getattr(event, "anchor", None) is not None
                or getattr(event, "tag", None) is not None
            ):
                raise ValueError("aliases, anchors, and explicit tags are forbidden")
        composed = yaml.compose(text, Loader=_NoDuplicateSafeLoader)
        if composed is None:
            raise ValueError("empty YAML")
        _validate_scalar_nodes(composed)
        document = yaml.load(text, Loader=_NoDuplicateSafeLoader)
        _validate_tree(document)
        if type(document) is not dict:
            raise ValueError("YAML must be a mapping")
        return cast(dict[str, object], document)
    except (UnicodeDecodeError, ValueError, yaml.YAMLError, RecursionError):
        _fail()


def _routing_selection(
    document: dict[str, object], selected: ModelRsdSelectedRoute
) -> tuple[str, int]:
    if set(document) != _ROUTING_TOP or type(document.get("tiers")) is not list:
        _fail()
    tier_names: set[str] = set()
    model_identities: set[tuple[str, str, str]] = set()
    matches: list[tuple[str, int]] = []
    canonical_tiers: list[ModelRoutingTier] = []
    for tier_value in cast(list[object], document["tiers"]):
        tier = _exact_mapping(tier_value, _TIER_REQUIRED, _TIER_FIELDS)
        if (
            not _nonblank(tier["name"])
            or not _nonnegative_number(tier["cost_per_1k_tokens"])
            or type(tier["models"]) is not list
            or type(tier["eval_before_accept"]) is not bool
            or not _nonnegative_int(tier["max_retries"])
            or (
                "eval_model" in tier
                and tier["eval_model"] is not None
                and not _nonblank(tier["eval_model"])
            )
        ):
            _fail()
        tier_name = cast(str, tier["name"])
        if tier_name in tier_names:
            _fail()
        tier_names.add(tier_name)
        typed_models: list[ModelTierModel] = []
        for row_value in cast(list[object], tier["models"]):
            row = _exact_mapping(row_value, _MODEL_REQUIRED, _MODEL_FIELDS)
            if (
                not _nonblank(row["id"])
                or not _nonblank(row["backend_id"])
                or not _positive_int(row["max_context_tokens"])
            ):
                _fail()
            if "fast_path_threshold_tokens" in row and not _nonnegative_int(
                row["fast_path_threshold_tokens"]
            ):
                _fail()
            use_for = _strict_use_for(row["use_for"])
            identity = (tier_name, cast(str, row["backend_id"]), cast(str, row["id"]))
            if identity in model_identities:
                _fail()
            model_identities.add(identity)
            try:
                typed_models.append(
                    ModelTierModel.model_validate(
                        {
                            "id": row["id"],
                            "backend_ref": row["backend_id"],
                            "max_context_tokens": row["max_context_tokens"],
                            "use_for": use_for,
                            "fast_path_threshold_tokens": row.get(
                                "fast_path_threshold_tokens"
                            ),
                        },
                        strict=True,
                    )
                )
            except (OverflowError, TypeError, ValidationError, ValueError):
                _fail()
            if (
                tier_name == selected.tier_name
                and row["backend_id"] == selected.backend_id
            ):
                matches.append(
                    (cast(str, row["id"]), cast(int, row["max_context_tokens"]))
                )
        try:
            canonical_tiers.append(
                ModelRoutingTier.model_validate(
                    {
                        "name": tier_name,
                        "models": tuple(typed_models),
                        "eval_before_accept": tier["eval_before_accept"],
                        "eval_model": tier.get("eval_model"),
                        "cost_per_1k_tokens": float(
                            cast(int | float, tier["cost_per_1k_tokens"])
                        ),
                        "cost": _strict_tier_cost(tier["cost"]),
                        "max_retries": tier["max_retries"],
                    },
                    strict=True,
                )
            )
        except (OverflowError, TypeError, ValidationError, ValueError):
            _fail()
    try:
        canonical = ModelDelegationConfig.model_validate(
            {"tiers": tuple(canonical_tiers)}, strict=True
        )
        ModelDelegationConfig.model_validate(
            canonical.model_dump(mode="python"), strict=True
        )
    except (OverflowError, TypeError, ValidationError, ValueError):
        _fail()
    if len(matches) != 1:
        _fail()
    return matches[0]


def _validate_bifrost_document(document: dict[str, object]) -> list[dict[str, object]]:
    if (
        set(document) != _BIFROST_TOP
        or not _nonblank(document["config_version"])
        or not _nonblank(document["schema_version"])
        or type(document["backends"]) is not list
    ):
        _fail()
    backends: list[dict[str, object]] = []
    backend_ids: set[str] = set()
    for value in cast(list[object], document["backends"]):
        backend = _exact_mapping(value, _BACKEND_REQUIRED, _BACKEND_FIELDS)
        if (
            not _nonblank(backend["backend_id"])
            or not _nonblank(backend["model_name"])
            or not _nonblank(backend["tier"])
            or type(backend["endpoint_url"]) not in {str, type(None)}
            or not _positive_int(backend["max_tokens"])
            or not _positive_int(backend["timeout_ms"])
            or not _string_list(backend["capabilities"], unique=True)
        ):
            _fail()
        for key in ("endpoint_url_env", "api_key_env", "api_key_ref", "secret_ref"):
            if (
                key in backend
                and backend[key] is not None
                and not _nonblank(backend[key])
            ):
                _fail()
        if (
            _nonblank(backend.get("secret_ref"))
            and _nonblank(backend.get("api_key_ref"))
            and backend["secret_ref"] != backend["api_key_ref"]
        ):
            _fail()
        if "extra_headers" in backend:
            headers = backend["extra_headers"]
            if type(headers) is not dict or not all(
                _nonblank(key) and _nonblank(value)
                for key, value in cast(dict[object, object], headers).items()
            ):
                _fail()
        backend_id = cast(str, backend["backend_id"])
        if backend_id in backend_ids:
            _fail()
        backend_ids.add(backend_id)
        backends.append(backend)
    rules = document["routing_rules"]
    rule_required = frozenset(
        {
            "rule_id",
            "priority",
            "task_class",
            "task_class_contract_version",
            "backend_policy_version",
            "match_operation_types",
            "match_capabilities",
            "backend_ids",
            "fallback_policy",
            "shadow_policy_id",
        }
    )
    rule_fields = rule_required | frozenset(
        {"latency_sla_ms", "cost_ceiling_usd_per_1k_tokens"}
    )
    if type(rules) is not list:
        _fail()
    rule_ids: set[str] = set()
    for value in cast(list[object], rules):
        rule = _exact_mapping(value, rule_required, rule_fields)
        fallback = _exact_mapping(
            rule["fallback_policy"], frozenset({"action", "max_retries", "on_exhaust"})
        )
        if (
            not _nonblank(rule["rule_id"])
            or not _nonnegative_int(rule["priority"])
            or not _nonblank(rule["task_class"])
            or not _nonblank(rule["task_class_contract_version"])
            or not _nonblank(rule["backend_policy_version"])
            or not _string_list(rule["match_operation_types"], unique=True)
            or not _string_list(rule["match_capabilities"], unique=True)
            or not _string_list(rule["backend_ids"], unique=True)
            or not _nonblank(rule["shadow_policy_id"])
            or not _nonblank(fallback["action"])
            or not _nonnegative_int(fallback["max_retries"])
            or not _nonblank(fallback["on_exhaust"])
        ):
            _fail()
        if "latency_sla_ms" in rule and not _positive_int(rule["latency_sla_ms"]):
            _fail()
        if "cost_ceiling_usd_per_1k_tokens" in rule and not _nonnegative_number(
            rule["cost_ceiling_usd_per_1k_tokens"]
        ):
            _fail()
        rule_id = cast(str, rule["rule_id"])
        if (
            rule_id in rule_ids
            or not set(cast(list[str], rule["backend_ids"])) <= backend_ids
        ):
            _fail()
        rule_ids.add(rule_id)
    defaults = document["default_backends"]
    if (
        not _string_list(defaults, unique=True)
        or not set(cast(list[str], defaults)) <= backend_ids
    ):
        _fail()
    quota = _exact_mapping(
        document["provider_quota_policy"],
        frozenset({"schema_version", "default_disposition", "providers"}),
    )
    if (
        not _nonblank(quota["schema_version"])
        or not _nonblank(quota["default_disposition"])
        or type(quota["providers"]) is not list
    ):
        _fail()
    provider_ids: set[str] = set()
    for value in cast(list[object], quota["providers"]):
        provider = _exact_mapping(
            value,
            frozenset({"provider_id", "match_endpoint_host", "codes"}),
            frozenset(
                {
                    "provider_id",
                    "match_endpoint_host",
                    "required_path_prefix",
                    "required_path_prefix_hint",
                    "codes",
                }
            ),
        )
        if (
            not _nonblank(provider["provider_id"])
            or not _nonblank(provider["match_endpoint_host"])
            or type(provider["codes"]) is not list
        ):
            _fail()
        provider_id = cast(str, provider["provider_id"])
        if provider_id in provider_ids:
            _fail()
        provider_ids.add(provider_id)
        for key in ("required_path_prefix", "required_path_prefix_hint"):
            if key in provider and not _nonblank(provider[key]):
                _fail()
        for code_value in cast(list[object], provider["codes"]):
            code = _exact_mapping(
                code_value,
                frozenset({"code", "disposition", "alert"}),
                frozenset(
                    {
                        "code",
                        "disposition",
                        "alert",
                        "alert_hint",
                        "reset_from",
                        "fallback_cooldown_seconds",
                    }
                ),
            )
            if (
                not _nonblank(code["code"])
                or not _nonblank(code["disposition"])
                or type(code["alert"]) is not bool
            ):
                _fail()
            for key in ("alert_hint", "reset_from"):
                if key in code and not _nonblank(code[key]):
                    _fail()
            if (
                "fallback_cooldown_seconds" in code
                and type(code["fallback_cooldown_seconds"]) is not int
            ):
                _fail()
    saturation = _exact_mapping(
        document["saturation_policy"],
        frozenset({"schema_version", "default_max_wait_ms", "tiers"}),
    )
    if (
        not _nonblank(saturation["schema_version"])
        or not _nonnegative_int(saturation["default_max_wait_ms"])
        or type(saturation["tiers"]) is not list
    ):
        _fail()
    seen_tiers: set[str] = set()
    for value in cast(list[object], saturation["tiers"]):
        row = _exact_mapping(
            value, frozenset({"tier", "max_wait_ms", "poll_interval_ms"})
        )
        if (
            not _nonblank(row["tier"])
            or not _nonnegative_int(row["max_wait_ms"])
            or not _positive_int(row["poll_interval_ms"])
            or cast(str, row["tier"]) in seen_tiers
        ):
            _fail()
        seen_tiers.add(cast(str, row["tier"]))
    breaker = _exact_mapping(
        document["circuit_breaker"], frozenset({"failure_threshold", "window_seconds"})
    )
    failover = _exact_mapping(
        document["failover"], frozenset({"max_attempts", "backoff_base_ms"})
    )
    shadow = _exact_mapping(
        document["shadow_mode"],
        frozenset(
            {
                "enabled",
                "policy_version",
                "log_sample_rate",
                "comparison_logging_enabled",
                "max_shadow_latency_ms",
            }
        ),
    )
    if (
        not _positive_int(breaker["failure_threshold"])
        or not _positive_int(breaker["window_seconds"])
        or not _positive_int(failover["max_attempts"])
        or not _positive_int(failover["backoff_base_ms"])
        or type(shadow["enabled"]) is not bool
        or not _nonblank(shadow["policy_version"])
        or not _nonnegative_number(shadow["log_sample_rate"])
        or type(shadow["comparison_logging_enabled"]) is not bool
        or not _nonnegative_number(shadow["max_shadow_latency_ms"])
    ):
        _fail()
    try:
        typed = ModelBifrostDelegationConfig.model_validate(document)
        ModelBifrostDelegationConfig.model_validate(
            typed.model_dump(mode="python"), strict=True
        )
    except (OverflowError, TypeError, ValidationError, ValueError):
        _fail()
    return backends


def _derive_bifrost_backend(
    document: dict[str, object], selected: ModelRsdSelectedRoute
) -> dict[str, object]:
    matches = [
        row
        for row in _validate_bifrost_document(document)
        if row["backend_id"] == selected.backend_id
    ]
    if len(matches) != 1 or matches[0]["tier"] != selected.tier_name:
        _fail()
    return matches[0]


def _derive_endpoint(
    document: dict[str, object], served_model: str
) -> dict[str, object]:
    if (
        set(document) != {"registry_schema_version", "endpoints"}
        or not _nonblank(document["registry_schema_version"])
        or type(document["endpoints"]) is not list
    ):
        _fail()
    ids: set[str] = set()
    matches: list[dict[str, object]] = []
    typed_endpoints: list[ModelRsdEndpointContract] = []
    for value in cast(list[object], document["endpoints"]):
        endpoint = _exact_mapping(value, _ENDPOINT_REQUIRED, _ENDPOINT_FIELDS)
        if (
            not all(
                _nonblank(endpoint[key])
                for key in {"id", "base_url", "model_id", "provider"}
            )
            or type(endpoint["context_window"]) not in {int, type(None)}
            or (
                endpoint["context_window"] is not None
                and not _positive_int(endpoint["context_window"])
            )
            or not _string_list(endpoint["capabilities"], unique=True)
            or not all(
                type(endpoint[key]) is str
                for key in {
                    "context_window_source",
                    "cost_basis",
                    "health_check_path",
                    "declared_by",
                    "endpoint_ref",
                }
            )
        ):
            _fail()
        endpoint_id, model_id = (
            cast(str, endpoint["id"]),
            cast(str, endpoint["model_id"]),
        )
        if endpoint_id in ids:
            _fail()
        ids.add(endpoint_id)
        if model_id == served_model:
            matches.append(endpoint)
        try:
            typed_endpoint = ModelRsdEndpointContract.model_validate(endpoint)
            typed_endpoints.append(
                ModelRsdEndpointContract.model_validate(
                    typed_endpoint.model_dump(mode="python"), strict=True
                )
            )
        except (OverflowError, TypeError, ValidationError, ValueError):
            _fail()
    if len(matches) != 1:
        _fail()
    if len(typed_endpoints) != len(ids) or matches[0]["context_window"] is None:
        _fail()
    return matches[0]


def _strict_registry_document(document: dict[str, object]) -> ModelLlmModelRegistry:
    if (
        set(document) != _REGISTRY_TOP
        or not all(_nonblank(document[key]) for key in _REGISTRY_TOP - {"models"})
        or type(document["models"]) is not dict
    ):
        _fail()
    models = cast(dict[str, object], document["models"])
    if not models:
        _fail()
    for key, value in models.items():
        if not _nonblank(key):
            _fail()
        profile = _exact_mapping(value, _REGISTRY_REQUIRED, _REGISTRY_FIELDS)
        if (
            profile["model_id"] != key
            or not all(
                _nonblank(profile[field])
                for field in _REGISTRY_REQUIRED
                - {"context_window", "pricing_per_1m_input", "pricing_per_1m_output"}
            )
            or not _positive_int(profile["context_window"])
            or not _canonical_pricing_decimal(profile["pricing_per_1m_input"])
            or not _canonical_pricing_decimal(profile["pricing_per_1m_output"])
        ):
            _fail()
        if "model_name" in profile and not _nonblank(profile["model_name"]):
            _fail()
        if "served_model_names" in profile:
            served_model_names = profile["served_model_names"]
            if (
                type(served_model_names) is not dict
                or not served_model_names
                or not all(
                    _nonblank(environment) and _nonblank(served_name)
                    for environment, served_name in cast(
                        dict[object, object], served_model_names
                    ).items()
                )
            ):
                _fail()
        for optional in ("requires_api_key_env", "requires_secret_ref", "notes"):
            if optional in profile and not _nonblank(profile[optional]):
                _fail()
    try:
        registry = ModelLlmModelRegistry.model_validate(document)
        return ModelLlmModelRegistry.model_validate(
            registry.model_dump(mode="python"), strict=True
        )
    except (OverflowError, TypeError, ValidationError, ValueError):
        _fail()


def _revalidate(
    request: ModelRsdOfflineDeliveryMatrixInput,
) -> ModelRsdOfflineDeliveryMatrixInput:
    try:
        if (
            type(request) is not ModelRsdOfflineDeliveryMatrixInput
            or request.__pydantic_extra__
        ):
            raise ValueError("invalid request")
        return ModelRsdOfflineDeliveryMatrixInput.model_validate(
            request.model_dump(mode="python"), strict=True
        )
    except (AttributeError, OverflowError, TypeError, ValidationError, ValueError):
        _fail()


def validate_rsd_offline_delivery_matrix(
    request: ModelRsdOfflineDeliveryMatrixInput,
) -> ModelRsdOfflineDeliveryMatrixOutput:
    """Validate one pinned C0 route bundle without I/O, effects, or authority."""

    checked = _revalidate(request)
    view = checked.route_contract
    routing_hash = routing_tiers_sha256(view.routing_tiers_yaml)
    bifrost_hash = routing_tiers_sha256(view.bifrost_delegation_yaml)
    endpoint_hash = routing_tiers_sha256(view.endpoint_registry_yaml)
    registry_raw_hash = routing_tiers_sha256(view.model_registry_yaml)
    pins = {
        "routing_tiers_raw_sha256": routing_hash,
        "bifrost_delegation_raw_sha256": bifrost_hash,
        "endpoint_registry_raw_sha256": endpoint_hash,
        "model_registry_raw_sha256": registry_raw_hash,
        "model_registry_canonical_sha256": MODEL_REGISTRY_SHA256,
    }
    if (
        routing_hash != ROUTING_TIERS_SHA256
        or bifrost_hash != BIFROST_DELEGATION_SHA256
        or endpoint_hash != ENDPOINT_REGISTRY_SHA256
        or registry_raw_hash != MODEL_REGISTRY_RAW_SHA256
        or route_contract_bundle_sha256(pins) != ROUTE_CONTRACT_BUNDLE_SHA256
    ):
        _fail()
    _route_model_id, route_context = _routing_selection(
        _parse_yaml(view.routing_tiers_yaml), view.selected_route
    )
    backend = _derive_bifrost_backend(
        _parse_yaml(view.bifrost_delegation_yaml), view.selected_route
    )
    served_model = cast(str, backend["model_name"])
    endpoint = _derive_endpoint(_parse_yaml(view.endpoint_registry_yaml), served_model)
    registry = _strict_registry_document(_parse_yaml(view.model_registry_yaml))
    if registry.model_registry_hash != MODEL_REGISTRY_SHA256:
        _fail()
    profile_candidates = [
        profile
        for profile in registry.models.values()
        if profile.model_name == served_model
    ]
    if len(profile_candidates) != 1 and _nonblank(backend.get("endpoint_url_env")):
        profile_candidates = [
            profile
            for profile in profile_candidates
            if profile.endpoint_env == backend["endpoint_url_env"]
        ]
    if len(profile_candidates) != 1:
        _fail()
    profile = profile_candidates[0]
    endpoint_context = cast(int, endpoint["context_window"])
    if (
        profile.model_id != view.selected_route.model_registry_key
        or profile.model_name != served_model
        or backend["model_name"] != served_model
        or endpoint["model_id"] != served_model
        or route_context > endpoint_context
        or route_context > profile.context_window
    ):
        _fail()
    return ModelRsdOfflineDeliveryMatrixOutput(
        routing_tiers_sha256=routing_hash,
        bifrost_delegation_sha256=bifrost_hash,
        endpoint_registry_sha256=endpoint_hash,
        model_registry_raw_sha256=registry_raw_hash,
        model_registry_hash=registry.model_registry_hash,
        route_contract_bundle_sha256=ROUTE_CONTRACT_BUNDLE_SHA256,
        tier_name=view.selected_route.tier_name,
        backend_id=view.selected_route.backend_id,
        model_registry_key=view.selected_route.model_registry_key,
        served_model=served_model,
        endpoint_provider=cast(str, endpoint["provider"]),
        registry_provider_class=profile.provider,
        route_max_context_tokens=route_context,
        endpoint_context_window=endpoint_context,
    )


class HandlerRsdOfflineDeliveryMatrix:
    """Pure ONEX COMPUTE handler for offline C0 validation."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self, request: ModelRsdOfflineDeliveryMatrixInput
    ) -> ModelRsdOfflineDeliveryMatrixOutput:
        return validate_rsd_offline_delivery_matrix(request)
