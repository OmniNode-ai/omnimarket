# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inference protocol request-shaping config."""

from __future__ import annotations

import fnmatch
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from omnimarket.inference.delegation_config_provenance import resolve_path_config

_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "configs" / "inference_protocols.v1.yaml"
)
_CONFIG_PATH_ENV = "INFERENCE_PROTOCOL_CONFIG_PATH"


class ModelInferencePromptDirective(BaseModel):
    """Outbound prompt directive applied at the provider-call boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    placement: Literal["user_prefix", "system_suffix"] = Field(
        ...,
        description="Where the directive is inserted in the outbound message set.",
    )
    text: str = Field(..., min_length=1, description="Directive text to insert.")
    separator: str = Field(
        default="\n",
        description="Separator between directive text and existing content.",
    )
    apply_once: bool = Field(
        default=True,
        description="Avoid duplicate insertion when the prompt already carries the directive.",
    )

    @field_validator("text", "separator")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if value == "":
            raise ValueError("value must be non-empty")
        return value


class ModelInferenceProtocolProfile(BaseModel):
    """Match rule for applying provider request directives."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(..., min_length=1)
    enabled: bool = True
    backend_ids: tuple[str, ...] = Field(default_factory=tuple)
    task_types: tuple[str, ...] = Field(default_factory=tuple)
    model_name_patterns: tuple[str, ...] = Field(default_factory=tuple)
    system_prompt_contains: tuple[str, ...] = Field(default_factory=tuple)
    directive: ModelInferencePromptDirective
    request_options: dict[str, Any] = Field(default_factory=dict)


class ModelInferenceProtocolSelection(BaseModel):
    """Caller-owned selection of one configured provider protocol profile.

    Most callers intentionally retain the legacy automatic profile matching.
    A caller that has a durable output contract can instead select one exact
    profile.  The selected profile is still checked against its declared model,
    backend, and task applicability constraints before any request is sent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str = Field(..., min_length=1)
    task_type: str | None = Field(default=None, min_length=1)
    backend_id: str | None = Field(default=None, min_length=1)


class ModelInferenceProtocolConfig(BaseModel):
    """Root config for provider request-shaping profiles."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["inference_protocols.v1"]
    profiles: tuple[ModelInferenceProtocolProfile, ...] = Field(default_factory=tuple)


def _read_config(path: Path) -> ModelInferenceProtocolConfig:
    if not path.exists():
        raise FileNotFoundError(f"inference protocol config not found at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected YAML mapping at root for {path}")
    try:
        return ModelInferenceProtocolConfig.model_validate(data)
    except ValidationError as exc:
        msg = f"inference protocol config schema validation failed: {exc}"
        raise ValueError(msg) from exc


def load_inference_protocol_config(
    config_path: str | Path | None = None,
) -> ModelInferenceProtocolConfig:
    """Load provider protocol directives from a typed YAML config."""

    if config_path is not None:
        resolved = Path(config_path)
    else:
        # Resolve the env selector through the delegation-path provenance surface
        # (OMN-12967) so a cold runtime logs which protocol config it loaded.
        resolved, _ = resolve_path_config(_CONFIG_PATH_ENV, _DEFAULT_CONFIG_PATH)
    return _load_inference_protocol_config_cached(str(resolved))


@lru_cache(maxsize=8)
def _load_inference_protocol_config_cached(
    config_path: str,
) -> ModelInferenceProtocolConfig:
    return _read_config(Path(config_path))


def apply_inference_protocol_directives(
    *,
    system_prompt: str,
    prompt: str,
    model: str,
    task_type: str | None = None,
    backend_id: str | None = None,
    config: ModelInferenceProtocolConfig | None = None,
) -> tuple[str, str]:
    """Return outbound ``(system_prompt, prompt)`` after configured directives."""

    next_system_prompt, next_prompt, _ = apply_inference_protocol(
        system_prompt=system_prompt,
        prompt=prompt,
        model=model,
        task_type=task_type,
        backend_id=backend_id,
        config=config,
    )
    return next_system_prompt, next_prompt


