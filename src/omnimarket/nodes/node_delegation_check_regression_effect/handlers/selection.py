# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which delegation checks a change selects: the changed keys of a settings document, matched against the settings' rules."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from fnmatch import fnmatchcase

from pydantic import JsonValue

from omnimarket.nodes.node_delegation_check_regression_effect.models.model_check_regression import (
    EnumChangeKind,
)
from omnimarket.nodes.node_delegation_check_regression_effect.overlay import (
    ModelCheckRegressionSettings,
)


def _flatten(node: JsonValue, path: str) -> Iterator[tuple[str, JsonValue]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _flatten(value, f"{path}/{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _flatten(value, f"{path}[{index}]")
    else:
        yield path, node


def changed_keys(
    previous: Mapping[str, JsonValue] | None, new: Mapping[str, JsonValue] | None
) -> tuple[str, ...]:
    """The slash-joined leaf paths whose value differs between two documents, sorted; empty when either is missing."""
    if previous is None or new is None:
        return ()
    before = dict(_flatten(dict(previous), ""))
    after = dict(_flatten(dict(new), ""))
    return tuple(
        sorted(
            k
            for k in before.keys() | after.keys()
            if before.get(k, _MISSING) != after.get(k, _MISSING)
        )
    )


_MISSING = object()


def select_checks(
    settings: ModelCheckRegressionSettings,
    change_kind: EnumChangeKind,
    subject: str,
    keys: tuple[str, ...],
) -> tuple[str, ...]:
    """The checks any rule selects for a change of this kind and subject with any of these keys, in settings order."""
    selected: list[str] = []
    for rule in settings.key_check_map:
        if rule.change_kind is not change_kind:
            continue
        if not fnmatchcase(subject, rule.subject_glob):
            continue
        if not any(fnmatchcase(k, rule.key_glob) for k in keys):
            continue
        selected.extend(c for c in rule.checks if c not in selected)
    return tuple(selected)


__all__ = ["changed_keys", "select_checks"]
