# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real backend of the one-copy check's referrer reader: files under the registry root (OMN-19998)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import yaml

from omnimarket.nodes.node_one_copy_check_compute.models import (
    ModelReferrer,
    ModelReferrerSource,
)
from omnimarket.nodes.node_one_copy_check_compute.protocols.protocol_referrer_reader import (
    ReferrerReadError,
)


class FileReferrerReader:
    """The real backend: each source's file under ``root``, or the subtree of a YAML file its dotted ``yaml_key`` names."""

    def __init__(self, root: Path, sources: Sequence[ModelReferrerSource]) -> None:
        self._root = root
        self._sources = tuple(sources)

    def read(self) -> tuple[ModelReferrer, ...]:
        return tuple(self._one(source) for source in self._sources)

    def _one(self, source: ModelReferrerSource) -> ModelReferrer:
        path = self._root / source.path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ReferrerReadError(
                f"source {source.id}: {source.path}: {exc}"
            ) from None
        if source.yaml_key is not None:
            try:
                node: object = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                raise ReferrerReadError(
                    f"source {source.id}: {source.path} is not YAML: {exc}"
                ) from None
            for part in source.yaml_key.split("."):
                if not isinstance(node, dict) or part not in node:
                    raise ReferrerReadError(
                        f"source {source.id}: key {source.yaml_key!r} is missing in {source.path}"
                    )
                node = node[part]
            text = yaml.safe_dump(node)
        return ModelReferrer(id=source.id, kind=source.kind, text=text)
