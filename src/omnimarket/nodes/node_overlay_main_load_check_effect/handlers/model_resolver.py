# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolves a model class spec to a pydantic BaseModel subclass in this tree (OMN-23963)."""

from __future__ import annotations

import importlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

import omnimarket


class ModelUnresolvedError(Exception):
    """The model class named by the pin exists nowhere in this tree."""


class ModelImportFailedError(Exception):
    """The model class could not be imported."""


@dataclass(frozen=True)
class ModelSearchRoot:
    package: str
    directory: Path


def default_search_roots() -> tuple[ModelSearchRoot, ...]:
    return (
        ModelSearchRoot(
            package="omnimarket", directory=Path(omnimarket.__file__).parent
        ),
    )


_CLASS_LINE = re.compile(r"^class (\w+)\(", re.MULTILINE)


@lru_cache(maxsize=4)
def _class_index(roots: tuple[ModelSearchRoot, ...]) -> dict[str, tuple[str, ...]]:
    """Every class defined at module level under the roots, by name, with the modules that define it (read once per roots)."""
    index: dict[str, list[str]] = {}
    for root in roots:
        for py_file in sorted(root.directory.rglob("*.py")):
            try:
                text = py_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            rel = py_file.relative_to(root.directory)
            parts = (
                rel.parts[:-1]
                if rel.name == "__init__.py"
                else rel.with_suffix("").parts
            )
            module = ".".join((root.package, *parts))
            for name in _CLASS_LINE.findall(text):
                index.setdefault(name, []).append(module)
    return {name: tuple(modules) for name, modules in index.items()}


def _find_candidates(roots: Sequence[ModelSearchRoot], class_name: str) -> list[str]:
    return list(_class_index(tuple(roots)).get(class_name, ()))


def _model_class(module_name: str, class_name: str) -> type[BaseModel]:
    cls = getattr(importlib.import_module(module_name), class_name)
    if isinstance(cls, type) and issubclass(cls, BaseModel):
        return cls
    raise TypeError(f"{module_name}.{class_name} is not a pydantic BaseModel subclass")


def resolve_model(spec: str, roots: Sequence[ModelSearchRoot]) -> type[BaseModel]:
    parts = spec.split(":")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ModelImportFailedError(f"malformed spec: {spec!r}")
    module_name, class_name = parts
    try:
        return _model_class(module_name, class_name)
    except ModuleNotFoundError as exc:
        # Only the pinned module itself (or a parent package) being absent means the model moved; any other missing module is an
        # import failure of the model's own chain and must refuse.
        missing = exc.name or ""
        if not (module_name == missing or module_name.startswith(missing + ".")):
            raise ModelImportFailedError(f"{type(exc).__name__}: {exc}") from None
    except AttributeError:
        pass
    except Exception as exc:
        raise ModelImportFailedError(f"{type(exc).__name__}: {exc}") from None
    candidates = _find_candidates(roots, class_name)
    if not candidates:
        raise ModelUnresolvedError(f"{class_name} exists nowhere in this tree")
    if len(candidates) > 1:
        raise ModelImportFailedError(
            f"{class_name} found in multiple modules: {list(candidates)}"
        )
    try:
        return _model_class(candidates[0], class_name)
    except Exception as exc:
        raise ModelImportFailedError(f"{type(exc).__name__}: {exc}") from None


def resolve_spec(spec: str, roots: Sequence[ModelSearchRoot]) -> str:
    """The ``module:class`` spec that imports in this tree for a pin's spec (a moved model's new module)."""
    cls = resolve_model(spec, roots)
    return f"{cls.__module__}:{cls.__name__}"
