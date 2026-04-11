"""Model inference endpoint prober.

For every LLM_*_URL env var, check GET /v1/models returns HTTP 200 with at least one model.
"""

from __future__ import annotations

import os
import re

import httpx
from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_environment_health_scanner.handlers.handler_environment_health_scanner import (
    EnumHealthFindingSeverity,
    EnumSubsystem,
    ModelHealthFinding,
    ModelSubsystemResult,
    aggregate_status,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)

_LLM_URL_PATTERN = re.compile(r"^LLM_.+_URL$")
_DEFAULT_TIMEOUT = 5.0


class ModelEndpointSpec(BaseModel):
    """Spec for a single model inference endpoint to probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    env_var: str
    url: str


def probe_model_endpoints(
    specs: list[ModelEndpointSpec] | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT,
) -> ModelSubsystemResult:
    findings: list[ModelHealthFinding] = []

    if specs is None:
        specs = _collect_specs_from_env()

    for spec in specs:
        models_url = spec.url.rstrip("/") + "/v1/models"
        try:
            resp = httpx.get(models_url, timeout=timeout_seconds)
            if resp.status_code != 200:
                findings.append(
                    ModelHealthFinding(
                        subsystem=EnumSubsystem.MODEL_ENDPOINTS,
                        severity=EnumHealthFindingSeverity.FAIL,
                        subject=spec.env_var,
                        message=f"Endpoint {spec.env_var} ({models_url}) returned HTTP {resp.status_code}",
                        evidence=f"GET {models_url}",
                    )
                )
                continue
            try:
                body = resp.json()
            except Exception:
                findings.append(
                    ModelHealthFinding(
                        subsystem=EnumSubsystem.MODEL_ENDPOINTS,
                        severity=EnumHealthFindingSeverity.WARN,
                        subject=spec.env_var,
                        message=f"Endpoint {spec.env_var} returned non-JSON body",
                        evidence=f"GET {models_url}",
                    )
                )
                continue
            models = body.get("data", [])
            if not models:
                findings.append(
                    ModelHealthFinding(
                        subsystem=EnumSubsystem.MODEL_ENDPOINTS,
                        severity=EnumHealthFindingSeverity.WARN,
                        subject=spec.env_var,
                        message=f"Endpoint {spec.env_var} returned no models in /v1/models response",
                        evidence=f'GET {models_url} → {{"data": []}}',
                    )
                )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as exc:
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.MODEL_ENDPOINTS,
                    severity=EnumHealthFindingSeverity.FAIL,
                    subject=spec.env_var,
                    message=f"Endpoint {spec.env_var} ({models_url}) unreachable: {type(exc).__name__}",
                    evidence=f"GET {models_url}",
                )
            )

    status = aggregate_status(findings) if findings else EnumReadinessStatus.PASS
    return ModelSubsystemResult(
        subsystem=EnumSubsystem.MODEL_ENDPOINTS,
        status=status,
        check_count=len(specs),
        valid_zero=True,
        findings=findings,
        evidence_source="GET /v1/models per LLM_*_URL env var",
    )


def _collect_specs_from_env() -> list[ModelEndpointSpec]:
    specs = []
    for key, val in os.environ.items():
        if _LLM_URL_PATTERN.match(key) and val.startswith("http"):
            specs.append(ModelEndpointSpec(env_var=key, url=val))
    return specs
