# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed inference protocol request-shaping config."""

from __future__ import annotations

import fnmatch
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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

    env_path = os.environ.get(_CONFIG_PATH_ENV, "").strip()
    resolved = Path(config_path or env_path or _DEFAULT_CONFIG_PATH)
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

    resolved_config = config or load_inference_protocol_config()
    next_system_prompt = system_prompt
    next_prompt = prompt
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
    return next_system_prompt, next_prompt


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
    "apply_inference_protocol_directives",
    "load_inference_protocol_config",
]
