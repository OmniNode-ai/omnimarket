# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve a context profile by name from the YAML data under ``pack/profiles``."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from pydantic import ValidationError

from omnimarket.inference.delegation_config_provenance import resolve_path_config
from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_request import (
    ModelContextProfileResolveRequest,
)
from omnimarket.models.context_profile_resolve.model_context_profile_resolve_result import (
    EnumContextProfileResolveStatus,
    ModelContextProfileResolveResult,
)
from omnimarket.pack.model_context_profile import ModelContextProfile
from omnimarket.routing.task_class_contract_path import (
    TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH,
    TASK_CLASS_CONTRACT_PATH_ENV_KEY,
)

_PROFILES_DIR = Path(__file__).resolve().parents[3] / "pack" / "profiles"
_PROFILE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_MODEL_ROLE_FIELDS = (
    "extraction_task_class",
    "selection_task_class",
    "summary_task_class",
)


def _failure(
    name: str, failure_class: EnumContextPackFailure, *errors: str
) -> ModelContextProfileResolveResult:
    return ModelContextProfileResolveResult(
        status=EnumContextProfileResolveStatus.FAILED,
        profile_name=name,
        failure_class=failure_class,
        errors=tuple(errors),
    )


def _model_role_errors(profile: ModelContextProfile) -> tuple[str, ...]:
    roles = {
        field: task_class
        for field in _MODEL_ROLE_FIELDS
        if (task_class := getattr(profile, field)) is not None
    }
    if not roles:
        return ()
    try:
        path, _ = resolve_path_config(
            TASK_CLASS_CONTRACT_PATH_ENV_KEY,
            TASK_CLASS_CONTRACT_PACKAGED_DEFAULT_PATH,
        )
        authority = load_task_class_authority(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return (f"model-role routing contract could not be loaded: {exc}",)

    errors: list[str] = []
    for field, task_class in roles.items():
        if task_class not in authority.task_classes:
            errors.append(f"{field}: no routing-contract entry for {task_class!r}")
        elif refusal := authority.unroutable_refusal(task_class):
            errors.append(f"{field}: {refusal}")
    return tuple(errors)


class HandlerContextProfileResolver:
    """One resolver by profile name; a name never reaches the filesystem as a path."""

    def __init__(self, profiles_dir: Path | None = None) -> None:
        self._profiles_dir = profiles_dir if profiles_dir is not None else _PROFILES_DIR

    def handle(
        self, request: ModelContextProfileResolveRequest
    ) -> ModelContextProfileResolveResult:
        name = request.profile_name
        path = self._profiles_dir / f"{name}.yaml"
        if not _PROFILE_NAME.fullmatch(name) or not path.is_file():
            return _failure(
                name,
                EnumContextPackFailure.PROFILE_NOT_FOUND,
                f"no context profile named {name!r}",
            )
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            return _failure(name, EnumContextPackFailure.INVALID_PROFILE, str(exc))
        if not isinstance(loaded, dict):
            return _failure(
                name,
                EnumContextPackFailure.INVALID_PROFILE,
                f"profile {name!r} is not a YAML mapping",
            )
        try:
            profile = ModelContextProfile.model_validate(loaded)
        except ValidationError as exc:
            return _failure(
                name,
                EnumContextPackFailure.INVALID_PROFILE,
                *(
                    f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                    for err in exc.errors()
                ),
            )
        if errors := _model_role_errors(profile):
            return _failure(name, EnumContextPackFailure.INVALID_PROFILE, *errors)
        return ModelContextProfileResolveResult(
            status=EnumContextProfileResolveStatus.OK,
            profile_name=name,
            profile=profile,
            profile_content_hash=profile.content_hash(),
        )


__all__ = ["HandlerContextProfileResolver"]
