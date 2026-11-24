# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The mirror set, read from the forwarder's own contract.

The names are never copied here: a copy is a second list that drifts the day the forwarder's set is widened. The
forwarder's installed contract is read on each call, inbound and outbound together, each name once.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import yaml


class MirrorTopicsUnavailableError(Exception):
    """The forwarder contract could not be read or declares no mirror topics; names the reason."""


class ForwarderContractMirrorTopics:
    """Reads ``config.gateway_forwarder.mirror_topics`` of the forwarder contract of an installed package."""

    def __init__(
        self, forwarder_module: str, *, contract_path: Path | None = None
    ) -> None:
        self._module = forwarder_module
        self._path = contract_path

    def _resolve_path(self) -> Path:
        if self._path is not None:
            return self._path
        spec = importlib.util.find_spec(self._module)
        if spec is None or not spec.submodule_search_locations:
            raise MirrorTopicsUnavailableError(
                f"forwarder package {self._module} is not importable"
            )
        return Path(next(iter(spec.submodule_search_locations))) / "contract.yaml"

    def mirror_topic_names(self) -> tuple[str, ...]:
        path = self._resolve_path()
        try:
            loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise MirrorTopicsUnavailableError(
                f"forwarder contract unreadable: {type(exc).__name__}"
            ) from None
        try:
            mirror = loaded["config"]["gateway_forwarder"]["mirror_topics"]
            declared = [*mirror["inbound"], *mirror["outbound"]]
        except (KeyError, TypeError):
            raise MirrorTopicsUnavailableError(
                "forwarder contract declares no mirror_topics"
            ) from None
        names = tuple(dict.fromkeys(str(name) for name in declared))
        if not names:
            raise MirrorTopicsUnavailableError(
                "forwarder contract declares no mirror_topics"
            )
        return names


__all__ = ["ForwarderContractMirrorTopics", "MirrorTopicsUnavailableError"]
