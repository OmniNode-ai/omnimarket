# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reader for node deployment overlays (OMN-20935).

Deployment facts (hosts, endpoints, repository paths) are supplied by whoever runs
the system, never by this package. A node that needs one looks it up in this order:
an explicit value from the caller, then a setting from the environment, then the
node's overlay file, ``<root>/<node name>/overlay.yaml``, found under the first
root in ``ONEX_SKILL_OVERLAY_ROOTS`` (an ``os.pathsep`` separated list) that holds
one. When none supplies the value the node refuses with an error that names the
setting and how to supply it. There is no packaged fallback value.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
OVERLAY_FILE_NAME = "overlay.yaml"


class NodeOverlayError(ValueError):
    """The deployment overlay is invalid, or a required deployment fact is absent."""


def find_node_overlay(
    node_name: str, environ: Mapping[str, str] | None = None
) -> Path | None:
    """Return the first ``<root>/<node_name>/overlay.yaml`` in root order, or ``None``."""
    env = os.environ if environ is None else environ
    for root in env.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep):
        if not root:
            continue
        candidate = Path(root) / node_name / OVERLAY_FILE_NAME
        if candidate.is_file():
            return candidate
    return None


def load_node_overlay(
    node_name: str,
    *,
    fields: frozenset[str],
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Read the node's overlay as a mapping of ``fields`` to non-empty strings.

    Returns an empty mapping when no root supplies an overlay. A present overlay that
    is not a mapping, carries a field outside ``fields`` or a value that is not a
    non-empty string raises :class:`NodeOverlayError`: a broken overlay must not read
    as an absent one.
    """
    path = find_node_overlay(node_name, environ)
    if path is None:
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise NodeOverlayError(
            f"{OVERLAY_ROOTS_ENV}: overlay {path} is unreadable ({type(exc).__name__})"
        ) from None
    if not isinstance(raw, dict):
        raise NodeOverlayError(f"{OVERLAY_ROOTS_ENV}: overlay {path} must be a mapping")
    unknown = sorted(set(raw) - fields)
    if unknown:
        raise NodeOverlayError(
            f"{OVERLAY_ROOTS_ENV}: overlay {path} carries unknown fields {unknown}; "
            f"{node_name} reads {sorted(fields)}"
        )
    bad = sorted(
        key
        for key, value in raw.items()
        if not isinstance(value, str) or not value.strip()
    )
    if bad:
        raise NodeOverlayError(
            f"{OVERLAY_ROOTS_ENV}: overlay {path} fields {bad} must be non-empty strings"
        )
    return {key: value.strip() for key, value in raw.items()}


def load_structured_overlay[ModelT: BaseModel](
    overlay_id: str,
    model: type[ModelT],
    environ: Mapping[str, str] | None = None,
    *,
    explicit: object | None = None,
    file_env: str | None = None,
    missing_message: str | None = None,
) -> ModelT:
    """Validate an explicit document, a pinned file, or the first overlay, in order.

    ``file_env`` names an environment variable containing a YAML file path. An
    unset or blank variable defers to ``<root>/<overlay_id>/overlay.yaml`` under
    ``ONEX_SKILL_OVERLAY_ROOTS``. ``environ`` defaults to the process environment.
    An explicit model or document takes precedence without reading either file.

    No source raises :class:`NodeOverlayError` naming the id and model fields.
    ``missing_message`` lets a key reader name the requested field and its input variables.
    A broken selected source refuses without trying a lower-priority source.
    Errors name the source, exception type and invalid fields, never document
    values or exception messages, which may contain private deployment facts.
    """
    if explicit is not None:
        raw = explicit
        source = f"explicit overlay {overlay_id}"
    else:
        env = os.environ if environ is None else environ
        pinned = env.get(file_env, "").strip() if file_env else ""
        path = Path(pinned) if pinned else find_node_overlay(overlay_id, env)
        if path is None:
            if missing_message is not None:
                raise NodeOverlayError(missing_message)
            settings = ", ".join(f"{overlay_id}.{name}" for name in model.model_fields)
            raise NodeOverlayError(
                not_configured_message(
                    overlay_id, settings or overlay_id, (file_env,) if file_env else ()
                )
            )
        source = f"{file_env if pinned else OVERLAY_ROOTS_ENV}: overlay {path}"
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise NodeOverlayError(
                f"{source} is unreadable ({type(exc).__name__})"
            ) from None
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(str(part) for part in err["loc"]) or "<document>"
                for err in exc.errors(include_input=False, include_context=False)
            }
        )
        raise NodeOverlayError(
            f"{source} is invalid for {overlay_id} (ValidationError): fields {fields}"
        ) from None


def not_configured_message(
    node_name: str, setting: str, env_vars: tuple[str, ...] = ()
) -> str:
    """The error text of a deployment fact that nothing supplied."""
    overlay = (
        f"supply an overlay with field `{setting}` at "
        f"<root>/{node_name}/{OVERLAY_FILE_NAME} under a root named in {OVERLAY_ROOTS_ENV}"
    )
    sources = [f"set {name}" for name in env_vars] + [overlay]
    return f"{node_name}: {setting} is not configured; " + ", or ".join(sources)


def resolve_setting(
    node_name: str,
    setting: str,
    *,
    fields: frozenset[str],
    env_vars: tuple[str, ...] = (),
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve one deployment fact: the first set variable in ``env_vars``, else the overlay.

    Raises :class:`NodeOverlayError` naming how to supply it when neither does.
    """
    env = os.environ if environ is None else environ
    for name in env_vars:
        value = env.get(name, "").strip()
        if value:
            return value
    value = load_node_overlay(node_name, fields=fields, environ=env).get(setting, "")
    if not value:
        raise NodeOverlayError(not_configured_message(node_name, setting, env_vars))
    return value


def resolve_overlay_file(
    node_name: str,
    default: Path,
    *,
    env_var: str,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve a whole-file deployment config: the file ``env_var`` names, else the overlay.

    ``env_var`` pins a file and wins. Otherwise the first ``<root>/<node_name>/overlay.yaml``
    under ``ONEX_SKILL_OVERLAY_ROOTS`` is the file. With neither, ``default`` (the neutral
    file the package ships) is returned. A pinned file that does not exist raises
    :class:`NodeOverlayError` rather than falling through to a lower source.
    """
    env = os.environ if environ is None else environ
    pinned = env.get(env_var, "").strip()
    if pinned:
        path = Path(pinned)
        if not path.is_file():
            raise NodeOverlayError(f"{env_var}: {path} is not a file")
        return path
    return find_node_overlay(node_name, env) or default


SWARM_REGISTRY_NODE = "node_swarm_registry_compute"
SWARM_REGISTRY_ENV = "OMNIMARKET_SWARM_ENDPOINT_REGISTRY"


def resolve_swarm_endpoint_registry_path(default: Path) -> Path:
    """The swarm endpoint registry in force: pinned file, else overlay, else ``default``."""
    return resolve_overlay_file(
        SWARM_REGISTRY_NODE, default, env_var=SWARM_REGISTRY_ENV
    )
