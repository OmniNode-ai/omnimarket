from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import ValidationError

from omnimarket.config.settings import Settings
from omnimarket.nodes.node_env_parity_compute.models.model_env_parity_compute_request import (
    EnumEnvParityConsistency,
    ModelEnvParityComputeRequest,
    ModelEnvParityContractConfig,
    ModelEnvParityVariableRule,
)
from omnimarket.nodes.node_env_parity_compute.models.model_env_parity_compute_result import (
    ModelEnvParityComputeResult,
    ModelEnvParityGap,
    ModelEnvParityLaneVariableResult,
)


class HandlerEnvParityCompute:
    """Contract-driven env parity checker for runtime lane snapshots."""

    def __init__(self, contract_path: Path | None = None) -> None:
        self._contract_path = contract_path or Path(__file__).resolve().parents[1] / (
            "contract.yaml"
        )

    def handle(
        self, request: ModelEnvParityComputeRequest
    ) -> ModelEnvParityComputeResult:
        config_result = self._load_contract_config()
        if isinstance(config_result, str):
            return _error_result(request, config_result)
        config = config_result

        lanes_result = _select_lanes(request, config)
        if isinstance(lanes_result, str):
            return _error_result(request, lanes_result)
        lanes = lanes_result

        variables_result = _select_variables(request, config)
        if isinstance(variables_result, str):
            return _error_result(request, variables_result)
        variables = variables_result

        lane_results: list[ModelEnvParityLaneVariableResult] = []
        gaps: list[ModelEnvParityGap] = []
        settings_validation_errors = _settings_validation_errors(
            request.env_by_lane, lanes
        )
        for lane, errors in settings_validation_errors.items():
            gaps.extend(
                ModelEnvParityGap(
                    lane=lane,
                    variable_name="Settings",
                    reason="settings_validation",
                    detail=error,
                )
                for error in errors
            )

        for rule in variables:
            candidate_lanes = rule.required_lanes or lanes
            unknown_rule_lanes = [lane for lane in candidate_lanes if lane not in lanes]
            if unknown_rule_lanes:
                return _error_result(
                    request,
                    "contract env_parity variable "
                    f"{rule.name} references lanes outside selected lanes: "
                    + ", ".join(unknown_rule_lanes),
                )
            fingerprints_by_lane: dict[str, str] = {}
            for lane in candidate_lanes:
                lane_env = request.env_by_lane.get(lane)
                lane_requires_rule = _rule_required_in_lane(rule, lane_env)
                if lane_env is None:
                    if lane_requires_rule:
                        gaps.append(
                            ModelEnvParityGap(
                                lane=lane,
                                variable_name=rule.name,
                                reason="lane_missing",
                                detail=f"missing env snapshot for lane {lane}",
                            )
                        )
                    lane_results.append(
                        ModelEnvParityLaneVariableResult(
                            lane=lane,
                            variable_name=rule.name,
                            present=False,
                        )
                    )
                    continue

                raw_value = lane_env.get(rule.name)
                present = _has_text(raw_value)
                fingerprint = _fingerprint(raw_value) if present else None
                if fingerprint is not None:
                    fingerprints_by_lane[lane] = fingerprint
                lane_results.append(
                    ModelEnvParityLaneVariableResult(
                        lane=lane,
                        variable_name=rule.name,
                        present=present,
                        fingerprint=fingerprint,
                    )
                )
                if lane_requires_rule and not present:
                    gaps.append(
                        ModelEnvParityGap(
                            lane=lane,
                            variable_name=rule.name,
                            reason="missing_required_env",
                            detail=f"{rule.name} is required in lane {lane}",
                        )
                    )

            if (
                rule.consistency == EnumEnvParityConsistency.FINGERPRINT
                and len(set(fingerprints_by_lane.values())) > 1
            ):
                divergent_lanes = ", ".join(sorted(fingerprints_by_lane))
                gaps.append(
                    ModelEnvParityGap(
                        lane=",".join(sorted(fingerprints_by_lane)),
                        variable_name=rule.name,
                        reason="value_mismatch",
                        detail=(
                            f"{rule.name} fingerprints differ across lanes: "
                            f"{divergent_lanes}"
                        ),
                    )
                )

        gaps_sorted = sorted(
            gaps, key=lambda gap: (gap.variable_name, gap.lane, gap.reason, gap.detail)
        )
        return ModelEnvParityComputeResult(
            status="passed" if not gaps_sorted else "gaps_detected",
            parity_ok=not gaps_sorted,
            scope=request.scope,
            lanes_checked=lanes,
            variables_checked=sorted(rule.name for rule in variables),
            lane_results=sorted(
                lane_results,
                key=lambda result: (result.variable_name, result.lane),
            ),
            gaps=gaps_sorted,
            settings_validation_errors=settings_validation_errors,
            correlation_id=request.correlation_id,
        )

    def _load_contract_config(self) -> ModelEnvParityContractConfig | str:
        try:
            raw_contract = yaml.safe_load(
                self._contract_path.read_text(encoding="utf-8")
            )
        except (OSError, yaml.YAMLError) as exc:
            return f"failed to read env parity contract: {exc}"
        if not isinstance(raw_contract, dict):
            return "env parity contract must parse to a mapping"
        raw_config = raw_contract.get("env_parity")
        if not isinstance(raw_config, dict):
            return "contract is missing env_parity config"
        try:
            return ModelEnvParityContractConfig.model_validate(raw_config)
        except ValidationError as exc:
            return "invalid env_parity contract config: " + exc.json(include_url=False)


