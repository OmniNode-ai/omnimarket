# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The 21 private-value overlay keys and how each is read.

A key is ``<overlay id>.<field>``. :func:`resolve_overlay_key` reads it from an explicit value, then
the environment variable the key lists (only ``lab_hosts.hosts`` and
``lab_hosts.registry_root_candidates`` list one), then the field of ``<root>/<id>/overlay.yaml``
under the first root in ``ONEX_SKILL_OVERLAY_ROOTS`` that holds one. When none supplies it the
reader raises :class:`NodeOverlayError` naming the key, the variables that supply it and the
overlay file. There is no packaged fallback value.

The two ``*_home_env(s)`` keys hold variable *names*; :func:`resolve_clone_home` reads the clone path
they name.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, TypeAdapter, ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
    not_configured_message,
)
from omnimarket.models.overlay_keys import (
    ModelHygieneVocabOverlay,
    ModelIdleSlotFillOverlay,
    ModelLabHostsOverlay,
    ModelLabHostsTable,
    ModelRemoteLaneOverlay,
    ModelReposOverlay,
    ModelThroughputTickOverlay,
)

LAB_HOSTS_FILE_ENV = "ONEX_LAB_RUN_HOSTS"
REGISTRY_ROOT_ENV = "ONEX_REGISTRY_ROOT"

OVERLAY_DOCUMENT_MODELS: Mapping[str, type[BaseModel]] = {
    "repos": ModelReposOverlay,
    "lab_hosts": ModelLabHostsOverlay,
    "hygiene_vocab": ModelHygieneVocabOverlay,
    "remote_lane": ModelRemoteLaneOverlay,
    "idle_slot_fill": ModelIdleSlotFillOverlay,
    "throughput_tick": ModelThroughputTickOverlay,
}

# Fields of a document that are not overlay keys: the table shape carries them.
_NON_KEY_FIELDS: Mapping[str, frozenset[str]] = {"lab_hosts": frozenset({"bus_lane"})}

_KEY_ENV_VARS: Mapping[str, tuple[str, ...]] = {
    "lab_hosts.hosts": (LAB_HOSTS_FILE_ENV,),
    "lab_hosts.registry_root_candidates": (REGISTRY_ROOT_ENV,),
}


@dataclass(frozen=True)
class OverlayKeySpec:
    """One overlay key: its overlay id, its field and the variables that supply it besides the overlay."""

    overlay_id: str
    field: str
    env_vars: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.overlay_id}.{self.field}"


def _build_specs() -> tuple[OverlayKeySpec, ...]:
    specs: list[OverlayKeySpec] = []
    for overlay_id, model in OVERLAY_DOCUMENT_MODELS.items():
        for field in model.model_fields:
            if field in _NON_KEY_FIELDS.get(overlay_id, frozenset()):
                continue
            key = f"{overlay_id}.{field}"
            specs.append(OverlayKeySpec(overlay_id, field, _KEY_ENV_VARS.get(key, ())))
    return tuple(specs)


OVERLAY_KEYS: tuple[OverlayKeySpec, ...] = _build_specs()
_SPECS_BY_KEY: Mapping[str, OverlayKeySpec] = {spec.key: spec for spec in OVERLAY_KEYS}


def overlay_key_spec(key: str) -> OverlayKeySpec:
    """The spec of ``key``, or a :class:`NodeOverlayError` naming the keys that exist."""
    spec = _SPECS_BY_KEY.get(key)
    if spec is None:
        raise NodeOverlayError(
            f"{key}: not an overlay key; the keys are {sorted(_SPECS_BY_KEY)}"
        )
    return spec


def _field_adapter(spec: OverlayKeySpec) -> TypeAdapter[Any]:
    model = OVERLAY_DOCUMENT_MODELS[spec.overlay_id]
    info = model.model_fields[spec.field]
    # The field's constraints (length, pattern) ride in ``metadata``, not in the bare annotation.
    return TypeAdapter(Annotated[info.annotation, *info.metadata])


