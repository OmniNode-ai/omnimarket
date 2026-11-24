# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory source-diff backend, and the comparison the git backend shares (OMN-26275).

``diff_toml_texts`` compares two TOML texts key by key with ``tomllib``. A key is the dotted path of a
leaf value; a list of tables that each carry a ``name`` (``[[package]]`` in ``uv.lock``) is keyed by
that name, so a reordered lock is not a change. Values are compared by their JSON rendering, so ``1``,
``"1"`` and ``true`` differ.
"""

from __future__ import annotations

import json
import tomllib
from collections import Counter
from collections.abc import Mapping, Sequence

from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelDataFileChange,
    ModelKeyChange,
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _rendered(value: object) -> str:
    return value if isinstance(value, str) else _canonical(value)


def _named_tables(value: list[object]) -> bool:
    return bool(value) and all(
        isinstance(item, dict) and isinstance(item.get("name"), str) for item in value
    )


def _flatten(value: object, prefix: str, out: dict[str, object]) -> None:
    if isinstance(value, dict) and value:
        for key, child in value.items():
            _flatten(child, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(value, list) and _named_tables(value):
        seen: Counter[str] = Counter()
        for item in value:
            assert isinstance(item, dict)
            name = item["name"]
            label = name if seen[name] == 0 else f"{name}#{seen[name]}"
            seen[name] += 1
            rest = {k: v for k, v in item.items() if k != "name"}
            _flatten(rest, f"{prefix}.{label}" if prefix else label, out)
    else:
        out[prefix] = value


def flatten_toml(text: str) -> dict[str, object]:
    """Dotted leaf key -> value of a TOML text. Raises ``tomllib.TOMLDecodeError`` on bad TOML."""
    out: dict[str, object] = {}
    _flatten(tomllib.loads(text), "", out)
    return out


def diff_toml_texts(
    path: str, base: str | None, head: str | None
) -> ModelDataFileChange:
    """The changed keys between two texts; an absent or unparseable side is a parse failure."""
    if base is None or head is None:
        return ModelDataFileChange(path=path, parse_failed=True)
    try:
        before, after = flatten_toml(base), flatten_toml(head)
    except (tomllib.TOMLDecodeError, ValueError):
        return ModelDataFileChange(path=path, parse_failed=True)
    changed = []
    for key in sorted({*before, *after}):
        old = _canonical(before[key]) if key in before else None
        new = _canonical(after[key]) if key in after else None
        if old != new:
            changed.append(
                ModelKeyChange(
                    key=key,
                    base_value=_rendered(before[key]) if key in before else None,
                    head_value=_rendered(after[key]) if key in after else None,
                )
            )
    return ModelDataFileChange(path=path, changed_keys=tuple(changed))


class SourceFileDiffInMemory:
    """Answers the (base text, head text) pairs it was built with; ``None`` stands for an absent side."""

    def __init__(self, texts: Mapping[str, tuple[str | None, str | None]]) -> None:
        self._texts = dict(texts)

    def changes(
        self, repo_root: str, base_ref: str, head_ref: str, paths: Sequence[str]
    ) -> tuple[ModelDataFileChange, ...]:
        return tuple(
            diff_toml_texts(path, *self._texts.get(path, (None, None)))
            for path in paths
            if path in self._texts
        )