def _select_lanes(
    request: ModelEnvParityComputeRequest,
    config: ModelEnvParityContractConfig,
) -> list[str] | str:
    lanes = request.lanes or config.lanes
    unknown = [lane for lane in lanes if lane not in config.lanes]
    if unknown:
        return "requested lanes are not declared by contract: " + ", ".join(unknown)
    return lanes


def _select_variables(
    request: ModelEnvParityComputeRequest,
    config: ModelEnvParityContractConfig,
) -> list[ModelEnvParityVariableRule] | str:
    if not request.variable_names:
        return config.variables
    by_name = {rule.name: rule for rule in config.variables}
    unknown = [name for name in request.variable_names if name not in by_name]
    if unknown:
        return "requested variables are not declared by contract: " + ", ".join(unknown)
    return [by_name[name] for name in request.variable_names]


def _settings_validation_errors(
    env_by_lane: dict[str, dict[str, str | None]], lanes: list[str]
) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    env_to_field = {
        field_name.upper(): field_name for field_name in Settings.model_fields
    }
    for lane in lanes:
        raw_env = env_by_lane.get(lane, {})
        settings_kwargs: dict[str, object] = {}
        for env_name, raw_value in raw_env.items():
            field_name = env_to_field.get(env_name.upper())
            if field_name is not None and raw_value is not None:
                settings_kwargs[field_name] = raw_value
        try:
            settings = Settings.model_validate(settings_kwargs)
        except ValidationError as exc:
            errors[lane] = [
                f"Settings parse error: {item['loc']}: {item['msg']}"
                for item in cast(list[dict[str, Any]], exc.errors(include_url=False))
            ]
            continue
        service_errors = settings.validate_required_services()
        if service_errors:
            errors[lane] = service_errors
    return errors


def _error_result(
    request: ModelEnvParityComputeRequest, message: str
) -> ModelEnvParityComputeResult:
    return ModelEnvParityComputeResult(
        status="error",
        parity_ok=False,
        scope=request.scope,
        correlation_id=request.correlation_id,
        error=message,
    )


def _has_text(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _rule_required_in_lane(
    rule: ModelEnvParityVariableRule, lane_env: dict[str, str | None] | None
) -> bool:
    if rule.required_when is None:
        return True
    if lane_env is None:
        return True
    return str(lane_env.get(rule.required_when) or "").strip().lower() == "true"


def _fingerprint(value: str | None) -> str:
    if value is None:
        return ""
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:16]


__all__ = ["HandlerEnvParityCompute"]