def _validate_explicit(spec: OverlayKeySpec, value: object) -> Any:
    try:
        checked = _field_adapter(spec).validate_python(value)
    except ValidationError as exc:
        raise NodeOverlayError(
            f"{spec.key}: the explicit value is invalid ({type(exc).__name__})"
        ) from None
    if checked in ((), ""):
        raise NodeOverlayError(f"{spec.key}: the explicit value is empty")
    return checked


def _hosts_from_file(spec: OverlayKeySpec, path_text: str) -> Any:
    path = Path(path_text)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise NodeOverlayError(
            f"{spec.key}: {LAB_HOSTS_FILE_ENV} names {path}, which is unreadable "
            f"({type(exc).__name__})"
        ) from None
    try:
        return ModelLabHostsTable.model_validate(raw).hosts
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(str(part) for part in err["loc"]) or "<document>"
                for err in exc.errors()
            }
        )
        raise NodeOverlayError(
            f"{spec.key}: {LAB_HOSTS_FILE_ENV} file {path} is invalid: fields {fields}"
        ) from None


def resolve_overlay_key(
    key: str,
    *,
    explicit: object | None = None,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Resolve one overlay key: explicit value, then its variable, then the overlay field.

    Raises :class:`NodeOverlayError` naming ``key`` when nothing supplies it.
    """
    spec = overlay_key_spec(key)
    env = os.environ if environ is None else environ
    if explicit is not None:
        return _validate_explicit(spec, explicit)
    if key == "lab_hosts.hosts":
        pinned = env.get(LAB_HOSTS_FILE_ENV, "").strip()
        if pinned:
            return _hosts_from_file(spec, pinned)
    elif key == "lab_hosts.registry_root_candidates":
        pinned = env.get(REGISTRY_ROOT_ENV, "").strip()
        if pinned:
            return _validate_explicit(spec, (pinned,))
    document = load_structured_overlay(
        spec.overlay_id,
        OVERLAY_DOCUMENT_MODELS[spec.overlay_id],
        env,
        missing_message=not_configured_message(spec.overlay_id, key, spec.env_vars),
    )
    return getattr(document, spec.field)


def resolve_clone_home(
    key: str,
    *,
    environ: Mapping[str, str] | None = None,
    registry_root: Path | None = None,
) -> str:
    """The clone path a ``*_home_env(s)`` key names: the first variable it lists that is set.

    With none set, the sibling directory of ``registry_root`` named by the role's directory name
    (``repos.private_data`` or ``repos.knowledge_base``) when it exists. Otherwise raises
    :class:`NodeOverlayError` naming ``key``.
    """
    roles = {
        "repos.private_data_home_envs": "private_data",
        "repos.knowledge_base_home_env": "knowledge_base",
    }
    role = roles.get(key)
    if role is None:
        raise NodeOverlayError(
            f"{key}: not a clone-home key; the keys are {sorted(roles)}"
        )
    env = os.environ if environ is None else environ
    names = resolve_overlay_key(key, environ=env)
    for name in (names,) if isinstance(names, str) else names:
        value = env.get(name, "").strip()
        if value:
            return value
    if registry_root is not None:
        sibling = registry_root.parent / resolve_overlay_key(
            f"repos.{role}", environ=env
        )
        if sibling.is_dir():
            return str(sibling)
    raise NodeOverlayError(
        f"{key}: none of the variables it names ({names}) is set and no sibling directory of "
        "the registry root holds the clone"
    )


__all__ = [
    "LAB_HOSTS_FILE_ENV",
    "OVERLAY_DOCUMENT_MODELS",
    "OVERLAY_KEYS",
    "REGISTRY_ROOT_ENV",
    "OverlayKeySpec",
    "overlay_key_spec",
    "resolve_clone_home",
    "resolve_overlay_key",
]