def apply_inference_protocol(
    *,
    system_prompt: str,
    prompt: str,
    model: str,
    task_type: str | None = None,
    backend_id: str | None = None,
    selection: ModelInferenceProtocolSelection | None = None,
    config: ModelInferenceProtocolConfig | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Return outbound prompt data and provider request options.

    With ``selection`` set, only that exact profile is considered.  Unknown,
    disabled, or inapplicable profiles fail closed instead of quietly falling
    back to auto-matching a prompt literal.
    """

    resolved_config = config or load_inference_protocol_config()
    if selection is not None:
        if task_type is not None or backend_id is not None:
            raise ValueError(
                "selection owns task_type and backend_id; do not pass duplicate "
                "protocol match arguments"
            )
        selected_profile = _resolve_selected_profile(
            selection,
            system_prompt=system_prompt,
            model=model,
            config=resolved_config,
        )
        next_system_prompt, next_prompt = _apply_directive(
            selected_profile.directive,
            system_prompt=system_prompt,
            prompt=prompt,
        )
        return next_system_prompt, next_prompt, dict(selected_profile.request_options)

    next_system_prompt = system_prompt
    next_prompt = prompt
    request_options: dict[str, Any] = {}
    for profile in resolved_config.profiles:
        if not _profile_matches(
            profile,
            system_prompt=next_system_prompt,
            model=model,
            task_type=task_type,
            backend_id=backend_id,
        ):
            continue
        next_system_prompt, next_prompt = _apply_directive(
            profile.directive,
            system_prompt=next_system_prompt,
            prompt=next_prompt,
        )
        request_options = _merge_request_options(
            request_options,
            profile.request_options,
        )
    return next_system_prompt, next_prompt, request_options


def _resolve_selected_profile(
    selection: ModelInferenceProtocolSelection,
    *,
    system_prompt: str,
    model: str,
    config: ModelInferenceProtocolConfig | None = None,
) -> ModelInferenceProtocolProfile:
    """Resolve one profile and prove it applies to the outbound request."""

    resolved_config = config or load_inference_protocol_config()
    profile = next(
        (
            candidate
            for candidate in resolved_config.profiles
            if candidate.profile_id == selection.profile_id
        ),
        None,
    )
    if profile is None:
        raise ValueError(
            f"unknown inference protocol profile: {selection.profile_id!r}"
        )
    if not profile.enabled:
        raise ValueError(
            f"inference protocol profile is disabled: {selection.profile_id!r}"
        )
    if not _profile_matches(
        profile,
        system_prompt=system_prompt,
        model=model,
        task_type=selection.task_type,
        backend_id=selection.backend_id,
    ):
        raise ValueError(
            "inference protocol profile does not apply to the requested "
            f"model/task/backend: {selection.profile_id!r}"
        )
    return profile


def _merge_request_options(
    base: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_request_options(existing, value)
        else:
            merged[key] = value
    return merged


def _profile_matches(
    profile: ModelInferenceProtocolProfile,
    *,
    system_prompt: str,
    model: str,
    task_type: str | None,
    backend_id: str | None,
) -> bool:
    if not profile.enabled:
        return False
    if profile.backend_ids and backend_id not in profile.backend_ids:
        return False
    if profile.model_name_patterns and not any(
        fnmatch.fnmatchcase(model.lower(), pattern.lower())
        for pattern in profile.model_name_patterns
    ):
        return False
    if profile.task_types:
        if task_type is not None:
            return task_type in profile.task_types
        if not profile.system_prompt_contains:
            return False
    if profile.system_prompt_contains and task_type is None:
        normalized = system_prompt.lower()
        return any(
            marker.lower() in normalized for marker in profile.system_prompt_contains
        )
    return True


def _apply_directive(
    directive: ModelInferencePromptDirective,
    *,
    system_prompt: str,
    prompt: str,
) -> tuple[str, str]:
    if directive.placement == "user_prefix":
        if directive.apply_once and prompt.lstrip().startswith(directive.text):
            return system_prompt, prompt
        return system_prompt, f"{directive.text}{directive.separator}{prompt}"

    if directive.apply_once and directive.text in system_prompt:
        return system_prompt, prompt
    return f"{system_prompt}{directive.separator}{directive.text}", prompt


__all__ = [
    "ModelInferencePromptDirective",
    "ModelInferenceProtocolConfig",
    "ModelInferenceProtocolProfile",
    "ModelInferenceProtocolSelection",
    "apply_inference_protocol",
    "apply_inference_protocol_directives",
    "load_inference_protocol_config",
]
